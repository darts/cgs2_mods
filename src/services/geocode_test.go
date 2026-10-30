package main

import (
	"context"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const testLocationsJSON = `{"results":[{"name":"Berlin","latitude":52.52437,"longitude":13.41053,"timezone":"Europe/Berlin","country":"Germany","admin1":"Berlin"}]}`

func testLocationRequest() LocationSearchRequest {
	return LocationSearchRequest{RequestID: "search-1", Query: "Berlin", Language: "en", Approved: true, Endpoint: "https://geocoding-api.open-meteo.com/v1/search", DNSServers: "9.9.9.9,149.112.112.112", DNSPort: "53", DNSRotation: true}
}
func stageLocation(t *testing.T, c *Controller, r LocationSearchRequest) {
	t.Helper()
	b, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	data := "[preferences]\nendpoint=https://ignored.example\n[request]\npayload=" + base64.StdEncoding.EncodeToString(b) + "\n"
	if err := atomicWrite(path(c.store.Root, locationRequestPath), []byte(data)); err != nil {
		t.Fatal(err)
	}
}
func readLocationStatus(t *testing.T, c *Controller) LocationSearchStatus {
	t.Helper()
	b, err := os.ReadFile(path(c.store.Root, locationStatusPath))
	if err != nil {
		t.Fatal(err)
	}
	var s LocationSearchStatus
	if err := json.Unmarshal(b, &s); err != nil {
		t.Fatal(err)
	}
	return s
}

func TestLocationRequestValidation(t *testing.T) {
	r := testLocationRequest()
	if err := r.Validate(); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*LocationSearchRequest){
		"no approval":           func(r *LocationSearchRequest) { r.Approved = false },
		"no DNS grant":          func(r *LocationSearchRequest) { r.DNSRotation = false },
		"query short":           func(r *LocationSearchRequest) { r.Query = "a" },
		"query oversized":       func(r *LocationSearchRequest) { r.Query = strings.Repeat("a", 201) },
		"query control":         func(r *LocationSearchRequest) { r.Query = "Berlin\nsecret" },
		"query bidi":            func(r *LocationSearchRequest) { r.Query = "\u202eBerlin" },
		"query invalid UTF8":    func(r *LocationSearchRequest) { r.Query = "ab\xff" },
		"invalid ID":            func(r *LocationSearchRequest) { r.RequestID = "../../file" },
		"language":              func(r *LocationSearchRequest) { r.Language = "en-US" },
		"HTTP":                  func(r *LocationSearchRequest) { r.Endpoint = "http://example.com/search" },
		"query URL":             func(r *LocationSearchRequest) { r.Endpoint += "?secret=1" },
		"credentials":           func(r *LocationSearchRequest) { r.Endpoint = "https://user:password@example.com/search" },
		"vendor":                func(r *LocationSearchRequest) { r.Endpoint = "https://api.qingping.co/search" },
		"vendor alias":          func(r *LocationSearchRequest) { r.Endpoint = "https://api.cleargrass.com/search" },
		"loopback":              func(r *LocationSearchRequest) { r.Endpoint = "https://127.0.0.1/"; r.AllowLAN = true },
		"reserved":              func(r *LocationSearchRequest) { r.Endpoint = "https://192.0.2.1/" },
		"private without grant": func(r *LocationSearchRequest) { r.Endpoint = "https://192.168.1.3/" },
		"bad resolver":          func(r *LocationSearchRequest) { r.DNSServers = "resolver.example" },
		"bad port":              func(r *LocationSearchRequest) { r.DNSPort = "0" },
	} {
		t.Run(name, func(t *testing.T) {
			bad := r
			mutate(&bad)
			if err := bad.Validate(); err == nil {
				t.Fatal("accepted invalid search approval")
			}
		})
	}
	r.Query = "München, Germany"
	r.Endpoint = "https://192.168.1.3:8443/search"
	r.AllowLAN, r.DNSRotation = true, false
	u, err := locationURL(r)
	if err != nil || u.Port() != "8443" || u.Query().Get("name") != r.Query || u.Query().Get("count") != "10" {
		t.Fatalf("explicit LAN/Unicode/custom-port request rejected: %v %v", u, err)
	}
}

func TestLocationDraftAndResultBounds(t *testing.T) {
	c := controllerFixture(t)
	r := testLocationRequest()
	stageLocation(t, c, r)
	if got, err := readLocationRequest(c.store.Root); err != nil || got != r {
		t.Fatalf("draft round trip failed: %+v %v", got, err)
	}
	for _, draft := range []string{"[request]\npayload=bad!\n", "[request]\npayload=e30=\npayload=e30=\n", strings.Repeat("a", 16385), "[request]\npayload=" + base64.StdEncoding.EncodeToString([]byte(`{"approved":true,"unknown":1}`))} {
		atomicWrite(path(c.store.Root, locationRequestPath), []byte(draft))
		if _, err := readLocationRequest(c.store.Root); err == nil {
			t.Fatal("accepted malformed draft")
		}
	}
	if got, err := parseLocations([]byte(testLocationsJSON)); err != nil || len(got) != 1 || got[0].Timezone != "Europe/Berlin" {
		t.Fatalf("valid location rejected: %v %v", got, err)
	}
	if got, err := parseLocations([]byte(`{"generationtime_ms":0.1}`)); err != nil || len(got) != 0 {
		t.Fatal("Open-Meteo empty result rejected")
	}
	for _, body := range []string{"null", "[]", `{"error":true,"reason":"secret"}`, strings.Replace(testLocationsJSON, "52.52437", "999", 1), strings.Replace(testLocationsJSON, "Europe/Berlin", "Europe/Invalid", 1), strings.Replace(testLocationsJSON, `"country":"Germany"`, `"country":"bad\ntext"`, 1), strings.Replace(testLocationsJSON, `"latitude":52.52437,`, "", 1), `{"results":[` + strings.Repeat(`{},`, 10) + `{ }]}`} {
		if _, err := parseLocations([]byte(body)); err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatalf("invalid or private provider response handling: %s %v", body, err)
		}
	}
}

func TestLocationHTTPSConsentTLSQueryAndPrivacy(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.TLS.ServerName != "example.com" || !strings.HasPrefix(r.Host, "example.com:") || r.URL.Query().Get("name") != "München 80331" || r.URL.Query().Get("count") != "10" || r.URL.Query().Get("language") != "en" || len(r.URL.Query()) != 4 {
			t.Error("unexpected TLS identity or query")
		}
		io.WriteString(w, testLocationsJSON)
	}))
	defer server.Close()
	roots := x509.NewCertPool()
	roots.AddCert(server.Certificate())
	_, port, _ := net.SplitHostPort(server.Listener.Addr().String())
	r := testLocationRequest()
	r.Query, r.Endpoint = "München 80331", "https://example.com:"+port+"/search"
	answer := DNSAnswer{Host: "example.com", IPv4: []string{"1.1.1.1"}, ExpiresAt: time.Now().Add(time.Minute)}
	allowed, dials := false, 0
	opts := networkHTTPOptions{roots: roots, dial: func(ctx context.Context, network, address string) (net.Conn, error) {
		dials++
		if !allowed || network != "tcp4" || address != "1.1.1.1:"+port {
			return nil, errors.New("socket not pinned to permitted endpoint")
		}
		return (&net.Dialer{}).DialContext(ctx, "tcp4", server.Listener.Addr().String())
	}}
	permit := func(_ context.Context, p EndpointPermit) error {
		if p.Service != "geocode" || p.Hostname != "example.com" || p.Protocol != "tcp" || p.IPv4 != "1.1.1.1" {
			return errors.New("wrong permission metadata")
		}
		allowed = true
		return nil
	}
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	if got, err := fetchLocations(context.Background(), r, answer, permit, opts); err != nil || len(got) != 1 || calls.Load() != 1 || dials != 1 {
		t.Fatalf("lookup failed: %v %v", got, err)
	}
	if _, err := fetchLocations(context.Background(), r, answer, func(context.Context, EndpointPermit) error { return errors.New("denied") }, opts); err == nil || dials != 1 {
		t.Fatal("permission denial opened a socket")
	}
	r.Approved = false
	if _, err := fetchLocations(context.Background(), r, answer, permit, opts); err == nil || dials != 1 {
		t.Fatal("unapproved lookup opened a socket")
	}
	r.Approved = true
	r.Endpoint = "https://wrong.example:" + port + "/search"
	answer.Host = "wrong.example"
	if _, err := fetchLocations(context.Background(), r, answer, func(context.Context, EndpointPermit) error { return nil }, opts); err == nil || calls.Load() != 1 || strings.Contains(err.Error(), "München") || strings.Contains(err.Error(), "80331") {
		t.Fatalf("wrong TLS name accepted or query leaked: %v", err)
	}
}

func TestLocationHTTPSRejectsUnsafeResponsesAndAnswers(t *testing.T) {
	for _, mode := range []string{"redirect", "body", "HTTP error", "invalid JSON"} {
		t.Run(mode, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				switch mode {
				case "redirect":
					http.Redirect(w, r, "https://unapproved.example/", http.StatusFound)
				case "body":
					io.WriteString(w, strings.Repeat("x", 128*1024+1))
				case "HTTP error":
					http.Error(w, "private diagnostic", 500)
				default:
					io.WriteString(w, "invalid private diagnostic")
				}
			}))
			defer server.Close()
			roots := x509.NewCertPool()
			roots.AddCert(server.Certificate())
			_, port, _ := net.SplitHostPort(server.Listener.Addr().String())
			r := testLocationRequest()
			r.Endpoint = "https://example.com:" + port
			opts := networkHTTPOptions{roots: roots, dial: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp4", server.Listener.Addr().String())
			}}
			_, err := fetchLocations(context.Background(), r, DNSAnswer{Host: "example.com", IPv4: []string{"1.1.1.1"}, ExpiresAt: time.Now().Add(time.Minute)}, func(context.Context, EndpointPermit) error { return nil }, opts)
			if err == nil || calls.Load() != 1 || strings.Contains(err.Error(), "private diagnostic") {
				t.Fatalf("unsafe response handling: %v", err)
			}
		})
	}
	r := testLocationRequest()
	opts := networkHTTPOptions{dial: func(context.Context, string, string) (net.Conn, error) {
		t.Fatal("unsafe answer dialed")
		return nil, nil
	}}
	for _, answer := range []DNSAnswer{{Host: "other.example", IPv4: []string{"1.1.1.1"}}, {Host: "geocoding-api.open-meteo.com", IPv4: []string{"192.168.1.1"}, ExpiresAt: time.Now().Add(time.Minute)}, {Host: "geocoding-api.open-meteo.com", IPv4: []string{"1.1.1.1"}}, {Host: "geocoding-api.open-meteo.com", IPv4: []string{"1.1.1.1"}, ExpiresAt: time.Now().Add(-time.Second)}} {
		if _, err := fetchLocations(context.Background(), r, answer, func(context.Context, EndpointPermit) error { return nil }, opts); err == nil {
			t.Fatal("unsafe DNS answer accepted")
		}
	}
}

func TestLocationPolicyPreservesOtherOwnersAndRejectsBypass(t *testing.T) {
	f := newPolicyFixture(t)
	f.config = defaults() // Recurring services and saved DNS remain disabled.
	writeJSON(path(f.s.Root, "/etc/cgs2-privacy/services.json"), f.config)
	others := []Lease{{"time", "udp:162.159.200.1:123", 130, "old-time"}, {"weather", "tcp:1.1.1.1:443", 130, "old-weather"}}
	f.seed(others)
	r := testLocationRequest()
	permit := f.s.permitGeocode(f.config, r)
	p := EndpointPermit{Service: "geocode", Hostname: "geocoding-api.open-meteo.com", Protocol: "udp", IPv4: "9.9.9.9", Port: 53, ExpiresAt: time.Now().Add(10 * time.Second)}
	if err := permit(context.Background(), p); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*EndpointPermit){
		"vendor name":    func(p *EndpointPermit) { p.Hostname = "api.qingping.co" },
		"other name":     func(p *EndpointPermit) { p.Hostname = "other.example" },
		"other resolver": func(p *EndpointPermit) { p.IPv4 = "8.8.8.8" },
		"owner":          func(p *EndpointPermit) { p.Service = "weather" },
		"no expiry":      func(p *EndpointPermit) { p.ExpiresAt = time.Time{} },
		"expired":        func(p *EndpointPermit) { p.ExpiresAt = time.Now().Add(-time.Second) },
		"private HTTPS":  func(p *EndpointPermit) { p.Protocol = "tcp"; p.Port = 443; p.IPv4 = "192.168.1.1" },
	} {
		t.Run(name, func(t *testing.T) {
			bad := p
			mutate(&bad)
			before := f.filterRuns
			if err := permit(context.Background(), bad); err == nil || f.filterRuns != before {
				t.Fatal("invalid permission reached filter")
			}
		})
	}
	p.Protocol, p.Port, p.IPv4 = "tcp", 443, "1.1.1.1"
	if err := permit(context.Background(), p); err != nil {
		t.Fatal(err)
	}
	if err := f.s.revoke("geocode", r.RequestID); err != nil {
		t.Fatal(err)
	}
	f.assertKernel(testBroker, others[0].Endpoint, others[1].Endpoint)
	got, _ := f.s.leases()
	if !reflect.DeepEqual(got, others) || f.policy() != testBroker+"\n" {
		t.Fatal("lookup altered unrelated permissions")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := permit(ctx, p); err == nil {
		t.Fatal("cancelled grant accepted")
	}
	changed := f.config
	changed.RequestID = "changed"
	writeJSON(path(f.s.Root, "/etc/cgs2-privacy/services.json"), changed)
	if err := permit(context.Background(), p); err == nil {
		t.Fatal("stale configuration granted permission")
	}
	f.assertKernel(testBroker, others[0].Endpoint, others[1].Endpoint)
}

func TestLocationControllerApprovalReplayAndCompletion(t *testing.T) {
	c := controllerFixture(t)
	r := testLocationRequest()
	r.Approved = false
	stageLocation(t, c, r)
	c.store.Filter = func(string, string) error { t.Fatal("unapproved or duplicate search touched filter"); return nil }
	c.geocodeRun = func(context.Context, Config, LocationSearchRequest) ([]LocationResult, error) {
		t.Fatal("unapproved or duplicate search ran network")
		return nil, nil
	}
	if err := c.startGeocode(); err == nil || readLocationStatus(t, c).OK {
		t.Fatal("unapproved request started")
	}
	r.Approved = true
	stageLocation(t, c, r)
	if err := c.consumeLocationID(r.RequestID); err != nil {
		t.Fatal(err)
	}
	if err := c.startGeocode(); err == nil {
		t.Fatal("consumed ID replayed")
	}
	// Simulate a daemon restart: IDs are retained, not just remembered in RAM.
	c.locationID = ""
	if err := c.consumeLocationID(r.RequestID); err == nil {
		t.Fatal("worker restart allowed replay")
	}
	r.RequestID = "search-2"
	stageLocation(t, c, r)
	c.store.Filter = func(string, string) error { return nil }
	var runs atomic.Int32
	c.geocodeRun = func(context.Context, Config, LocationSearchRequest) ([]LocationResult, error) {
		runs.Add(1)
		return parseLocations([]byte(testLocationsJSON))
	}
	before, _ := os.ReadFile(path(c.store.Root, "/etc/cgs2-privacy/services.json"))
	if err := c.startGeocode(); err != nil {
		t.Fatal(err)
	}
	if s := readLocationStatus(t, c); !s.Busy || s.RequestID != r.RequestID {
		t.Fatalf("missing busy status: %+v", s)
	}
	c.jobs.Wait()
	c.done(<-c.results)
	s := readLocationStatus(t, c)
	after, _ := os.ReadFile(path(c.store.Root, "/etc/cgs2-privacy/services.json"))
	if !s.OK || s.Busy || len(s.Results) != 1 || runs.Load() != 1 || string(before) != string(after) || len(c.cancel) != 0 {
		t.Fatalf("incorrect result or recurring config mutation: %+v", s)
	}
	if err := c.startGeocode(); err == nil || runs.Load() != 1 {
		t.Fatal("completed search consent reused")
	}
	used, _ := os.ReadFile(path(c.store.Root, locationUsedPath))
	if strings.Contains(string(used), "Berlin") || strings.Contains(string(used), "open-meteo") {
		t.Fatal("consent history retained private query/provider")
	}
}

func TestLocationControllerApplyCancellationAndWorkerRecovery(t *testing.T) {
	c := controllerFixture(t)
	r := testLocationRequest()
	stageLocation(t, c, r)
	started := make(chan struct{})
	c.geocodeRun = func(ctx context.Context, _ Config, _ LocationSearchRequest) ([]LocationResult, error) {
		close(started)
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if err := c.startGeocode(); err != nil {
		t.Fatal(err)
	}
	<-started
	next := c.config
	next.RequestID = "changed-configuration"
	controllerDraft(t, c, next)
	if err := c.apply(); err != nil {
		t.Fatal(err)
	}
	c.jobs.Wait()
	c.done(<-c.results)
	if s := readLocationStatus(t, c); s.Busy || s.OK || !strings.Contains(s.Message, "cancelled") {
		t.Fatalf("cancelled result replaced current status: %+v", s)
	}
	// A killed worker's successor clears transient policy and marks the old
	// status interrupted. No request staging file is executed at startup.
	if err := c.publishLocation(LocationSearchStatus{RequestID: "interrupted", Busy: true}); err != nil {
		t.Fatal(err)
	}
	writeJSON(path(c.store.Root, "/run/cgs2-services/leases.json"), []Lease{{"geocode", "tcp:1.1.1.1:443", 130, "interrupted"}})
	if err := c.store.reset(); err != nil {
		t.Fatal(err)
	}
	if err := c.recoverLocationStatus(); err != nil {
		t.Fatal(err)
	}
	if s := readLocationStatus(t, c); s.Busy || s.OK || !strings.Contains(s.Message, "interrupted") {
		t.Fatalf("restart left search busy or reran it: %+v", s)
	}
	if got, err := c.store.leases(); err != nil || len(got) != 0 {
		t.Fatal("restart retained temporary permissions")
	}
}

func TestLocationCorruptOptionalStatusDoesNotBlockStartup(t *testing.T) {
	c := controllerFixture(t)
	if err := atomicWrite(path(c.store.Root, locationStatusPath), []byte("{broken")); err != nil {
		t.Fatal(err)
	}
	c.geocodeRun = func(context.Context, Config, LocationSearchRequest) ([]LocationResult, error) {
		t.Fatal("startup replayed search")
		return nil, nil
	}
	if err := c.recoverLocationStatus(); err != nil {
		t.Fatal("optional corrupt status blocked startup", err)
	}
	if s := readLocationStatus(t, c); s.Busy || s.OK {
		t.Fatal("corrupt optional status became successful")
	}
	// Simulate a status path that cannot be replaced, without changing any
	// user directory permissions. Regular configuration remains readable.
	if err := os.Remove(path(c.store.Root, locationStatusPath)); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(path(c.store.Root, locationStatusPath), 0700); err != nil {
		t.Fatal(err)
	}
	if err := c.recoverLocationStatus(); err != nil {
		t.Fatal("unreadable optional status blocked startup", err)
	}
	if _, err := readConfig(c.store.Root); err != nil {
		t.Fatal("unreadable optional status damaged configuration", err)
	}
	stageLocation(t, c, testLocationRequest())
	c.store.Filter = func(string, string) error { t.Fatal("failed busy-status write granted permission"); return nil }
	if err := c.startGeocode(); err == nil || len(c.cancel) != 0 {
		t.Fatal("unwritable status allowed search to start")
	}
}

func TestLocationColdBootCannotReplayStagedConsent(t *testing.T) {
	c := controllerFixture(t)
	r := testLocationRequest()
	stageLocation(t, c, r)
	if err := c.consumeLocationID(r.RequestID); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(path(c.store.Root, "/run")); err != nil {
		t.Fatal(err)
	}
	c.store.Filter = func(string, string) error { t.Fatal("cold-boot replay reached firewall"); return nil }
	c.geocodeRun = func(context.Context, Config, LocationSearchRequest) ([]LocationResult, error) {
		t.Fatal("cold-boot replay reached network")
		return nil, nil
	}
	if err := c.startGeocode(); err == nil {
		t.Fatal("cold boot replayed persisted staging request")
	}
	marker, err := os.ReadFile(path(c.store.Root, locationLastIDPath))
	if err != nil || strings.Contains(string(marker), "Berlin") || strings.Contains(string(marker), "open-meteo") {
		t.Fatal("persistent marker contains lookup data or disappeared")
	}
	if info, err := os.Stat(path(c.store.Root, locationLastIDPath)); err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("persistent consumption marker is not private")
	}
	if err := atomicWrite(path(c.store.Root, locationLastIDPath), []byte("broken")); err != nil {
		t.Fatal(err)
	}
	r.RequestID = "fresh-after-corruption"
	stageLocation(t, c, r)
	if err := c.startGeocode(); err == nil {
		t.Fatal("corrupt consumed marker permitted search")
	}
	if _, err := readConfig(c.store.Root); err != nil {
		t.Fatal("corrupt geocode marker affected recurring service config")
	}
	if err := c.recoverLocationStatus(); err != nil {
		t.Fatal("corrupt marker blocked worker startup")
	}
}

func TestLocationPolicyConfigurationAndCancellationWhileLocked(t *testing.T) {
	for _, mode := range []string{"configuration", "cancellation", "expiry"} {
		t.Run(mode, func(t *testing.T) {
			f := newPolicyFixture(t)
			var uptime atomic.Int64
			uptime.Store(100)
			f.s.Uptime = func() float64 { return float64(uptime.Load()) }
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			p := EndpointPermit{Service: "geocode", Hostname: "geocoding-api.open-meteo.com", Protocol: "tcp", IPv4: "1.1.1.1", Port: 443, ExpiresAt: time.Now().Add(time.Minute)}
			permit := f.s.permitGeocode(f.config, testLocationRequest())
			result := make(chan error, 1)
			if err := f.s.locked(func() error {
				go func() { result <- permit(ctx, p) }()
				// Give the worker enough time to reach the contested file lock.
				time.Sleep(50 * time.Millisecond)
				switch mode {
				case "configuration":
					changed := f.config
					changed.RequestID = "changed-while-waiting"
					return writeJSON(path(f.s.Root, "/etc/cgs2-privacy/services.json"), changed)
				case "cancellation":
					cancel()
				case "expiry":
					// The lease must not be recalculated after acquiring the lock.
					uptime.Store(200)
				}
				return nil
			}); err != nil {
				t.Fatal(err)
			}
			if err := <-result; err == nil || f.filterRuns != 0 {
				t.Fatal("stale request mutated policy after waiting for lock")
			}
			f.assertKernel(testBroker)
		})
	}
}

func TestLocationDNSAliasUsesOriginalApprovalAndMinimumTTL(t *testing.T) {
	f := newPolicyFixture(t)
	f.config = defaults()
	writeJSON(path(f.s.Root, "/etc/cgs2-privacy/services.json"), f.config)
	r := testLocationRequest()
	permit := f.s.permitGeocode(f.config, r)
	queries := []string{}
	exchange := func(ctx context.Context, resolver DNSResolverConfig, host, protocol string, query []byte, callback PermitEndpoint) ([]byte, error) {
		queries = append(queries, host)
		if resolver.approvedHostname != "geocoding-api.open-meteo.com" || resolver.Service != "geocode" {
			t.Fatal("DNS alias lost original approved hostname")
		}
		if err := callback(ctx, EndpointPermit{Service: resolver.Service, Hostname: resolver.approvedHostname, Protocol: protocol, IPv4: resolver.IPv4, Port: resolver.Port, ExpiresAt: time.Now().Add(4 * time.Second)}); err != nil {
			return nil, err
		}
		if len(queries) == 1 {
			return testDNSReply(query, []dnsRecord{{name: host, cname: "edge.example", ttl: 3}}), nil
		}
		return testDNSReply(query, []dnsRecord{{name: host, ip: "1.1.1.1", ttl: 3600}}), nil
	}
	answer, err := resolveIPv4(context.Background(), "geocoding-api.open-meteo.com", DNSResolverConfig{Service: "geocode", IPv4: "9.9.9.9", Port: 53}, permit, exchange)
	if err != nil || len(queries) != 2 || answer.Host != "geocoding-api.open-meteo.com" || time.Until(answer.ExpiresAt) > 3*time.Second || len(answer.IPv4) != 1 {
		t.Fatalf("incorrect alias resolution: %+v %v", answer, err)
	}
	if err := f.s.revoke("geocode", r.RequestID); err != nil {
		t.Fatal(err)
	}
	f.assertKernel(testBroker)
}

func TestLocationFailureAndStopRevokeOnlyTheirOwnLease(t *testing.T) {
	for _, mode := range []string{"provider failure", "stop"} {
		t.Run(mode, func(t *testing.T) {
			f := newPolicyFixture(t)
			other := Lease{"weather", "tcp:1.1.1.1:443", 130, "other-generation"}
			f.seed([]Lease{other})
			c := controllerFixture(t)
			c.store, c.config = f.s, f.config
			r := testLocationRequest()
			stageLocation(t, c, r)
			started := make(chan struct{})
			c.geocodeRun = func(ctx context.Context, conf Config, r LocationSearchRequest) ([]LocationResult, error) {
				err := c.store.permitGeocode(conf, r)(ctx, EndpointPermit{Service: "geocode", Hostname: "geocoding-api.open-meteo.com", Protocol: "tcp", IPv4: "1.1.1.1", Port: 443, ExpiresAt: time.Now().Add(time.Minute)})
				close(started)
				if err != nil {
					return nil, err
				}
				if mode == "stop" {
					<-ctx.Done()
					return nil, ctx.Err()
				}
				return nil, errors.New("provider unavailable")
			}
			if err := c.startGeocode(); err != nil {
				t.Fatal(err)
			}
			<-started
			if mode == "stop" {
				c.stopJobs()
			}
			c.jobs.Wait()
			c.done(<-c.results)
			f.assertKernel(testBroker, other.Endpoint)
			got, err := f.s.leases()
			if err != nil || !reflect.DeepEqual(got, []Lease{other}) || f.policy() != testBroker+"\n" || readLocationStatus(t, c).OK {
				t.Fatalf("failure damaged another owner's permission: %v %v", got, err)
			}
		})
	}
}
