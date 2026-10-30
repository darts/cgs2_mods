package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net"
	"strings"
	"time"
)

// Unlike recurring service permissions, this grant belongs to one explicitly
// approved request. The saved DNS and weather switches need not be enabled.
func (s *Store) permitGeocode(conf Config, r LocationSearchRequest) PermitEndpoint {
	return func(ctx context.Context, p EndpointPermit) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := r.Validate(); err != nil {
			return err
		}
		if p.Service != "geocode" {
			return errors.New("location-search permission owner mismatch")
		}
		u, _ := urlParse(r.Endpoint)
		if !strings.EqualFold(u.host, p.Hostname) {
			return errors.New("hostname is not part of this location-search approval")
		}
		endpoint := fmt.Sprintf("%s:%s:%d", p.Protocol, p.IPv4, p.Port)
		if !endpointValid(endpoint) {
			return errors.New("invalid location-search endpoint")
		}
		allowed := false
		dnsPort, _ := number(r.DNSPort, 1, 65535)
		if net.ParseIP(u.host) == nil && r.DNSRotation && (p.Protocol == "tcp" || p.Protocol == "udp") && int(p.Port) == dnsPort {
			for _, resolver := range comma(r.DNSServers) {
				if resolver == p.IPv4 {
					allowed = true
				}
			}
		}
		if p.Protocol == "tcp" && int(p.Port) == u.port {
			_, err := networkIPv4(p.IPv4, r.AllowLAN)
			if err == nil && (net.ParseIP(u.host) == nil || u.host == p.IPv4) {
				allowed = true
			}
		}
		if !allowed {
			return errors.New("endpoint is not part of this location-search approval")
		}
		if net.ParseIP(u.host) == nil && p.ExpiresAt.IsZero() {
			return errors.New("location-search hostname permission requires expiry")
		}
		duration := 35 * time.Second
		if deadline, ok := ctx.Deadline(); ok && time.Until(deadline) < duration {
			duration = time.Until(deadline)
		}
		if !p.ExpiresAt.IsZero() && time.Until(p.ExpiresAt) < duration {
			duration = time.Until(p.ExpiresAt)
		}
		// Convert TTL to boot-relative time before any lock wait or NTP clock
		// adjustment. Lock contention may shorten but never extend consent.
		until := s.Uptime() + duration.Seconds()
		return s.changeLeases(func(old []Lease) ([]Lease, error) {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			current, err := readConfig(s.Root)
			if err != nil || current != conf {
				return nil, errors.New("service settings changed during location search")
			}
			if math.IsInf(until, 0) || math.IsNaN(until) || duration <= 0 || until <= s.Uptime() || !p.ExpiresAt.IsZero() && !time.Now().Before(p.ExpiresAt) {
				return nil, errors.New("location-search permission expired while waiting")
			}
			next := make([]Lease, 0, len(old)+1)
			for _, l := range old {
				if !(l.Service == "geocode" && l.Generation == r.RequestID && l.Endpoint == endpoint) && l.Until > s.Uptime() {
					next = append(next, l)
				}
			}
			return append(next, Lease{"geocode", endpoint, until, r.RequestID}), nil
		})
	}
}
