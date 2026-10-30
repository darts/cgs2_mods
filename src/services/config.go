package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
	_ "time/tzdata"
)

type Config struct {
	Version         int    `json:"version"`
	RequestID       string `json:"request_id"`
	TimeEnabled     bool   `json:"time_enabled"`
	WeatherEnabled  bool   `json:"weather_enabled"`
	DNSEnabled      bool   `json:"dns_enabled"`
	DNSRotation     bool   `json:"allow_dns_rotation"`
	AllowLAN        bool   `json:"allow_custom_lan"`
	TimeServers     string `json:"time_servers"`
	TimePort        string `json:"time_port"`
	TimeHours       string `json:"time_interval_hours"`
	TimeTimezone    string `json:"time_timezone"`
	WeatherURL      string `json:"weather_url"`
	Latitude        string `json:"weather_latitude"`
	Longitude       string `json:"weather_longitude"`
	Location        string `json:"weather_location"`
	WeatherTimezone string `json:"weather_timezone"`
	TemperatureUnit string `json:"weather_temperature_unit"`
	WeatherMinutes  string `json:"weather_interval_minutes"`
	DNSServers      string `json:"dns_servers"`
	DNSPort         string `json:"dns_port"`
}

func defaults() Config {
	return Config{Version: 1, TimeServers: "162.159.200.1,162.159.200.123", TimePort: "123", TimeHours: "6", TimeTimezone: "Etc/UTC", WeatherURL: "https://api.open-meteo.com/v1/forecast", WeatherTimezone: "Etc/UTC", TemperatureUnit: "celsius", WeatherMinutes: "30", DNSServers: "9.9.9.9,149.112.112.112", DNSPort: "53"}
}
func comma(s string) []string {
	var out []string
	for _, v := range strings.Split(s, ",") {
		if t := strings.TrimSpace(v); t != "" {
			out = append(out, t)
		}
	}
	return out
}
func number(s string, low, high int) (int, error) {
	if !regexp.MustCompile(`^[0-9]+$`).MatchString(s) {
		return 0, errors.New("expected a whole number")
	}
	n, e := strconv.Atoi(s)
	if e != nil || n < low || n > high {
		return 0, fmt.Errorf("must be between %d and %d", low, high)
	}
	return n, nil
}
func forbiddenHost(s string) bool {
	s = strings.ToLower(s)
	return strings.Contains(s, "qingping") || strings.Contains(s, "cleargrass")
}
func validIPv4(s string, private bool) bool {
	ip := net.ParseIP(s)
	if ip == nil || ip.To4() == nil || ip.String() != s || ip.IsUnspecified() || ip.IsLoopback() || ip.IsMulticast() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip[12] == 0 || ip[12] >= 224 {
		return false
	}
	if !private && (ip.IsPrivate() || ip[12] == 100 && ip[13] >= 64 && ip[13] <= 127) {
		return false
	}
	return true
}
func validHost(s string, private bool) bool {
	if net.ParseIP(s) != nil {
		_, err := networkIPv4(s, private)
		return err == nil
	}
	if len(s) > 253 || forbiddenHost(s) || strings.ContainsAny(s, "/:@ \\%") || strings.HasSuffix(s, ".") {
		return false
	}
	parts := strings.Split(s, ".")
	if len(parts) < 2 {
		return false
	}
	for _, p := range parts {
		if !regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$`).MatchString(p) {
			return false
		}
	}
	return true
}
func validZone(s string) bool {
	if !regexp.MustCompile(`^[A-Za-z0-9_+/-]{1,100}$`).MatchString(s) || strings.Contains(s, "..") || strings.HasPrefix(s, "/") {
		return false
	}
	_, e := time.LoadLocation(s)
	return e == nil
}

func (c Config) Validate() error {
	if c.Version != 1 || len(c.RequestID) > 100 {
		return errors.New("unsupported settings version or request identifier")
	}
	for name, s := range map[string]string{"location": c.Location, "timezone": c.TimeTimezone, "weather timezone": c.WeatherTimezone} {
		if len(s) > 200 || strings.ContainsAny(s, "\x00\r\n") {
			return fmt.Errorf("invalid %s", name)
		}
	}
	if !validZone(c.TimeTimezone) || !validZone(c.WeatherTimezone) {
		return errors.New("enter a valid IANA timezone")
	}
	for name, v := range map[string]string{"NTP port": c.TimePort, "DNS port": c.DNSPort} {
		if _, e := number(v, 1, 65535); e != nil {
			return fmt.Errorf("%s: %w", name, e)
		}
	}
	if _, e := number(c.TimeHours, 1, 8760); e != nil {
		return fmt.Errorf("time interval: %w", e)
	}
	if _, e := number(c.WeatherMinutes, 1, 10080); e != nil {
		return fmt.Errorf("weather interval: %w", e)
	}
	servers := comma(c.TimeServers)
	if len(servers) == 0 || len(servers) > 4 {
		return errors.New("enter one to four time servers")
	}
	for _, host := range servers {
		if !validHost(host, c.AllowLAN) {
			return fmt.Errorf("invalid or forbidden time server %q; private addresses need LAN permission", host)
		}
		if c.TimeEnabled && net.ParseIP(host) == nil && (!c.DNSEnabled || !c.DNSRotation) {
			return errors.New("time hostnames require DNS and explicit DNS rotation approval")
		}
	}
	dns := comma(c.DNSServers)
	if len(dns) == 0 || len(dns) > 4 {
		return errors.New("enter one to four numeric DNS servers")
	}
	for _, ip := range dns {
		if _, err := networkIPv4(ip, true); err != nil {
			return errors.New("DNS servers must be unicast IPv4 addresses")
		}
	}
	u, e := url.Parse(c.WeatherURL)
	if e != nil || u.Scheme != "https" || u.User != nil || u.Fragment != "" || u.RawQuery != "" || u.ForceQuery || u.Opaque != "" || strings.HasSuffix(u.Host, ":") || strings.ContainsAny(u.Host, "[]") || !validHost(u.Hostname(), c.AllowLAN) || len(c.WeatherURL) > 1024 {
		return errors.New("weather URL must be HTTPS with an approved host, no credentials, query or fragment")
	}
	if u.Port() != "" {
		if _, e := number(u.Port(), 1, 65535); e != nil {
			return fmt.Errorf("weather port: %w", e)
		}
	}
	if c.TemperatureUnit != "celsius" && c.TemperatureUnit != "fahrenheit" {
		return errors.New("temperature unit must be celsius or fahrenheit")
	}
	if c.WeatherEnabled && net.ParseIP(u.Hostname()) == nil && (!c.DNSEnabled || !c.DNSRotation) {
		return errors.New("weather hostname requires DNS and explicit DNS rotation approval")
	}
	for name, s := range map[string]string{"latitude": c.Latitude, "longitude": c.Longitude} {
		if s == "" && !c.WeatherEnabled {
			continue
		}
		n, e := strconv.ParseFloat(s, 64)
		lim := 180.0
		if name == "latitude" {
			lim = 90
		}
		if e != nil || n != n || n < -lim || n > lim {
			return fmt.Errorf("invalid weather %s", name)
		}
	}
	return nil
}

func decodeConfig(b []byte) (Config, error) {
	var c Config
	if len(b) > 16384 {
		return c, errors.New("settings too large")
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	e := d.Decode(&c)
	if e == nil {
		var extra any
		if d.Decode(&extra) != io.EOF {
			e = errors.New("trailing settings data")
		}
	}
	return c, e
}
func readConfig(root string) (Config, error) {
	b, e := os.ReadFile(path(root, "/etc/cgs2-privacy/services.json"))
	if e != nil {
		return Config{}, e
	}
	c, e := decodeConfig(b)
	if e == nil {
		e = c.Validate()
	}
	return c, e
}
func readDraft(root string) (Config, error) {
	b, e := os.ReadFile(path(root, "/data/etc/cgs2-services-draft.ini"))
	if e != nil {
		return Config{}, e
	}
	if len(b) > 32768 {
		return Config{}, errors.New("draft too large")
	}
	section := ""
	payload := ""
	found := false
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "[") {
			section = line
			continue
		}
		if section == "[request]" && strings.HasPrefix(line, "payload=") {
			if found {
				return Config{}, errors.New("duplicate draft payload")
			}
			found = true
			payload = strings.TrimPrefix(line, "payload=")
			if strings.HasPrefix(payload, `"`) && strings.HasSuffix(payload, `"`) && len(payload) >= 2 {
				payload = payload[1 : len(payload)-1]
			}
		}
	}
	raw, e := base64.StdEncoding.DecodeString(payload)
	if e != nil || payload == "" {
		return Config{}, errors.New("invalid draft encoding")
	}
	return decodeConfig(raw)
}
func path(root, p string) string {
	if root == "" {
		return p
	}
	return filepath.Join(root, p)
}
func atomicWrite(p string, b []byte) error {
	if e := os.MkdirAll(filepath.Dir(p), 0700); e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(p), ".cgs2-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(0600); e == nil {
		_, e = f.Write(b)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e == nil {
		e = os.Rename(name, p)
	}
	if e == nil {
		if d, de := os.Open(filepath.Dir(p)); de == nil {
			e = d.Sync()
			d.Close()
		}
	}
	return e
}
func writeJSON(p string, v any) error {
	b, e := json.MarshalIndent(v, "", "  ")
	if e != nil {
		return e
	}
	return atomicWrite(p, append(b, '\n'))
}
