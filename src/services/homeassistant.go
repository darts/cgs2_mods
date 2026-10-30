package main

import (
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

// Shown in Home Assistant; tests keep it equal to MOD_VERSION in scripts/patch_app.py.
const modVersion = "V0.1"

const (
	haSwitchFile = "/data/etc/cgs2-homeassistant.ini"
	haStateFile  = "/data/etc/cgs2-homeassistant-state.json"
)

// The local publisher uses fixed units, independent of display preferences.
var haSensors = []struct {
	key, name, class, unit, icon string
	precision                    int
	diagnostic                   bool
}{
	{"temperature", "Temperature", "temperature", "°C", "", 1, false},
	{"humidity", "Humidity", "humidity", "%", "", 0, false},
	{"co2", "CO2", "carbon_dioxide", "ppm", "", 0, false},
	{"pm25", "PM2.5", "pm25", "µg/m³", "", 0, false},
	{"pm10", "PM10", "pm10", "µg/m³", "", 0, false},
	{"tvoc_index", "eTVOC index", "", "", "mdi:molecule", 0, false},
	{"noise", "Noise", "sound_pressure", "dB", "", 0, false},
	{"battery", "Battery", "battery", "%", "", 0, true},
}

// Ignore legacy firmware packets. Null readings mark an unavailable sensor.
func haTemplate(key string) string {
	return "{{ value_json.get('" + key + "', '') if value_json.get('version') == 1 else '' }}"
}

type haDesired struct {
	MQTT     bool // a broker is saved for the local publisher
	Enabled  bool // and the Home Assistant switch is on
	Broker   brokerSettings
	ClientID string
	Topic    string
	Payload  []byte
}

type haState struct {
	Broker      string `json:"broker"`
	Topic       string `json:"topic"`
	PayloadHash string `json:"payload_sha256"`
	PublishedAt string `json:"published_at"`
}

type haOutcome struct {
	action string
	want   haDesired
}

type haJob struct {
	cancel   context.CancelFunc
	next     time.Time
	failures int
	// The discovery is retained. Publish it once after each start, in case
	// the broker lost it, and after each change.
	announced bool
	refresh   bool
	publish   func(context.Context, brokerSettings, string, *x509.CertPool, []mqttMessage) error
}

func (w haDesired) broker() string {
	if !w.MQTT {
		return ""
	}
	return net.JoinHostPort(w.Broker.Host, strconv.Itoa(w.Broker.Port))
}

func payloadHash(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func readHomeAssistant(root string) (haDesired, error) {
	var want haDesired
	conf, e := readLiveConfig(root)
	if e != nil {
		return want, e
	}
	if !conf.Enabled {
		return want, nil
	}
	enabled := true
	if s, e := readQtINI(path(root, haSwitchFile)); e == nil {
		enabled = s["homeassistant"]["discovery"] != "false"
	} else if !os.IsNotExist(e) {
		return want, e
	}
	mac, e := os.ReadFile(path(root, "/sys/class/net/wlan0/address"))
	if e != nil {
		return want, errors.New("the Wi-Fi MAC address is not available")
	}
	hw, e := net.ParseMAC(strings.TrimSpace(string(mac)))
	if e != nil || len(hw) != 6 {
		return want, errors.New("the Wi-Fi MAC address is not valid")
	}
	firmware := "unknown"
	if release, e := os.ReadFile(path(root, "/qingping/etc/os-release")); e == nil {
		for _, line := range strings.Split(string(release), "\n") {
			if v, ok := strings.CutPrefix(strings.TrimSpace(line), "CLEARGRASS_VERSION="); ok && v != "" {
				firmware = v
			}
		}
	}
	id := "cgs2_" + strings.ReplaceAll(hw.String(), ":", "")
	want = haDesired{MQTT: true, Enabled: enabled, ClientID: "cgs2-ha-" + id[len("cgs2_"):],
		Broker:  conf.Broker,
		Topic:   "homeassistant/device/" + id + "/config",
		Payload: haDiscoveryPayload(id, hw.String(), firmware, conf.Topic, conf.Seconds)}
	return want, nil
}

func haDiscoveryPayload(id, mac, firmware, stateTopic string, seconds int) []byte {
	components := map[string]any{}
	for _, s := range haSensors {
		c := map[string]any{"platform": "sensor", "name": s.name, "unique_id": id + "_" + s.key,
			"value_template": haTemplate(s.key), "state_class": "measurement",
			"suggested_display_precision": s.precision,
			// Only fresh readings extend sensor availability.
			"expire_after": max(3*seconds, 180)}
		if s.class != "" {
			c["device_class"] = s.class
		}
		if s.unit != "" {
			c["unit_of_measurement"] = s.unit
		}
		if s.icon != "" {
			c["icon"] = s.icon
		}
		if s.diagnostic {
			c["entity_category"] = "diagnostic"
		}
		components[s.key] = c
	}
	b, _ := json.Marshal(map[string]any{
		"device": map[string]any{"identifiers": []string{id}, "name": "Qingping Air Monitor 2",
			"manufacturer": "Qingping", "model": "Air Monitor 2", "model_id": "CGS2",
			"sw_version": firmware + " (local " + modVersion + ")", "connections": [][]string{{"mac", mac}}},
		"origin":      map[string]any{"name": "Qingping Air Monitor 2 local control", "sw_version": modVersion},
		"state_topic": stateTopic,
		"components":  components,
	})
	return b
}

func readHAState(root string) haState {
	var state haState
	if b, e := os.ReadFile(path(root, haStateFile)); e == nil {
		if json.Unmarshal(b, &state) != nil {
			state = haState{}
		}
	}
	return state
}

// haAction returns "publish", "remove" or "". A removal needs the broker that
// holds the discovery; after MQTT is off, its permission is gone.
func haAction(want haDesired, state haState, announced bool) string {
	if want.Enabled {
		if !announced || state.Broker != want.broker() || state.Topic != want.Topic || state.PayloadHash != payloadHash(want.Payload) {
			return "publish"
		}
		return ""
	}
	if want.MQTT && state.Topic == want.Topic && state.Broker == want.broker() {
		return "remove"
	}
	return ""
}

func (c *Controller) setHAStatus(message string) {
	if c.status.HomeAssistant.Message != message {
		c.status.HomeAssistant.Message = message
		c.publish()
	}
}

// The local publisher requests discovery when it reconnects. The broker may have
// lost retained discovery even though our local publication record still exists.
func (c *Controller) refreshHomeAssistant() {
	c.ha.refresh = true
	c.ha.next = time.Time{}
}

func (s *Store) permitMQTT(ctx context.Context, broker brokerSettings) error {
	return s.locked(func() error {
		if err := ctx.Err(); err != nil {
			return err
		}
		pending, err := s.policyPending()
		if err != nil {
			return err
		}
		if pending {
			return errors.New("broker permission update is pending")
		}
		policy, err := s.bootPolicy()
		if err != nil {
			return err
		}
		current, err := readLiveConfig(s.Root)
		if err != nil || !current.Enabled || current.Broker != broker {
			return errors.New("the applied MQTT settings changed")
		}
		endpoint := "tcp:" + broker.Host + ":" + strconv.Itoa(broker.Port)
		for _, allowed := range policy {
			if allowed == endpoint {
				return nil
			}
		}
		return errors.New("the saved broker has no active MQTT permission")
	})
}

func (c *Controller) homeAssistantTick(now time.Time) {
	if c.ha.cancel != nil || now.Before(c.ha.next) {
		return
	}
	c.ha.next = now.Add(30 * time.Second)
	want, e := readHomeAssistant(c.store.Root)
	if e != nil {
		c.setHAStatus("Could not read the MQTT settings: " + e.Error())
		return
	}
	state := readHAState(c.store.Root)
	action := haAction(want, state, c.ha.announced && !c.ha.refresh)
	if action == "" {
		switch {
		case !want.MQTT:
			c.setHAStatus("MQTT is off")
		case !want.Enabled:
			c.setHAStatus("Off")
		default:
			c.setHAStatus("Published to the broker")
		}
		return
	}
	publish := c.ha.publish
	if publish == nil {
		publish = publishRetained
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	c.ha.cancel = cancel
	c.ha.refresh = false
	c.ha.announced = false
	message := mqttMessage{Topic: want.Topic, Payload: want.Payload}
	if action == "remove" {
		// An empty retained message removes the device from Home Assistant.
		message.Payload = nil
	}
	c.jobs.Add(1)
	go func() {
		defer c.jobs.Done()
		e := c.store.permitMQTT(ctx, want.Broker)
		if e == nil {
			e = publish(ctx, want.Broker, want.ClientID, nil, []mqttMessage{message})
		}
		cancel()
		select {
		case c.results <- jobResult{service: "homeassistant", err: e, ha: &haOutcome{action, want}}:
		case <-c.stopping:
		}
	}()
}

func (c *Controller) homeAssistantDone(r jobResult) {
	c.ha.cancel = nil
	now := time.Now()
	if r.err != nil {
		c.ha.failures++
		c.ha.next = now.Add(time.Minute << min(c.ha.failures-1, 4))
		c.setHAStatus("Could not update the broker: " + r.err.Error())
		return
	}
	c.ha.failures = 0
	state := haState{}
	if r.ha.action == "publish" {
		state = haState{Broker: r.ha.want.broker(), Topic: r.ha.want.Topic, PayloadHash: payloadHash(r.ha.want.Payload), PublishedAt: now.UTC().Format(time.RFC3339)}
	}
	if e := writeJSON(path(c.store.Root, haStateFile), state); e != nil {
		c.setHAStatus("Could not save the Home Assistant state: " + e.Error())
		return
	}
	if r.ha.action == "publish" {
		c.ha.announced = true
		c.status.HomeAssistant.LastSuccess = state.PublishedAt
		c.status.HomeAssistant.Message = "Published to the broker"
		c.publish()
	} else {
		c.setHAStatus("Removed from the broker")
	}
}

// haStop ends a publication in progress, so that the worker stops in time.
func (c *Controller) haStop() {
	if c.ha.cancel != nil {
		c.ha.cancel()
	}
}
