package admission

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	"battlefield/gateway/internal/config"
)

func store(t *testing.T, patch map[string]any) *config.Store {
	t.Helper()
	s := config.NewStore()
	s.SetDefenses(config.Hardened(), "test")
	b, _ := json.Marshal(patch)
	if _, err := s.Merge(b); err != nil {
		t.Fatal(err)
	}
	return s
}

// Mission 06 regression: a noisy tenant's excess must not burn global tokens.
func TestTenantQuotaCheckedBeforeGlobalLimit(t *testing.T) {
	s := store(t, map[string]any{"rate_limit": 0.0001, "burst": 5, "max_inflight": 1000})
	s.SetTenant(config.Tenant{Name: "noisy", Rate: 0.0001, Burst: 1, Weight: 1, Priority: 2})
	c := New(s)
	ctx := context.Background()
	if _, err := c.Acquire(ctx, "noisy"); err != nil {
		t.Fatalf("first noisy request should pass: %v", err)
	}
	for i := 0; i < 50; i++ {
		_, err := c.Acquire(ctx, "noisy")
		var se *ShedError
		if !errors.As(err, &se) || se.Reason != "tenant_quota" {
			t.Fatalf("noisy excess should be shed by tenant quota, got %v", err)
		}
	}
	// 4 global tokens must remain for a well-behaved tenant.
	for i := 0; i < 4; i++ {
		if _, err := c.Acquire(ctx, "acme"); err != nil {
			t.Fatalf("acme request %d starved: %v", i, err)
		}
	}
}

func TestQueueFullEvictsLowerPriority(t *testing.T) {
	s := store(t, map[string]any{"rate_limit": 1e6, "burst": 1e6, "max_inflight": 1, "max_queue": 1, "queue_timeout_ms": 2000})
	c := New(s)
	ctx := context.Background()
	release, err := c.Acquire(ctx, "acme") // takes the only slot
	if err != nil {
		t.Fatal(err)
	}
	batchErr := make(chan error, 1)
	go func() {
		_, err := c.Acquire(ctx, "batch-jobs") // queues (priority 2)
		batchErr <- err
	}()
	for c.Stats().Queued == 0 {
	}
	interactive := make(chan error, 1)
	go func() {
		rel, err := c.Acquire(ctx, "acme") // priority 0: evicts the batch waiter
		if rel != nil {
			rel()
		}
		interactive <- err
	}()
	var se *ShedError
	if err := <-batchErr; !errors.As(err, &se) || se.Reason != "evicted_by_priority" {
		t.Fatalf("batch waiter should be evicted, got %v", err)
	}
	release()
	if err := <-interactive; err != nil {
		t.Fatalf("interactive request should be admitted, got %v", err)
	}
}
