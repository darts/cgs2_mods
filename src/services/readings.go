package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"time"
)

const mqttAppliedFile = "/data/etc/cgs2-mqtt-applied.ini"

type liveConfig struct {
	Enabled         bool
	Broker          brokerSettings
	ClientID, Topic string
	Seconds         int
}

// Draft edits and the native firmware settings never authorize a connection.
func readLiveConfig(root string) (liveConfig, error) {
	var conf liveConfig
	ini, err := readQtINI(path(root, mqttAppliedFile))
	if os.IsNotExist(err) {
		return conf, nil
	}
	if err != nil {
		return conf, err
	}
	s := ini["mqtt"]
	if s["enabled"] != "true" {
		return conf, nil
	}
	if s["tls"] != "true" && s["tls"] != "false" {
		return conf, errors.New("the saved TLS setting is not valid")
	}
	if !validIPv4(s["host"], true) {
		return conf, errors.New("the saved broker is not a unicast IPv4 address")
	}
	port, err := number(s["port"], 1, 65535)
	if err != nil {
		return conf, errors.New("the saved broker port is not valid")
	}
	for _, key := range []string{"username", "password", "client_id", "pub_topic"} {
		v := s[key]
		if v == "" || len(v) > 4096 || strings.ContainsAny(v, "\x00\r\n") {
			return conf, errors.New("the saved MQTT settings are incomplete or invalid")
		}
	}
	if strings.ContainsAny(s["pub_topic"], "+#") {
		return conf, errors.New("publish topics cannot contain wildcards")
	}
	seconds := 60
	if s["report_seconds"] != "" {
		seconds, err = number(s["report_seconds"], 10, 3600)
		if err != nil {
			return conf, errors.New("the saved report interval is not valid")
		}
	}
	return liveConfig{true, brokerSettings{s["host"], s["username"], s["password"], port, s["tls"] == "true"}, s["client_id"], s["pub_topic"], seconds}, nil
}

// The display replaces this one RAM snapshot every five seconds. There is no
// history queue. A stopped display cannot keep publishing old readings.
func currentReadings(root string, now time.Time) ([]byte, error) {
	ini, err := readQtINI(path(root, "/run/cgs2-readings.ini"))
	if err != nil {
		return nil, errors.New("waiting for current readings")
	}
	var snapshot struct {
		Version     int      `json:"version"`
		Timestamp   int64    `json:"timestamp"`
		Temperature *float64 `json:"temperature"`
		Humidity    *float64 `json:"humidity"`
		CO2         *float64 `json:"co2"`
		PM25        *float64 `json:"pm25"`
		PM10        *float64 `json:"pm10"`
		TVOC        *float64 `json:"tvoc_index"`
		Noise       *float64 `json:"noise"`
		Battery     *float64 `json:"battery"`
	}
	if err = json.Unmarshal([]byte(ini["readings"]["snapshot"]), &snapshot); err != nil || snapshot.Version != 1 {
		return nil, errors.New("the current readings are not valid")
	}
	age := now.Sub(time.Unix(snapshot.Timestamp, 0))
	if age > 20*time.Second || age < -5*time.Second {
		return nil, errors.New("waiting for fresh readings from the display")
	}
	return json.Marshal(snapshot)
}

type mqttJob struct {
	config       liveConfig
	generation   uint64
	cancel       context.CancelFunc
	session      *mqttSession
	next, report time.Time
	failures     int
	connected    bool
}

type mqttOutcome struct {
	generation      uint64
	session         *mqttSession
	sent, connected bool
}

func (c *Controller) mqttStop() {
	c.mqtt.generation++
	if c.mqtt.cancel != nil {
		c.mqtt.cancel()
		c.mqtt.cancel = nil
	}
	c.mqtt.session.close()
	c.mqtt.session = nil
}

func (c *Controller) mqttStatus(message string) {
	if c.status.MQTT.Message != message {
		c.status.MQTT.Message = message
		c.publish()
	}
}

func (c *Controller) mqttTick(now time.Time) {
	conf, err := readLiveConfig(c.store.Root)
	if err != nil || conf != c.mqtt.config {
		c.mqttStop()
		c.mqtt.config = conf
		c.mqtt.next, c.mqtt.report = time.Time{}, time.Time{}
		c.mqtt.failures = 0
		c.mqtt.connected = false
		if err != nil {
			c.mqttStatus("Could not read MQTT settings: " + err.Error())
			return
		}
	}
	if !conf.Enabled {
		c.mqttStatus("Off")
		return
	}
	if c.mqtt.cancel != nil || now.Before(c.mqtt.next) {
		return
	}
	var payload []byte
	if !now.Before(c.mqtt.report) {
		payload, err = currentReadings(c.store.Root, now)
		if err != nil {
			c.mqttStatus(err.Error())
			// Keep an existing connection alive, but never replay stale data.
			if c.mqtt.session == nil {
				c.mqtt.next = now.Add(5 * time.Second)
				return
			}
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	c.mqtt.cancel = cancel
	out := &mqttOutcome{generation: c.mqtt.generation, session: c.mqtt.session}
	c.mqtt.session = nil // the job owns the session until its result is handled
	c.jobs.Add(1)
	go func() {
		defer c.jobs.Done()
		err := c.store.permitMQTT(ctx, conf.Broker)
		if err == nil && out.session == nil {
			out.session, err = openMQTT(ctx, conf.Broker, conf.ClientID, nil)
			out.connected = err == nil
		}
		if err == nil {
			if len(payload) > 0 {
				// A slow connection must not age the snapshot before publication.
				payload, err = currentReadings(c.store.Root, time.Now())
				if err == nil {
					err = out.session.publish(ctx, []mqttMessage{{conf.Topic, payload}}, false)
				}
				out.sent = err == nil
			} else {
				err = out.session.ping(ctx)
			}
		}
		cancel()
		if err != nil {
			out.session.close()
			out.session = nil
		}
		select {
		case c.results <- jobResult{service: "mqtt", mqtt: out, err: err}:
		case <-c.stopping:
			out.session.close()
		}
	}()
}

func (c *Controller) mqttDone(r jobResult) {
	if r.mqtt.generation != c.mqtt.generation {
		r.mqtt.session.close()
		return
	}
	c.mqtt.cancel = nil
	c.mqtt.session = r.mqtt.session
	now := time.Now()
	if r.err != nil {
		c.mqtt.failures++
		c.mqtt.next = now.Add(5 * time.Second << min(c.mqtt.failures-1, 6))
		c.mqttStatus("Could not publish readings: " + r.err.Error())
		return
	}
	c.mqtt.failures = 0
	if r.mqtt.connected {
		if c.mqtt.connected {
			c.refreshHomeAssistant()
		}
		c.mqtt.connected = true
	}
	if r.mqtt.sent {
		c.mqtt.report = now.Add(time.Duration(c.mqtt.config.Seconds) * time.Second)
		c.status.MQTT.LastSuccess = now.UTC().Format(time.RFC3339)
		c.status.MQTT.Message = "Publishing current readings"
		c.publish()
	}
	c.mqtt.next = now.Add(15 * time.Second)
	if c.mqtt.report.Before(c.mqtt.next) {
		c.mqtt.next = c.mqtt.report
	}
}
