package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// These tests never start the real daemon, invoke a clock setter, or grant a
// socket permission. The filter is a host-only stub under an isolated root.
func controllerFixture(t *testing.T) *Controller {
	t.Helper()
	root := t.TempDir()
	s := newStore(root)
	s.Uptime = func() float64 { return 100 }
	s.Filter = func(string, string) error { return nil }
	if err := atomicWrite(path(root, "/etc/cgs2-privacy/allowlist.conf"), nil); err != nil {
		t.Fatal(err)
	}
	conf := defaults()
	conf.RequestID = "original-request"
	if err := writeJSON(path(root, "/etc/cgs2-privacy/services.json"), conf); err != nil {
		t.Fatal(err)
	}
	return &Controller{store: s, config: conf, results: make(chan jobResult, 8), cancel: map[string]context.CancelFunc{}, next: map[string]time.Time{}, failures: map[string]int{}}
}

func controllerDraft(t *testing.T, c *Controller, conf Config) {
	t.Helper()
	b, err := json.Marshal(conf)
	if err != nil {
		t.Fatal(err)
	}
	data := "[request]\npayload=" + base64.StdEncoding.EncodeToString(b) + "\n"
	if err := atomicWrite(path(c.store.Root, "/data/etc/cgs2-services-draft.ini"), []byte(data)); err != nil {
		t.Fatal(err)
	}
}

func controllerStatus(t *testing.T, c *Controller) Status {
	t.Helper()
	b, err := os.ReadFile(path(c.store.Root, "/run/cgs2-services/status.json"))
	if err != nil {
		t.Fatal(err)
	}
	var s Status
	if err := json.Unmarshal(b, &s); err != nil {
		t.Fatal(err)
	}
	return s
}

func TestControllerDisabledJobsDoNotRequestNetwork(t *testing.T) {
	c := controllerFixture(t)
	c.store.Filter = func(string, string) error { t.Fatal("disabled job touched policy"); return nil }
	for _, service := range []string{"time", "weather"} {
		if err := c.startJob(service, true); err == nil {
			t.Fatalf("disabled %s job started", service)
		}
	}
	if len(c.cancel) != 0 || len(c.results) != 0 {
		t.Fatal("disabled jobs allocated active workers")
	}
}

func TestControllerApplyCancelsOldGenerationAndPublishesRequest(t *testing.T) {
	c := controllerFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	c.cancel["weather"] = cancel
	c.next["weather"] = time.Now().Add(time.Hour)
	c.failures["weather"] = 5
	c.status.Weather.Snapshot = &WeatherSnapshot{Source: "old.example"}
	next := c.config
	next.RequestID = "new-request"
	next.TimeEnabled = true
	controllerDraft(t, c, next)
	if err := c.apply(); err != nil {
		t.Fatal(err)
	}
	if ctx.Err() != context.Canceled || len(c.cancel) != 0 || len(c.next) != 0 || len(c.failures) != 0 {
		t.Fatal("old work was not cancelled and reset")
	}
	got, err := readConfig(c.store.Root)
	if err != nil || got.RequestID != next.RequestID || !got.TimeEnabled {
		t.Fatalf("new configuration missing: %+v %v", got, err)
	}
	status := controllerStatus(t, c)
	if !status.ApplyOK || status.LastApplyID != next.RequestID || status.Time.Message != "Waiting to synchronize" || status.Weather.Snapshot != nil || len(status.Approved) != 0 {
		t.Fatalf("incorrect applied status: %+v", status)
	}
}

func TestControllerInvalidApplyRetainsRunningConfiguration(t *testing.T) {
	c := controllerFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c.cancel["weather"] = cancel
	next := c.config
	next.RequestID = "rejected-request"
	next.TimePort = "0"
	controllerDraft(t, c, next)
	c.store.Filter = func(string, string) error { t.Fatal("invalid draft touched firewall"); return nil }
	if err := c.apply(); err == nil {
		t.Fatal("invalid draft applied")
	}
	if ctx.Err() != nil || c.config.RequestID != "original-request" {
		t.Fatal("failed save cancelled current configuration")
	}
	status := controllerStatus(t, c)
	if status.ApplyOK || status.LastApplyID != "rejected-request" || !strings.Contains(status.Message, "not applied") {
		t.Fatalf("incorrect rejected status: %+v", status)
	}
}

func TestControllerFilterFailureDoesNotCommitDraft(t *testing.T) {
	c := controllerFixture(t)
	next := c.config
	next.RequestID = "new-request"
	controllerDraft(t, c, next)
	c.store.Filter = func(string, string) error { return errors.New("fixture filter failure") }
	if err := c.apply(); err == nil {
		t.Fatal("failed firewall allowed settings commit")
	}
	got, err := readConfig(c.store.Root)
	if err != nil || got.RequestID != "original-request" {
		t.Fatalf("configuration changed after filter failure: %+v %v", got, err)
	}
	if c.status.ApplyOK || !strings.Contains(c.status.Message, "fixture filter failure") {
		t.Fatalf("missing failure status: %+v", c.status)
	}
}

func TestControllerReusedApplyIDRejected(t *testing.T) {
	c := controllerFixture(t)
	next := c.config
	next.TimeEnabled = true
	controllerDraft(t, c, next)
	if err := c.apply(); err == nil {
		t.Fatal("reused generation identifier accepted")
	}
	if c.config.TimeEnabled {
		t.Fatal("reused generation changed current settings")
	}
}

func TestControllerStaleResultCannotReplaceNewState(t *testing.T) {
	c := controllerFixture(t)
	c.status.Weather.Message = "new generation running"
	_, cancel := context.WithCancel(context.Background())
	defer cancel()
	c.cancel["weather"] = cancel
	c.done(jobResult{service: "weather", generation: "old-request", weather: &WeatherSnapshot{Source: "stale.example"}})
	if c.status.Weather.Message != "new generation running" || c.status.Weather.Snapshot != nil || len(c.cancel) != 1 || len(c.next) != 0 {
		t.Fatal("stale worker modified current generation")
	}
}

func TestControllerPermissionFailureNeverOpensSocket(t *testing.T) {
	for _, service := range []string{"time", "weather"} {
		t.Run(service, func(t *testing.T) {
			c := controllerFixture(t)
			c.config.TimeEnabled = service == "time"
			c.config.TimeServers = "162.159.200.1" // one peer; deny before its UDP dial
			c.config.WeatherEnabled = service == "weather"
			c.config.WeatherURL = "https://1.1.1.1/v1/forecast"
			c.config.Latitude = "52.5"
			c.config.Longitude = "13.4"
			if err := writeJSON(path(c.store.Root, "/etc/cgs2-privacy/services.json"), c.config); err != nil {
				t.Fatal(err)
			}
			var calls atomic.Int32
			c.store.Filter = func(string, string) error {
				if calls.Add(1) == 1 {
					return errors.New("fixture denies permission")
				}
				return nil
			}
			if err := c.startJob(service, true); err != nil {
				t.Fatal(err)
			}
			select {
			case r := <-c.results:
				if r.err == nil || !strings.Contains(r.err.Error(), "fixture denies permission") {
					t.Fatalf("lost permission failure: %v", r.err)
				}
				c.done(r)
			case <-time.After(2 * time.Second):
				t.Fatal("permission failure did not finish immediately")
			}
			state := c.status.Time
			if service == "weather" {
				state = c.status.Weather
			}
			if state.LastSuccess != "" || state.Snapshot != nil || !strings.Contains(state.Message, "fixture denies permission") || len(c.cancel) != 0 || c.failures[service] != 1 {
				t.Fatalf("incorrect failure state: %+v", state)
			}
			if !c.next[service].After(time.Now()) || c.next[service].After(time.Now().Add(61*time.Second)) {
				t.Fatal("failure retry is not bounded to a minute")
			}
		})
	}
}

func TestControllerSuccessAndBackoff(t *testing.T) {
	c := controllerFixture(t)
	c.failures["weather"] = 4
	weather := &WeatherSnapshot{Source: "weather.example"}
	c.done(jobResult{service: "weather", generation: c.config.RequestID, weather: weather})
	if c.status.Weather.Snapshot != weather || c.status.Weather.LastSuccess == "" || c.failures["weather"] != 0 {
		t.Fatal("successful result not published")
	}
	for i := 0; i < 10; i++ {
		c.done(jobResult{service: "weather", generation: c.config.RequestID, err: errors.New("offline")})
	}
	// Retry backoff cannot exceed the user's regular 30-minute interval.
	remaining := time.Until(c.next["weather"])
	if remaining < 29*time.Minute || remaining > 30*time.Minute {
		t.Fatalf("unexpected backoff %s", remaining)
	}
	if c.status.Weather.Snapshot != weather {
		t.Fatal("temporary error discarded the last known forecast")
	}
}

func TestControllerTimeSyncRetriesFailedWeather(t *testing.T) {
	c := controllerFixture(t)
	c.config.WeatherEnabled = true
	previous := &WeatherSnapshot{Source: "previous.example"}
	c.status.Weather.Snapshot = previous
	for i := 0; i < 6; i++ {
		c.done(jobResult{service: "weather", generation: c.config.RequestID, err: errors.New("certificate is not yet valid")})
	}
	if time.Until(c.next["weather"]) < 29*time.Minute {
		t.Fatal("fixture did not reach weather backoff")
	}
	c.done(jobResult{service: "time", generation: c.config.RequestID})
	if c.next["weather"].After(time.Now()) || c.failures["weather"] != 0 {
		t.Fatal("successful time sync left weather waiting in backoff")
	}
	if c.status.Weather.Snapshot != previous {
		t.Fatal("retry discarded the previous forecast")
	}
	// A fresh request that still fails must back off instead of spinning.
	c.done(jobResult{service: "weather", generation: c.config.RequestID, timeSyncs: c.timeSyncs, err: errors.New("provider unavailable")})
	if time.Until(c.next["weather"]) < 59*time.Second || c.failures["weather"] != 1 {
		t.Fatal("post-sync retry bypassed failure backoff")
	}
}

func TestControllerWeatherFinishingAcrossTimeSync(t *testing.T) {
	for _, failed := range []bool{false, true} {
		c := controllerFixture(t)
		c.config.WeatherEnabled = true
		_, cancel := context.WithCancel(context.Background())
		defer cancel()
		c.cancel["weather"] = cancel
		result := jobResult{service: "weather", generation: c.config.RequestID, timeSyncs: c.timeSyncs}
		if failed {
			result.err = errors.New("certificate is not yet valid")
		} else {
			result.weather = &WeatherSnapshot{Source: "weather.example"}
		}
		c.done(jobResult{service: "time", generation: c.config.RequestID})
		if _, running := c.cancel["weather"]; !running {
			t.Fatal("time sync displaced the in-flight weather request")
		}
		c.done(result)
		if failed && (c.next["weather"].After(time.Now()) || c.failures["weather"] != 0) {
			t.Fatal("weather failure from before clock correction was not retried")
		}
		if !failed && (time.Until(c.next["weather"]) < 29*time.Minute || c.status.Weather.Snapshot != result.weather) {
			t.Fatal("successful concurrent weather was unnecessarily retried")
		}
	}
}

func TestControllerTimeSyncKeepsUnrelatedWeatherSchedule(t *testing.T) {
	for _, mode := range []string{"failed time", "disabled weather", "healthy weather", "old generation"} {
		t.Run(mode, func(t *testing.T) {
			c := controllerFixture(t)
			c.config.WeatherEnabled = mode != "disabled weather"
			if mode != "healthy weather" {
				c.failures["weather"] = 3
			}
			next := time.Now().Add(20 * time.Minute)
			c.next["weather"] = next
			r := jobResult{service: "time", generation: c.config.RequestID}
			if mode == "failed time" {
				r.err = errors.New("clock differs by over five minutes; use Sync now to authorize this correction")
			} else if mode == "old generation" {
				r.generation = "previous-request"
			}
			c.done(r)
			if c.next["weather"] != next {
				t.Fatal("unrelated time result rescheduled weather")
			}
		})
	}
}
