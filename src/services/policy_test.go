package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

const testBroker = "tcp:192.168.1.2:1883"

type policyFixture struct {
	t          *testing.T
	s          *Store
	config     Config
	now        float64
	mu         sync.Mutex
	kernel     []string
	applies    int
	filterRuns int
	failApply  bool
	afterApply func()
}

func newPolicyFixture(t *testing.T) *policyFixture {
	t.Helper()
	f := &policyFixture{t: t, now: 100, config: testServiceConfig(), kernel: []string{testBroker}}
	f.s = newStore(t.TempDir())
	f.s.Uptime = func() float64 { return f.now }
	if err := atomicWrite(path(f.s.Root, "/etc/cgs2-privacy/allowlist.conf"), []byte(testBroker+"\n")); err != nil {
		t.Fatal(err)
	}
	if err := writeJSON(path(f.s.Root, "/etc/cgs2-privacy/services.json"), f.config); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(path(f.s.Root, "/run/cgs2-services"), 0700); err != nil {
		t.Fatal(err)
	}
	f.s.Filter = func(action, file string) error {
		f.mu.Lock()
		f.filterRuns++
		f.mu.Unlock()
		b, err := os.ReadFile(file)
		if err != nil {
			return err
		}
		var endpoints []string
		for _, endpoint := range strings.Fields(string(b)) {
			if !endpointValid(endpoint) {
				return errors.New("mock filter rejects invalid endpoint")
			}
			endpoints = append(endpoints, endpoint)
		}
		info, err := os.Stat(file)
		if err != nil || info.Mode().Perm() != 0600 {
			return errors.New("policy candidate must be private")
		}
		if action == "dry-run" {
			return nil
		}
		if action != "apply" {
			return errors.New("unexpected filter operation")
		}
		if pending, err := f.s.policyPending(); err != nil || !pending {
			return errors.New("kernel mutation attempted without transaction marker")
		}
		f.mu.Lock()
		defer f.mu.Unlock()
		f.applies++
		if f.failApply {
			return errors.New("injected kernel apply failure")
		}
		f.kernel = endpoints
		if f.afterApply != nil {
			f.afterApply()
		}
		return nil
	}
	return f
}

func (f *policyFixture) seed(leases []Lease) {
	f.t.Helper()
	if err := writeJSON(path(f.s.Root, "/run/cgs2-services/leases.json"), leases); err != nil {
		f.t.Fatal(err)
	}
	f.kernel = union([]string{testBroker}, leases, f.now)
}

func (f *policyFixture) policy() string {
	f.t.Helper()
	b, err := os.ReadFile(path(f.s.Root, "/etc/cgs2-privacy/allowlist.conf"))
	if err != nil {
		f.t.Fatal(err)
	}
	return string(b)
}

func (f *policyFixture) assertKernel(want ...string) {
	f.t.Helper()
	f.mu.Lock()
	got := append([]string{}, f.kernel...)
	f.mu.Unlock()
	sort.Strings(want)
	sort.Strings(got)
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		f.t.Fatalf("kernel policy = %v, want %v", got, want)
	}
}

func TestPolicyMQTTOnlyPersistsItsEndpointAndPreservesServiceLeases(t *testing.T) {
	f := newPolicyFixture(t)
	ls := []Lease{{"time", "udp:162.159.200.1:123", 130, f.config.RequestID}, {"weather", "tcp:93.184.216.34:443", 130, f.config.RequestID}, {"weather", "udp:9.9.9.9:53", 105, f.config.RequestID}}
	f.seed(ls)
	if err := f.s.mqtt(true, "192.168.1.3", 8883); err != nil {
		t.Fatal(err)
	}
	if got := f.policy(); got != "tcp:192.168.1.3:8883\n" {
		t.Fatalf("boot policy contains non-MQTT grants: %q", got)
	}
	f.assertKernel("tcp:192.168.1.3:8883", ls[0].Endpoint, ls[1].Endpoint, ls[2].Endpoint)
	if err := f.s.mqtt(false, "", 0); err != nil {
		t.Fatal(err)
	}
	if f.policy() != "" {
		t.Fatal("MQTT revocation did not clear persistent policy")
	}
	f.assertKernel(ls[0].Endpoint, ls[1].Endpoint, ls[2].Endpoint)
	got, err := f.s.leases()
	if err != nil || !reflect.DeepEqual(got, ls) {
		t.Fatalf("other service leases changed: %v", err)
	}
}

func TestPolicySharedEndpointSurvivesOnlyOneOwnerRevocation(t *testing.T) {
	f := newPolicyFixture(t)
	f.seed([]Lease{{"weather", testBroker, 130, f.config.RequestID}})
	if err := f.s.mqtt(false, "", 0); err != nil {
		t.Fatal(err)
	}
	f.assertKernel(testBroker)
	if err := f.s.revoke("weather", f.config.RequestID); err != nil {
		t.Fatal(err)
	}
	f.assertKernel()
}

func TestPolicyMQTTApplyFailureRestoresExactPersistentPreimage(t *testing.T) {
	f := newPolicyFixture(t)
	preimage := "# local approval\n" + testBroker + "\n"
	if err := atomicWrite(path(f.s.Root, "/etc/cgs2-privacy/allowlist.conf"), []byte(preimage)); err != nil {
		t.Fatal(err)
	}
	f.seed([]Lease{{"time", "udp:162.159.200.1:123", 130, f.config.RequestID}})
	f.failApply = true
	if err := f.s.mqtt(true, "192.168.1.3", 8883); err == nil {
		t.Fatal("kernel failure hidden")
	}
	if f.policy() != preimage {
		t.Fatal("persistent policy preimage was not restored")
	}
	f.assertKernel(testBroker, "udp:162.159.200.1:123")
	if pending, err := f.s.policyPending(); err != nil || !pending {
		t.Fatal("failed transaction did not retain repair marker")
	}
	f.failApply = false
	if err := f.s.expire(); err != nil {
		t.Fatal(err)
	}
	if pending, err := f.s.policyPending(); err != nil || pending {
		t.Fatal("successful repair did not clear marker")
	}
}

func TestPolicyRefusesUnknownPersistentPolicy(t *testing.T) {
	for _, policy := range []string{"invalid-permission\n", "udp:162.159.200.1:123\n", testBroker + "\ntcp:192.168.1.3:1883\n"} {
		t.Run(policy, func(t *testing.T) {
			f := newPolicyFixture(t)
			if err := atomicWrite(path(f.s.Root, "/etc/cgs2-privacy/allowlist.conf"), []byte(policy)); err != nil {
				t.Fatal(err)
			}
			if err := f.s.mqtt(false, "", 0); err == nil {
				t.Fatal("unknown policy was overwritten")
			}
			if f.policy() != policy || f.applies != 0 {
				t.Fatal("refused policy was mutated")
			}
		})
	}
}

func TestPolicyInvalidDraftNeverTouchesExistingApprovals(t *testing.T) {
	f := newPolicyFixture(t)
	f.seed([]Lease{{"time", "udp:162.159.200.1:123", 130, f.config.RequestID}})
	c := f.config
	c.WeatherURL = "http://api.open-meteo.com/"
	if err := f.s.commit(c); err == nil {
		t.Fatal("invalid config committed")
	}
	if f.applies != 0 || f.policy() != testBroker+"\n" {
		t.Fatal("validation failure changed policy")
	}
	f.assertKernel(testBroker, "udp:162.159.200.1:123")
}

func TestPolicyCommitClearsTransientGenerationButPreservesMQTT(t *testing.T) {
	f := newPolicyFixture(t)
	f.seed([]Lease{{"time", "udp:162.159.200.1:123", 130, f.config.RequestID}})
	c := f.config
	c.RequestID = "next-generation"
	c.TimeEnabled = false
	if err := f.s.commit(c); err != nil {
		t.Fatal(err)
	}
	f.assertKernel(testBroker)
	if f.policy() != testBroker+"\n" {
		t.Fatal("service save changed MQTT boot approval")
	}
	got, err := readConfig(f.s.Root)
	if err != nil || got.RequestID != c.RequestID || !got.WeatherEnabled {
		t.Fatalf("wrong saved config: %+v, %v", got, err)
	}
	ls, err := f.s.leases()
	if err != nil || len(ls) != 0 {
		t.Fatalf("old leases persisted: %v", err)
	}
}

func TestPolicyLeasePersistenceFailureRestoresKernelAndReportsRollbackFailure(t *testing.T) {
	f := newPolicyFixture(t)
	leasePath := path(f.s.Root, "/run/cgs2-services/leases.json")
	f.afterApply = func() {
		f.afterApply = nil
		if err := os.Mkdir(leasePath, 0700); err != nil {
			t.Fatal(err)
		}
		f.failApply = true
	}
	err := f.s.changeLeases(func([]Lease) ([]Lease, error) {
		return []Lease{{"time", "udp:162.159.200.1:123", 130, f.config.RequestID}}, nil
	})
	if err == nil || !strings.Contains(err.Error(), "injected kernel apply failure") {
		t.Fatalf("rollback failure was hidden: %v", err)
	}
}

func TestPolicyExpiryReconcilesUntrackedCrashWindowWithoutMetadataChange(t *testing.T) {
	f := newPolicyFixture(t)
	// Simulate termination after installing a rule and before writing its lease.
	if err := f.s.beginPolicy(); err != nil {
		t.Fatal(err)
	}
	f.kernel = []string{testBroker, "tcp:93.184.216.34:443"}
	if err := f.s.expire(); err != nil {
		t.Fatal(err)
	}
	f.assertKernel(testBroker)
	if f.applies != 1 {
		t.Fatal("unchanged metadata skipped kernel reconciliation")
	}
	if _, err := os.Stat(path(f.s.Root, "/run/cgs2-services/leases.json")); !os.IsNotExist(err) {
		t.Fatal("unchanged metadata needlessly rewritten")
	}
	if pending, err := f.s.policyPending(); err != nil || pending {
		t.Fatal("repair marker not cleared")
	}
}

func TestPolicyIdleTicksDoNotLaunchFilterOrRewriteLeaseMetadata(t *testing.T) {
	for _, liveLease := range []bool{false, true} {
		t.Run(fmt.Sprint(liveLease), func(t *testing.T) {
			f := newPolicyFixture(t)
			if liveLease {
				f.seed([]Lease{{"weather", "tcp:93.184.216.34:443", 130, f.config.RequestID}})
			}
			leasePath := path(f.s.Root, "/run/cgs2-services/leases.json")
			before, _ := os.Stat(leasePath)
			for i := 0; i < 3; i++ {
				if err := f.s.expire(); err != nil {
					t.Fatal(err)
				}
			}
			if f.filterRuns != 0 {
				t.Fatal("healthy idle ticks launched filter subprocesses")
			}
			after, _ := os.Stat(leasePath)
			if before == nil && after != nil || before != nil && (after == nil || !os.SameFile(before, after) || !before.ModTime().Equal(after.ModTime())) {
				t.Fatal("healthy idle ticks rewrote lease metadata")
			}
		})
	}
}

func TestPolicyInterruptedMQTTPersistenceIsReconciled(t *testing.T) {
	f := newPolicyFixture(t)
	// Simulate a revoked broker persisted just before the CLI was terminated.
	if err := f.s.beginPolicy(); err != nil {
		t.Fatal(err)
	}
	if err := atomicWrite(path(f.s.Root, "/etc/cgs2-privacy/allowlist.conf"), nil); err != nil {
		t.Fatal(err)
	}
	if err := f.s.expire(); err != nil {
		t.Fatal(err)
	}
	f.assertKernel()
	if pending, err := f.s.policyPending(); err != nil || pending {
		t.Fatal("interrupted MQTT update was not repaired")
	}
}

func TestPolicySuccessfulTransactionsClearRepairMarker(t *testing.T) {
	operations := map[string]func(*policyFixture) error{
		"mqtt": func(f *policyFixture) error { return f.s.mqtt(true, "192.168.1.3", 1883) },
		"leases": func(f *policyFixture) error {
			return f.s.changeLeases(func([]Lease) ([]Lease, error) {
				return []Lease{{"weather", "tcp:93.184.216.34:443", 130, f.config.RequestID}}, nil
			})
		},
		"commit": func(f *policyFixture) error { c := f.config; c.RequestID = "next"; return f.s.commit(c) },
		"reset":  func(f *policyFixture) error { return f.s.reset() },
	}
	for name, operation := range operations {
		t.Run(name, func(t *testing.T) {
			f := newPolicyFixture(t)
			if err := operation(f); err != nil {
				t.Fatal(err)
			}
			if pending, err := f.s.policyPending(); err != nil || pending {
				t.Fatalf("completed %s left a repair marker", name)
			}
		})
	}
}

func TestPolicyResetRevokesTransientPermissionsDespiteCorruptMetadata(t *testing.T) {
	f := newPolicyFixture(t)
	f.kernel = []string{testBroker, "tcp:93.184.216.34:443"}
	if err := atomicWrite(path(f.s.Root, "/run/cgs2-services/leases.json"), []byte("not JSON")); err != nil {
		t.Fatal(err)
	}
	if err := f.s.reset(); err != nil {
		t.Fatal(err)
	}
	f.assertKernel(testBroker)
	leases, err := f.s.leases()
	if err != nil || len(leases) != 0 {
		t.Fatalf("corrupt metadata not reset: %v", err)
	}
}

func TestPolicyResetRevokesKernelEvenIfLeaseMetadataCannotBeWritten(t *testing.T) {
	f := newPolicyFixture(t)
	f.kernel = []string{testBroker, "tcp:93.184.216.34:443"}
	if err := os.Mkdir(path(f.s.Root, "/run/cgs2-services/leases.json"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := f.s.reset(); err == nil {
		t.Fatal("unwritable lease metadata error was hidden")
	}
	f.assertKernel(testBroker)
}

func TestPolicyCorruptBootResetDeniesEgressWithoutOverwritingEvidence(t *testing.T) {
	f := newPolicyFixture(t)
	badPolicy := "invalid-permission\n"
	if err := atomicWrite(path(f.s.Root, "/etc/cgs2-privacy/allowlist.conf"), []byte(badPolicy)); err != nil {
		t.Fatal(err)
	}
	f.kernel = []string{testBroker, "tcp:93.184.216.34:443"}
	if err := f.s.reset(); err == nil {
		t.Fatal("corrupt persistent policy error was hidden")
	}
	f.assertKernel()
	if f.policy() != badPolicy {
		t.Fatal("corrupt policy evidence overwritten")
	}
}

func TestPolicyExpiryAndScopedRevocation(t *testing.T) {
	f := newPolicyFixture(t)
	f.seed([]Lease{{"time", "udp:162.159.200.1:123", 100, "old"}, {"time", "udp:162.159.200.123:123", 130, "new"}, {"weather", "tcp:93.184.216.34:443", 130, "old"}})
	if err := f.s.expire(); err != nil {
		t.Fatal(err)
	}
	f.assertKernel(testBroker, "udp:162.159.200.123:123", "tcp:93.184.216.34:443")
	if err := f.s.revoke("time", "old"); err != nil {
		t.Fatal(err)
	}
	f.assertKernel(testBroker, "udp:162.159.200.123:123", "tcp:93.184.216.34:443")
	f.now = 130
	if err := f.s.expire(); err != nil {
		t.Fatal(err)
	}
	f.assertKernel(testBroker)
}

func TestPolicyConcurrentMQTTAndServiceSavesPreserveBoth(t *testing.T) {
	f := newPolicyFixture(t)
	start := make(chan struct{})
	errors := make(chan error, 2)
	c := f.config
	c.RequestID = "new-config"
	c.WeatherMinutes = "45"
	go func() { <-start; errors <- f.s.commit(c) }()
	go func() { <-start; errors <- f.s.mqtt(true, "192.168.1.3", 8883) }()
	close(start)
	for n := 0; n < 2; n++ {
		if err := <-errors; err != nil {
			t.Fatal(err)
		}
	}
	if f.policy() != "tcp:192.168.1.3:8883\n" {
		t.Fatal("concurrent service save lost MQTT approval")
	}
	got, err := readConfig(f.s.Root)
	if err != nil || got.WeatherMinutes != "45" {
		t.Fatal("concurrent MQTT save lost services config")
	}
	f.assertKernel("tcp:192.168.1.3:8883")
}

func TestPolicyConcurrentLeaseAndMQTTUpdatesPreserveUnion(t *testing.T) {
	f := newPolicyFixture(t)
	start := make(chan struct{})
	errs := make(chan error, 12)
	for i := 0; i < 6; i++ {
		go func(i int) { <-start; errs <- f.s.mqtt(true, fmt.Sprintf("192.168.1.%d", 10+i), 1883) }(i)
		go func() {
			<-start
			errs <- f.s.permitFor(f.config, "weather")(context.Background(), EndpointPermit{Service: "weather", Hostname: "api.open-meteo.com", Protocol: "tcp", IPv4: "93.184.216.34", Port: 443, ExpiresAt: time.Now().Add(time.Minute)})
		}()
	}
	close(start)
	for i := 0; i < 12; i++ {
		if err := <-errs; err != nil {
			t.Fatal(err)
		}
	}
	f.assertKernel(strings.TrimSpace(f.policy()), "tcp:93.184.216.34:443")
	ls, err := f.s.leases()
	if err != nil || len(ls) != 1 {
		t.Fatalf("duplicate/lost transient lease: %v, %v", ls, err)
	}
}

func TestPolicyNumericServiceCannotAuthorizeDifferentAddress(t *testing.T) {
	for _, service := range []string{"time", "weather"} {
		t.Run(service, func(t *testing.T) {
			f := newPolicyFixture(t)
			p := EndpointPermit{Service: service, Hostname: "162.159.200.1", Protocol: "udp", IPv4: "8.8.8.8", Port: 123}
			if service == "weather" {
				f.config.WeatherURL = "https://162.159.200.1/weather"
				p.Protocol, p.Port = "tcp", 443
			}
			if err := writeJSON(path(f.s.Root, "/etc/cgs2-privacy/services.json"), f.config); err != nil {
				t.Fatal(err)
			}
			permit := f.s.permitFor(f.config, service)
			if err := permit(context.Background(), p); err == nil {
				t.Fatal("numeric host approved a different IP")
			}
			p.IPv4 = p.Hostname
			if err := permit(context.Background(), p); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestPolicyResolverOnlyForApprovedNameAndEnabledOwner(t *testing.T) {
	for _, mutation := range []string{"vendor", "other-name", "unknown-owner", "disabled-owner", "no-rotation", "unapproved-resolver"} {
		t.Run(mutation, func(t *testing.T) {
			f := newPolicyFixture(t)
			owner := "weather"
			p := EndpointPermit{Service: owner, Hostname: "api.open-meteo.com", Protocol: "udp", IPv4: "9.9.9.9", Port: 53, ExpiresAt: time.Now().Add(time.Minute)}
			switch mutation {
			case "vendor":
				p.Hostname = "api.qingping.co"
			case "other-name":
				p.Hostname = "unapproved.example"
			case "unknown-owner":
				owner, p.Service = "unknown", "unknown"
			case "disabled-owner":
				f.config.WeatherEnabled = false
			case "no-rotation":
				f.config.DNSRotation = false
			case "unapproved-resolver":
				p.IPv4 = "8.8.8.8"
			}
			if err := f.s.permitFor(f.config, owner)(context.Background(), p); err == nil {
				t.Fatal("unapproved DNS request accepted")
			}
			if f.applies != 0 {
				t.Fatal("rejected DNS request changed kernel policy")
			}
		})
	}
	f := newPolicyFixture(t)
	p := EndpointPermit{Service: "weather", Hostname: "api.open-meteo.com", Protocol: "tcp", IPv4: "9.9.9.9", Port: 53, ExpiresAt: time.Now().Add(time.Minute)}
	if err := f.s.permitFor(f.config, "weather")(context.Background(), p); err != nil {
		t.Fatal(err)
	}
}

func TestPolicyRejectsExpiredCanceledChangedAndUnsafeEndpoints(t *testing.T) {
	for _, mutation := range []string{"expired", "missing-expiry", "canceled", "changed", "reused-id", "private", "reserved", "owner"} {
		t.Run(mutation, func(t *testing.T) {
			f := newPolicyFixture(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			p := EndpointPermit{Service: "weather", Hostname: "api.open-meteo.com", Protocol: "tcp", IPv4: "93.184.216.34", Port: 443, ExpiresAt: time.Now().Add(time.Minute)}
			switch mutation {
			case "expired":
				p.ExpiresAt = time.Now().Add(-time.Second)
			case "missing-expiry":
				p.ExpiresAt = time.Time{}
			case "canceled":
				cancel()
			case "changed":
				c := f.config
				c.RequestID = "different"
				if err := writeJSON(path(f.s.Root, "/etc/cgs2-privacy/services.json"), c); err != nil {
					t.Fatal(err)
				}
			case "reused-id":
				c := f.config
				c.WeatherURL = "https://weather.example/forecast"
				if err := writeJSON(path(f.s.Root, "/etc/cgs2-privacy/services.json"), c); err != nil {
					t.Fatal(err)
				}
			case "private":
				p.IPv4 = "192.168.1.5"
			case "reserved":
				p.IPv4 = "198.18.0.1"
			case "owner":
				p.Service = "time"
			}
			if err := f.s.permitFor(f.config, "weather")(ctx, p); err == nil {
				t.Fatal("unsafe permission accepted")
			}
			if f.applies != 0 {
				t.Fatal("rejected permission changed kernel policy")
			}
		})
	}
}

func TestPolicyTTLCannotBeExtendedByLockDelay(t *testing.T) {
	f := newPolicyFixture(t)
	calls := 0
	f.s.Uptime = func() float64 {
		calls++
		if calls == 1 {
			return 100
		}
		return 110
	}
	p := EndpointPermit{Service: "weather", Hostname: "api.open-meteo.com", Protocol: "tcp", IPv4: "93.184.216.34", Port: 443, ExpiresAt: time.Now().Add(time.Second)}
	if err := f.s.permitFor(f.config, "weather")(context.Background(), p); err == nil {
		t.Fatal("expired permission extended by lock wait")
	}
	if f.applies != 0 {
		t.Fatal("expired permission applied")
	}
}

func TestPolicyCanceledWhileWaitingForLockCannotGrant(t *testing.T) {
	f := newPolicyFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	f.s.Uptime = func() float64 { cancel(); return 100 }
	p := EndpointPermit{Service: "weather", Hostname: "api.open-meteo.com", Protocol: "tcp", IPv4: "93.184.216.34", Port: 443, ExpiresAt: time.Now().Add(time.Minute)}
	if err := f.s.permitFor(f.config, "weather")(ctx, p); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation lost: %v", err)
	}
	if f.applies != 0 {
		t.Fatal("canceled request applied")
	}
}

func TestPolicyEndpointLimitFailsBeforeKernelMutation(t *testing.T) {
	f := newPolicyFixture(t)
	var endpoints []string
	for i := 1; i <= 65; i++ {
		endpoints = append(endpoints, fmt.Sprintf("tcp:1.1.1.%d:443", i))
	}
	if err := f.s.apply(endpoints); err == nil || f.applies != 0 {
		t.Fatal("too many endpoints reached kernel")
	}
}
