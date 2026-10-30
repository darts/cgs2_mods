package main

// Network operations deliberately have no implicit resolver, proxy, redirect,
// fallback provider, or clock setter. The controller owns consent and leases.
import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

type EndpointPermit struct {
	Service, Hostname, Protocol, IPv4 string
	Port                              uint16
	ExpiresAt                         time.Time // zero means an explicitly configured static address
}
type PermitEndpoint func(context.Context, EndpointPermit) error

type DNSResolverConfig struct {
	Service          string
	IPv4             string
	Port             uint16
	Timeout          time.Duration
	approvedHostname string // bound internally to the original approved query
}
type DNSAnswer struct {
	Host      string
	IPv4      []string
	ExpiresAt time.Time
}

func networkHost(host string) (string, error) {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	if len(host) == 0 || len(host) > 253 {
		return "", errors.New("invalid hostname length")
	}
	for _, label := range strings.Split(host, ".") {
		if len(label) == 0 || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return "", errors.New("invalid hostname label")
		}
		for _, c := range label {
			if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-') {
				return "", errors.New("hostname must use ASCII letters, digits and hyphens")
			}
		}
	}
	return host, nil
}

// Public presets must never resolve to private, loopback, link-local, reserved,
// benchmarking or documentation networks. Custom LAN endpoints additionally
// allow RFC1918 addresses; no mode permits loopback or multicast.
func networkIPv4(value string, allowPrivate bool) (netip.Addr, error) {
	ip, err := netip.ParseAddr(value)
	if err != nil || !ip.Is4() {
		return netip.Addr{}, errors.New("a numeric IPv4 address is required")
	}
	if ip.IsPrivate() {
		if allowPrivate {
			return ip, nil
		}
		return netip.Addr{}, errors.New("public service resolved to a private address")
	}
	for _, block := range []string{"0.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"} {
		if netip.MustParsePrefix(block).Contains(ip) {
			return netip.Addr{}, errors.New("unusable or non-public IPv4 address")
		}
	}
	return ip, nil
}

func networkPermit(ctx context.Context, permit PermitEndpoint, ep EndpointPermit) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if permit == nil {
		return errors.New("network permission callback is required")
	}
	if !ep.ExpiresAt.IsZero() && !time.Now().Before(ep.ExpiresAt) {
		return errors.New("endpoint permission expired")
	}
	return permit(ctx, ep)
}

func networkTimeout(ctx context.Context, requested, fallback time.Duration, expires time.Time) (context.Context, context.CancelFunc) {
	if requested <= 0 {
		requested = fallback
	}
	if requested > time.Minute {
		requested = time.Minute
	}
	deadline := time.Now().Add(requested)
	if !expires.IsZero() && expires.Before(deadline) {
		deadline = expires
	}
	return context.WithDeadline(ctx, deadline)
}

func dnsName(packet []byte, offset int) (string, int, error) {
	var labels []string
	next, expanded := -1, 0
	visited := make(map[int]bool)
	for steps := 0; steps < 128; steps++ {
		if offset < 0 || offset >= len(packet) || visited[offset] {
			return "", 0, errors.New("invalid DNS name pointer")
		}
		visited[offset] = true
		n := int(packet[offset])
		if n&0xc0 == 0xc0 {
			if offset+1 >= len(packet) {
				return "", 0, io.ErrUnexpectedEOF
			}
			if next < 0 {
				next = offset + 2
			}
			offset = (n&0x3f)<<8 | int(packet[offset+1])
			continue
		}
		if n&0xc0 != 0 {
			return "", 0, errors.New("unsupported DNS label encoding")
		}
		offset++
		if n == 0 {
			if next < 0 {
				next = offset
			}
			name, err := networkHost(strings.Join(labels, "."))
			return name, next, err
		}
		if offset+n > len(packet) {
			return "", 0, io.ErrUnexpectedEOF
		}
		expanded += n + 1
		if expanded > 254 {
			return "", 0, errors.New("DNS name is too long")
		}
		labels = append(labels, string(packet[offset:offset+n]))
		offset += n
	}
	return "", 0, errors.New("DNS name exceeded pointer limit")
}

type dnsRecord struct {
	name, cname, ip string
	ttl             uint32
}

func dnsQuery(host string, id uint16) []byte {
	p := make([]byte, 12)
	binary.BigEndian.PutUint16(p, id)
	binary.BigEndian.PutUint16(p[2:], 0x0100)
	binary.BigEndian.PutUint16(p[4:], 1)
	for _, label := range strings.Split(host, ".") {
		p = append(p, byte(len(label)))
		p = append(p, label...)
	}
	return append(p, 0, 0, 1, 0, 1) // A, IN only
}

func parseDNS(packet []byte, id uint16, host string) ([]dnsRecord, bool, error) {
	if len(packet) < 12 || len(packet) > 16384 {
		return nil, false, errors.New("invalid DNS packet length")
	}
	flags := binary.BigEndian.Uint16(packet[2:])
	if binary.BigEndian.Uint16(packet) != id || flags&0x8000 == 0 || flags&0x7800 != 0 || binary.BigEndian.Uint16(packet[4:]) != 1 {
		return nil, false, errors.New("DNS reply does not match query")
	}
	name, pos, err := dnsName(packet, 12)
	if err != nil || pos+4 > len(packet) || name != host || binary.BigEndian.Uint16(packet[pos:]) != 1 || binary.BigEndian.Uint16(packet[pos+2:]) != 1 {
		return nil, false, errors.New("DNS reply question does not match")
	}
	if flags&0x0200 != 0 {
		return nil, true, nil
	}
	if flags&15 != 0 {
		return nil, false, fmt.Errorf("DNS server returned status %d", flags&15)
	}
	pos += 4
	counts := []int{int(binary.BigEndian.Uint16(packet[6:])), int(binary.BigEndian.Uint16(packet[8:])), int(binary.BigEndian.Uint16(packet[10:]))}
	if counts[0]+counts[1]+counts[2] > 128 {
		return nil, false, errors.New("too many DNS records")
	}
	var records []dnsRecord
	for section, count := range counts {
		for n := 0; n < count; n++ {
			// Root labels are legal in OPT records, but cannot be answer owners.
			var owner string
			if pos < len(packet) && packet[pos] == 0 {
				pos++
			} else {
				owner, pos, err = dnsName(packet, pos)
				if err != nil {
					return nil, false, err
				}
			}
			if pos+10 > len(packet) {
				return nil, false, io.ErrUnexpectedEOF
			}
			kind, class := binary.BigEndian.Uint16(packet[pos:]), binary.BigEndian.Uint16(packet[pos+2:])
			ttl, size := binary.BigEndian.Uint32(packet[pos+4:]), int(binary.BigEndian.Uint16(packet[pos+8:]))
			pos += 10
			end := pos + size
			if end > len(packet) {
				return nil, false, io.ErrUnexpectedEOF
			}
			if section == 0 && class == 1 {
				r := dnsRecord{name: owner, ttl: ttl}
				switch kind {
				case 1:
					if size != 4 {
						return nil, false, errors.New("invalid DNS A record")
					}
					r.ip = net.IP(packet[pos:end]).String()
					records = append(records, r)
				case 5:
					var consumed int
					r.cname, consumed, err = dnsName(packet, pos)
					if err != nil || consumed != end {
						return nil, false, errors.New("invalid DNS CNAME record")
					}
					records = append(records, r)
				}
			}
			pos = end
		}
	}
	if pos != len(packet) {
		return nil, false, errors.New("trailing DNS packet data")
	}
	return records, false, nil
}

func dnsExchange(ctx context.Context, resolver DNSResolverConfig, host, protocol string, query []byte, permit PermitEndpoint) ([]byte, error) {
	deadline, _ := ctx.Deadline()
	approvedHost := resolver.approvedHostname
	if approvedHost == "" {
		approvedHost = host
	}
	if err := networkPermit(ctx, permit, EndpointPermit{Service: resolver.Service, Hostname: approvedHost, Protocol: protocol, IPv4: resolver.IPv4, Port: resolver.Port, ExpiresAt: deadline}); err != nil {
		return nil, err
	}
	conn, err := (&net.Dialer{}).DialContext(ctx, protocol+"4", net.JoinHostPort(resolver.IPv4, strconv.Itoa(int(resolver.Port))))
	if err != nil {
		return nil, fmt.Errorf("DNS %s connection failed: %w", protocol, err)
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	if err = conn.SetDeadline(deadline); err != nil {
		return nil, err
	}
	if protocol == "tcp" {
		wire := make([]byte, 2, len(query)+2)
		binary.BigEndian.PutUint16(wire, uint16(len(query)))
		wire = append(wire, query...)
		if _, err = io.Copy(conn, bytes.NewReader(wire)); err != nil {
			return nil, err
		}
		header := make([]byte, 2)
		if _, err = io.ReadFull(conn, header); err != nil {
			return nil, err
		}
		size := int(binary.BigEndian.Uint16(header))
		if size < 12 || size > 16384 {
			return nil, errors.New("invalid DNS TCP length")
		}
		packet := make([]byte, size)
		_, err = io.ReadFull(conn, packet)
		return packet, err
	}
	if _, err = conn.Write(query); err != nil {
		return nil, err
	}
	packet := make([]byte, 16385)
	n, err := conn.Read(packet)
	return packet[:n], err
}

func ResolveIPv4(ctx context.Context, host string, resolver DNSResolverConfig, permit PermitEndpoint) (DNSAnswer, error) {
	return resolveIPv4(ctx, host, resolver, permit, dnsExchange)
}

type dnsExchanger func(context.Context, DNSResolverConfig, string, string, []byte, PermitEndpoint) ([]byte, error)

// The injected exchange is used only by host fixtures; production always uses
// dnsExchange, which requires consent immediately before opening its socket.
func resolveIPv4(ctx context.Context, host string, resolver DNSResolverConfig, permit PermitEndpoint, exchange dnsExchanger) (DNSAnswer, error) {
	host, err := networkHost(host)
	if err != nil {
		return DNSAnswer{}, err
	}
	resolver.approvedHostname = host
	if ip, err := netip.ParseAddr(host); err == nil && ip.Is4() {
		return DNSAnswer{Host: host, IPv4: []string{ip.String()}}, nil
	}
	ip, err := networkIPv4(resolver.IPv4, true)
	if err != nil {
		return DNSAnswer{}, fmt.Errorf("DNS resolver: %w", err)
	}
	resolver.IPv4 = ip.String()
	if resolver.Port == 0 || (resolver.Service != "weather" && resolver.Service != "time" && resolver.Service != "geocode") {
		return DNSAnswer{}, errors.New("DNS service and port must be explicit")
	}
	ctx, cancel := networkTimeout(ctx, resolver.Timeout, 5*time.Second, time.Time{})
	defer cancel()
	current, seen, aliases := host, map[string]bool{host: true}, 0
	expires := time.Now().Add(time.Hour) // upper bound only; never increase a TTL
	for queries := 0; queries < 9; queries++ {
		queried := current
		var nonce [2]byte
		if _, err = rand.Read(nonce[:]); err != nil {
			return DNSAnswer{}, err
		}
		id := binary.BigEndian.Uint16(nonce[:])
		query := dnsQuery(current, id)
		packet, err := exchange(ctx, resolver, current, "udp", query, permit)
		if err != nil {
			return DNSAnswer{}, err
		}
		records, truncated, err := parseDNS(packet, id, current)
		if err != nil {
			return DNSAnswer{}, err
		}
		if truncated {
			packet, err = exchange(ctx, resolver, current, "tcp", query, permit)
			if err != nil {
				return DNSAnswer{}, err
			}
			records, truncated, err = parseDNS(packet, id, current)
			if err != nil {
				return DNSAnswer{}, err
			}
			if truncated {
				return DNSAnswer{}, errors.New("truncated TCP DNS reply")
			}
		}
		received := time.Now()
		for {
			var ips []string
			cname := ""
			matched := false
			for _, r := range records {
				if r.name != current {
					continue
				}
				matched = true
				if r.ttl == 0 {
					return DNSAnswer{}, errors.New("DNS TTL is zero; no persistent permission can be granted")
				}
				if end := received.Add(time.Duration(r.ttl) * time.Second); end.Before(expires) {
					expires = end
				}
				if r.ip != "" {
					ips = append(ips, r.ip)
				}
				if r.cname != "" {
					if cname != "" && cname != r.cname {
						return DNSAnswer{}, errors.New("conflicting DNS CNAME records")
					}
					cname = r.cname
				}
			}
			if len(ips) > 0 {
				if cname != "" || len(ips) > 16 {
					return DNSAnswer{}, errors.New("ambiguous or excessive DNS answers")
				}
				if !time.Now().Before(expires) {
					return DNSAnswer{}, errors.New("DNS answer expired while following aliases")
				}
				sort.Strings(ips)
				unique := ips[:0]
				for _, ip := range ips {
					if len(unique) == 0 || unique[len(unique)-1] != ip {
						unique = append(unique, ip)
					}
				}
				return DNSAnswer{Host: host, IPv4: unique, ExpiresAt: expires}, nil
			}
			if cname == "" {
				if matched || current == queried {
					return DNSAnswer{}, errors.New("DNS reply has no matching IPv4 address")
				}
				break
			}
			aliases++
			if aliases > 8 || seen[cname] {
				return DNSAnswer{}, errors.New("DNS alias chain is cyclic or too long")
			}
			seen[cname] = true
			current = cname
		}
	}
	return DNSAnswer{}, errors.New("DNS query limit exceeded")
}

type WeatherRequest struct {
	URL                       string
	Latitude, Longitude       float64
	Timezone, TemperatureUnit string
	AllowPrivate              bool
	Timeout                   time.Duration
}
type WeatherCurrent struct {
	Time        string  `json:"time"`
	Temperature float64 `json:"temperature"`
	Humidity    float64 `json:"humidity"`
	WeatherCode int     `json:"weather_code"`
	IsDay       bool    `json:"is_day"`
	WindSpeed   float64 `json:"wind_speed"`
}
type WeatherDay struct {
	Date                     string  `json:"date"`
	WeatherCode              int     `json:"weather_code"`
	MinimumTemperature       float64 `json:"minimum_temperature"`
	MaximumTemperature       float64 `json:"maximum_temperature"`
	PrecipitationProbability float64 `json:"precipitation_probability"`
}
type WeatherSnapshot struct {
	UpdatedAt       time.Time      `json:"updated_at"`
	Source          string         `json:"source"`
	Latitude        float64        `json:"latitude"`
	Longitude       float64        `json:"longitude"`
	Timezone        string         `json:"timezone"`
	TemperatureUnit string         `json:"temperature_unit"`
	WindSpeedUnit   string         `json:"wind_speed_unit"`
	Current         WeatherCurrent `json:"current"`
	Daily           []WeatherDay   `json:"daily"`
}
type networkHTTPOptions struct {
	dial  func(context.Context, string, string) (net.Conn, error)
	roots *x509.CertPool
}

func weatherURL(request WeatherRequest) (*url.URL, error) {
	u, err := url.Parse(request.URL)
	if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.Fragment != "" || u.RawQuery != "" || u.ForceQuery || u.Opaque != "" || strings.HasSuffix(u.Host, ":") {
		return nil, errors.New("weather endpoint must be an HTTPS URL without credentials, query or fragment")
	}
	if _, err = networkHost(u.Hostname()); err != nil {
		return nil, err
	}
	if u.Port() != "" {
		if p, e := strconv.ParseUint(u.Port(), 10, 16); e != nil || p == 0 {
			return nil, errors.New("invalid HTTPS port")
		}
	}
	if !networkFinite(request.Latitude, -90, 90) || !networkFinite(request.Longitude, -180, 180) {
		return nil, errors.New("invalid weather coordinates")
	}
	if request.TemperatureUnit != "celsius" && request.TemperatureUnit != "fahrenheit" {
		return nil, errors.New("temperature unit must be celsius or fahrenheit")
	}
	if len(request.Timezone) == 0 || len(request.Timezone) > 80 || strings.ContainsAny(request.Timezone, "\r\n\x00") {
		return nil, errors.New("invalid weather timezone")
	}
	q := url.Values{"latitude": {strconv.FormatFloat(request.Latitude, 'f', 6, 64)}, "longitude": {strconv.FormatFloat(request.Longitude, 'f', 6, 64)}, "timezone": {request.Timezone}, "temperature_unit": {request.TemperatureUnit}, "wind_speed_unit": {"kmh"}, "forecast_days": {"3"}, "current": {"temperature_2m,relative_humidity_2m,weather_code,is_day,wind_speed_10m"}, "daily": {"weather_code,temperature_2m_min,temperature_2m_max,precipitation_probability_max"}}
	u.RawQuery = q.Encode()
	return u, nil
}

func FetchWeather(ctx context.Context, request WeatherRequest, answer DNSAnswer, permit PermitEndpoint) (WeatherSnapshot, error) {
	return fetchWeather(ctx, request, answer, permit, networkHTTPOptions{})
}

func networkAnswerLease(host string, ips []string, expires time.Time) error {
	if ip, err := netip.ParseAddr(host); err == nil && ip.Is4() {
		if len(ips) != 1 || ips[0] != ip.String() {
			return errors.New("numeric endpoint does not match its approved address")
		}
	} else if expires.IsZero() {
		return errors.New("hostname addresses require an expiring DNS answer")
	}
	return nil
}

func fetchWeather(ctx context.Context, request WeatherRequest, answer DNSAnswer, permit PermitEndpoint, opts networkHTTPOptions) (WeatherSnapshot, error) {
	u, err := weatherURL(request)
	if err != nil {
		return WeatherSnapshot{}, err
	}
	body, err := fetchNetworkJSON(ctx, "weather", u, answer, request.AllowPrivate, request.Timeout, 256*1024, permit, opts)
	if err != nil {
		return WeatherSnapshot{}, err
	}
	host, _ := networkHost(u.Hostname())
	return parseWeather(body, host, request.TemperatureUnit)
}

// The only HTTPS transport: numeric dialing, hostname-authenticated TLS, no
// proxies or redirects, bounded bodies and permission before every socket.
func fetchNetworkJSON(ctx context.Context, service string, u *url.URL, answer DNSAnswer, allowPrivate bool, timeout time.Duration, maxBody int64, permit PermitEndpoint, opts networkHTTPOptions) ([]byte, error) {
	var err error
	host, _ := networkHost(u.Hostname())
	if answer.Host != host || len(answer.IPv4) == 0 || len(answer.IPv4) > 16 {
		return nil, errors.New(service + " DNS answer does not match endpoint")
	}
	if err = networkAnswerLease(host, answer.IPv4, answer.ExpiresAt); err != nil {
		return nil, err
	}
	port := uint16(443)
	if u.Port() != "" {
		n, _ := strconv.ParseUint(u.Port(), 10, 16)
		port = uint16(n)
	}
	for _, ip := range answer.IPv4 {
		if _, err = networkIPv4(ip, allowPrivate); err != nil {
			return nil, err
		}
	}
	ctx, cancel := networkTimeout(ctx, timeout, 20*time.Second, answer.ExpiresAt)
	defer cancel()
	dial := opts.dial
	if dial == nil {
		dial = (&net.Dialer{Timeout: 8 * time.Second}).DialContext
	}
	transport := &http.Transport{Proxy: nil, DisableKeepAlives: true, DisableCompression: true, TLSHandshakeTimeout: 8 * time.Second, ResponseHeaderTimeout: 10 * time.Second, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12, ServerName: u.Hostname(), RootCAs: opts.roots}}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		if address != net.JoinHostPort(u.Hostname(), strconv.Itoa(int(port))) {
			return nil, errors.New("unexpected HTTPS destination")
		}
		var last error
		for _, ip := range answer.IPv4 {
			if err := networkPermit(ctx, permit, EndpointPermit{Service: service, Hostname: host, Protocol: "tcp", IPv4: ip, Port: port, ExpiresAt: answer.ExpiresAt}); err != nil {
				return nil, err
			}
			conn, err := dial(ctx, "tcp4", net.JoinHostPort(ip, strconv.Itoa(int(port))))
			if err == nil {
				return conn, nil
			}
			last = err
		}
		return nil, last
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("HTTPS redirects are disabled") }}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "CGS2-Local-Services/1.0")
	response, err := client.Do(req)
	if err != nil {
		return nil, errors.New(service + " HTTPS request failed: " + networkHTTPError(err))
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		failure := &providerError{Service: service, Status: response.StatusCode}
		if response.StatusCode == http.StatusTooManyRequests || response.StatusCode == http.StatusServiceUnavailable {
			failure.RetryAt = providerRetryAt(response.Header.Get("Retry-After"), time.Now())
		}
		return nil, failure
	}
	if response.ContentLength > maxBody {
		return nil, errors.New(service + " response is too large")
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, maxBody+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > maxBody {
		return nil, errors.New(service + " response is too large")
	}
	return body, nil
}

func networkHTTPError(err error) string {
	var ue *url.Error
	if errors.As(err, &ue) {
		return ue.Err.Error()
	}
	return err.Error()
}

// Keep provider throttling separate from transport failures. The controller
// must not shorten this delay when the owner refreshes or corrects the clock.
type providerError struct {
	Service string
	Status  int
	RetryAt time.Time
}

func (e *providerError) Error() string {
	return fmt.Sprintf("%s server returned HTTP %d", e.Service, e.Status)
}

func providerRetryAt(value string, now time.Time) time.Time {
	value = strings.TrimSpace(value)
	if seconds, err := strconv.ParseUint(value, 10, 63); err == nil {
		// Bound arithmetic, not a provider's requested cooldown.
		seconds = min(seconds, uint64((1<<63-1)/int64(time.Second)))
		return now.Add(time.Duration(seconds) * time.Second)
	}
	if deadline, err := http.ParseTime(value); err == nil && deadline.After(now) {
		return now.Add(deadline.Sub(now)) // retain a monotonic deadline across clock corrections
	}
	return time.Time{}
}
func networkFinite(n, low, high float64) bool {
	return !math.IsNaN(n) && !math.IsInf(n, 0) && n >= low && n <= high
}
func weatherCodeValid(n int) bool {
	switch n {
	case 0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99:
		return true
	}
	return false
}

func parseWeather(body []byte, source, unit string) (WeatherSnapshot, error) {
	var raw struct {
		Latitude, Longitude *float64
		Timezone            string
		Error               bool
		CurrentUnits        map[string]string `json:"current_units"`
		DailyUnits          map[string]string `json:"daily_units"`
		Current             struct {
			Time        string
			Temperature *float64 `json:"temperature_2m"`
			Humidity    *float64 `json:"relative_humidity_2m"`
			Code        *int     `json:"weather_code"`
			IsDay       *int     `json:"is_day"`
			Wind        *float64 `json:"wind_speed_10m"`
		}
		Daily struct {
			Time []string
			Code []*int     `json:"weather_code"`
			Min  []*float64 `json:"temperature_2m_min"`
			Max  []*float64 `json:"temperature_2m_max"`
			Rain []*float64 `json:"precipitation_probability_max"`
		}
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return WeatherSnapshot{}, errors.New("weather response is not valid JSON")
	}
	if raw.Error {
		return WeatherSnapshot{}, errors.New("weather provider rejected the request")
	}
	c := raw.Current
	if raw.Latitude == nil || raw.Longitude == nil || !networkFinite(*raw.Latitude, -90, 90) || !networkFinite(*raw.Longitude, -180, 180) || len(raw.Timezone) == 0 || len(raw.Timezone) > 80 || c.Temperature == nil || c.Humidity == nil || c.Code == nil || c.IsDay == nil || c.Wind == nil {
		return WeatherSnapshot{}, errors.New("weather response is missing required fields")
	}
	if _, err := time.Parse("2006-01-02T15:04", c.Time); err != nil {
		return WeatherSnapshot{}, errors.New("invalid weather observation time")
	}
	tempUnit := "°C"
	low, high := -100.0, 70.0
	if unit == "fahrenheit" {
		tempUnit = "°F"
		low, high = -148, 158
	}
	if raw.CurrentUnits["temperature_2m"] != tempUnit || raw.DailyUnits["temperature_2m_min"] != tempUnit || raw.DailyUnits["temperature_2m_max"] != tempUnit || raw.CurrentUnits["relative_humidity_2m"] != "%" || raw.CurrentUnits["wind_speed_10m"] != "km/h" || raw.DailyUnits["precipitation_probability_max"] != "%" {
		return WeatherSnapshot{}, errors.New("unexpected weather measurement units")
	}
	if !networkFinite(*c.Temperature, low, high) || !networkFinite(*c.Humidity, 0, 100) || !weatherCodeValid(*c.Code) || (*c.IsDay != 0 && *c.IsDay != 1) || !networkFinite(*c.Wind, 0, 500) {
		return WeatherSnapshot{}, errors.New("weather values are out of range")
	}
	n := len(raw.Daily.Time)
	if n < 1 || n > 7 || len(raw.Daily.Code) != n || len(raw.Daily.Min) != n || len(raw.Daily.Max) != n || len(raw.Daily.Rain) != n {
		return WeatherSnapshot{}, errors.New("invalid daily forecast lengths")
	}
	out := WeatherSnapshot{UpdatedAt: time.Now().UTC(), Source: source, Latitude: *raw.Latitude, Longitude: *raw.Longitude, Timezone: raw.Timezone, TemperatureUnit: tempUnit, WindSpeedUnit: "km/h", Current: WeatherCurrent{c.Time, *c.Temperature, *c.Humidity, *c.Code, *c.IsDay == 1, *c.Wind}}
	previous := ""
	for i, date := range raw.Daily.Time {
		if _, err := time.Parse("2006-01-02", date); err != nil || date <= previous {
			return WeatherSnapshot{}, errors.New("invalid daily forecast date")
		}
		previous = date
		lo, hi, code, rain := raw.Daily.Min[i], raw.Daily.Max[i], raw.Daily.Code[i], raw.Daily.Rain[i]
		if lo == nil || hi == nil || code == nil || rain == nil || !networkFinite(*lo, low, high) || !networkFinite(*hi, low, high) || *lo > *hi || !weatherCodeValid(*code) || !networkFinite(*rain, 0, 100) {
			return WeatherSnapshot{}, errors.New("invalid daily forecast values")
		}
		out.Daily = append(out.Daily, WeatherDay{date, *code, *lo, *hi, *rain})
	}
	return out, nil
}

type NTPRequest struct {
	Hostname     string
	ServerIPs    []string
	Port         uint16
	Timeout      time.Duration
	AllowPrivate bool
	ExpiresAt    time.Time
}
type NTPSample struct {
	Time   time.Time
	RTT    time.Duration
	Server string
}

// Decode the unique NTP era within 2020..2100, including the 2036 rollover.
func ntpTimestamp(raw []byte) (time.Time, error) {
	if len(raw) != 8 || bytes.Equal(raw, make([]byte, 8)) {
		return time.Time{}, errors.New("missing NTP timestamp")
	}
	seconds := int64(binary.BigEndian.Uint32(raw))
	fraction := uint64(binary.BigEndian.Uint32(raw[4:]))
	for era := int64(0); era <= 1; era++ {
		stamp := time.Unix(seconds+era*(1<<32)-2208988800, int64(fraction*1e9>>32)).UTC()
		if !stamp.Before(time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)) && stamp.Before(time.Date(2100, 1, 1, 0, 0, 0, 0, time.UTC)) {
			return stamp, nil
		}
	}
	return time.Time{}, errors.New("NTP timestamp outside supported 2020–2100 range")
}

func parseNTP(packet, nonce []byte, rtt time.Duration) (time.Time, error) {
	if len(packet) < 48 || len(packet) > 1024 || len(nonce) != 8 {
		return time.Time{}, errors.New("invalid NTP packet size")
	}
	version := (packet[0] >> 3) & 7
	if packet[0]&7 != 4 || (version != 3 && version != 4) || packet[0]>>6 == 3 || packet[1] < 1 || packet[1] > 15 {
		return time.Time{}, errors.New("NTP reply is not from a synchronized server")
	}
	if !bytes.Equal(packet[24:32], nonce) {
		return time.Time{}, errors.New("NTP originate token does not match")
	}
	if rtt <= 0 || rtt > 5*time.Second {
		return time.Time{}, errors.New("NTP round trip is outside supported bounds")
	}
	receive, err := ntpTimestamp(packet[32:40])
	if err != nil {
		return time.Time{}, err
	}
	transmit, err := ntpTimestamp(packet[40:48])
	if err != nil {
		return time.Time{}, err
	}
	processing := transmit.Sub(receive)
	if processing < 0 || processing > rtt {
		return time.Time{}, errors.New("inconsistent NTP server timestamps")
	}
	// Equivalent to the RFC5905 symmetric-path offset calculation, using a
	// monotonic round trip and a randomized transmit token rather than local
	// wall-clock timestamps. This does not authenticate the remote server.
	return transmit.Add((rtt - processing) / 2), nil
}

func QueryNTP(ctx context.Context, request NTPRequest, permit PermitEndpoint) (NTPSample, error) {
	if request.Port == 0 || len(request.ServerIPs) < 1 || len(request.ServerIPs) > 8 {
		return NTPSample{}, errors.New("NTP needs one to eight IPv4 servers and an explicit port")
	}
	host, err := networkHost(request.Hostname)
	if err != nil {
		return NTPSample{}, err
	}
	request.Hostname = host
	if err = networkAnswerLease(host, request.ServerIPs, request.ExpiresAt); err != nil {
		return NTPSample{}, err
	}
	for _, ip := range request.ServerIPs {
		if _, err := networkIPv4(ip, request.AllowPrivate); err != nil {
			return NTPSample{}, err
		}
	}
	ctx, cancel := networkTimeout(ctx, request.Timeout, 25*time.Second, request.ExpiresAt)
	defer cancel()
	var best NTPSample
	var bestReceived time.Time
	var last error
	for _, ip := range request.ServerIPs {
		if err := networkPermit(ctx, permit, EndpointPermit{Service: "time", Hostname: request.Hostname, Protocol: "udp", IPv4: ip, Port: request.Port, ExpiresAt: request.ExpiresAt}); err != nil {
			return NTPSample{}, err
		}
		sample, received, err := queryNTPServer(ctx, ip, request.Port)
		if err != nil {
			last = err
			continue
		}
		if bestReceived.IsZero() || sample.RTT < best.RTT {
			best, bestReceived = sample, received
		}
	}
	if bestReceived.IsZero() {
		if last == nil {
			last = errors.New("no usable NTP sample")
		}
		return NTPSample{}, last
	}
	if err := ctx.Err(); err != nil {
		return NTPSample{}, err
	}
	best.Time = best.Time.Add(time.Since(bestReceived))
	return best, nil
}

func queryNTPServer(ctx context.Context, ip string, port uint16) (NTPSample, time.Time, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	conn, err := (&net.Dialer{}).DialContext(ctx, "udp4", net.JoinHostPort(ip, strconv.Itoa(int(port))))
	if err != nil {
		return NTPSample{}, time.Time{}, err
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	deadline, _ := ctx.Deadline()
	if err = conn.SetDeadline(deadline); err != nil {
		return NTPSample{}, time.Time{}, err
	}
	query := make([]byte, 48)
	query[0] = 0x23 // NTPv4 client mode
	if _, err = rand.Read(query[40:48]); err != nil {
		return NTPSample{}, time.Time{}, err
	}
	start := time.Now()
	if _, err = conn.Write(query); err != nil {
		return NTPSample{}, time.Time{}, err
	}
	packet := make([]byte, 1025)
	n, err := conn.Read(packet)
	received := time.Now()
	if err != nil {
		return NTPSample{}, time.Time{}, err
	}
	rtt := received.Sub(start)
	stamp, err := parseNTP(packet[:n], query[40:48], rtt)
	if err != nil {
		return NTPSample{}, time.Time{}, err
	}
	return NTPSample{Time: stamp, RTT: rtt, Server: net.JoinHostPort(ip, strconv.Itoa(int(port)))}, received, nil
}
