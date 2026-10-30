package main

import (
	"bytes"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestProviderRetryAt(t *testing.T) {
	now := time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC)
	for _, value := range []string{"120", now.Add(2 * time.Minute).Format(http.TimeFormat)} {
		if got := providerRetryAt(value, now); !got.Equal(now.Add(2 * time.Minute)) {
			t.Fatalf("%q: %v", value, got)
		}
	}
	for _, value := range []string{"", "-1", "soon", now.Add(-time.Hour).Format(http.TimeFormat)} {
		if !providerRetryAt(value, now).IsZero() {
			t.Fatalf("accepted %q", value)
		}
	}
}

func TestWeatherCooldownBlocksManualAndClockRetries(t *testing.T) {
	for _, status := range []int{429, 503} {
		c := controllerFixture(t)
		c.config.WeatherEnabled = true
		until := time.Now().Add(2 * time.Hour)
		c.done(jobResult{service: "weather", generation: c.config.RequestID,
			err: errors.Join(errors.New("fixture"), &providerError{Service: "weather", Status: status, RetryAt: until})})
		if c.next["weather"].Before(until) {
			t.Fatal("shortened provider cooldown")
		}
		if c.startJob("weather", true) == nil {
			t.Fatal("manual refresh bypassed cooldown")
		}
		c.done(jobResult{service: "time", generation: c.config.RequestID})
		if c.next["weather"].Before(until) {
			t.Fatal("clock correction bypassed cooldown")
		}
		c.config.RequestID = "changed-settings"
		c.next = map[string]time.Time{}
		if c.startJob("weather", false) == nil {
			t.Fatal("settings change bypassed cooldown")
		}
		c.weatherCooldown = time.Now().Add(-time.Second)
		c.retryWeatherAfterSync()
		if _, exists := c.next["weather"]; exists {
			t.Fatal("expired cooldown prevented retry")
		}
	}
}

func TestWeather429WithoutHeaderUsesRegularInterval(t *testing.T) {
	c := controllerFixture(t)
	c.done(jobResult{service: "weather", generation: c.config.RequestID,
		err: &providerError{Service: "weather", Status: 429}})
	if delay := time.Until(c.next["weather"]); delay < 29*time.Minute || delay > 31*time.Minute {
		t.Fatalf("unexpected delay %s", delay)
	}
}

func TestServiceLogRemainsBoundedAndKeepsRecentOutput(t *testing.T) {
	l := &serviceLog{name: filepath.Join(t.TempDir(), "worker.log")}
	for i := 0; i < 40; i++ {
		p := bytes.Repeat([]byte{byte('a' + i%26)}, 128*1024)
		if n, err := l.Write(p); err != nil || n != len(p) {
			t.Fatalf("%d %v", n, err)
		}
	}
	large := bytes.Repeat([]byte("z"), 2*serviceLogLimit)
	if n, err := l.Write(large); err != nil || n != len(large) {
		t.Fatalf("%d %v", n, err)
	}
	for _, name := range []string{l.name, l.name + ".old"} {
		info, err := os.Stat(name)
		if err != nil || info.Size() > serviceLogLimit {
			t.Fatalf("unbounded log: %v %v", info, err)
		}
	}
	got, err := os.ReadFile(l.name)
	if err != nil || len(got) != serviceLogLimit || !bytes.Equal(got, large[:serviceLogLimit]) {
		t.Fatal("newest output missing")
	}
}

func TestServiceLogBoundsOutputFromOlderVersions(t *testing.T) {
	l := &serviceLog{name: filepath.Join(t.TempDir(), "worker.log")}
	os.WriteFile(l.name, bytes.Repeat([]byte("old"), serviceLogLimit), 0600)
	if _, err := l.Write([]byte("new")); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{l.name, l.name + ".old"} {
		info, err := os.Stat(name)
		if err != nil || info.Size() > serviceLogLimit {
			t.Fatalf("unbounded old output: %v %v", info, err)
		}
	}
}
