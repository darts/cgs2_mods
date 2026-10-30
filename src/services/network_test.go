package main

import (
	"bytes"
	"context"
	"crypto/x509"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestIPv4Boundaries(t *testing.T) {
	for _, ip := range []string{"1.1.1.1", "162.159.200.1", "8.8.8.8"} {
		if _, err := networkIPv4(ip, false); err != nil {
			t.Fatalf("public %s: %v", ip, err)
		}
	}
	for _, ip := range []string{"10.1.2.3", "172.16.1.1", "192.168.1.1"} {
		if _, err := networkIPv4(ip, false); err == nil {
			t.Fatalf("accepted private public endpoint %s", ip)
		}
		if _, err := networkIPv4(ip, true); err != nil {
			t.Fatalf("custom LAN %s: %v", ip, err)
		}
	}
	for _, ip := range []string{"0.1.2.3", "127.0.0.1", "169.254.1.1", "100.64.1.1", "192.0.2.1", "198.18.0.1", "198.51.100.1", "203.0.113.1", "224.0.0.1", "255.255.255.255", "::1", "::ffff:1.1.1.1", "1.2.3.999", "01.2.3.4"} {
		if _, err := networkIPv4(ip, true); err == nil {
			t.Fatalf("accepted unusable address %s", ip)
		}
	}
}

func testDNSName(name string) []byte {
	var b []byte
	for _, label := range strings.Split(name, ".") {
		b = append(b, byte(len(label)))
		b = append(b, label...)
	}
	return append(b, 0)
}

func testDNSReply(query []byte, records []dnsRecord) []byte {
	p := append([]byte(nil), query...)
	binary.BigEndian.PutUint16(p[2:], 0x8180)
	binary.BigEndian.PutUint16(p[6:], uint16(len(records)))
	for _, r := range records {
		p = append(p, testDNSName(r.name)...)
		header := make([]byte, 10)
		kind := uint16(1)
		data := []byte(net.ParseIP(r.ip).To4())
		if r.cname != "" {
			kind = 5
			data = testDNSName(r.cname)
		}
		binary.BigEndian.PutUint16(header, kind)
		binary.BigEndian.PutUint16(header[2:], 1)
		binary.BigEndian.PutUint32(header[4:], r.ttl)
		binary.BigEndian.PutUint16(header[8:], uint16(len(data)))
		p = append(p, header...)
		p = append(p, data...)
	}
	return p
}

func TestDNSParserRejectsMalformedAndUnrelated(t *testing.T) {
	query := dnsQuery("weather.example", 17)
	valid := testDNSReply(query, []dnsRecord{{name: "weather.example", ip: "1.1.1.1", ttl: 30}})
	if records, truncated, err := parseDNS(valid, 17, "weather.example"); err != nil || truncated || len(records) != 1 {
		t.Fatalf("valid: %v %v %v", records, truncated, err)
	}
	for name, mutate := range map[string]func([]byte) []byte{
		"wrong ID":       func(p []byte) []byte { p[1]++; return p },
		"not response":   func(p []byte) []byte { p[2] &= 0x7f; return p },
		"wrong question": func(p []byte) []byte { p[13] = 'x'; return p },
		"short record":   func(p []byte) []byte { return p[:len(p)-1] },
		"trailing bytes": func(p []byte) []byte { return append(p, 0) },
		"pointer cycle":  func(p []byte) []byte { p[12] = 0xc0; p[13] = 12; return p },
	} {
		t.Run(name, func(t *testing.T) {
			if _, _, err := parseDNS(mutate(append([]byte(nil), valid...)), 17, "weather.example"); err == nil {
				t.Fatal("accepted malformed DNS")
			}
		})
	}
	additional := append([]byte(nil), valid...)
	binary.BigEndian.PutUint16(additional[6:], 0)
	binary.BigEndian.PutUint16(additional[10:], 1)
	if records, _, err := parseDNS(additional, 17, "weather.example"); err != nil || len(records) != 0 {
		t.Fatalf("additional-section address trusted: %v %v", records, err)
	}
	compressed := append([]byte(nil), query...)
	binary.BigEndian.PutUint16(compressed[2:], 0x8180)
	binary.BigEndian.PutUint16(compressed[6:], 1)
	compressed = append(compressed, 0xc0, 12, 0, 1, 0, 1, 0, 0, 0, 30, 0, 4, 1, 1, 1, 1)
	if r, _, err := parseDNS(compressed, 17, "weather.example"); err != nil || len(r) != 1 {
		t.Fatalf("valid compression rejected: %v", err)
	}
}

func TestDNSResolutionCNAMEAndTTL(t *testing.T) {
	var queries []string
	start := time.Now()
	exchange := func(_ context.Context, _ DNSResolverConfig, host, protocol string, query []byte, _ PermitEndpoint) ([]byte, error) {
		queries = append(queries, host+"/"+protocol)
		if host == "weather.example" && protocol == "udp" {
			p := testDNSReply(query, nil)
			binary.BigEndian.PutUint16(p[2:], 0x8380)
			return p, nil
		}
		if host == "weather.example" {
			return testDNSReply(query, []dnsRecord{{name: host, cname: "edge.example", ttl: 7}}), nil
		}
		return testDNSReply(query, []dnsRecord{{name: host, ip: "1.1.1.1", ttl: 600}, {name: "unrelated.example", ip: "8.8.8.8", ttl: 1}}), nil
	}
	baseExchange := exchange
	exchange = func(ctx context.Context, resolver DNSResolverConfig, host, protocol string, query []byte, permit PermitEndpoint) ([]byte, error) {
		if resolver.approvedHostname != "weather.example" {
			t.Fatalf("CNAME permission owner changed to %q", resolver.approvedHostname)
		}
		return baseExchange(ctx, resolver, host, protocol, query, permit)
	}
	answer, err := resolveIPv4(context.Background(), "WEATHER.EXAMPLE.", DNSResolverConfig{Service: "weather", IPv4: "1.1.1.1", Port: 53}, nil, exchange)
	if err != nil {
		t.Fatal(err)
	}
	if answer.Host != "weather.example" || len(answer.IPv4) != 1 || answer.IPv4[0] != "1.1.1.1" {
		t.Fatalf("incorrect chain result: %+v", answer)
	}
	if answer.ExpiresAt.Before(start.Add(7*time.Second)) || answer.ExpiresAt.After(time.Now().Add(7*time.Second)) {
		t.Fatalf("did not use minimum chain TTL: %s", answer.ExpiresAt)
	}
	if strings.Join(queries, ",") != "weather.example/udp,weather.example/tcp,edge.example/udp" {
		t.Fatalf("unexpected queries %v", queries)
	}
}

func TestDNSResolutionFailsClosed(t *testing.T) {
	for name, records := range map[string][]dnsRecord{
		"zero TTL":  {{name: "weather.example", ip: "1.1.1.1", ttl: 0}},
		"cycle":     {{name: "weather.example", cname: "edge.example", ttl: 10}, {name: "edge.example", cname: "weather.example", ttl: 10}},
		"conflict":  {{name: "weather.example", cname: "edge.example", ttl: 10}, {name: "weather.example", ip: "1.1.1.1", ttl: 10}},
		"unrelated": {{name: "other.example", ip: "1.1.1.1", ttl: 10}},
	} {
		t.Run(name, func(t *testing.T) {
			exchange := func(_ context.Context, _ DNSResolverConfig, _, _ string, q []byte, _ PermitEndpoint) ([]byte, error) {
				return testDNSReply(q, records), nil
			}
			if _, err := resolveIPv4(context.Background(), "weather.example", DNSResolverConfig{Service: "weather", IPv4: "1.1.1.1", Port: 53}, nil, exchange); err == nil {
				t.Fatal("unsafe answer accepted")
			}
		})
	}
	called := false
	answer, err := resolveIPv4(context.Background(), "162.159.200.1", DNSResolverConfig{}, nil, func(context.Context, DNSResolverConfig, string, string, []byte, PermitEndpoint) ([]byte, error) {
		called = true
		return nil, nil
	})
	if err != nil || called || !answer.ExpiresAt.IsZero() {
		t.Fatalf("numeric address used DNS: %+v %v", answer, err)
	}
}

func TestDNSWireUDPAndTCP(t *testing.T) {
	for _, protocol := range []string{"udp", "tcp"} {
		t.Run(protocol, func(t *testing.T) {
			query := dnsQuery("weather.example", 42)
			response := testDNSReply(query, []dnsRecord{{name: "weather.example", ip: "1.1.1.1", ttl: 30}})
			var port int
			done := make(chan error, 1)
			if protocol == "udp" {
				listener, err := net.ListenPacket("udp4", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				defer listener.Close()
				port = listener.LocalAddr().(*net.UDPAddr).Port
				go func() {
					b := make([]byte, 512)
					n, peer, err := listener.ReadFrom(b)
					if err == nil && !bytes.Equal(b[:n], query) {
						err = errors.New("wrong UDP query")
					}
					if err == nil {
						_, err = listener.WriteTo(response, peer)
					}
					done <- err
				}()
			} else {
				listener, err := net.Listen("tcp4", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				defer listener.Close()
				port = listener.Addr().(*net.TCPAddr).Port
				go func() {
					conn, err := listener.Accept()
					if err != nil {
						done <- err
						return
					}
					defer conn.Close()
					header := make([]byte, 2)
					_, err = io.ReadFull(conn, header)
					if err == nil {
						body := make([]byte, binary.BigEndian.Uint16(header))
						_, err = io.ReadFull(conn, body)
						if err == nil && !bytes.Equal(body, query) {
							err = errors.New("wrong TCP query")
						}
					}
					if err == nil {
						binary.BigEndian.PutUint16(header, uint16(len(response)))
						_, err = conn.Write(append(header, response...))
					}
					done <- err
				}()
			}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			permits := 0
			got, err := dnsExchange(ctx, DNSResolverConfig{Service: "weather", IPv4: "127.0.0.1", Port: uint16(port)}, "weather.example", protocol, query, func(_ context.Context, e EndpointPermit) error {
				permits++
				if e.Service != "weather" || e.Protocol != protocol || e.Port != uint16(port) || e.ExpiresAt.IsZero() {
					return errors.New("incorrect permission")
				}
				return nil
			})
			if err != nil || !bytes.Equal(got, response) || permits != 1 {
				t.Fatalf("exchange: %v, permits %d", err, permits)
			}
			if err := <-done; err != nil {
				t.Fatal(err)
			}
		})
	}
}

const testWeatherJSON = `{"latitude":52.5,"longitude":13.4,"timezone":"Europe/Berlin","current_units":{"temperature_2m":"°C","relative_humidity_2m":"%","wind_speed_10m":"km/h"},"daily_units":{"temperature_2m_min":"°C","temperature_2m_max":"°C","precipitation_probability_max":"%"},"current":{"time":"2026-10-20T12:00","temperature_2m":14.5,"relative_humidity_2m":62,"weather_code":3,"is_day":1,"wind_speed_10m":12.4},"daily":{"time":["2026-10-20","2026-10-21","2026-10-22"],"weather_code":[3,61,1],"temperature_2m_min":[8,7,9],"temperature_2m_max":[15,16,17],"precipitation_probability_max":[10,80,20]}}`

func TestWeatherModelValidation(t *testing.T) {
	good, err := parseWeather([]byte(testWeatherJSON), "weather.example", "celsius")
	if err != nil {
		t.Fatal(err)
	}
	if good.Current.Temperature != 14.5 || !good.Current.IsDay || len(good.Daily) != 3 || good.Source != "weather.example" {
		t.Fatalf("wrong model: %+v", good)
	}
	for name, mutate := range map[string]func(map[string]any){
		"missing value":       func(v map[string]any) { delete(v["current"].(map[string]any), "temperature_2m") },
		"null value":          func(v map[string]any) { v["current"].(map[string]any)["relative_humidity_2m"] = nil },
		"impossible humidity": func(v map[string]any) { v["current"].(map[string]any)["relative_humidity_2m"] = 101 },
		"unknown code":        func(v map[string]any) { v["current"].(map[string]any)["weather_code"] = 99.5 },
		"wrong units":         func(v map[string]any) { v["current_units"].(map[string]any)["temperature_2m"] = "°F" },
		"bad date":            func(v map[string]any) { v["current"].(map[string]any)["time"] = "2026-02-30T12:00" },
		"length mismatch":     func(v map[string]any) { v["daily"].(map[string]any)["temperature_2m_min"] = []any{8} },
		"duplicate dates": func(v map[string]any) {
			v["daily"].(map[string]any)["time"] = []any{"2026-10-20", "2026-10-20", "2026-10-22"}
		},
	} {
		t.Run(name, func(t *testing.T) {
			var v map[string]any
			if err := json.Unmarshal([]byte(testWeatherJSON), &v); err != nil {
				t.Fatal(err)
			}
			mutate(v)
			body, _ := json.Marshal(v)
			if _, err := parseWeather(body, "weather.example", "celsius"); err == nil {
				t.Fatal("accepted invalid response")
			}
		})
	}
}

func TestHTTPSPinsAddressAndAuthenticatesHostname(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.TLS.ServerName != "example.com" || !strings.HasPrefix(r.Host, "example.com:") {
			t.Errorf("wrong TLS identity or Host: %q %q", r.TLS.ServerName, r.Host)
		}
		if r.URL.Query().Get("forecast_days") != "3" || r.URL.Query().Get("timezone") != "Europe/Berlin" {
			t.Error("missing explicit query fields")
		}
		io.WriteString(w, testWeatherJSON)
	}))
	defer server.Close()
	roots := x509.NewCertPool()
	roots.AddCert(server.Certificate())
	_, port, _ := net.SplitHostPort(server.Listener.Addr().String())
	request := WeatherRequest{URL: "https://example.com:" + port + "/forecast", Latitude: 52.5, Longitude: 13.4, Timezone: "Europe/Berlin", TemperatureUnit: "celsius"}
	answer := DNSAnswer{Host: "example.com", IPv4: []string{"1.1.1.1"}, ExpiresAt: time.Now().Add(time.Minute)}
	permitted := false
	dials := 0
	opts := networkHTTPOptions{roots: roots, dial: func(ctx context.Context, network, address string) (net.Conn, error) {
		dials++
		if !permitted || network != "tcp4" || address != "1.1.1.1:"+port {
			return nil, errors.New("dial was not explicitly permitted and pinned")
		}
		return (&net.Dialer{}).DialContext(ctx, "tcp4", server.Listener.Addr().String())
	}}
	permit := func(_ context.Context, e EndpointPermit) error {
		if e.Service != "weather" || e.Hostname != "example.com" || e.Protocol != "tcp" || e.IPv4 != "1.1.1.1" {
			return errors.New("incorrect endpoint")
		}
		permitted = true
		return nil
	}
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	if _, err := fetchWeather(context.Background(), request, answer, permit, opts); err != nil {
		t.Fatal(err)
	}
	if dials != 1 || requests.Load() != 1 {
		t.Fatalf("unexpected requests/dials %d/%d", requests.Load(), dials)
	}
	permitted = false
	if _, err := fetchWeather(context.Background(), request, answer, func(context.Context, EndpointPermit) error { return errors.New("denied") }, opts); err == nil || dials != 1 {
		t.Fatal("denied request dialed")
	}
	request.URL = "https://wrong.example:" + port + "/forecast"
	answer.Host = "wrong.example"
	opts.dial = func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "tcp4", server.Listener.Addr().String())
	}
	if _, err := fetchWeather(context.Background(), request, answer, func(context.Context, EndpointPermit) error { return nil }, opts); err == nil {
		t.Fatal("accepted wrong TLS hostname")
	}
}

func TestHTTPSDoesNotFollowRedirectsOrUnboundedBodies(t *testing.T) {
	for _, mode := range []string{"redirect", "large body", "HTTP error"} {
		t.Run(mode, func(t *testing.T) {
			requests := 0
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests++
				switch mode {
				case "redirect":
					http.Redirect(w, r, "https://unauthorized.example/", http.StatusFound)
				case "large body":
					io.WriteString(w, strings.Repeat("x", 256*1024+1))
				default:
					http.Error(w, "private diagnostic body", http.StatusServiceUnavailable)
				}
			}))
			defer server.Close()
			roots := x509.NewCertPool()
			roots.AddCert(server.Certificate())
			_, port, _ := net.SplitHostPort(server.Listener.Addr().String())
			opts := networkHTTPOptions{roots: roots, dial: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp4", server.Listener.Addr().String())
			}}
			_, err := fetchWeather(context.Background(), WeatherRequest{URL: "https://example.com:" + port, Timezone: "UTC", TemperatureUnit: "celsius"}, DNSAnswer{Host: "example.com", IPv4: []string{"1.1.1.1"}, ExpiresAt: time.Now().Add(time.Minute)}, func(context.Context, EndpointPermit) error { return nil }, opts)
			if err == nil || requests != 1 || strings.Contains(err.Error(), "private diagnostic") {
				t.Fatalf("unsafe response handling: %v requests=%d", err, requests)
			}
		})
	}
}

func TestWeatherRejectsPermissionBypassInputs(t *testing.T) {
	for _, endpoint := range []string{"http://example.com", "https://user:password@example.com", "https://example.com?secret=1", "https://example.com?", "https://example.com/#fragment", "https://example.com:", "https://example.com:0"} {
		if _, err := weatherURL(WeatherRequest{URL: endpoint, Timezone: "UTC", TemperatureUnit: "celsius"}); err == nil {
			t.Fatalf("accepted URL %s", endpoint)
		}
	}
	called := false
	opts := networkHTTPOptions{dial: func(context.Context, string, string) (net.Conn, error) {
		called = true
		return nil, errors.New("unexpected dial")
	}}
	for name, answer := range map[string]DNSAnswer{"private": {Host: "example.com", IPv4: []string{"192.168.1.1"}, ExpiresAt: time.Now().Add(time.Minute)}, "wrong name": {Host: "other.example", IPv4: []string{"1.1.1.1"}}, "expired": {Host: "example.com", IPv4: []string{"1.1.1.1"}, ExpiresAt: time.Now().Add(-time.Second)}, "missing expiry": {Host: "example.com", IPv4: []string{"1.1.1.1"}}} {
		t.Run(name, func(t *testing.T) {
			if _, err := fetchWeather(context.Background(), WeatherRequest{URL: "https://example.com/", Timezone: "UTC", TemperatureUnit: "celsius"}, answer, func(context.Context, EndpointPermit) error { return nil }, opts); err == nil || called {
				t.Fatal("invalid endpoint was used")
			}
		})
	}
}

func testNTPStamp(stamp time.Time) []byte {
	b := make([]byte, 8)
	binary.BigEndian.PutUint32(b, uint32(stamp.Unix()+2208988800))
	binary.BigEndian.PutUint32(b[4:], uint32((uint64(stamp.Nanosecond())<<32)/1e9))
	return b
}
func testNTPReply(nonce []byte, receive, transmit time.Time) []byte {
	p := make([]byte, 48)
	p[0] = 0x24
	p[1] = 2
	copy(p[24:32], nonce)
	copy(p[32:40], testNTPStamp(receive))
	copy(p[40:48], testNTPStamp(transmit))
	return p
}

func TestNTPTimestampsAndValidation(t *testing.T) {
	nonce := []byte{1, 2, 3, 4, 5, 6, 7, 8}
	for _, year := range []int{2026, 2036, 2040, 2099} {
		stamp := time.Date(year, 7, 1, 12, 0, 0, 0, time.UTC)
		p := testNTPReply(nonce, stamp, stamp.Add(10*time.Millisecond))
		got, err := parseNTP(p, nonce, 110*time.Millisecond)
		if err != nil || got.Sub(stamp.Add(60*time.Millisecond)) > time.Nanosecond || got.Before(stamp.Add(60*time.Millisecond-time.Nanosecond)) {
			t.Fatalf("era %d: %s %v", year, got, err)
		}
	}
	stamp := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
	valid := testNTPReply(nonce, stamp, stamp)
	for name, mutate := range map[string]func([]byte) []byte{
		"origin": func(p []byte) []byte { p[24]++; return p }, "unsynchronized": func(p []byte) []byte { p[0] |= 0xc0; return p }, "client mode": func(p []byte) []byte { p[0] = 0x23; return p }, "wrong version": func(p []byte) []byte { p[0] = 0x14; return p }, "kiss of death": func(p []byte) []byte { p[1] = 0; return p }, "stratum 16": func(p []byte) []byte { p[1] = 16; return p }, "missing transmit": func(p []byte) []byte { clear(p[40:48]); return p }, "backwards processing": func(p []byte) []byte { copy(p[32:40], testNTPStamp(stamp.Add(time.Second))); return p }, "excess processing": func(p []byte) []byte { copy(p[40:48], testNTPStamp(stamp.Add(time.Second))); return p }, "short": func(p []byte) []byte { return p[:47] },
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := parseNTP(mutate(append([]byte(nil), valid...)), nonce, 10*time.Millisecond); err == nil {
				t.Fatal("accepted invalid NTP")
			}
		})
	}
	for _, rtt := range []time.Duration{0, -time.Second, 6 * time.Second} {
		if _, err := parseNTP(valid, nonce, rtt); err == nil {
			t.Fatalf("accepted RTT %s", rtt)
		}
	}
	for _, stamp := range []time.Time{time.Date(2019, 1, 1, 0, 0, 0, 0, time.UTC), time.Date(2100, 1, 1, 0, 0, 0, 0, time.UTC)} {
		if _, err := ntpTimestamp(testNTPStamp(stamp)); err == nil {
			t.Fatal("accepted unsupported era")
		}
	}
}

func TestNTPUDPUsesCustomPortAndBoundedTimeout(t *testing.T) {
	for _, mode := range []string{"valid", "bad origin", "stratum", "silent"} {
		t.Run(mode, func(t *testing.T) {
			listener, err := net.ListenPacket("udp4", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			done := make(chan error, 1)
			stamp := time.Date(2040, 1, 1, 12, 0, 0, 0, time.UTC)
			go func() {
				b := make([]byte, 512)
				n, peer, err := listener.ReadFrom(b)
				if err != nil {
					done <- err
					return
				}
				if n != 48 || b[0] != 0x23 {
					done <- errors.New("invalid query")
					return
				}
				if mode != "silent" {
					p := testNTPReply(b[40:48], stamp, stamp)
					if mode == "bad origin" {
						p[24]++
					}
					if mode == "stratum" {
						p[1] = 0
					}
					_, err = listener.WriteTo(p, peer)
				}
				done <- err
			}()
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			port := uint16(listener.LocalAddr().(*net.UDPAddr).Port)
			sample, received, err := queryNTPServer(ctx, "127.0.0.1", port)
			if mode == "valid" {
				if err != nil || sample.Time.Before(stamp) || sample.Time.After(stamp.Add(time.Second)) || sample.RTT <= 0 || received.IsZero() || sample.Server != "127.0.0.1:"+strconv.Itoa(int(port)) {
					t.Fatalf("invalid sample: %+v %v", sample, err)
				}
			} else if err == nil {
				t.Fatal("accepted invalid/silent server")
			}
			if err := <-done; err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestNTPRequiresConsentBeforeAnySocket(t *testing.T) {
	var called bool
	_, err := QueryNTP(context.Background(), NTPRequest{Hostname: "time.example", ServerIPs: []string{"162.159.200.1"}, Port: 1123, ExpiresAt: time.Now().Add(time.Minute)}, func(_ context.Context, e EndpointPermit) error {
		called = true
		if e.Port != 1123 || e.Protocol != "udp" || e.Service != "time" {
			t.Fatal("wrong NTP permission")
		}
		return errors.New("not approved")
	})
	if err == nil || !called {
		t.Fatal("missing consent enforcement")
	}
	if _, err := QueryNTP(context.Background(), NTPRequest{Hostname: "127.0.0.1", ServerIPs: []string{"127.0.0.1"}, Port: 123, AllowPrivate: true}, nil); err == nil {
		t.Fatal("loopback allowed")
	}
}

func TestDNSAnswerLeaseRequiredAndLiteralPinned(t *testing.T) {
	if err := networkAnswerLease("weather.example", []string{"1.1.1.1"}, time.Time{}); err == nil {
		t.Fatal("hostname without TTL accepted")
	}
	if err := networkAnswerLease("1.1.1.1", []string{"8.8.8.8"}, time.Time{}); err == nil {
		t.Fatal("numeric endpoint changed address")
	}
	if err := networkAnswerLease("1.1.1.1", []string{"1.1.1.1"}, time.Time{}); err != nil {
		t.Fatal(err)
	}
}

func TestNTPRejectsUnconnectedReplySource(t *testing.T) {
	server, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	other, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	done := make(chan error, 1)
	stamp := time.Date(2040, 1, 1, 12, 0, 0, 0, time.UTC)
	go func() {
		query := make([]byte, 48)
		_, peer, err := server.ReadFrom(query)
		if err == nil {
			invalid := testNTPReply(query[40:48], stamp, stamp)
			invalid[1] = 0 // Would fail immediately if the wrong source were accepted.
			_, err = other.WriteTo(invalid, peer)
		}
		if err == nil {
			_, err = server.WriteTo(testNTPReply(query[40:48], stamp, stamp), peer)
		}
		done <- err
	}()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, _, err := queryNTPServer(ctx, "127.0.0.1", uint16(server.LocalAddr().(*net.UDPAddr).Port)); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func FuzzDNSReplyParser(f *testing.F) {
	query := dnsQuery("weather.example", 17)
	f.Add(testDNSReply(query, []dnsRecord{{name: "weather.example", ip: "1.1.1.1", ttl: 30}}))
	f.Add([]byte{0, 17, 128})
	f.Fuzz(func(t *testing.T, packet []byte) { _, _, _ = parseDNS(packet, 17, "weather.example") })
}
