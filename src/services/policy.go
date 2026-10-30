package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type Lease struct {
	Service    string  `json:"service"`
	Endpoint   string  `json:"endpoint"`
	Until      float64 `json:"until_uptime"`
	Generation string  `json:"generation"`
}
type Store struct {
	Root   string
	Filter func(string, string) error
	Uptime func() float64
}

func newStore(root string) *Store {
	s := &Store{Root: root}
	s.Uptime = func() float64 {
		b, e := os.ReadFile("/proc/uptime")
		fields := strings.Fields(string(b))
		if e != nil || len(fields) == 0 {
			return math.Inf(1)
		}
		v, e := strconv.ParseFloat(fields[0], 64)
		if e != nil || math.IsNaN(v) || math.IsInf(v, 0) || v < 0 {
			return math.Inf(1)
		}
		return v
	}
	s.Filter = func(action, config string) error {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, path(root, "/usr/local/sbin/cgs2-filter"), action, "--config", config)
		if b, e := cmd.CombinedOutput(); e != nil {
			return fmt.Errorf("filter %s failed: %s", action, strings.TrimSpace(string(b)))
		}
		return nil
	}
	return s
}
func (s *Store) locked(fn func() error) error {
	p := path(s.Root, "/run/cgs2-services-config.lock")
	if e := os.MkdirAll(path(s.Root, "/run/cgs2-services"), 0700); e != nil {
		return e
	}
	f, e := os.OpenFile(p, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return e
	}
	defer f.Close()
	deadline := time.Now().Add(12 * time.Second)
	for {
		e = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if e == nil {
			break
		}
		if !errors.Is(e, syscall.EWOULDBLOCK) || time.Now().After(deadline) {
			return errors.New("settings controller busy")
		}
		time.Sleep(25 * time.Millisecond)
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	return fn()
}
func endpointValid(v string) bool {
	p := strings.Split(v, ":")
	if len(p) != 3 || (p[0] != "udp" && p[0] != "tcp") || !validIPv4(p[1], true) {
		return false
	}
	_, e := number(p[2], 1, 65535)
	return e == nil
}
func (s *Store) bootPolicy() ([]string, error) {
	b, e := os.ReadFile(path(s.Root, "/etc/cgs2-privacy/allowlist.conf"))
	if e != nil {
		return nil, e
	}
	var out []string
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if !endpointValid(line) {
			return nil, errors.New("existing persistent policy is invalid; refusing mutation")
		}
		out = append(out, line)
	}
	return out, nil
}
func (s *Store) leases() ([]Lease, error) {
	b, e := os.ReadFile(path(s.Root, "/run/cgs2-services/leases.json"))
	if os.IsNotExist(e) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	var leases []Lease
	if e = json.Unmarshal(b, &leases); e != nil {
		return nil, e
	}
	for _, l := range leases {
		if (l.Service != "time" && l.Service != "weather" && l.Service != "geocode") || !endpointValid(l.Endpoint) {
			return nil, errors.New("invalid transient permission state")
		}
	}
	return leases, nil
}
func union(boot []string, ls []Lease, now float64) []string {
	m := map[string]bool{}
	for _, v := range boot {
		m[v] = true
	}
	for _, l := range ls {
		if l.Until > now {
			m[l.Endpoint] = true
		}
	}
	out := make([]string, 0, len(m))
	for v := range m {
		out = append(out, v)
	}
	sort.Strings(out)
	return out
}

// Every policy transaction holds the global lock. The marker is created before
// either persistent policy changes or kernel changes, and removed only after
// both kernel rules and metadata describe the same state. It survives a killed
// CLI process in RAM; after a reboot the kernel and /run both start clean.
func (s *Store) policyPending() (bool, error) {
	_, err := os.Lstat(path(s.Root, "/run/cgs2-services/policy-pending"))
	if os.IsNotExist(err) {
		return false, nil
	}
	return err == nil, err
}
func (s *Store) beginPolicy() error {
	pending, err := s.policyPending()
	if err != nil || pending {
		return err
	}
	return atomicWrite(path(s.Root, "/run/cgs2-services/policy-pending"), []byte("pending\n"))
}
func (s *Store) finishPolicy() error {
	return os.Remove(path(s.Root, "/run/cgs2-services/policy-pending"))
}

func (s *Store) apply(endpoints []string) error {
	if len(endpoints) > 64 {
		return errors.New("too many approved endpoints")
	}
	f, e := os.CreateTemp(path(s.Root, "/run/cgs2-services"), "policy-")
	if e != nil {
		return e
	}
	defer os.Remove(f.Name())
	f.Chmod(0600)
	text := strings.Join(endpoints, "\n")
	if text != "" {
		text += "\n"
	}
	_, e = f.WriteString(text)
	f.Close()
	if e != nil {
		return e
	}
	if e = s.Filter("dry-run", f.Name()); e != nil {
		return e
	}
	if e = s.beginPolicy(); e != nil {
		return e
	}
	return s.Filter("apply", f.Name())
}

// The existing boot policy contains only the persistent MQTT endpoint. Service
// DNS mappings are never written into it and cannot trigger a boot failure.
func (s *Store) mqtt(enabled bool, host string, port int) error {
	return s.locked(func() error {
		old, e := s.bootPolicy()
		if e != nil {
			return e
		}
		if len(old) > 1 || len(old) == 1 && !strings.HasPrefix(old[0], "tcp:") {
			return errors.New("manual persistent policy needs explicit migration before MQTT changes")
		}
		ls, e := s.leases()
		if e != nil {
			return e
		}
		var next []string
		if enabled {
			v := fmt.Sprintf("tcp:%s:%d", host, port)
			if !endpointValid(v) {
				return errors.New("invalid broker endpoint")
			}
			next = []string{v}
		}
		oldBytes, e := os.ReadFile(path(s.Root, "/etc/cgs2-privacy/allowlist.conf"))
		if e != nil {
			return e
		}
		nextBytes := []byte(strings.Join(next, "\n"))
		if len(next) > 0 {
			nextBytes = append(nextBytes, '\n')
		}
		if e = s.beginPolicy(); e != nil {
			return e
		}
		if e = atomicWrite(path(s.Root, "/etc/cgs2-privacy/allowlist.conf"), nextBytes); e != nil {
			return errors.Join(e, atomicWrite(path(s.Root, "/etc/cgs2-privacy/allowlist.conf"), oldBytes))
		}
		if e = s.apply(union(next, ls, s.Uptime())); e != nil {
			if restore := atomicWrite(path(s.Root, "/etc/cgs2-privacy/allowlist.conf"), oldBytes); restore != nil {
				return fmt.Errorf("%v; restoring persistent policy: %w", e, restore)
			}
			return e
		}
		return s.finishPolicy()
	})
}
func (s *Store) changeLeases(change func([]Lease) ([]Lease, error)) error {
	return s.locked(func() error {
		boot, e := s.bootPolicy()
		if e != nil {
			return e
		}
		old, e := s.leases()
		if e != nil {
			return e
		}
		next, e := change(old)
		if e != nil {
			return e
		}
		if e = s.apply(union(boot, next, s.Uptime())); e != nil {
			return e
		}
		if e = writeJSON(path(s.Root, "/run/cgs2-services/leases.json"), next); e != nil {
			return errors.Join(e,
				writeJSON(path(s.Root, "/run/cgs2-services/leases.json"), old),
				s.apply(union(boot, old, s.Uptime())))
		}
		return s.finishPolicy()
	})
}
func (s *Store) reset() error {
	return s.locked(func() error {
		boot, err := s.bootPolicy()
		if err != nil {
			// Corrupt persistent state cannot be trusted for a recovery policy.
			// Preserve the file for USB inspection but revoke all IP exceptions;
			// the filter retains its USB/loopback debugging exemptions.
			return errors.Join(err, s.apply(nil))
		}
		// Recovery must not depend on decoding the metadata being discarded.
		if err := s.apply(boot); err != nil {
			return err
		}
		if err := writeJSON(path(s.Root, "/run/cgs2-services/leases.json"), []Lease{}); err != nil {
			return err
		}
		return s.finishPolicy()
	})
}
func (s *Store) revoke(service, generation string) error {
	return s.changeLeases(func(old []Lease) ([]Lease, error) {
		var next []Lease
		for _, l := range old {
			if !(l.Service == service && l.Generation == generation) {
				next = append(next, l)
			}
		}
		return next, nil
	})
}
func (s *Store) expire() error {
	return s.locked(func() error {
		old, e := s.leases()
		if e != nil {
			return e
		}
		var next []Lease
		for _, l := range old {
			if l.Until > s.Uptime() {
				next = append(next, l)
			}
		}
		boot, e := s.bootPolicy()
		if e != nil {
			return e
		}
		pending, e := s.policyPending()
		if e != nil {
			return e
		}
		if len(old) == len(next) && !pending {
			return nil
		}
		if e = s.apply(union(boot, next, s.Uptime())); e != nil {
			return e
		}
		// A pending transaction needs reconciliation even if no lease expired.
		// Healthy idle ticks do not launch filter processes or rewrite metadata.
		if len(old) != len(next) {
			if e := writeJSON(path(s.Root, "/run/cgs2-services/leases.json"), next); e != nil {
				return e
			}
		}
		return s.finishPolicy()
	})
}
func (s *Store) approved() []string {
	var out []string
	s.locked(func() error {
		b, e := s.bootPolicy()
		if e != nil {
			return e
		}
		l, e := s.leases()
		if e != nil {
			return e
		}
		out = union(b, l, s.Uptime())
		return nil
	})
	if out == nil {
		return []string{}
	}
	return out
}
func (s *Store) commit(c Config) error {
	if e := c.Validate(); e != nil {
		return e
	}
	return s.locked(func() error {
		b, e := s.bootPolicy()
		if e != nil {
			return e
		}
		old, e := s.leases()
		if e != nil {
			return e
		}
		configPath := path(s.Root, "/etc/cgs2-privacy/services.json")
		oldConfig, e := os.ReadFile(configPath)
		if e != nil {
			return e
		}
		if e = s.apply(b); e != nil {
			return e
		}
		// Remove the old generation's lease metadata before committing a new
		// configuration. A crash between these steps leaves the old config with
		// fewer permissions, never a new config with resurrectable old leases.
		if e = writeJSON(path(s.Root, "/run/cgs2-services/leases.json"), []Lease{}); e != nil {
			return errors.Join(e,
				writeJSON(path(s.Root, "/run/cgs2-services/leases.json"), old),
				s.apply(union(b, old, s.Uptime())))
		}
		if e = writeJSON(configPath, c); e != nil {
			return errors.Join(e, atomicWrite(configPath, oldConfig),
				writeJSON(path(s.Root, "/run/cgs2-services/leases.json"), old),
				s.apply(union(b, old, s.Uptime())))
		}
		return s.finishPolicy()
	})
}
func (s *Store) permitFor(c Config, service string) PermitEndpoint {
	return func(ctx context.Context, p EndpointPermit) error {
		if e := ctx.Err(); e != nil {
			return e
		}
		if p.Service != "" && p.Service != service {
			return errors.New("permission owner mismatch")
		}
		if e := c.Validate(); e != nil {
			return e
		}
		if service != "time" && service != "weather" || service == "time" && !c.TimeEnabled || service == "weather" && !c.WeatherEnabled {
			return errors.New("service is not enabled")
		}
		v := fmt.Sprintf("%s:%s:%d", p.Protocol, p.IPv4, p.Port)
		if !endpointValid(v) {
			return errors.New("invalid requested endpoint")
		}
		// Worker gets permissions only for configured services/resolvers, never an
		// unrestricted 'allow anything' callback. DNS results still need HTTPS TLS.
		allowed := false
		approvedName := false
		if service == "time" {
			for _, host := range comma(c.TimeServers) {
				if net.ParseIP(host) == nil && strings.EqualFold(host, p.Hostname) {
					approvedName = true
				}
			}
		} else {
			u, _ := urlParse(c.WeatherURL)
			approvedName = net.ParseIP(u.host) == nil && strings.EqualFold(u.host, p.Hostname)
		}
		dnsPort, _ := number(c.DNSPort, 1, 65535)
		if c.DNSEnabled && c.DNSRotation && approvedName && (p.Protocol == "udp" || p.Protocol == "tcp") && int(p.Port) == dnsPort {
			for _, ip := range comma(c.DNSServers) {
				if ip == p.IPv4 {
					allowed = true
				}
			}
		}
		if service == "time" && c.TimeEnabled && p.Protocol == "udp" {
			port, _ := number(c.TimePort, 1, 65535)
			for _, host := range comma(c.TimeServers) {
				_, addressError := networkIPv4(p.IPv4, c.AllowLAN)
				if strings.EqualFold(host, p.Hostname) && int(p.Port) == port && addressError == nil && (net.ParseIP(host) == nil || host == p.IPv4) {
					allowed = true
				}
			}
		}
		if service == "weather" && c.WeatherEnabled && p.Protocol == "tcp" {
			u, _ := urlParse(c.WeatherURL)
			_, addressError := networkIPv4(p.IPv4, c.AllowLAN)
			if strings.EqualFold(u.host, p.Hostname) && u.port == int(p.Port) && addressError == nil && (net.ParseIP(u.host) == nil || u.host == p.IPv4) {
				allowed = true
			}
		}
		if !allowed {
			return errors.New("endpoint is not part of the approved service")
		}
		if net.ParseIP(p.Hostname) == nil && p.ExpiresAt.IsZero() {
			return errors.New("hostname permission requires an expiry")
		}
		duration := 35 * time.Second
		if !p.ExpiresAt.IsZero() {
			remaining := time.Until(p.ExpiresAt)
			if remaining <= 0 {
				return errors.New("resolved address has expired")
			}
			if remaining < duration {
				duration = remaining
			}
		}
		// Capture the absolute monotonic expiry before waiting for the global
		// lock; lock contention must never extend a DNS TTL.
		until := s.Uptime() + duration.Seconds()
		return s.changeLeases(func(old []Lease) ([]Lease, error) {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			current, e := readConfig(s.Root)
			if e != nil || current != c {
				return nil, errors.New("settings changed during request")
			}
			if math.IsInf(until, 0) || math.IsNaN(until) || until <= s.Uptime() || !p.ExpiresAt.IsZero() && !time.Now().Before(p.ExpiresAt) {
				return nil, errors.New("permission expired while waiting for the controller")
			}
			next := make([]Lease, 0, len(old)+1)
			for _, l := range old {
				if !(l.Service == service && l.Endpoint == v) && l.Until > s.Uptime() {
					next = append(next, l)
				}
			}
			return append(next, Lease{service, v, until, c.RequestID}), nil
		})
	}
}
