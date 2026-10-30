package main

import (
	"context"
	"encoding/json"
	"net"
	"strings"
	"testing"
	"time"
)

func saveSnapshot(t *testing.T, root string, payload map[string]any) {
	t.Helper()
	b, _ := json.Marshal(payload)
	// QSettings quotes JSON and escapes its quotes and backslashes.
	quoted := strings.ReplaceAll(strings.ReplaceAll(string(b), `\`, `\\`), `"`, `\"`)
	if err := atomicWrite(path(root, "/run/cgs2-readings.ini"), []byte("[readings]\nsnapshot=\""+quoted+"\"\n")); err != nil {
		t.Fatal(err)
	}
}

func TestCurrentReadingsRejectsStaleAndInvalidSnapshotsAndStripsExtraFields(t *testing.T) {
	root := t.TempDir()
	now := time.Now()
	for _, age := range []time.Duration{-time.Minute, 0, 21 * time.Second} {
		saveSnapshot(t, root, map[string]any{"version": 1, "timestamp": now.Add(-age).Unix(), "temperature": 22.5, "humidity": nil, "history": []int{999}, "serial": "private"})
		data, err := currentReadings(root, now)
		if age != 0 {
			if err == nil {
				t.Fatal("stale or future readings accepted")
			}
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		var got map[string]any
		json.Unmarshal(data, &got)
		if len(got) != 10 || got["temperature"] != 22.5 || got["humidity"] != nil || got["history"] != nil || got["serial"] != nil {
			t.Fatalf("%v", got)
		}
	}
	saveSnapshot(t, root, map[string]any{"version": 1, "timestamp": now.Unix(), "temperature": "not a number"})
	if _, err := currentReadings(root, now); err == nil {
		t.Fatal("invalid readings accepted")
	}
}

func TestMQTTIgnoresNativeSettingsAndUnsavedDrafts(t *testing.T) {
	c := controllerFixture(t)
	for _, file := range []string{"/data/etc/setting.ini", "/data/etc/cgs2-mqtt.ini"} {
		atomicWrite(path(c.store.Root, file), []byte(haSettings))
	}
	conf, err := readLiveConfig(c.store.Root)
	if err != nil || conf.Enabled {
		t.Fatalf("%+v %v", conf, err)
	}
	c.mqttTick(time.Now())
	if c.mqtt.cancel != nil || c.status.MQTT.Message != "Off" {
		t.Fatal("unsaved settings started MQTT")
	}
}

func TestMQTTCurrentReportCadenceAndRevocation(t *testing.T) {
	c := haFixture(t, haSettings)
	conf, err := readLiveConfig(c.store.Root)
	if err != nil {
		t.Fatal(err)
	}
	client, broker := net.Pipe()
	f := &brokerFixture{}
	done := make(chan struct{})
	// The pipe starts after CONNECT; use a small peer that handles only live publishes/pings.
	go func() {
		defer close(done)
		defer broker.Close()
		for {
			kind, body, err := readPacket(broker)
			if err != nil {
				return
			}
			if kind == 0xc0 {
				writePacket(broker, 0xd0, nil)
				continue
			}
			if kind != 0x32 {
				f.err = context.Canceled
				return
			}
			n := int(body[0])<<8 | int(body[1])
			f.published = append(f.published, mqttMessage{string(body[2 : 2+n]), body[4+n:]})
			writePacket(broker, 0x40, body[2+n:4+n])
		}
	}()
	c.mqtt.config = conf
	c.mqtt.session = &mqttSession{conn: client}
	saveSnapshot(t, c.store.Root, map[string]any{"version": 1, "timestamp": time.Now().Unix(), "co2": 550})
	c.mqttTick(time.Now())
	c.done(<-c.results)
	if c.status.MQTT.LastSuccess == "" || time.Until(c.mqtt.report) < 59*time.Second {
		t.Fatal("current report did not follow the saved cadence")
	}
	c.mqttTick(time.Now())
	if c.mqtt.cancel != nil {
		t.Fatal("reported twice before the next interval")
	}
	// Revocation closes the persistent connection before another ping/publish.
	atomicWrite(path(c.store.Root, "/etc/cgs2-privacy/allowlist.conf"), nil)
	c.mqtt.next = time.Time{}
	c.mqttTick(time.Now())
	c.done(<-c.results)
	<-done
	if c.mqtt.session != nil || c.mqtt.failures != 1 || len(f.published) != 1 || f.err != nil {
		t.Fatalf("revocation: %v, %d publishes", f.err, len(f.published))
	}
	if !strings.Contains(c.status.MQTT.Message, "no active MQTT permission") {
		t.Fatal(c.status.MQTT.Message)
	}
}
