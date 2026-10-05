// Package backend discovers GPU workers, polls their load signals and tracks
// per-GPU health (breaker, outlier ejection, operator weights).
package backend

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// WorkerState mirrors the worker's /state endpoint.
type WorkerState struct {
	Node         string         `json:"node"`
	Pool         string         `json:"pool"`
	Version      string         `json:"version"`
	Mode         string         `json:"mode"`
	Slots        int            `json:"slots"`
	Active       int            `json:"active"`
	QueueDepth   int            `json:"queue_depth"`
	KVUsed       int            `json:"kv_used"`
	KVBudget     int            `json:"kv_budget"`
	KVUtil       float64        `json:"kv_util"`
	TTFTEWMAMs   float64        `json:"ttft_ewma_ms"`
	TokensPerSec float64        `json:"tokens_per_sec"`
	Chaos        map[string]any `json:"chaos"`
}

type Backend struct {
	Addr       string // host:port
	Pool       string
	Inflight   atomic.Int64
	KVInflight atomic.Int64 // KV tokens of requests the gateway has in flight here
	Breaker    *Breaker

	mu           sync.Mutex
	name         string
	state        WorkerState
	healthy      bool
	pollFails    int
	lastPoll     time.Time
	ttftEWMA     float64 // seconds, as observed by the gateway
	errEWMA      float64
	weight       float64
	manualEject  bool
	gone         bool // no longer a ready endpoint; kept only to drain in-flight requests
	outlierUntil time.Time
}

func (b *Backend) Name() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.name != "" {
		return b.name
	}
	return b.Addr
}

func (b *Backend) URL() string { return "http://" + b.Addr }

// Snapshot is a consistent copy of a backend's view for routing decisions.
type Snapshot struct {
	B            *Backend    `json:"-"`
	Name         string      `json:"name"`
	Addr         string      `json:"addr"`
	Pool         string      `json:"pool"`
	Healthy      bool        `json:"healthy"`
	Inflight     int64       `json:"inflight"`
	KVInflight   int64       `json:"kv_inflight"`
	State        WorkerState `json:"state"`
	TTFTEWMAMs   float64     `json:"gw_ttft_ewma_ms"`
	ErrorRate    float64     `json:"gw_error_rate"`
	Weight       float64     `json:"weight"`
	ManualEject  bool        `json:"manual_eject"`
	Gone         bool        `json:"gone"`
	OutlierEject bool        `json:"outlier_eject"`
	Breaker      string      `json:"breaker"`
	LastPollAgo  float64     `json:"last_poll_ago_s"`
}

func (b *Backend) Snapshot() Snapshot {
	br := b.Breaker.State().String()
	b.mu.Lock()
	defer b.mu.Unlock()
	name := b.name
	if name == "" {
		name = b.Addr
	}
	return Snapshot{
		B: b, Name: name, Addr: b.Addr, Pool: b.Pool, Healthy: b.healthy,
		Inflight: b.Inflight.Load(), KVInflight: b.KVInflight.Load(), State: b.state, TTFTEWMAMs: b.ttftEWMA * 1000,
		ErrorRate: b.errEWMA, Weight: b.weight, ManualEject: b.manualEject, Gone: b.gone,
		OutlierEject: time.Now().Before(b.outlierUntil), Breaker: br,
		LastPollAgo: time.Since(b.lastPoll).Seconds(),
	}
}

// Observe feeds a finished attempt into the gateway-side EWMAs. hardFail is a
// broken GPU (connection error, 500, 502), not an overloaded one. ttft is only
// recorded for short prompts so long-prompt prefill doesn't look like sickness.
func (b *Backend) Observe(ttft time.Duration, hardFail bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	e := 0.0
	if hardFail {
		e = 1
	} else if ttft > 0 {
		if b.ttftEWMA == 0 {
			b.ttftEWMA = ttft.Seconds()
		}
		b.ttftEWMA = 0.85*b.ttftEWMA + 0.15*ttft.Seconds()
	}
	b.errEWMA = 0.9*b.errEWMA + 0.1*e
}

func (b *Backend) SetWeight(w float64) {
	b.mu.Lock()
	b.weight = w
	b.mu.Unlock()
}

func (b *Backend) SetManualEject(v bool) {
	b.mu.Lock()
	b.manualEject = v
	b.mu.Unlock()
}

func (b *Backend) EjectOutlier(d time.Duration) {
	b.mu.Lock()
	b.outlierUntil = time.Now().Add(d)
	b.mu.Unlock()
}

// Registry owns all backends across pools.
type Registry struct {
	mu       sync.RWMutex
	backends map[string]*Backend // by addr
	pools    map[string]string   // pool -> "host:port" DNS target or comma list
	client   *http.Client
	events   func(string)
	slices   *sliceClient       // nil outside a cluster
	weights  map[string]float64 // operator weights by node name, survive rediscovery
	ejects   map[string]bool    // manual ejections by node name
}

func NewRegistry(pools map[string]string, events func(string)) *Registry {
	return &Registry{
		backends: map[string]*Backend{},
		pools:    pools,
		client:   &http.Client{Timeout: 400 * time.Millisecond},
		events:   events,
		weights:  map[string]float64{},
		ejects:   map[string]bool{},
		slices:   newSliceClient(),
	}
}

// ParsePools parses "stable=gpu-headless:9000,canary=gpu-canary-headless:9000"
// or "stable=127.0.0.1:9001|127.0.0.1:9002".
func ParsePools(s string) map[string]string {
	out := map[string]string{}
	for _, part := range strings.Split(s, ",") {
		kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(kv) == 2 {
			out[kv[0]] = kv[1]
		}
	}
	return out
}

func (r *Registry) Run(ctx context.Context) {
	disc := time.NewTicker(time.Second)
	poll := time.NewTicker(500 * time.Millisecond)
	r.discover()
	for {
		select {
		case <-ctx.Done():
			return
		case <-disc.C:
			r.discover()
		case <-poll.C:
			r.pollAll()
		}
	}
}

func (r *Registry) discover() {
	seen := map[string]string{}
	for pool, target := range r.pools {
		for _, t := range strings.Split(target, "|") {
			host, port, err := net.SplitHostPort(t)
			if err != nil {
				continue
			}
			if ip := net.ParseIP(host); ip != nil {
				seen[t] = pool
				continue
			}
			if r.slices != nil {
				svc := strings.SplitN(host, ".", 2)[0]
				if addrs, err := r.slices.ready(svc, port); err == nil {
					for _, a := range addrs {
						seen[a] = pool
					}
					continue
				}
				// API unavailable: fall through to DNS
			}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			ips, err := net.DefaultResolver.LookupHost(ctx, host)
			cancel()
			if err != nil {
				continue // keep existing backends on transient DNS failures
			}
			for _, ip := range ips {
				seen[net.JoinHostPort(ip, port)] = pool
			}
		}
	}
	if len(seen) == 0 && r.slices == nil {
		return // DNS hiccup: keep what we have
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	for addr, pool := range seen {
		if _, ok := r.backends[addr]; !ok {
			b := &Backend{Addr: addr, Pool: pool, weight: 1, healthy: true}
			b.Breaker = NewBreaker(func(from, to BreakerState) {
				r.events(fmt.Sprintf("breaker %s: %s -> %s", b.Name(), from, to))
			})
			r.backends[addr] = b
			r.events(fmt.Sprintf("discovered %s backend %s", pool, addr))
		}
	}
	for addr, b := range r.backends {
		if _, ok := seen[addr]; !ok && b.Inflight.Load() > 0 {
			b.mu.Lock()
			if !b.gone {
				b.gone = true // still draining in-flight requests, but never route here again
				go r.events(fmt.Sprintf("draining backend %s (%s) - no longer a ready endpoint", b.name, addr))
			}
			b.mu.Unlock()
			continue
		}
		if _, ok := seen[addr]; !ok {
			delete(r.backends, addr)
			r.events(fmt.Sprintf("removed backend %s (%s) - no longer a ready endpoint", b.Name(), addr))
		}
	}
}

func (r *Registry) pollAll() {
	for _, b := range r.All() {
		go r.poll(b)
	}
}

func (r *Registry) poll(b *Backend) {
	resp, err := r.client.Get(b.URL() + "/state")
	var st WorkerState
	if err == nil {
		err = json.NewDecoder(resp.Body).Decode(&st)
		resp.Body.Close()
	}
	r.mu.RLock()
	w, hasW := r.weights[st.Node]
	ej := r.ejects[st.Node]
	r.mu.RUnlock()
	b.mu.Lock()
	defer b.mu.Unlock()
	if err != nil {
		b.pollFails++
		if b.pollFails == 2 && b.healthy {
			b.healthy = false
			go r.events(fmt.Sprintf("health: %s unreachable (%v)", b.name, err))
		}
		return
	}
	if !b.healthy {
		go r.events(fmt.Sprintf("health: %s healthy again", st.Node))
	}
	b.pollFails, b.healthy, b.state, b.lastPoll = 0, true, st, time.Now()
	if b.Inflight.Load() == 0 && b.ttftEWMA > 0 {
		// No traffic (e.g. ejected): let our view converge to what the GPU reports.
		b.ttftEWMA = 0.8*b.ttftEWMA + 0.2*st.TTFTEWMAMs/1000
	}
	if b.name == "" && st.Node != "" {
		b.name = st.Node
		if hasW {
			b.weight = w
		}
		b.manualEject = ej
	}
}

func (r *Registry) All() []*Backend {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*Backend, 0, len(r.backends))
	for _, b := range r.backends {
		out = append(out, b)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Addr < out[j].Addr })
	return out
}

func (r *Registry) Snapshots() []Snapshot {
	all := r.All()
	out := make([]Snapshot, len(all))
	for i, b := range all {
		out[i] = b.Snapshot()
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func (r *Registry) ByName(name string) *Backend {
	for _, b := range r.All() {
		if b.Name() == name || b.Addr == name {
			return b
		}
	}
	return nil
}

// SetWeight remembers the weight by node name so it survives pod IP changes.
func (r *Registry) SetWeight(name string, w float64) bool {
	b := r.ByName(name)
	if b == nil {
		return false
	}
	b.SetWeight(w)
	r.mu.Lock()
	r.weights[b.Name()] = w
	r.mu.Unlock()
	return true
}

// SetEject remembers a manual ejection by node name so it survives pod restarts.
func (r *Registry) SetEject(name string, v bool) bool {
	b := r.ByName(name)
	if b == nil {
		return false
	}
	b.SetManualEject(v)
	r.mu.Lock()
	if v {
		r.ejects[b.Name()] = true
	} else {
		delete(r.ejects, b.Name())
	}
	r.mu.Unlock()
	return true
}

// Overrides returns operator weights and ejections (for persistence).
func (r *Registry) Overrides() (map[string]float64, map[string]bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	w, e := map[string]float64{}, map[string]bool{}
	for k, v := range r.weights {
		w[k] = v
	}
	for k, v := range r.ejects {
		e[k] = v
	}
	return w, e
}

// LoadOverrides restores persisted operator state before discovery runs.
func (r *Registry) LoadOverrides(w map[string]float64, e map[string]bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for k, v := range w {
		r.weights[k] = v
	}
	for k, v := range e {
		r.ejects[k] = v
	}
}
