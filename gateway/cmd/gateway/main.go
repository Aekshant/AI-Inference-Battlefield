// AI Gateway: the only thing standing between users and the GPU fleet.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"battlefield/gateway/internal/admission"
	"battlefield/gateway/internal/backend"
	"battlefield/gateway/internal/config"
	"battlefield/gateway/internal/metrics"
	"battlefield/gateway/internal/router"
	"battlefield/gateway/internal/stats"
)

type Event struct {
	TS  time.Time `json:"ts"`
	Msg string    `json:"msg"`
}

type Gateway struct {
	cfg    *config.Store
	reg    *backend.Registry
	adm    *admission.Controller
	rt     *router.Router
	rec    *stats.Recorder
	client *http.Client

	stateFile string

	mu        sync.Mutex
	events    []Event
	canary    float64
	cache     map[string][]byte
	cacheKeys []string
	budget    retryBudget
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	gin.SetMode(gin.ReleaseMode)
	g := &Gateway{
		cfg:   config.NewStore(),
		rt:    &router.Router{},
		rec:   stats.New(),
		cache: map[string][]byte{},
		client: &http.Client{Transport: &http.Transport{
			MaxIdleConns: 4096, MaxIdleConnsPerHost: 1024, IdleConnTimeout: 60 * time.Second,
			DialContext: (&net.Dialer{Timeout: 2 * time.Second}).DialContext,
		}},
	}
	g.stateFile = os.Getenv("STATE_FILE")
	g.adm = admission.New(g.cfg)
	pools := backend.ParsePools(env("POOLS", "stable=gpu-headless:9000,canary=gpu-canary-headless:9000"))
	g.reg = backend.NewRegistry(pools, g.event)
	if !g.restore() {
		if p := os.Getenv("DEFENSE_PRESET"); p != "" {
			if d, ok := config.Preset(p); ok {
				g.cfg.SetDefenses(d, "startup preset "+p)
			}
		}
	}
	g.cfg.OnChange(g.event)
	g.cfg.OnChange(func(string) { g.save() })
	ctx := context.Background()
	go g.reg.Run(ctx)
	go g.outlierLoop()
	go g.metricsLoop()

	r := gin.New()
	r.Use(gin.Recovery())
	r.POST("/v1/generate", g.generate)
	r.GET("/healthz", func(c *gin.Context) { c.String(200, "ok") })
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	a := r.Group("/admin")
	a.GET("/state", g.state)
	a.GET("/stats", g.statsHandler)
	a.GET("/events", g.eventsHandler)
	a.GET("/defenses", func(c *gin.Context) { c.JSON(200, g.cfg.Defenses()) })
	a.POST("/defenses", g.setDefenses)
	a.POST("/defenses/preset/:name", g.preset)
	a.GET("/tenants", func(c *gin.Context) { c.JSON(200, g.cfg.Tenants()) })
	a.POST("/tenants", g.setTenant)
	a.POST("/backends/:name/weight", g.setWeight)
	a.POST("/backends/:name/eject", g.eject(true))
	a.POST("/backends/:name/restore", g.eject(false))
	a.GET("/canary", func(c *gin.Context) { c.JSON(200, gin.H{"weight": g.canaryWeight()}) })
	a.POST("/canary", g.setCanary)
	a.POST("/chaos/:name", g.chaos(""))
	a.POST("/chaos/:name/reset", g.chaos("/reset"))
	a.POST("/chaos/:name/crash", g.chaos("/crash"))

	addr := env("ADDR", ":8080")
	log.Printf("gateway listening on %s pools=%v", addr, pools)
	srv := &http.Server{Addr: addr, Handler: r, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}

func (g *Gateway) event(msg string) {
	log.Printf("event: %s", msg)
	g.mu.Lock()
	g.events = append(g.events, Event{time.Now(), msg})
	if len(g.events) > 500 {
		g.events = g.events[len(g.events)-500:]
	}
	g.mu.Unlock()
}

func (g *Gateway) canaryWeight() float64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.canary
}

// ---------------------------------------------------------------- inference

type genMeta struct {
	Prompt    string `json:"prompt"`
	MaxTokens int    `json:"max_tokens"`
	Stream    bool   `json:"stream"`
	Tenant    string `json:"tenant"`
	Session   string `json:"session"`
}

type outcome struct {
	status   int
	ttft     time.Duration
	wrote    bool // bytes already sent to the client: can't retry
	node     string
	err      error
	cacheVal []byte
}

func (g *Gateway) generate(c *gin.Context) {
	start := time.Now()
	raw, err := io.ReadAll(io.LimitReader(c.Request.Body, 4<<20))
	if err != nil {
		c.JSON(400, gin.H{"error": "bad body"})
		return
	}
	var m genMeta
	if err := json.Unmarshal(raw, &m); err != nil || m.Prompt == "" {
		c.JSON(400, gin.H{"error": "need JSON with prompt"})
		return
	}
	if m.MaxTokens == 0 {
		m.MaxTokens = 32
	}
	tenant := firstNonEmpty(c.GetHeader("X-Tenant"), m.Tenant, "default")
	d := g.cfg.Defenses()

	pool := "stable"
	switch v := strings.ToLower(c.GetHeader("X-Model-Version")); v {
	case "canary", "stable":
		pool = v
	case "":
		if w := g.canaryWeight(); w > 0 && rand.Float64() < w {
			pool = "canary"
		}
	default:
		// Pin to whichever pool currently serves that model version.
		pool = ""
		for _, s := range g.reg.Snapshots() {
			if strings.EqualFold(s.State.Version, v) {
				pool = s.Pool
				break
			}
		}
		if pool == "" {
			c.JSON(404, gin.H{"error": "no pool serves model version " + v})
			return
		}
	}

	finish := func(kind string, code int, node string, ttft time.Duration) {
		metrics.Requests.WithLabelValues(kind, strconv.Itoa(code), pool, tenant).Inc()
		g.rec.Record(kind, pool, node, time.Since(start), ttft)
		if kind == "ok" {
			metrics.Latency.WithLabelValues(pool, tenant).Observe(time.Since(start).Seconds())
		}
	}
	shed := func(code int, reason string) {
		metrics.Shed.WithLabelValues(reason, tenant).Inc()
		c.Header("Retry-After", "1")
		c.JSON(code, gin.H{"error": "shed", "reason": reason})
		finish("shed", code, "", 0)
	}

	release, err := g.adm.Acquire(c.Request.Context(), tenant)
	if err != nil {
		var se *admission.ShedError
		if errors.As(err, &se) {
			shed(se.Code, se.Reason)
		} else {
			finish("error", 499, "", 0)
		}
		return
	}
	defer release()

	g.budget.request()
	promptChars := len(m.Prompt)
	req := router.Request{
		Pool: pool, Prompt: m.Prompt, KVTokens: promptChars/4 + m.MaxTokens,
		HashKey: m.Session, Exclude: map[string]bool{},
		LongPrompt: promptChars >= d.LongPromptChars,
	}
	maxAttempts := 1
	if d.Retries {
		maxAttempts = 3
	}
	var last outcome
	attempts := 0
	for attempts < maxAttempts {
		b, err := g.rt.Pick(g.reg.Snapshots(), req, d)
		if errors.Is(err, router.ErrSaturated) {
			shed(503, "backpressure")
			return
		}
		if errors.Is(err, router.ErrKVFull) {
			shed(503, "kv_full")
			return
		}
		if err != nil {
			if pool == "canary" && d.Fallback {
				pool, req.Pool = "stable", "stable" // canary pool empty: fall back to stable
				metrics.Fallbacks.WithLabelValues("canary_to_stable").Inc()
				continue
			}
			last = outcome{status: 503, err: err}
			break
		}
		if d.CircuitBreaker && !b.Breaker.Allow() {
			req.Exclude[b.Addr] = true
			continue
		}
		attempts++
		b.KVInflight.Add(int64(req.KVTokens))
		last = g.attempt(c, b, raw, m.Stream, d, attempts)
		b.KVInflight.Add(-int64(req.KVTokens))
		failed := last.err != nil
		// Overload (503 capacity, 504 attempt timeout) is a load signal: retry
		// elsewhere, but don't trip breakers or eject - that cascades the load.
		hard := failed && (last.status == 0 || last.status == 500 || last.status == 502)
		if d.CircuitBreaker {
			switch {
			case errors.Is(last.err, context.Canceled) && c.Request.Context().Err() != nil:
				b.Breaker.Cancel()
			case hard:
				b.Breaker.Failure()
			default:
				b.Breaker.Success()
			}
		}
		obsTTFT := last.ttft
		if req.LongPrompt || promptChars > 2000 {
			obsTTFT = 0
		}
		b.Observe(obsTTFT, hard)
		res := "ok"
		if failed {
			res = "fail"
		}
		metrics.BackendRequests.WithLabelValues(b.Name(), b.Pool, res).Inc()
		if !failed {
			metrics.TTFT.WithLabelValues(pool, b.Name()).Observe(last.ttft.Seconds())
			finish("ok", 200, b.Name(), last.ttft)
			if last.cacheVal != nil {
				g.cachePut(m.Prompt, last.cacheVal)
			}
			return
		}
		if last.wrote || c.Request.Context().Err() != nil || last.status == 413 || last.status == 400 {
			break
		}
		req.Exclude[b.Addr] = true
		if attempts < maxAttempts {
			if !g.budget.allowRetry(d.RetryBudget) {
				metrics.Retries.WithLabelValues("budget_exhausted").Inc()
				break
			}
			metrics.Retries.WithLabelValues(fmt.Sprintf("status_%d", last.status)).Inc()
		}
	}

	if last.wrote {
		finish("error", 502, last.node, 0) // stream broke mid-flight
		return
	}
	if d.Fallback && !m.Stream {
		if v := g.cacheGet(m.Prompt); v != nil {
			metrics.Fallbacks.WithLabelValues("cache").Inc()
			c.Header("X-Fallback", "cache")
			c.Data(200, "application/json", v)
			finish("fallback", 200, "", 0)
			return
		}
	}
	code := last.status
	if code == 0 || code == 200 {
		code = 502
	}
	if d.Fallback {
		metrics.Fallbacks.WithLabelValues("degraded").Inc()
		c.Header("X-Fallback", "degraded")
		c.JSON(200, gin.H{"text": "The assistant is under heavy load. Please retry shortly.", "fallback": "degraded"})
		finish("fallback", 200, "", 0)
		return
	}
	msg := "upstream failed"
	if last.err != nil {
		msg = last.err.Error()
	}
	c.JSON(code, gin.H{"error": msg, "attempts": attempts})
	finish("error", code, last.node, 0)
}

// attempt sends one request to one GPU. For streams, the attempt timeout only
// covers time-to-first-token; after that the stream runs until done.
func (g *Gateway) attempt(c *gin.Context, b *backend.Backend, raw []byte, stream bool, d config.Defenses, n int) outcome {
	b.Inflight.Add(1)
	defer b.Inflight.Add(-1)
	node := b.Name()
	attemptTimeout := time.Duration(d.AttemptTimeoutMs) * time.Millisecond
	ctx, cancel := context.WithTimeout(c.Request.Context(), 120*time.Second)
	defer cancel()
	ttftTimer := time.AfterFunc(attemptTimeout, cancel)
	defer ttftTimer.Stop()

	t0 := time.Now()
	hreq, _ := http.NewRequestWithContext(ctx, "POST", b.URL()+"/v1/generate", bytes.NewReader(withQueueTimeout(raw, attemptTimeout)))
	hreq.Header.Set("Content-Type", "application/json")
	resp, err := g.client.Do(hreq)
	if err != nil {
		if ctx.Err() != nil && c.Request.Context().Err() == nil {
			err = fmt.Errorf("attempt timeout after %s on %s", attemptTimeout, node)
			return outcome{status: 504, err: err, node: node}
		}
		return outcome{err: err, node: node}
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return outcome{status: resp.StatusCode, err: fmt.Errorf("%s: %d %s", node, resp.StatusCode, bytes.TrimSpace(body)), node: node}
	}
	headers := func() {
		c.Header("X-Node", node)
		c.Header("X-Pool", b.Pool)
		c.Header("X-Attempts", strconv.Itoa(n))
	}

	if !stream {
		body, err := io.ReadAll(resp.Body)
		if err != nil {
			if ctx.Err() != nil && c.Request.Context().Err() == nil {
				return outcome{status: 504, err: fmt.Errorf("attempt timeout after %s on %s", attemptTimeout, node), node: node}
			}
			return outcome{err: err, node: node}
		}
		var meta struct {
			TTFTMs float64 `json:"ttft_ms"`
		}
		_ = json.Unmarshal(body, &meta)
		headers()
		c.Data(200, "application/json", body)
		return outcome{status: 200, ttft: time.Duration(meta.TTFTMs * float64(time.Millisecond)), node: node, cacheVal: body}
	}

	// Streaming: wait for the worker's first-token marker before committing.
	rd := bufio.NewReader(resp.Body)
	var ttft time.Duration
	wrote := false
	for {
		line, err := rd.ReadBytes('\n')
		if len(line) > 0 {
			if ttft == 0 && (bytes.HasPrefix(line, []byte(": first-token")) || bytes.HasPrefix(line, []byte("data:"))) {
				ttft = time.Since(t0)
				ttftTimer.Stop()
			}
			if !wrote {
				headers()
				c.Header("Content-Type", "text/event-stream")
				c.Header("Cache-Control", "no-cache")
				c.Status(200)
				wrote = true
			}
			if _, werr := c.Writer.Write(line); werr != nil {
				return outcome{wrote: true, err: werr, ttft: ttft, node: node}
			}
			c.Writer.Flush()
		}
		if err == io.EOF {
			return outcome{status: 200, ttft: ttft, wrote: true, node: node}
		}
		if err != nil {
			if ttft == 0 && ctx.Err() != nil && c.Request.Context().Err() == nil {
				return outcome{status: 504, wrote: wrote, err: fmt.Errorf("no first token within %s on %s", attemptTimeout, node), node: node}
			}
			return outcome{wrote: wrote, err: err, ttft: ttft, node: node}
		}
	}
}

func withQueueTimeout(raw []byte, t time.Duration) []byte {
	var m map[string]any
	if json.Unmarshal(raw, &m) != nil {
		return raw
	}
	m["queue_timeout_s"] = t.Seconds()
	out, _ := json.Marshal(m)
	return out
}

func firstNonEmpty(v ...string) string {
	for _, s := range v {
		if s != "" {
			return s
		}
	}
	return ""
}

// ---------------------------------------------------------------- fallback cache

func key(p string) string {
	h := sha1.Sum([]byte(p))
	return hex.EncodeToString(h[:])
}

func (g *Gateway) cachePut(prompt string, v []byte) {
	k := key(prompt)
	g.mu.Lock()
	defer g.mu.Unlock()
	if _, ok := g.cache[k]; !ok {
		g.cacheKeys = append(g.cacheKeys, k)
		if len(g.cacheKeys) > 2000 {
			delete(g.cache, g.cacheKeys[0])
			g.cacheKeys = g.cacheKeys[1:]
		}
	}
	g.cache[k] = v
}

func (g *Gateway) cacheGet(prompt string) []byte {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.cache[key(prompt)]
}

// ---------------------------------------------------------------- retry budget

// retryBudget allows retries only while retries/requests over the last 10s
// stays under the configured fraction, so retries can't amplify an outage.
type retryBudget struct {
	mu      sync.Mutex
	reqs    [10]int
	retries [10]int
	stamps  [10]int64
}

func (r *retryBudget) slot() int {
	now := time.Now().Unix()
	i := int(now % 10)
	if r.stamps[i] != now {
		r.stamps[i], r.reqs[i], r.retries[i] = now, 0, 0
	}
	return i
}

func (r *retryBudget) request() {
	r.mu.Lock()
	r.reqs[r.slot()]++
	r.mu.Unlock()
}

func (r *retryBudget) allowRetry(frac float64) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	i := r.slot()
	now := time.Now().Unix()
	var reqs, retries int
	for k := range r.stamps {
		if now-r.stamps[k] < 10 {
			reqs += r.reqs[k]
			retries += r.retries[k]
		}
	}
	if float64(retries+1) > frac*float64(reqs)+3 {
		return false
	}
	r.retries[i]++
	return true
}

// ---------------------------------------------------------------- background loops

func effectiveTTFT(s backend.Snapshot) float64 {
	if s.TTFTEWMAMs > s.State.TTFTEWMAMs {
		return s.TTFTEWMAMs
	}
	return s.State.TTFTEWMAMs
}

// outlierLoop ejects a GPU for 15s when its TTFT is >3x the pool median (and
// >800ms) or its error rate is high, as long as another GPU remains.
func (g *Gateway) outlierLoop() {
	for range time.Tick(time.Second) {
		if !g.cfg.Defenses().OutlierEjection {
			continue
		}
		byPool := map[string][]backend.Snapshot{}
		for _, s := range g.reg.Snapshots() {
			if s.Healthy && !s.ManualEject {
				byPool[s.Pool] = append(byPool[s.Pool], s)
			}
		}
		for _, ss := range byPool {
			if len(ss) < 2 {
				continue
			}
			ts := make([]float64, len(ss))
			for i, s := range ss {
				ts[i] = effectiveTTFT(s)
			}
			sort.Float64s(ts)
			median := ts[len(ts)/2]
			if len(ts)%2 == 0 {
				median = ts[len(ts)/2-1] // lower median: robust when 1 of 2 is slow
			}
			active := 0
			for _, s := range ss {
				if !s.OutlierEject {
					active++
				}
			}
			for _, s := range ss {
				if s.OutlierEject {
					continue
				}
				t := effectiveTTFT(s)
				slow := t > 3*median && t > 800
				erroring := s.ErrorRate > 0.5
				if (slow || erroring) && active > 1 {
					s.B.EjectOutlier(15 * time.Second)
					active--
					g.event(fmt.Sprintf("outlier ejection: %s ttft=%.0fms (pool median %.0fms) err=%.2f -> ejected 15s", s.Name, t, median, s.ErrorRate))
				}
			}
		}
	}
}

func (g *Gateway) metricsLoop() {
	defenses := map[string]func(config.Defenses) bool{
		"admission":        func(d config.Defenses) bool { return d.Admission },
		"backpressure":     func(d config.Defenses) bool { return d.Backpressure },
		"kv_aware":         func(d config.Defenses) bool { return d.KVAware },
		"outlier_ejection": func(d config.Defenses) bool { return d.OutlierEjection },
		"health_checks":    func(d config.Defenses) bool { return d.HealthChecks },
		"circuit_breaker":  func(d config.Defenses) bool { return d.CircuitBreaker },
		"retries":          func(d config.Defenses) bool { return d.Retries },
		"fallback":         func(d config.Defenses) bool { return d.Fallback },
		"tenant_quotas":    func(d config.Defenses) bool { return d.TenantQuotas },
		"long_prompt_pool": func(d config.Defenses) bool { return d.LongPromptPool },
	}
	b2f := func(v bool) float64 {
		if v {
			return 1
		}
		return 0
	}
	for range time.Tick(time.Second) {
		d := g.cfg.Defenses()
		for name, f := range defenses {
			metrics.Defense.WithLabelValues(name).Set(b2f(f(d)))
		}
		st := g.adm.Stats()
		metrics.Inflight.Set(float64(st.Inflight))
		metrics.Queued.Set(float64(st.Queued))
		metrics.CanaryWeight.Set(g.canaryWeight())
		// Reset per-backend gauges so pods that are gone stop reporting (and alerting).
		for _, v := range []*prometheus.GaugeVec{metrics.BackendHealthy, metrics.BackendBreaker, metrics.BackendWeight,
			metrics.BackendEjected, metrics.BackendInfl, metrics.BackendTTFT} {
			v.Reset()
		}
		for _, s := range g.reg.Snapshots() {
			l := []string{s.Name, s.Pool}
			metrics.BackendHealthy.WithLabelValues(l...).Set(b2f(s.Healthy))
			metrics.BackendBreaker.WithLabelValues(l...).Set(map[string]float64{"closed": 0, "open": 1, "half_open": 2}[s.Breaker])
			metrics.BackendWeight.WithLabelValues(l...).Set(s.Weight)
			metrics.BackendEjected.WithLabelValues(l...).Set(b2f(s.ManualEject || s.OutlierEject))
			metrics.BackendInfl.WithLabelValues(l...).Set(float64(s.Inflight))
			metrics.BackendTTFT.WithLabelValues(l...).Set(effectiveTTFT(s) / 1000)
		}
	}
}

// ---------------------------------------------------------------- admin API

func (g *Gateway) state(c *gin.Context) {
	snaps := g.reg.Snapshots()
	d := g.cfg.Defenses()
	type bs struct {
		backend.Snapshot
		Score float64 `json:"score"`
	}
	out := make([]bs, len(snaps))
	for i, s := range snaps {
		sc := router.Score(s, d)
		if sc > 1e9 {
			sc = -1
		}
		out[i] = bs{s, sc}
	}
	c.JSON(200, gin.H{
		"defenses": d, "backends": out, "canary_weight": g.canaryWeight(),
		"admission": g.adm.Stats(), "tenants": g.cfg.Tenants(),
	})
}

func (g *Gateway) statsHandler(c *gin.Context) {
	n, _ := strconv.Atoi(c.DefaultQuery("seconds", "120"))
	c.JSON(200, g.rec.Series(n))
}

func (g *Gateway) eventsHandler(c *gin.Context) {
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "100"))
	g.mu.Lock()
	ev := g.events
	if len(ev) > limit {
		ev = ev[len(ev)-limit:]
	}
	out := append([]Event(nil), ev...)
	g.mu.Unlock()
	c.JSON(200, out)
}

func (g *Gateway) setDefenses(c *gin.Context) {
	raw, _ := io.ReadAll(c.Request.Body)
	d, err := g.cfg.Merge(raw)
	if err != nil {
		c.JSON(400, gin.H{"error": err.Error()})
		return
	}
	g.adm.Pump()
	c.JSON(200, d)
}

func (g *Gateway) preset(c *gin.Context) {
	d, ok := config.Preset(c.Param("name"))
	if !ok {
		c.JSON(404, gin.H{"error": "presets: naive, hardened"})
		return
	}
	g.cfg.SetDefenses(d, "defense preset: "+c.Param("name"))
	g.adm.Pump()
	c.JSON(200, d)
}

func (g *Gateway) setTenant(c *gin.Context) {
	var t config.Tenant
	if err := c.BindJSON(&t); err != nil || t.Name == "" {
		c.JSON(400, gin.H{"error": "need tenant with name"})
		return
	}
	g.cfg.SetTenant(t)
	c.JSON(200, t)
}

func (g *Gateway) setWeight(c *gin.Context) {
	var body struct {
		Weight float64 `json:"weight"`
	}
	if err := c.BindJSON(&body); err != nil || body.Weight < 0 || body.Weight > 1 {
		c.JSON(400, gin.H{"error": "weight must be 0..1"})
		return
	}
	if !g.reg.SetWeight(c.Param("name"), body.Weight) {
		c.JSON(404, gin.H{"error": "unknown backend"})
		return
	}
	g.event(fmt.Sprintf("weight: %s -> %.2f", c.Param("name"), body.Weight))
	g.save()
	c.JSON(200, gin.H{"backend": c.Param("name"), "weight": body.Weight})
}

func (g *Gateway) eject(v bool) gin.HandlerFunc {
	return func(c *gin.Context) {
		b := g.reg.ByName(c.Param("name"))
		if b == nil {
			c.JSON(404, gin.H{"error": "unknown backend"})
			return
		}
		g.reg.SetEject(b.Name(), v)
		if !v {
			b.EjectOutlier(0)
		}
		g.event(fmt.Sprintf("manual eject=%v: %s", v, b.Name()))
		g.save()
		c.JSON(200, gin.H{"backend": b.Name(), "ejected": v})
	}
}

func (g *Gateway) setCanary(c *gin.Context) {
	var body struct {
		Weight float64 `json:"weight"`
	}
	if err := c.BindJSON(&body); err != nil || body.Weight < 0 || body.Weight > 1 {
		c.JSON(400, gin.H{"error": "weight must be 0..1"})
		return
	}
	g.mu.Lock()
	g.canary = body.Weight
	g.mu.Unlock()
	g.save()
	g.event(fmt.Sprintf("canary weight -> %.0f%%", body.Weight*100))
	c.JSON(200, gin.H{"weight": body.Weight})
}

// chaos forwards a fault-injection call to one GPU (by name) or "all".
func (g *Gateway) chaos(suffix string) gin.HandlerFunc {
	return func(c *gin.Context) {
		raw, _ := io.ReadAll(c.Request.Body)
		if len(raw) == 0 {
			raw = []byte("{}")
		}
		name := c.Param("name")
		var targets []*backend.Backend
		if name == "all" {
			targets = g.reg.All()
		} else if b := g.reg.ByName(name); b != nil {
			targets = []*backend.Backend{b}
		}
		if len(targets) == 0 {
			c.JSON(404, gin.H{"error": "unknown backend"})
			return
		}
		results := gin.H{}
		for _, b := range targets {
			resp, err := g.client.Post(b.URL()+"/chaos"+suffix, "application/json", bytes.NewReader(raw))
			if err != nil {
				results[b.Name()] = err.Error()
				continue
			}
			var v any
			_ = json.NewDecoder(resp.Body).Decode(&v)
			resp.Body.Close()
			results[b.Name()] = v
		}
		g.event(fmt.Sprintf("chaos%s on %s: %s", suffix, name, raw))
		c.JSON(200, results)
	}
}
