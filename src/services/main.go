// CGS2 standalone time/weather controller. No init, USB, network-interface or
// firmware writes. All socket destinations pass the explicit policy callback.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type endpointURL struct {
	host string
	port int
}

func urlParse(s string) (endpointURL, error) {
	u, e := url.Parse(s)
	if e != nil {
		return endpointURL{}, e
	}
	p := 443
	if u.Port() != "" {
		p, e = strconv.Atoi(u.Port())
	}
	return endpointURL{u.Hostname(), p}, e
}

type FeatureStatus struct {
	Message     string           `json:"message"`
	LastSuccess string           `json:"last_success"`
	Snapshot    *WeatherSnapshot `json:"snapshot,omitempty"`
}
type Status struct {
	Message     string        `json:"message"`
	LastApplyID string        `json:"last_apply_id"`
	ApplyOK     bool          `json:"apply_ok"`
	Time        FeatureStatus `json:"time"`
	Weather     FeatureStatus `json:"weather"`
	DNS         FeatureStatus `json:"dns"`
	// Home Assistant MQTT discovery; see homeassistant.go.
	HomeAssistant FeatureStatus `json:"home_assistant"`
	MQTT          FeatureStatus `json:"mqtt"`
	Approved      []string      `json:"approved_servers"`
	Updated       string        `json:"updated_at"`
}
type controlRequest struct {
	Command string `json:"command"`
	reply   chan error
}
type jobResult struct {
	service, generation string
	timeSyncs           uint64
	err                 error
	weather             *WeatherSnapshot
	locations           []LocationResult
	ha                  *haOutcome
	mqtt                *mqttOutcome
}
type Controller struct {
	jobs            sync.WaitGroup
	stopping        chan struct{}
	store           *Store
	config          Config
	status          Status
	requests        chan controlRequest
	results         chan jobResult
	cancel          map[string]context.CancelFunc
	next            map[string]time.Time
	failures        map[string]int
	timeSyncs       uint64
	locationID      string
	geocodeRun      func(context.Context, Config, LocationSearchRequest) ([]LocationResult, error)
	ha              haJob
	mqtt            mqttJob
	weatherCooldown time.Time
}

func initialize(s *Store) error {
	if _, e := os.Stat(path(s.Root, "/etc/cgs2-privacy/services.json")); e == nil {
		return nil
	} else if !os.IsNotExist(e) {
		return e
	}
	c := defaults()
	b, _ := os.ReadFile(path(s.Root, "/data/etc/setting.ini"))
	section := ""
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "[") {
			section = line
		}
		if section == "[datetime]" && strings.HasPrefix(line, "timezone=") {
			v := strings.TrimPrefix(line, "timezone=")
			if validZone(v) {
				c.TimeTimezone = v
				c.WeatherTimezone = v
			}
		}
	}
	return writeJSON(path(s.Root, "/etc/cgs2-privacy/services.json"), c)
}
func (c *Controller) publish() {
	c.status.Updated = time.Now().UTC().Format(time.RFC3339)
	c.status.Approved = c.store.approved()
	if e := writeJSON(path(c.store.Root, "/run/cgs2-services/status.json"), c.status); e != nil {
		fmt.Fprintln(os.Stderr, "status write failed:", e)
	}
}
func (c *Controller) stopJobs() {
	c.cancelLocation()
	for _, cancel := range c.cancel {
		cancel()
	}
	c.cancel = map[string]context.CancelFunc{}
}
func (c *Controller) apply() error {
	draft, e := readDraft(c.store.Root)
	c.status.LastApplyID = draft.RequestID
	if e == nil {
		if draft.RequestID == "" || draft.RequestID == c.config.RequestID {
			e = errors.New("save request identifier is missing or has already been used")
		} else {
			e = draft.Validate()
		}
	}
	if e == nil {
		e = c.store.commit(draft)
	}
	c.status.ApplyOK = e == nil
	if e != nil {
		c.status.Message = "Settings were not applied: " + e.Error()
		c.publish()
		return e
	}
	c.stopJobs()
	c.config = draft
	c.next = map[string]time.Time{}
	if time.Now().Before(c.weatherCooldown) {
		c.next["weather"] = c.weatherCooldown
	}
	c.failures = map[string]int{}
	c.status.Time.Message = "Disabled"
	if draft.TimeEnabled {
		c.status.Time.Message = "Waiting to synchronize"
	}
	c.status.Weather.Message = "Disabled"
	if draft.WeatherEnabled {
		c.status.Weather.Message = "Waiting to update"
	}
	c.status.DNS.Message = "Disabled"
	if draft.DNSEnabled {
		c.status.DNS.Message = "Only used for enabled services"
	}
	c.status.Weather.Snapshot = nil
	c.status.Message = "Saved. Only enabled services and their approved destinations may connect."
	c.publish()
	return nil
}
func (c *Controller) resolve(ctx context.Context, conf Config, service, host string) (DNSAnswer, error) {
	if net.ParseIP(host) != nil {
		return DNSAnswer{Host: host, IPv4: []string{host}}, nil
	}
	if !conf.DNSEnabled || !conf.DNSRotation {
		return DNSAnswer{}, errors.New("DNS and address-rotation approval are required")
	}
	p, _ := number(conf.DNSPort, 1, 65535)
	var last error
	for _, ip := range comma(conf.DNSServers) {
		answer, e := ResolveIPv4(ctx, host, DNSResolverConfig{Service: service, IPv4: ip, Port: uint16(p), Timeout: 4 * time.Second}, c.store.permitFor(conf, service))
		if e == nil {
			return answer, nil
		}
		last = e
		if ctx.Err() != nil {
			break
		}
	}
	return DNSAnswer{}, last
}
func (c *Controller) runTime(ctx context.Context, conf Config, manual bool) error {
	port, _ := number(conf.TimePort, 1, 65535)
	var sample NTPSample
	var last error
	for _, host := range comma(conf.TimeServers) {
		answer, e := c.resolve(ctx, conf, "time", host)
		if e != nil {
			last = e
			continue
		}
		sample, e = QueryNTP(ctx, NTPRequest{Hostname: host, ServerIPs: answer.IPv4, Port: uint16(port), Timeout: 10 * time.Second, AllowPrivate: conf.AllowLAN, ExpiresAt: answer.ExpiresAt}, c.store.permitFor(conf, "time"))
		if e == nil {
			last = nil
			break
		}
		last = e
	}
	if last != nil {
		return last
	}
	if sample.Time.IsZero() {
		return errors.New("no valid time reply")
	}
	sampledAt := time.Now()
	if !manual && sample.Time.Sub(time.Now()).Abs() > 5*time.Minute {
		return errors.New("clock differs by over five minutes; use Sync now to authorize this correction")
	}
	if e := ctx.Err(); e != nil {
		return e
	}
	return c.store.locked(func() error {
		current, e := readConfig(c.store.Root)
		if e != nil || current != conf || !current.TimeEnabled {
			return errors.New("time settings changed")
		}
		if e = ctx.Err(); e != nil {
			return e
		}
		target := sample.Time.Add(time.Since(sampledAt))
		// An owner can adjust the clock while this job waits for the lock.
		// Recheck immediately before writing, using the aged network sample.
		if !manual && target.Sub(time.Now()).Abs() > 5*time.Minute {
			return errors.New("clock differs by over five minutes; use Sync now to authorize this correction")
		}
		return setClock(target)
	})
}
func (c *Controller) startJob(service string, manual bool) error {
	conf := c.config
	timeSyncs := c.timeSyncs
	if service == "time" && !conf.TimeEnabled || service == "weather" && !conf.WeatherEnabled {
		return errors.New("enable and save this service first")
	}
	if _, running := c.cancel[service]; running {
		return errors.New("an update is already running")
	}
	if service == "weather" && time.Now().Before(c.weatherCooldown) {
		return errors.New("weather provider requested a pause; try again after " + c.weatherCooldown.UTC().Format(time.RFC3339))
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	c.cancel[service] = cancel
	if service == "time" {
		c.status.Time.Message = "Synchronizing…"
	} else {
		c.status.Weather.Message = "Updating…"
	}
	c.publish()
	c.jobs.Add(1)
	go func() {
		defer c.jobs.Done()
		r := jobResult{service: service, generation: conf.RequestID, timeSyncs: timeSyncs}
		if service == "time" {
			r.err = c.runTime(ctx, conf, manual)
		} else {
			u, _ := urlParse(conf.WeatherURL)
			answer, e := c.resolve(ctx, conf, service, u.host)
			r.err = e
			if e == nil {
				lat, _ := strconv.ParseFloat(conf.Latitude, 64)
				lon, _ := strconv.ParseFloat(conf.Longitude, 64)
				snapshot, e := FetchWeather(ctx, WeatherRequest{URL: conf.WeatherURL, Latitude: lat, Longitude: lon, Timezone: conf.WeatherTimezone, TemperatureUnit: conf.TemperatureUnit, AllowPrivate: conf.AllowLAN, Timeout: 15 * time.Second}, answer, c.store.permitFor(conf, service))
				r.err = e
				if e == nil {
					r.weather = &snapshot
				}
			}
		}
		cancel()
		if e := c.store.revoke(service, conf.RequestID); e != nil {
			r.err = errors.Join(r.err, fmt.Errorf("could not revoke temporary service permission: %w", e))
		}
		select {
		case c.results <- r:
		case <-c.stopping:
		}
	}()
	return nil
}
func (c *Controller) done(r jobResult) {
	if r.service == "mqtt" {
		c.mqttDone(r)
		return
	}
	if r.service == "geocode" {
		c.locationDone(r)
		return
	}
	if r.service == "homeassistant" {
		c.homeAssistantDone(r)
		return
	}
	if r.generation != c.config.RequestID {
		return
	}
	delete(c.cancel, r.service)
	state := &c.status.Time
	if r.service == "weather" {
		state = &c.status.Weather
	}
	interval := time.Hour
	if r.service == "time" {
		n, _ := strconv.Atoi(c.config.TimeHours)
		interval = time.Duration(n) * time.Hour
	} else {
		n, _ := strconv.Atoi(c.config.WeatherMinutes)
		interval = time.Duration(n) * time.Minute
	}
	regularInterval := interval
	if r.err != nil {
		c.failures[r.service]++
		state.Message = r.err.Error()
		delay := time.Minute * time.Duration(1<<min(c.failures[r.service]-1, 6))
		if delay < interval {
			interval = delay
		}
		var provider *providerError
		if r.service == "weather" && errors.As(r.err, &provider) {
			if provider.Status == 429 || !provider.RetryAt.IsZero() {
				until := time.Now().Add(interval)
				if provider.Status == 429 {
					until = time.Now().Add(regularInterval)
				}
				if provider.RetryAt.After(until) {
					until = provider.RetryAt
				}
				if until.After(c.weatherCooldown) {
					c.weatherCooldown = until
				}
			}
		}
	} else {
		c.failures[r.service] = 0
		state.LastSuccess = time.Now().UTC().Format(time.RFC3339)
		state.Message = "Updated successfully"
		if r.weather != nil {
			state.Snapshot = r.weather
		}
	}
	c.next[r.service] = time.Now().Add(interval)
	if r.service == "weather" && c.weatherCooldown.After(c.next[r.service]) {
		c.next[r.service] = c.weatherCooldown
	}
	if r.service == "time" && r.err == nil {
		c.timeSyncs++
		// A bad clock can make HTTPS fail until an explicit time correction.
		// Do not leave weather waiting through the old failure backoff.
		if _, running := c.cancel["weather"]; !running && c.config.WeatherEnabled && c.failures["weather"] > 0 {
			c.retryWeatherAfterSync()
		}
	}
	if r.service == "weather" && r.err != nil && c.config.WeatherEnabled && r.timeSyncs < c.timeSyncs {
		// The failed request may have been in flight when time was corrected.
		// Its replacement captures the new counter, so it can retry only once.
		c.retryWeatherAfterSync()
	}
	c.publish()
}

func (c *Controller) retryWeatherAfterSync() {
	if time.Now().Before(c.weatherCooldown) {
		return
	}
	delete(c.next, "weather")
	c.failures["weather"] = 0
	c.status.Weather.Message = "Waiting to update after time sync"
}

func daemon(s *Store) error {
	if e := initialize(s); e != nil {
		return e
	}
	conf, e := readConfig(s.Root)
	if e != nil {
		return e
	}
	if e = s.reset(); e != nil {
		return e
	}
	c := &Controller{store: s, config: conf, stopping: make(chan struct{}), requests: make(chan controlRequest), results: make(chan jobResult, 8), cancel: map[string]context.CancelFunc{}, next: map[string]time.Time{}, failures: map[string]int{}}
	if e = c.recoverLocationStatus(); e != nil {
		return e
	}
	c.status.Message = "Services ready; only explicitly enabled features can connect."
	c.status.Time.Message = "Disabled"
	c.status.Weather.Message = "Disabled"
	c.status.DNS.Message = "Disabled"
	if conf.DNSEnabled {
		c.status.DNS.Message = "Only used for enabled services"
	}
	c.publish()
	socket := path(s.Root, "/run/cgs2-services/control.sock")
	os.Remove(socket)
	listener, e := net.Listen("unix", socket)
	if e != nil {
		return e
	}
	defer listener.Close()
	defer os.Remove(socket)
	os.Chmod(socket, 0600)
	defer func() {
		close(c.stopping)
		c.stopJobs()
		c.mqttStop()
		c.haStop()
		c.jobs.Wait()
		s.reset()
	}()
	go func() {
		for {
			conn, e := listener.Accept()
			if e != nil {
				return
			}
			go func() {
				defer conn.Close()
				conn.SetDeadline(time.Now().Add(20 * time.Second))
				var request struct {
					Command string `json:"command"`
				}
				decoder := json.NewDecoder(ioLimit(conn, 1024))
				if decoder.Decode(&request) != nil {
					return
				}
				r := controlRequest{Command: request.Command, reply: make(chan error, 1)}
				select {
				case c.requests <- r:
				case <-time.After(15 * time.Second):
					return
				}
				select {
				case err := <-r.reply:
					msg := "ok"
					if err != nil {
						msg = err.Error()
					}
					json.NewEncoder(conn).Encode(map[string]any{"ok": err == nil, "message": msg})
				case <-time.After(15 * time.Second):
				}
			}()
		}
	}()
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	for {
		select {
		case <-signals:
			return nil
		case request := <-c.requests:
			var err error
			switch request.Command {
			case "apply":
				err = c.apply()
			case "time-now":
				err = c.startJob("time", true)
			case "geocode":
				err = c.startGeocode()
			case "weather-now":
				err = c.startJob("weather", true)
			case "homeassistant":
				c.refreshHomeAssistant()
			case "stop":
				request.reply <- nil
				return nil
			case "ping":
			default:
				err = errors.New("unknown service action")
			}
			if err != nil && request.Command != "apply" {
				c.status.Message = err.Error()
				c.publish()
			}
			request.reply <- err
		case result := <-c.results:
			c.done(result)
		case <-ticker.C:
			if e = s.expire(); e != nil {
				c.stopJobs()
				return fmt.Errorf("transient permission expiry failed: %w", e)
			}
			for _, service := range []string{"time", "weather"} {
				enabled := c.config.TimeEnabled
				if service == "weather" {
					enabled = c.config.WeatherEnabled
				} else {
					enabled = c.config.TimeEnabled
				}
				if enabled {
					if _, running := c.cancel[service]; !running && !time.Now().Before(c.next[service]) {
						c.startJob(service, false)
					}
				}
			}
			c.homeAssistantTick(time.Now())
			c.mqttTick(time.Now())
		}
	}
}

func request(root, command string) error {
	conn, e := net.DialTimeout("unix", path(root, "/run/cgs2-services/control.sock"), 2*time.Second)
	if e != nil {
		return e
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(20 * time.Second))
	if e = json.NewEncoder(conn).Encode(map[string]string{"command": command}); e != nil {
		return e
	}
	var reply struct {
		OK      bool   `json:"ok"`
		Message string `json:"message"`
	}
	if e = json.NewDecoder(conn).Decode(&reply); e != nil {
		return e
	}
	if !reply.OK {
		return errors.New(reply.Message)
	}
	return nil
}
func supervise(s *Store) error {
	os.MkdirAll(path(s.Root, "/run/cgs2-services"), 0700)
	f, e := os.OpenFile(path(s.Root, "/run/cgs2-services/supervisor.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return e
	}
	defer f.Close()
	if e = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		return nil
	}
	log := &serviceLog{name: path(s.Root, "/run/cgs2-services/worker.log")}
	signal.Ignore(syscall.SIGHUP)
	exe, e := os.Executable()
	if e != nil {
		return e
	}
	for {
		cmd := exec.Command(exe, "daemon")
		cmd.Stdout = log
		cmd.Stderr = log
		err := cmd.Run()
		if reset := s.reset(); reset != nil {
			fmt.Fprintln(log, "worker exited; restoring MQTT-only policy failed:", reset)
			return fmt.Errorf("worker exited; restoring MQTT-only policy failed: %w", reset)
		}
		if err == nil {
			return nil
		}
		fmt.Fprintln(log, "service worker exited; transient permissions cleared:", err)
		time.Sleep(5 * time.Second)
	}
}
func start(s *Store) error {
	if request(s.Root, "ping") == nil {
		return nil
	}
	os.MkdirAll(path(s.Root, "/run/cgs2-services"), 0700)
	exe, e := os.Executable()
	if e != nil {
		return e
	}
	cmd := exec.Command(exe, "supervise")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if e = cmd.Start(); e != nil {
		return e
	}
	cmd.Process.Release()
	return nil
}
func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Usage: cgs2-services start|apply|time-now|weather-now|geocode|homeassistant|stop|status|initialize|mqtt-policy disable|mqtt-policy set IPv4 PORT")
		os.Exit(2)
	}
	s := newStore("")
	var err error
	switch os.Args[1] {
	case "initialize":
		err = initialize(s)
	case "start":
		err = start(s)
	case "supervise":
		err = supervise(s)
	case "daemon":
		err = daemon(s)
	case "runtime-reset":
		err = s.reset()
	case "status":
		b, e := os.ReadFile("/run/cgs2-services/status.json")
		err = e
		if e == nil {
			fmt.Print(string(b))
		}
	case "apply", "time-now", "weather-now", "geocode", "homeassistant", "stop", "ping":
		err = request("", os.Args[1])
	case "mqtt-policy":
		if len(os.Args) == 3 && os.Args[2] == "disable" {
			err = s.mqtt(false, "", 0)
		} else if len(os.Args) == 5 && os.Args[2] == "set" {
			p, e := number(os.Args[4], 1, 65535)
			err = e
			if e == nil {
				err = s.mqtt(true, os.Args[3], p)
			}
		} else {
			err = errors.New("invalid MQTT policy action")
		}
	default:
		err = errors.New("unknown command")
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// Use a bounded reader without accepting unbounded local socket input.
func ioLimit(c net.Conn, n int64) *io.LimitedReader { return &io.LimitedReader{R: c, N: n} }
