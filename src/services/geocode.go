package main

// Location lookup is a separately approved, one-shot action. Its request and
// results never become recurring service configuration or persistent policy.
import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

const locationRequestPath = "/data/etc/cgs2-location-search.ini"
const locationStatusPath = "/run/cgs2-services/location-search.json"
const locationUsedPath = "/run/cgs2-services/location-search-used.json"
const locationLastIDPath = "/data/etc/cgs2-location-search-last-id.json"

type LocationSearchRequest struct {
	RequestID   string `json:"request_id"`
	Query       string `json:"query"`
	Language    string `json:"language"`
	Approved    bool   `json:"approved"`
	Endpoint    string `json:"endpoint"`
	DNSServers  string `json:"dns_servers"`
	DNSPort     string `json:"dns_port"`
	DNSRotation bool   `json:"allow_dns_rotation"`
	AllowLAN    bool   `json:"allow_custom_lan"`
}
type LocationResult struct {
	Name      string  `json:"name"`
	Country   string  `json:"country"`
	Admin1    string  `json:"admin1"`
	Latitude  float64 `json:"latitude"`
	Longitude float64 `json:"longitude"`
	Timezone  string  `json:"timezone"`
}
type LocationSearchStatus struct {
	RequestID string           `json:"request_id"`
	Busy      bool             `json:"busy"`
	OK        bool             `json:"ok"`
	Message   string           `json:"message"`
	Results   []LocationResult `json:"results"`
}

func locationText(s string, limit int) bool {
	if !utf8.ValidString(s) || len(s) > limit {
		return false
	}
	for _, r := range s {
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			return false
		}
	}
	return true
}

func (r LocationSearchRequest) Validate() error {
	if !regexp.MustCompile(`^[A-Za-z0-9_-]{1,100}$`).MatchString(r.RequestID) {
		return errors.New("invalid location-search request identifier")
	}
	if !r.Approved {
		return errors.New("approve this one-time location search before connecting")
	}
	if !locationText(r.Query, 200) || utf8.RuneCountInString(strings.TrimSpace(r.Query)) < 2 {
		return errors.New("enter a city or postcode using 2 to 200 UTF-8 bytes")
	}
	if !regexp.MustCompile(`^[a-z]{2}$`).MatchString(r.Language) {
		return errors.New("location-search language must be a two-letter lowercase code")
	}
	u, err := url.Parse(r.Endpoint)
	if err != nil || len(r.Endpoint) > 1024 || u.Scheme != "https" || u.User != nil || u.Fragment != "" || u.RawQuery != "" || u.ForceQuery || u.Opaque != "" || strings.HasSuffix(u.Host, ":") || strings.ContainsAny(u.Host, "[]") || !validHost(u.Hostname(), r.AllowLAN) {
		return errors.New("location endpoint must be HTTPS with an approved host and no credentials, query or fragment")
	}
	if u.Port() != "" {
		if _, err := number(u.Port(), 1, 65535); err != nil {
			return errors.New("invalid location endpoint port")
		}
	}
	if _, err := number(r.DNSPort, 1, 65535); err != nil {
		return errors.New("invalid location-search DNS port")
	}
	servers := comma(r.DNSServers)
	if len(servers) < 1 || len(servers) > 4 || len(r.DNSServers) > 100 {
		return errors.New("location search needs one to four numeric DNS servers")
	}
	for _, ip := range servers {
		// A router may be an explicitly selected resolver even when the HTTPS
		// provider itself is restricted to public addresses.
		if _, err := networkIPv4(ip, true); err != nil {
			return errors.New("invalid location-search DNS server")
		}
	}
	if net.ParseIP(u.Hostname()) == nil && !r.DNSRotation {
		return errors.New("approve DNS resolution for this location-search hostname")
	}
	return nil
}

func readLocationRequest(root string) (LocationSearchRequest, error) {
	var r LocationSearchRequest
	f, err := os.Open(path(root, locationRequestPath))
	if err != nil {
		return r, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, 16385))
	if err != nil || len(b) > 16384 {
		return r, errors.New("location-search draft is unreadable or too large")
	}
	section, payload := "", ""
	found := false
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "[") {
			section = line
		} else if section == "[request]" && strings.HasPrefix(line, "payload=") {
			if found {
				return r, errors.New("duplicate location-search payload")
			}
			found = true
			payload = strings.TrimPrefix(line, "payload=")
			if len(payload) >= 2 && payload[0] == '"' && payload[len(payload)-1] == '"' {
				payload = payload[1 : len(payload)-1]
			}
		}
	}
	raw, err := base64.StdEncoding.Strict().DecodeString(payload)
	if err != nil || len(raw) == 0 || len(raw) > 8192 || !utf8.Valid(raw) {
		return r, errors.New("invalid location-search encoding")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(&r); err != nil {
		return r, errors.New("invalid location-search fields")
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return r, errors.New("trailing location-search data")
	}
	return r, r.Validate()
}

func locationURL(r LocationSearchRequest) (*url.URL, error) {
	if err := r.Validate(); err != nil {
		return nil, err
	}
	u, _ := url.Parse(r.Endpoint)
	u.RawQuery = url.Values{"name": {strings.TrimSpace(r.Query)}, "count": {"10"}, "language": {r.Language}, "format": {"json"}}.Encode()
	return u, nil
}

func parseLocations(body []byte) ([]LocationResult, error) {
	var raw struct {
		Error   bool `json:"error"`
		Results []struct {
			Name, Country, Admin1, Timezone string
			Latitude, Longitude             *float64
		} `json:"results"`
	}
	if !utf8.Valid(body) || json.Unmarshal(body, &raw) != nil || bytes.Equal(bytes.TrimSpace(body), []byte("null")) {
		return nil, errors.New("location response is not a JSON object")
	}
	if raw.Error {
		return nil, errors.New("location provider rejected the search")
	}
	if len(raw.Results) > 10 {
		return nil, errors.New("location response contains too many results")
	}
	out := make([]LocationResult, 0, len(raw.Results))
	for _, item := range raw.Results {
		if strings.TrimSpace(item.Name) == "" || !locationText(item.Name, 200) || !locationText(item.Country, 200) || !locationText(item.Admin1, 200) || item.Latitude == nil || item.Longitude == nil || !networkFinite(*item.Latitude, -90, 90) || !networkFinite(*item.Longitude, -180, 180) || !validZone(item.Timezone) {
			return nil, errors.New("location response has invalid names, coordinates or timezone")
		}
		out = append(out, LocationResult{item.Name, item.Country, item.Admin1, *item.Latitude, *item.Longitude, item.Timezone})
	}
	return out, nil
}

func fetchLocations(ctx context.Context, r LocationSearchRequest, answer DNSAnswer, permit PermitEndpoint, opts networkHTTPOptions) ([]LocationResult, error) {
	u, err := locationURL(r)
	if err != nil {
		return nil, err
	}
	body, err := fetchNetworkJSON(ctx, "geocode", u, answer, r.AllowLAN, 15*time.Second, 128*1024, permit, opts)
	if err != nil {
		return nil, err
	}
	return parseLocations(body)
}

func (c *Controller) runGeocode(ctx context.Context, conf Config, r LocationSearchRequest) ([]LocationResult, error) {
	u, _ := url.Parse(r.Endpoint)
	host, _ := networkHost(u.Hostname())
	answer := DNSAnswer{Host: host, IPv4: []string{host}}
	permit := c.store.permitGeocode(conf, r)
	if net.ParseIP(host) == nil {
		port, _ := number(r.DNSPort, 1, 65535)
		var err error
		for _, ip := range comma(r.DNSServers) {
			answer, err = ResolveIPv4(ctx, host, DNSResolverConfig{Service: "geocode", IPv4: ip, Port: uint16(port), Timeout: 4 * time.Second}, permit)
			if err == nil || ctx.Err() != nil {
				break
			}
		}
		if err != nil {
			return nil, err
		}
	}
	return fetchLocations(ctx, r, answer, permit, networkHTTPOptions{})
}

// IDs alone are retained in RAM across worker restarts. The last consumed ID
// also persists across a cold boot, so the still-staged request cannot reuse
// old consent. No query, location or provider is retained in either marker.
func (c *Controller) consumeLocationID(id string) error {
	last, err := os.ReadFile(path(c.store.Root, locationLastIDPath))
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	if err == nil {
		var previous string
		if len(last) > 256 || json.Unmarshal(last, &previous) != nil || !regexp.MustCompile(`^[A-Za-z0-9_-]{1,100}$`).MatchString(previous) {
			return errors.New("invalid last consumed location-search identifier")
		}
		if previous == id {
			return errors.New("location-search request has already been used")
		}
	}
	var used []string
	b, err := os.ReadFile(path(c.store.Root, locationUsedPath))
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	if len(b) > 128*1024 || len(b) != 0 && json.Unmarshal(b, &used) != nil {
		return errors.New("invalid consumed location-search request state")
	}
	for _, previous := range used {
		if previous == id {
			return errors.New("location-search request has already been used")
		}
	}
	if len(used) >= 1024 {
		return errors.New("location-search limit reached for this boot")
	}
	// Any failure consumes consent without starting a socket, rather than
	// risking replay when one of the two durable writes did not complete.
	if err := writeJSON(path(c.store.Root, locationLastIDPath), id); err != nil {
		return err
	}
	return writeJSON(path(c.store.Root, locationUsedPath), append(used, id))
}

func (c *Controller) publishLocation(s LocationSearchStatus) error {
	if s.Results == nil {
		s.Results = []LocationResult{}
	}
	return writeJSON(path(c.store.Root, locationStatusPath), s)
}

func (c *Controller) cancelLocation() {
	if cancel := c.cancel["geocode"]; cancel != nil {
		cancel()
		delete(c.cancel, "geocode")
	}
	if c.locationID != "" {
		c.publishLocation(LocationSearchStatus{RequestID: c.locationID, Message: "Location search cancelled; approve a new search to retry."})
		c.locationID = ""
	}
}

func (c *Controller) startGeocode() error {
	r, err := readLocationRequest(c.store.Root)
	if err != nil {
		if r.RequestID != "" && r.RequestID != c.locationID {
			c.publishLocation(LocationSearchStatus{RequestID: r.RequestID, Message: err.Error()})
		}
		return err
	}
	if err = c.consumeLocationID(r.RequestID); err != nil {
		return err
	}
	c.cancelLocation()
	if err = c.publishLocation(LocationSearchStatus{RequestID: r.RequestID, Busy: true, Message: "Searching the approved location provider…"}); err != nil {
		return err
	}
	c.locationID = r.RequestID
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	c.cancel["geocode"] = cancel
	conf := c.config
	run := c.geocodeRun
	if run == nil {
		run = c.runGeocode
	}
	c.jobs.Add(1)
	go func() {
		defer c.jobs.Done()
		locations, err := run(ctx, conf, r)
		if ctx.Err() != nil {
			err = ctx.Err()
		}
		cancel()
		if cleanup := c.store.revoke("geocode", r.RequestID); cleanup != nil {
			err = fmt.Errorf("could not revoke temporary location-search permission: %w", cleanup)
		}
		result := jobResult{service: "geocode", generation: r.RequestID, err: err, locations: locations}
		select {
		case c.results <- result:
		case <-c.stopping:
		}
	}()
	return nil
}

func (c *Controller) locationDone(r jobResult) {
	if r.generation != c.locationID {
		return
	}
	delete(c.cancel, "geocode")
	c.locationID = ""
	s := LocationSearchStatus{RequestID: r.generation, OK: r.err == nil, Message: "Select a location to use its coordinates and timezone.", Results: r.locations}
	if r.err != nil {
		s.Results = nil
		s.Message = "Location search failed: " + r.err.Error()
	} else if len(r.locations) == 0 {
		s.Message = "No matching locations. Try a nearby city or add a country."
	}
	c.publishLocation(s)
}

func (c *Controller) recoverLocationStatus() error {
	b, err := os.ReadFile(path(c.store.Root, locationStatusPath))
	if os.IsNotExist(err) {
		return nil
	}
	var s LocationSearchStatus
	if err != nil || len(b) > 128*1024 || json.Unmarshal(b, &s) != nil {
		// Optional UI status must never prevent recurring services from starting.
		// Starting a new search still requires successfully writing its busy
		// status before any network work can be scheduled.
		fmt.Fprintln(os.Stderr, "location-search status unavailable; no search resumed")
		c.publishLocation(LocationSearchStatus{Message: "Previous location-search status is unavailable; approve a new search to retry."})
		return nil
	}
	if s.Busy {
		if err := c.publishLocation(LocationSearchStatus{RequestID: s.RequestID, Message: "Location search was interrupted; approve a new search to retry."}); err != nil {
			fmt.Fprintln(os.Stderr, "location-search status unavailable; no search resumed")
		}
	}
	return nil
}
