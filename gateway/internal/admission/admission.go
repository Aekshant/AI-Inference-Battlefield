// Package admission decides whether a request may enter the fleet now, wait in
// a bounded queue, or be shed immediately.
//
//	tenant token bucket  -> 429 tenant_quota      (when tenant quotas are on)
//	global token bucket  -> 429 rate_limited
//	in-flight < max      -> admitted
//	queue full           -> evict a lower-priority waiter, else 503 queue_full
//	waited too long      -> 503 queue_timeout
//
// When a slot frees, the next waiter is chosen by priority class first, then by
// deficit round robin across tenants weighted by their share.
package admission

import (
	"context"
	"errors"
	"sort"
	"sync"
	"time"

	"battlefield/gateway/internal/config"
)

type ShedError struct {
	Code   int
	Reason string
}

func (e *ShedError) Error() string { return e.Reason }

var errEvicted = errors.New("evicted")

type bucket struct {
	tokens float64
	last   time.Time
}

func (b *bucket) take(rate float64, burst int, now time.Time) bool {
	if b.last.IsZero() {
		b.tokens = float64(burst)
	} else {
		b.tokens += now.Sub(b.last).Seconds() * rate
	}
	b.last = now
	if b.tokens > float64(burst) {
		b.tokens = float64(burst)
	}
	if b.tokens >= 1 {
		b.tokens--
		return true
	}
	return false
}

type waiter struct {
	tenant   string
	priority int
	ready    chan error // nil = admitted
	enq      time.Time
}

type Controller struct {
	cfg *config.Store

	mu       sync.Mutex
	global   bucket
	tenants  map[string]*bucket
	inflight int
	queues   map[string][]*waiter // by tenant
	deficit  map[string]int
	rrOrder  []string
	queued   int
}

func New(cfg *config.Store) *Controller {
	return &Controller{cfg: cfg, tenants: map[string]*bucket{}, queues: map[string][]*waiter{}, deficit: map[string]int{}}
}

type Stats struct {
	Inflight int            `json:"inflight"`
	Queued   int            `json:"queued"`
	ByTenant map[string]int `json:"queued_by_tenant"`
}

func (c *Controller) Stats() Stats {
	c.mu.Lock()
	defer c.mu.Unlock()
	s := Stats{Inflight: c.inflight, Queued: c.queued, ByTenant: map[string]int{}}
	for t, q := range c.queues {
		if len(q) > 0 {
			s.ByTenant[t] = len(q)
		}
	}
	return s
}

// Acquire blocks until the request is admitted or shed. The returned release
// func must be called exactly once when the request finishes.
func (c *Controller) Acquire(ctx context.Context, tenantName string) (func(), error) {
	d := c.cfg.Defenses()
	t := c.cfg.Tenant(tenantName)
	now := time.Now()

	c.mu.Lock()
	if !d.Admission {
		c.inflight++
		c.mu.Unlock()
		return c.release, nil
	}
	key := "all"
	if d.TenantQuotas {
		key = t.Name
		tb := c.tenants[t.Name]
		if tb == nil {
			tb = &bucket{}
			c.tenants[t.Name] = tb
		}
		if !tb.take(t.Rate, t.Burst, now) {
			c.mu.Unlock()
			return nil, &ShedError{429, "tenant_quota"}
		}
	}
	// Global limit comes after tenant quotas so a noisy tenant's excess can't
	// burn the shared tokens everyone else needs.
	if !c.global.take(d.RateLimit, d.Burst, now) {
		c.mu.Unlock()
		return nil, &ShedError{429, "rate_limited"}
	}
	if c.inflight < d.MaxInflight && c.queued == 0 {
		c.inflight++
		c.mu.Unlock()
		return c.release, nil
	}
	if c.queued >= d.MaxQueue && !c.evictLowerPriority(t.Priority, d) {
		c.mu.Unlock()
		return nil, &ShedError{503, "queue_full"}
	}
	w := &waiter{tenant: key, priority: t.Priority, ready: make(chan error, 1), enq: now}
	if !d.TenantQuotas {
		w.priority = 1
	}
	if _, ok := c.queues[key]; !ok {
		c.rrOrder = append(c.rrOrder, key)
	}
	c.queues[key] = append(c.queues[key], w)
	c.queued++
	c.dispatch()
	c.mu.Unlock()

	timer := time.NewTimer(time.Duration(d.QueueTimeoutMs) * time.Millisecond)
	defer timer.Stop()
	select {
	case err := <-w.ready:
		if err != nil {
			return nil, &ShedError{503, "evicted_by_priority"}
		}
		return c.release, nil
	case <-timer.C:
		if c.remove(w) {
			return nil, &ShedError{503, "queue_timeout"}
		}
	case <-ctx.Done():
		if c.remove(w) {
			return nil, ctx.Err()
		}
	}
	// Lost the race: we were admitted (or evicted) while timing out.
	if err := <-w.ready; err != nil {
		return nil, &ShedError{503, "evicted_by_priority"}
	}
	return c.release, nil
}

// evictLowerPriority drops the newest waiter with a strictly lower priority
// (higher number) than the incoming request. Caller holds c.mu.
func (c *Controller) evictLowerPriority(prio int, d config.Defenses) bool {
	if !d.TenantQuotas {
		return false
	}
	var victimKey string
	victimIdx, victimPrio := -1, prio
	for k, q := range c.queues {
		for i := len(q) - 1; i >= 0; i-- {
			if q[i].priority > victimPrio {
				victimKey, victimIdx, victimPrio = k, i, q[i].priority
			}
		}
	}
	if victimIdx < 0 {
		return false
	}
	q := c.queues[victimKey]
	w := q[victimIdx]
	c.queues[victimKey] = append(q[:victimIdx:victimIdx], q[victimIdx+1:]...)
	c.queued--
	w.ready <- errEvicted
	return true
}

func (c *Controller) remove(w *waiter) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	q := c.queues[w.tenant]
	for i, x := range q {
		if x == w {
			c.queues[w.tenant] = append(q[:i:i], q[i+1:]...)
			c.queued--
			return true
		}
	}
	return false
}

func (c *Controller) release() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.inflight--
	c.dispatch()
}

// Pump admits waiters if capacity grew (e.g. max_inflight raised at runtime).
func (c *Controller) Pump() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.dispatch()
}

// dispatch admits waiters while capacity allows. Caller holds c.mu.
func (c *Controller) dispatch() {
	d := c.cfg.Defenses()
	limit := d.MaxInflight
	if !d.Admission {
		limit = 1 << 30
	}
	for c.inflight < limit && c.queued > 0 {
		w := c.next()
		if w == nil {
			return
		}
		c.inflight++
		w.ready <- nil
	}
}

// next pops the next waiter: best priority class, then DRR by tenant weight.
func (c *Controller) next() *waiter {
	best := 1 << 30
	for _, q := range c.queues {
		if len(q) > 0 && q[0].priority < best {
			best = q[0].priority
		}
	}
	if best == 1<<30 {
		return nil
	}
	keys := append([]string(nil), c.rrOrder...)
	sort.Strings(keys)
	for round := 0; round < 64; round++ {
		for _, k := range keys {
			q := c.queues[k]
			if len(q) == 0 || q[0].priority != best {
				continue
			}
			if c.deficit[k] <= 0 {
				c.deficit[k] += max(1, c.cfg.Tenant(k).Weight)
				continue
			}
			c.deficit[k]--
			w := q[0]
			c.queues[k] = q[1:]
			c.queued--
			return w
		}
	}
	return nil
}
