package router

import (
	"testing"

	"battlefield/gateway/internal/backend"
	"battlefield/gateway/internal/config"
)

func snaps(n int) []backend.Snapshot {
	var out []backend.Snapshot
	for i := 0; i < n; i++ {
		name := string(rune('a' + i))
		out = append(out, backend.Snapshot{B: &backend.Backend{Addr: name}, Name: name, Addr: name, Pool: "stable",
			Healthy: true, Weight: 1, Breaker: "closed",
			State: backend.WorkerState{Slots: 32, KVBudget: 1000}})
	}
	return out
}

func TestPrefixRoutingIsSticky(t *testing.T) {
	r := &Router{}
	d := config.Hardened()
	d.Routing = "prefix"
	all := snaps(3)
	req := Request{Pool: "stable", Prompt: "You are assistant #3. Policy: be brief", Exclude: map[string]bool{}}
	first, _ := r.Pick(all, req, d)
	for i := 0; i < 20; i++ {
		if b, _ := r.Pick(all, req, d); b != first {
			t.Fatal("same prefix should land on the same GPU while load is balanced")
		}
	}
}

// Mission 04 regression: a request that fits nowhere is shed as kv_full.
func TestKVAwareShedsWhenNothingFits(t *testing.T) {
	d := config.Hardened()
	all := snaps(3)
	for i := range all {
		all[i].KVInflight = 900
	}
	_, err := (&Router{}).Pick(all, Request{Pool: "stable", KVTokens: 200, Exclude: map[string]bool{}}, d)
	if err != ErrKVFull {
		t.Fatalf("want ErrKVFull, got %v", err)
	}
}

// Mission 02 regression: the long-prompt GPU is fixed by name, not by eligibility.
func TestLongPromptReservationIsStable(t *testing.T) {
	d := config.Hardened()
	all := snaps(3)
	all[2].OutlierEject = true // "c" is ejected: must NOT move the reservation to "b"
	elig := Eligible(all, Request{Pool: "stable", Exclude: map[string]bool{}}, d)
	names := map[string]bool{}
	for _, s := range elig {
		names[s.Name] = true
	}
	if !names["a"] || !names["b"] {
		t.Fatalf("short traffic should use both remaining GPUs, got %v", names)
	}
}

// Mission 05 regression: a pod that left the endpoint list gets no traffic even
// with every defense off (it may still be draining in-flight requests).
func TestGoneBackendNeverRouted(t *testing.T) {
	d := config.Naive()
	all := snaps(3)
	all[1].Gone = true
	r := &Router{}
	for i := 0; i < 30; i++ {
		b, err := r.Pick(all, Request{Pool: "stable", Exclude: map[string]bool{}}, d)
		if err != nil || b.Addr == "b" {
			t.Fatalf("gone backend picked (err=%v)", err)
		}
	}
}
