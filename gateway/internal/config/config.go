// Package config holds the gateway's runtime-tunable defenses.
//
// Every defense starts disabled ("naive" preset) so each mission can show the
// system failing first, then surviving once the right weapon is switched on.
package config

import (
	"encoding/json"
	"sync"
)

type Defenses struct {
	// Attack #1: overload
	Admission      bool    `json:"admission"`        // token bucket + max in-flight + bounded queue
	RateLimit      float64 `json:"rate_limit"`       // global req/s admitted
	Burst          int     `json:"burst"`            // token bucket burst
	MaxInflight    int     `json:"max_inflight"`     // concurrent requests sent to the fleet
	MaxQueue       int     `json:"max_queue"`        // gateway queue length before shedding
	QueueTimeoutMs int     `json:"queue_timeout_ms"` // max time a request may wait in the gateway queue
	Backpressure   bool    `json:"backpressure"`     // shed when every GPU is saturated

	// Attack #2/#3: routing
	Routing         string `json:"routing"`          // round_robin | least_loaded | p2c | prefix
	KVAware         bool   `json:"kv_aware"`         // skip GPUs whose KV cache can't fit the request
	OutlierEjection bool   `json:"outlier_ejection"` // temporarily eject GPUs much slower than peers
	LongPromptPool  bool   `json:"long_prompt_pool"` // send huge prompts to a dedicated GPU subset
	LongPromptChars int    `json:"long_prompt_chars"`

	// Attack #4: failures
	HealthChecks     bool    `json:"health_checks"`   // skip GPUs that fail /state polls
	CircuitBreaker   bool    `json:"circuit_breaker"` // per-GPU breaker
	Retries          bool    `json:"retries"`         // retry on another GPU before first byte
	RetryBudget      float64 `json:"retry_budget"`    // max retries as fraction of requests
	Fallback         bool    `json:"fallback"`        // cached/degraded answer when all else fails
	AttemptTimeoutMs int     `json:"attempt_timeout_ms"`

	// Mission 03: noisy neighbours
	TenantQuotas bool `json:"tenant_quotas"` // per-tenant rate limits + weighted fair queueing
}

func Naive() Defenses {
	return Defenses{
		RateLimit: 120, Burst: 60, MaxInflight: 96, MaxQueue: 200, QueueTimeoutMs: 2000,
		Routing: "round_robin", LongPromptChars: 8000, RetryBudget: 0.2, AttemptTimeoutMs: 30000,
	}
}

func Hardened() Defenses {
	d := Naive()
	d.Admission, d.Backpressure = true, true
	d.Routing, d.KVAware, d.OutlierEjection, d.LongPromptPool = "p2c", true, true, true
	d.HealthChecks, d.CircuitBreaker, d.Retries, d.Fallback = true, true, true, true
	d.AttemptTimeoutMs = 6000
	d.RateLimit, d.Burst, d.QueueTimeoutMs = 150, 75, 1000
	d.TenantQuotas = true
	return d
}

func Preset(name string) (Defenses, bool) {
	switch name {
	case "naive":
		return Naive(), true
	case "hardened":
		return Hardened(), true
	}
	return Defenses{}, false
}

type Tenant struct {
	Name     string  `json:"name"`
	Rate     float64 `json:"rate"` // req/s
	Burst    int     `json:"burst"`
	Weight   int     `json:"weight"`   // fair-queue share
	Priority int     `json:"priority"` // 0 = interactive, 1 = standard, 2 = batch (shed first)
}

func DefaultTenants() map[string]Tenant {
	return map[string]Tenant{
		"default":    {Name: "default", Rate: 150, Burst: 75, Weight: 2, Priority: 1},
		"acme":       {Name: "acme", Rate: 60, Burst: 30, Weight: 3, Priority: 0},
		"globex":     {Name: "globex", Rate: 60, Burst: 30, Weight: 2, Priority: 1},
		"batch-jobs": {Name: "batch-jobs", Rate: 40, Burst: 20, Weight: 1, Priority: 2},
	}
}

// Store guards defenses and tenants; readers take cheap copies.
type Store struct {
	mu       sync.RWMutex
	defenses Defenses
	tenants  map[string]Tenant
	onChange []func(string)
}

func NewStore() *Store {
	return &Store{defenses: Naive(), tenants: DefaultTenants()}
}

func (s *Store) OnChange(f func(string)) { s.onChange = append(s.onChange, f) }

func (s *Store) notify(msg string) {
	for _, f := range s.onChange {
		f(msg)
	}
}

func (s *Store) Defenses() Defenses {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.defenses
}

// Merge applies a partial JSON patch on top of the current defenses.
func (s *Store) Merge(patch []byte) (Defenses, error) {
	s.mu.Lock()
	d := s.defenses
	if err := json.Unmarshal(patch, &d); err != nil {
		s.mu.Unlock()
		return d, err
	}
	s.defenses = d
	s.mu.Unlock()
	s.notify("defenses updated: " + string(patch))
	return d, nil
}

func (s *Store) SetDefenses(d Defenses, why string) {
	s.mu.Lock()
	s.defenses = d
	s.mu.Unlock()
	s.notify(why)
}

func (s *Store) Tenant(name string) Tenant {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if t, ok := s.tenants[name]; ok {
		return t
	}
	t := s.tenants["default"]
	t.Name = name
	return t
}

func (s *Store) Tenants() map[string]Tenant {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]Tenant, len(s.tenants))
	for k, v := range s.tenants {
		out[k] = v
	}
	return out
}

func (s *Store) SetTenant(t Tenant) {
	s.mu.Lock()
	s.tenants[t.Name] = t
	s.mu.Unlock()
	b, _ := json.Marshal(t)
	s.notify("tenant updated: " + string(b))
}

// Load replaces defenses and tenants wholesale (state restore at startup).
func (s *Store) Load(d Defenses, tenants map[string]Tenant) {
	s.mu.Lock()
	s.defenses = d
	if len(tenants) > 0 {
		s.tenants = tenants
	}
	s.mu.Unlock()
}
