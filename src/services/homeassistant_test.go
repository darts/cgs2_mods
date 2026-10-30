package main

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"
)

const haSettings = `[datetime]
timezone=Europe/Berlin
[mqtt]
enabled=true
client_id=cgs2-client
host=192.168.1.20
password="p\xe4ss, word"
port=1883
pub_topic=qingping/582D34000001/up
sub_topic=qingping/582D34000001/down
tls=false
username=homeassistant
`

func haFixture(t *testing.T, settings string) *Controller {
	t.Helper()
	c := controllerFixture(t)
	for name, content := range map[string]string{
		mqttAppliedFile:                    settings,
		"/sys/class/net/wlan0/address":     "58:2d:34:00:00:01\n",
		"/qingping/etc/os-release":         "NAME=Qingping\nCLEARGRASS_VERSION=4.5.6_0181 \n",
		"/etc/cgs2-privacy/allowlist.conf": "tcp:192.168.1.20:1883\n",
	} {
		if err := atomicWrite(path(c.store.Root, name), []byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	return c
}

func setHASwitch(t *testing.T, c *Controller, on bool) {
	t.Helper()
	value := "false"
	if on {
		value = "true"
	}
	if err := atomicWrite(path(c.store.Root, haSwitchFile), []byte("[homeassistant]\ndiscovery="+value+"\n")); err != nil {
		t.Fatal(err)
	}
}

func TestReadHomeAssistantUsesTheAppliedBroker(t *testing.T) {
	c := haFixture(t, haSettings)
	want, err := readHomeAssistant(c.store.Root)
	if err != nil {
		t.Fatal(err)
	}
	if !want.MQTT || !want.Enabled || want.Broker != (brokerSettings{Host: "192.168.1.20", Port: 1883, Username: "homeassistant", Password: "päss, word"}) ||
		want.ClientID != "cgs2-ha-582d34000001" || want.Topic != "homeassistant/device/cgs2_582d34000001/config" {
		t.Fatalf("%+v", want)
	}
	setHASwitch(t, c, false)
	if want, _ = readHomeAssistant(c.store.Root); !want.MQTT || want.Enabled {
		t.Fatal("the switch did not turn discovery off")
	}
	c = haFixture(t, strings.Replace(haSettings, "enabled=true", "enabled=false", 1))
	if want, err = readHomeAssistant(c.store.Root); err != nil || want.MQTT || want.Enabled {
		t.Fatalf("MQTT off: %+v %v", want, err)
	}
	for name, settings := range map[string]string{
		"loopback": strings.Replace(haSettings, "192.168.1.20", "127.0.0.1", 1),
		"port":     strings.Replace(haSettings, "port=1883", "port=0", 1),
		"topic":    strings.Replace(haSettings, "pub_topic=qingping/582D34000001/up", "pub_topic=", 1),
	} {
		if _, err := readHomeAssistant(haFixture(t, settings).store.Root); err == nil {
			t.Errorf("%s: invalid settings were accepted", name)
		}
	}
}

func TestDiscoveryPayloadDescribesEachSensor(t *testing.T) {
	var payload struct {
		Device     map[string]any            `json:"device"`
		Origin     map[string]any            `json:"origin"`
		StateTopic string                    `json:"state_topic"`
		Components map[string]map[string]any `json:"components"`
	}
	if err := json.Unmarshal(haDiscoveryPayload("cgs2_582d34000001", "58:2d:34:00:00:01", "4.5.6_0181", "qingping/up", 60), &payload); err != nil {
		t.Fatal(err)
	}
	if payload.StateTopic != "qingping/up" || payload.Origin["name"] == nil || payload.Device["sw_version"] != "4.5.6_0181 (local "+modVersion+")" ||
		payload.Device["identifiers"].([]any)[0] != "cgs2_582d34000001" {
		t.Fatalf("%+v", payload)
	}
	if len(payload.Components) != len(haSensors) {
		t.Fatalf("%d components", len(payload.Components))
	}
	for _, s := range haSensors {
		c := payload.Components[s.key]
		if c["platform"] != "sensor" || c["unique_id"] != "cgs2_582d34000001_"+s.key ||
			!strings.Contains(c["value_template"].(string), ".get('"+s.key+"', '')") {
			t.Errorf("%s: %+v", s.key, c)
		}
	}
	if payload.Components["pm25"]["unit_of_measurement"] != "µg/m³" || payload.Components["battery"]["entity_category"] != "diagnostic" {
		t.Fatal("units or categories changed")
	}
}

func TestHAActionPublishesOnStartAndChangeAndRemoves(t *testing.T) {
	want := haDesired{MQTT: true, Enabled: true, Broker: brokerSettings{Host: "192.168.1.20", Port: 1883}, Topic: "homeassistant/device/x/config", Payload: []byte("{}")}
	current := haState{Broker: "192.168.1.20:1883", Topic: want.Topic, PayloadHash: payloadHash(want.Payload)}
	off, otherBroker, changed := want, current, current
	off.Enabled = false
	otherBroker.Broker = "192.168.1.21:1883"
	changed.PayloadHash = payloadHash([]byte("old"))
	for name, test := range map[string]struct {
		want      haDesired
		state     haState
		announced bool
		action    string
	}{
		"first publish":         {want, haState{}, false, "publish"},
		"once after a start":    {want, current, false, "publish"},
		"up to date":            {want, current, true, ""},
		"payload changed":       {want, changed, true, "publish"},
		"new broker":            {want, otherBroker, true, "publish"},
		"switched off":          {off, current, true, "remove"},
		"already removed":       {off, haState{}, true, ""},
		"other broker holds it": {off, otherBroker, true, ""},
		"MQTT off":              {haDesired{}, current, false, ""},
	} {
		if got := haAction(test.want, test.state, test.announced); got != test.action {
			t.Errorf("%s: got %q, want %q", name, got, test.action)
		}
	}
}

func TestDiscoveryExpiryFollowsTheReportInterval(t *testing.T) {
	c := haFixture(t, haSettings)
	for draft, expiry := range map[string]float64{"": 180, "report_seconds=300\n": 900, "report_seconds=10\n": 180} {
		if err := atomicWrite(path(c.store.Root, mqttAppliedFile), []byte(haSettings+draft)); err != nil {
			t.Fatal(err)
		}
		want, err := readHomeAssistant(c.store.Root)
		if err != nil {
			t.Fatal(err)
		}
		var payload struct {
			Components map[string]map[string]any `json:"components"`
		}
		json.Unmarshal(want.Payload, &payload)
		if got := payload.Components["co2"]["expire_after"]; got != expiry {
			t.Errorf("%q: expire_after %v, want %v", draft, got, expiry)
		}
	}
}

func TestControllerPublishesRemovesAndBacksOff(t *testing.T) {
	c := haFixture(t, haSettings)
	var sent []mqttMessage
	var failure error
	c.ha.publish = func(_ context.Context, b brokerSettings, id string, _ *x509.CertPool, m []mqttMessage) error {
		if b.Host != "192.168.1.20" || id != "cgs2-ha-582d34000001" {
			t.Fatalf("broker %+v id %s", b, id)
		}
		sent = append(sent, m...)
		return failure
	}
	run := func() {
		t.Helper()
		c.ha.next = time.Time{}
		c.homeAssistantTick(time.Now())
		if c.ha.cancel != nil {
			c.homeAssistantDone(<-c.results)
		}
	}
	run()
	if len(sent) != 1 || len(sent[0].Payload) == 0 || c.status.HomeAssistant.Message != "Published to the broker" || c.status.HomeAssistant.LastSuccess == "" {
		t.Fatalf("publish: %d messages, %+v", len(sent), c.status.HomeAssistant)
	}
	run()
	if len(sent) != 1 {
		t.Fatal("an unchanged discovery was published again")
	}
	setHASwitch(t, c, false)
	run()
	if len(sent) != 2 || len(sent[1].Payload) != 0 || c.status.HomeAssistant.Message != "Removed from the broker" {
		t.Fatalf("remove: %d messages, %+v", len(sent), c.status.HomeAssistant)
	}
	run()
	if len(sent) != 2 || c.status.HomeAssistant.Message != "Off" {
		t.Fatalf("after removal: %d messages, %+v", len(sent), c.status.HomeAssistant)
	}
	setHASwitch(t, c, true)
	failure = errors.New("broker refused the connection: not authorized")
	c.ha.next = time.Time{}
	c.homeAssistantTick(time.Now())
	c.homeAssistantDone(<-c.results)
	if !strings.Contains(c.status.HomeAssistant.Message, "not authorized") || time.Until(c.ha.next) < 50*time.Second {
		t.Fatalf("failure: %+v next %v", c.status.HomeAssistant, time.Until(c.ha.next))
	}
	if _, err := os.Stat(path(c.store.Root, haStateFile)); err != nil {
		t.Fatal(err)
	}
	if readHAState(c.store.Root).Topic != "" {
		t.Fatal("a failed publish was recorded as published")
	}
}

func TestHADiscoveryRequiresThePersistentBrokerPermission(t *testing.T) {
	for _, mode := range []string{"revoked", "different broker", "pending", "weather lease"} {
		t.Run(mode, func(t *testing.T) {
			c := haFixture(t, haSettings)
			policy := ""
			if mode == "different broker" {
				policy = "tcp:192.168.1.21:1883\n"
			}
			if mode == "pending" {
				policy = "tcp:192.168.1.20:1883\n"
				if err := c.store.beginPolicy(); err != nil {
					t.Fatal(err)
				}
			}
			if err := atomicWrite(path(c.store.Root, "/etc/cgs2-privacy/allowlist.conf"), []byte(policy)); err != nil {
				t.Fatal(err)
			}
			if mode == "weather lease" {
				if err := writeJSON(path(c.store.Root, "/run/cgs2-services/leases.json"), []Lease{{Service: "weather", Endpoint: "tcp:192.168.1.20:1883", Until: 200}}); err != nil {
					t.Fatal(err)
				}
			}
			called := false
			c.ha.publish = func(context.Context, brokerSettings, string, *x509.CertPool, []mqttMessage) error {
				called = true
				return nil
			}
			c.homeAssistantTick(time.Now())
			if c.ha.cancel != nil {
				c.homeAssistantDone(<-c.results)
			}
			if called {
				t.Fatal("discovery contacted a broker without an active persistent permission")
			}
			if readHAState(c.store.Root).Topic != "" {
				t.Fatal("unapproved discovery recorded as published")
			}
		})
	}
}

func TestHAReconnectRefreshIsNotLostDuringPublication(t *testing.T) {
	c := haFixture(t, haSettings)
	c.ha.publish = func(context.Context, brokerSettings, string, *x509.CertPool, []mqttMessage) error { return nil }
	c.homeAssistantTick(time.Now())
	first := <-c.results
	// MQTT can reconnect while the independent discovery request is active.
	c.refreshHomeAssistant()
	c.homeAssistantDone(first)
	c.homeAssistantTick(time.Now())
	if c.ha.cancel == nil {
		t.Fatal("reconnect did not republish unchanged retained discovery")
	}
	c.homeAssistantDone(<-c.results)
	c.ha.next = time.Time{}
	c.homeAssistantTick(time.Now())
	if c.ha.cancel != nil {
		t.Fatal("a handled reconnect kept republishing")
	}
	c.ha.publish = func(context.Context, brokerSettings, string, *x509.CertPool, []mqttMessage) error {
		return errors.New("offline")
	}
	c.refreshHomeAssistant()
	c.homeAssistantTick(time.Now())
	c.homeAssistantDone(<-c.results)
	c.ha.next = time.Time{}
	c.homeAssistantTick(time.Now())
	if c.ha.cancel == nil {
		t.Fatal("failed refresh lost its retry because an older announcement exists")
	}
	c.homeAssistantDone(<-c.results)
}
