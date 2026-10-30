package main

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func testServiceConfig() Config {
	c := defaults()
	c.RequestID = "test-generation"
	c.TimeEnabled, c.WeatherEnabled, c.DNSEnabled, c.DNSRotation = true, true, true, true
	c.Latitude, c.Longitude = "52.52", "13.405"
	return c
}

func TestConfigDefaultsGrantNothing(t *testing.T) {
	c := defaults()
	if c.TimeEnabled || c.WeatherEnabled || c.DNSEnabled || c.DNSRotation || c.AllowLAN {
		t.Fatal("defaults must not approve network access")
	}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestConfigHostnameNeedsExplicitResolverAndRotation(t *testing.T) {
	for _, service := range []string{"time", "weather"} {
		t.Run(service, func(t *testing.T) {
			c := defaults()
			if service == "time" {
				c.TimeEnabled, c.TimeServers = true, "time.cloudflare.com"
			} else {
				c.WeatherEnabled, c.Latitude, c.Longitude = true, "52", "13"
			}
			if err := c.Validate(); err == nil {
				t.Fatal("hostname accepted without resolver approval")
			}
			c.DNSEnabled = true
			if err := c.Validate(); err == nil {
				t.Fatal("hostname accepted without DNS rotation consent")
			}
			c.DNSRotation = true
			if err := c.Validate(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestConfigRejectsUnsafeURLsAndVendorHosts(t *testing.T) {
	for _, endpoint := range []string{
		"http://api.open-meteo.com/v1/forecast", "https://user:password@api.open-meteo.com/v1/forecast",
		"https://api.open-meteo.com/v1/forecast?", "https://api.open-meteo.com/v1/forecast?key=secret",
		"https://api.open-meteo.com/v1/forecast#fragment", "https://api.open-meteo.com:/v1/forecast",
		"https://api.open-meteo.com:0/v1/forecast", "https://api.open-meteo.com:65536/v1/forecast",
		"https://[api.open-meteo.com]/v1/forecast", "https://[::1]/weather", "https:api.open-meteo.com",
		"https://127.0.0.1/weather", "https://169.254.169.254/weather", "https://192.0.2.4/weather",
		"https://api.qingping.co/weather", "https://api.cleargrass.com/weather",
	} {
		t.Run(endpoint, func(t *testing.T) {
			c := testServiceConfig()
			c.WeatherURL = endpoint
			if err := c.Validate(); err == nil {
				t.Fatal("unsafe weather endpoint accepted")
			}
		})
	}
	for _, host := range []string{"api.qingping.co", "api.cleargrass.com", "localhost", "127.0.0.1", "169.254.1.1", "198.18.0.1", "203.0.113.1", "pool.ntp.org;reboot"} {
		c := testServiceConfig()
		c.TimeServers = host
		if err := c.Validate(); err == nil {
			t.Errorf("unsafe time server accepted: %q", host)
		}
	}
}

func TestConfigLANEndpointsNeedExplicitPermission(t *testing.T) {
	c := testServiceConfig()
	c.TimeServers, c.WeatherURL = "192.168.1.2", "https://10.0.0.2:8443/weather"
	if err := c.Validate(); err == nil {
		t.Fatal("private service endpoints accepted without LAN opt-in")
	}
	c.AllowLAN = true
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	for _, host := range []string{"127.0.0.1", "169.254.1.1", "224.0.0.1", "255.255.255.255", "0.1.2.3", "100.64.0.1"} {
		c.TimeServers = host
		if err := c.Validate(); err == nil {
			t.Errorf("LAN opt-in unexpectedly permits unusable address %s", host)
		}
	}
}

func TestConfigFieldBounds(t *testing.T) {
	changes := map[string]func(*Config){
		"latitude NaN":       func(c *Config) { c.Latitude = "NaN" },
		"longitude infinity": func(c *Config) { c.Longitude = "+Inf" },
		"latitude bounds":    func(c *Config) { c.Latitude = "90.1" },
		"timezone traversal": func(c *Config) { c.TimeTimezone = "../../etc/passwd" },
		"timezone missing":   func(c *Config) { c.WeatherTimezone = "Not/AZone" },
		"invalid units":      func(c *Config) { c.TemperatureUnit = "kelvin" },
		"port option":        func(c *Config) { c.TimePort = "--help" },
		"port range":         func(c *Config) { c.DNSPort = "65536" },
		"location newline":   func(c *Config) { c.Location = "city\nsecret" },
		"too many servers":   func(c *Config) { c.TimeServers = "1.1.1.1,1.0.0.1,8.8.8.8,8.8.4.4,9.9.9.9" },
		"resolver hostname":  func(c *Config) { c.DNSServers = "dns.quad9.net" },
		"unknown version":    func(c *Config) { c.Version = 2 },
	}
	for name, change := range changes {
		t.Run(name, func(t *testing.T) {
			c := testServiceConfig()
			change(&c)
			if err := c.Validate(); err == nil {
				t.Fatal("invalid setting accepted")
			}
		})
	}
}

func TestConfigJSONRejectsUnknownOversizedAndTrailingData(t *testing.T) {
	b, err := json.Marshal(testServiceConfig())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := decodeConfig(append(b, '\n', ' ')); err != nil {
		t.Fatal(err)
	}
	for _, bad := range [][]byte{
		[]byte(`{"unknown":true}`), []byte(strings.Repeat(" ", 16385)),
		append(append([]byte{}, b...), []byte(` {}`)...), append(append([]byte{}, b...), 'x'),
		append(append([]byte{}, b...), []byte(` {"unfinished":`)...),
	} {
		if _, err := decodeConfig(bad); err == nil {
			t.Fatal("malformed or extra settings accepted")
		}
	}
}

func TestConfigDraftEncodingAndDuplicateGuard(t *testing.T) {
	root := t.TempDir()
	b, _ := json.Marshal(testServiceConfig())
	payload := base64.StdEncoding.EncodeToString(b)
	p := path(root, "/data/etc/cgs2-services-draft.ini")
	for _, text := range []string{"[request]\npayload=" + payload + "\n", "[request]\npayload=\"" + payload + "\"\n"} {
		if err := atomicWrite(p, []byte(text)); err != nil {
			t.Fatal(err)
		}
		c, err := readDraft(root)
		if err != nil || c.RequestID != "test-generation" {
			t.Fatalf("valid draft failed: %v", err)
		}
	}
	for _, text := range []string{
		"[other]\npayload=" + payload,
		"[request]\npayload=" + payload + "\npayload=" + payload,
		"[request]\npayload=\npayload=" + payload,
		"[request]\npayload=\"\"\"" + payload + "\"\"\"",
		"[request]\npayload=invalid base64!",
	} {
		if err := atomicWrite(p, []byte(text)); err != nil {
			t.Fatal(err)
		}
		if _, err := readDraft(root); err == nil {
			t.Fatal("invalid draft accepted")
		}
	}
}

func TestConfigAtomicWriteIsPrivateAndLeavesNoTemporaryFiles(t *testing.T) {
	p := filepath.Join(t.TempDir(), "private", "settings.json")
	for _, content := range []string{"first", "second"} {
		if err := atomicWrite(p, []byte(content)); err != nil {
			t.Fatal(err)
		}
		b, _ := os.ReadFile(p)
		info, _ := os.Stat(p)
		if string(b) != content || info.Mode().Perm() != 0600 {
			t.Fatal("incorrect data or permissions")
		}
	}
	entries, err := os.ReadDir(filepath.Dir(p))
	if err != nil || len(entries) != 1 {
		t.Fatalf("temporary files leaked: %v", err)
	}
}
