// Rollout controller: model registry + eval gate + canary with health gates.
//
//	candidate --eval gate--> 5% --> 25% --> 50% --> 100% (promoted)
//	                  \          any gate fails: canary weight -> 0 (rolled back)
//
// Health gates read Prometheus every few seconds during each step:
//
//	canary p99 TTFT  <= 1.3x stable
//	canary errors    <= 2%
//	canary $/token   <= 1.25x stable
package main

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

type Eval struct {
	Accuracy  float64   `json:"accuracy"`
	P50Ms     float64   `json:"p50_ms"`
	P99Ms     float64   `json:"p99_ms"`
	CostPer1K float64   `json:"cost_per_1k_tokens"`
	N         int       `json:"n"`
	TS        time.Time `json:"ts"`
	Passed    *bool     `json:"passed,omitempty"`
	Reasons   []string  `json:"reasons,omitempty"`
}

type Model struct {
	Version string `json:"version"`
	Pool    string `json:"pool"`
	Status  string `json:"status"` // serving | candidate | promoted | rejected | rolled_back
	Eval    *Eval  `json:"eval,omitempty"`
}

type Gates struct {
	MinAccuracy     float64 `json:"min_accuracy"`
	MaxEvalP99Ratio float64 `json:"max_eval_p99_ratio"`
	MaxTTFTRatio    float64 `json:"max_ttft_ratio"`
	MaxErrorRatio   float64 `json:"max_error_ratio"`
	MaxCostRatio    float64 `json:"max_cost_ratio"`
	MinRequests     float64 `json:"min_requests"`
}

type Check struct {
	TS     time.Time          `json:"ts"`
	Step   int                `json:"step"`
	Weight float64            `json:"weight"`
	Values map[string]float64 `json:"values"`
	Failed []string           `json:"failed,omitempty"`
}

type Rollout struct {
	ID          string    `json:"id"`
	Candidate   string    `json:"candidate"`
	Baseline    string    `json:"baseline"`
	Steps       []float64 `json:"steps"`
	StepSeconds int       `json:"step_seconds"`
	SkipEval    bool      `json:"skip_eval"`
	Gates       Gates     `json:"gates"`
	Status      string    `json:"status"` // running | promoted | rolled_back | rejected | aborted
	Step        int       `json:"step"`
	Weight      float64   `json:"weight"`
	Reason      string    `json:"reason,omitempty"`
	Started     time.Time `json:"started"`
	Finished    time.Time `json:"finished,omitempty"`
	Checks      []Check   `json:"checks"`
	abort       chan struct{}
}

type Event struct {
	TS  time.Time `json:"ts"`
	Msg string    `json:"msg"`
}

var (
	mWeight   = promauto.NewGauge(prometheus.GaugeOpts{Name: "rollout_canary_weight", Help: "Weight set by the controller"})
	mStep     = promauto.NewGauge(prometheus.GaugeOpts{Name: "rollout_step", Help: "Current rollout step index"})
	mRunning  = promauto.NewGauge(prometheus.GaugeOpts{Name: "rollout_running", Help: "1 while a rollout is in progress"})
	mOutcomes = promauto.NewCounterVec(prometheus.CounterOpts{Name: "rollout_outcomes_total", Help: "Finished rollouts"}, []string{"status"})
	mEvalAcc  = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "model_eval_accuracy", Help: "Offline eval accuracy"}, []string{"version"})
	mEvalP99  = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "model_eval_p99_seconds", Help: "Offline eval p99 latency"}, []string{"version"})
	mEvalCost = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "model_eval_cost_per_1k", Help: "Offline eval cost per 1k tokens"}, []string{"version"})
)

type Controller struct {
	mu       sync.Mutex
	models   map[string]*Model
	current  *Rollout
	history  []*Rollout
	events   []Event
	gateway  string
	prom     string
	client   *http.Client
	k8s      *http.Client
	k8sToken string
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	gin.SetMode(gin.ReleaseMode)
	c := &Controller{
		models: map[string]*Model{
			"v1": {Version: "v1", Pool: "stable", Status: "serving"},
			"v2": {Version: "v2", Pool: "canary", Status: "candidate"},
		},
		gateway: env("GATEWAY_URL", "http://gateway.battlefield:8080"),
		prom:    env("PROMETHEUS_URL", "http://prometheus.battlefield:9090"),
		client:  &http.Client{Timeout: 5 * time.Second},
	}
	c.initK8s()
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/healthz", func(ctx *gin.Context) { ctx.String(200, "ok") })
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))
	r.GET("/registry", c.registry)
	r.POST("/registry/:version/eval", c.postEval)
	r.POST("/rollouts", c.start)
	r.GET("/rollouts", c.list)
	r.GET("/rollouts/current", c.cur)
	r.POST("/rollouts/current/abort", c.abort)
	r.GET("/events", func(ctx *gin.Context) {
		c.mu.Lock()
		defer c.mu.Unlock()
		ctx.JSON(200, c.events)
	})
	fleet := NewFleet(c.event)
	fleet.Routes(r)
	if path := os.Getenv("STATE_FILE"); path != "" {
		c.restore(path, fleet)
		go c.persistLoop(path, fleet)
	}
	addr := env("ADDR", ":8082")
	log.Printf("controller on %s gateway=%s prom=%s", addr, c.gateway, c.prom)
	log.Fatal(http.ListenAndServe(addr, r))
}

func (c *Controller) event(format string, a ...any) {
	msg := fmt.Sprintf(format, a...)
	log.Print(msg)
	c.mu.Lock()
	c.events = append(c.events, Event{time.Now(), msg})
	if len(c.events) > 300 {
		c.events = c.events[len(c.events)-300:]
	}
	c.mu.Unlock()
}

func defaultGates() Gates {
	return Gates{MinAccuracy: 0.9, MaxEvalP99Ratio: 1.3, MaxTTFTRatio: 1.3, MaxErrorRatio: 0.02, MaxCostRatio: 1.25, MinRequests: 20}
}

// ---------------------------------------------------------------- registry + eval gate

func (c *Controller) registry(ctx *gin.Context) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ctx.JSON(200, c.models)
}

func (c *Controller) postEval(ctx *gin.Context) {
	var e Eval
	if err := ctx.BindJSON(&e); err != nil {
		return
	}
	v := ctx.Param("version")
	e.TS = time.Now()
	c.mu.Lock()
	m, ok := c.models[v]
	if !ok {
		m = &Model{Version: v, Pool: "canary", Status: "candidate"}
		c.models[v] = m
	}
	m.Eval = &e
	c.mu.Unlock()
	mEvalAcc.WithLabelValues(v).Set(e.Accuracy)
	mEvalP99.WithLabelValues(v).Set(e.P99Ms / 1000)
	mEvalCost.WithLabelValues(v).Set(e.CostPer1K)
	c.event("eval registered for %s: accuracy=%.3f p99=%.0fms cost/1k=%.4f n=%d", v, e.Accuracy, e.P99Ms, e.CostPer1K, e.N)
	ctx.JSON(200, m)
}

// evalGate compares the candidate's offline eval with the baseline's.
func (c *Controller) evalGate(cand, base string, g Gates) (bool, []string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	cm, bm := c.models[cand], c.models[base]
	if cm == nil || cm.Eval == nil {
		return false, []string{"no eval for " + cand + " (run: make eval)"}
	}
	var reasons []string
	ce := cm.Eval
	if ce.Accuracy < g.MinAccuracy {
		reasons = append(reasons, fmt.Sprintf("accuracy %.3f < %.2f", ce.Accuracy, g.MinAccuracy))
	}
	if bm != nil && bm.Eval != nil {
		be := bm.Eval
		if be.Accuracy-ce.Accuracy > 0.03 {
			reasons = append(reasons, fmt.Sprintf("accuracy dropped %.3f -> %.3f vs %s", be.Accuracy, ce.Accuracy, base))
		}
		if be.P99Ms > 0 && ce.P99Ms/be.P99Ms > g.MaxEvalP99Ratio {
			reasons = append(reasons, fmt.Sprintf("p99 %.0fms is %.2fx %s", ce.P99Ms, ce.P99Ms/be.P99Ms, base))
		}
		if be.CostPer1K > 0 && ce.CostPer1K/be.CostPer1K > g.MaxCostRatio {
			reasons = append(reasons, fmt.Sprintf("cost/1k %.4f is %.2fx %s", ce.CostPer1K, ce.CostPer1K/be.CostPer1K, base))
		}
	}
	passed := len(reasons) == 0
	ce.Passed, ce.Reasons = &passed, reasons
	return passed, reasons
}

// ---------------------------------------------------------------- rollouts

func (c *Controller) start(ctx *gin.Context) {
	ro := &Rollout{Baseline: "v1", Steps: []float64{0.05, 0.25, 0.5, 1.0}, StepSeconds: 30, Gates: defaultGates()}
	raw, _ := io.ReadAll(ctx.Request.Body)
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, ro); err != nil {
			ctx.JSON(400, gin.H{"error": err.Error()})
			return
		}
	}
	if ro.Candidate == "" {
		ro.Candidate = "v2"
	}
	c.mu.Lock()
	if c.current != nil && c.current.Status == "running" {
		c.mu.Unlock()
		ctx.JSON(409, gin.H{"error": "rollout already running", "rollout": c.current})
		return
	}
	ro.ID = strconv.FormatInt(time.Now().Unix(), 36)
	ro.Started = time.Now()
	ro.Status = "running"
	ro.abort = make(chan struct{})
	c.current = ro
	c.history = append(c.history, ro)
	c.mu.Unlock()
	c.event("rollout %s started: %s (baseline %s) steps=%v hold=%ds skip_eval=%v", ro.ID, ro.Candidate, ro.Baseline, ro.Steps, ro.StepSeconds, ro.SkipEval)
	go c.run(ro)
	ctx.JSON(202, ro)
}

func (c *Controller) finish(ro *Rollout, status, reason string) {
	c.setWeight(0)
	if status == "promoted" {
		c.setWeight(1)
	} else {
		go c.scaleCanary(0) // shrink back to a single canary pod
	}
	c.mu.Lock()
	ro.Status, ro.Reason, ro.Finished = status, reason, time.Now()
	if m := c.models[ro.Candidate]; m != nil {
		m.Status = status
	}
	c.mu.Unlock()
	mRunning.Set(0)
	mOutcomes.WithLabelValues(status).Inc()
	c.event("rollout %s %s: %s", ro.ID, status, reason)
}

func (c *Controller) run(ro *Rollout) {
	mRunning.Set(1)
	if !ro.SkipEval {
		ok, reasons := c.evalGate(ro.Candidate, ro.Baseline, ro.Gates)
		if !ok {
			c.finish(ro, "rejected", fmt.Sprintf("eval gate: %v", reasons))
			return
		}
		c.event("rollout %s: eval gate passed", ro.ID)
	}
	for i, w := range ro.Steps {
		c.mu.Lock()
		ro.Step, ro.Weight = i, w
		c.mu.Unlock()
		mStep.Set(float64(i))
		c.scaleCanary(w)
		if err := c.setWeight(w); err != nil {
			c.finish(ro, "aborted", "gateway unreachable: "+err.Error())
			return
		}
		c.event("rollout %s: step %d -> canary %.0f%%", ro.ID, i, w*100)
		if w >= 1 {
			break
		}
		deadline := time.Now().Add(time.Duration(ro.StepSeconds) * time.Second)
		healthyChecks := 0
		for time.Now().Before(deadline) || healthyChecks == 0 {
			select {
			case <-ro.abort:
				c.finish(ro, "aborted", "aborted by operator")
				return
			case <-time.After(5 * time.Second):
			}
			chk := c.check(ro, i, w)
			c.mu.Lock()
			ro.Checks = append(ro.Checks, chk)
			c.mu.Unlock()
			if len(chk.Failed) > 0 {
				c.finish(ro, "rolled_back", fmt.Sprintf("health gate failed at %.0f%%: %v", w*100, chk.Failed))
				return
			}
			if chk.Values["canary_requests"] >= ro.Gates.MinRequests {
				healthyChecks++
			}
			if time.Since(ro.Started) > 15*time.Minute {
				c.finish(ro, "aborted", "not enough canary traffic to judge")
				return
			}
		}
	}
	c.finish(ro, "promoted", "all health gates passed")
}

// check evaluates the live health gates for the canary pool.
func (c *Controller) check(ro *Rollout, step int, w float64) Check {
	q := func(expr string) float64 {
		v, err := c.query(expr)
		if err != nil {
			log.Printf("prom: %v", err)
			return 0
		}
		return v
	}
	// Window starts at the rollout so a previous candidate's samples can't leak in.
	win := fmt.Sprintf("%ds", int(math.Max(15, math.Min(60, time.Since(ro.Started).Seconds()))))
	cand, base := ro.Candidate, ro.Baseline
	ttft := func(v string) string {
		return `histogram_quantile(0.99, sum by (le) (rate(worker_ttft_seconds_bucket{version="` + v + `"}[` + win + `])))`
	}
	costTok := func(v string) string {
		sel := `{version="` + v + `"}[` + win + `]`
		return `sum(rate(worker_cost_dollars_total` + sel + `)) / clamp_min(sum(rate(worker_output_tokens_total` + sel + `)) + sum(rate(worker_prompt_tokens_total` + sel + `)), 0.001)`
	}
	vals := map[string]float64{
		"canary_requests":  q(`sum(increase(worker_requests_total{version="` + cand + `"}[` + win + `]))`),
		"canary_ttft_p99":  q(ttft(cand)),
		"stable_ttft_p99":  q(ttft(base)),
		"canary_err_ratio": q(`sum(rate(gw_requests_total{pool="canary",outcome="error"}[` + win + `])) / clamp_min(sum(rate(gw_requests_total{pool="canary"}[` + win + `])), 0.001)`),
		"canary_cost_tok":  q(costTok(cand)),
		"stable_cost_tok":  q(costTok(base)),
	}
	chk := Check{TS: time.Now(), Step: step, Weight: w, Values: vals}
	if !(vals["canary_requests"] >= ro.Gates.MinRequests) {
		return chk // not enough data to judge yet
	}
	g := ro.Gates
	if vals["stable_ttft_p99"] > 0 && vals["canary_ttft_p99"]/vals["stable_ttft_p99"] > g.MaxTTFTRatio {
		r := vals["canary_ttft_p99"] / vals["stable_ttft_p99"]
		chk.Failed = append(chk.Failed, fmt.Sprintf("p99 TTFT %.2fx stable (max %.2f)", r, g.MaxTTFTRatio))
	}
	if e := vals["canary_err_ratio"]; e > g.MaxErrorRatio {
		chk.Failed = append(chk.Failed, fmt.Sprintf("error ratio %.1f%% (max %.1f%%)", e*100, g.MaxErrorRatio*100))
	}
	if vals["stable_cost_tok"] > 0 && vals["canary_cost_tok"]/vals["stable_cost_tok"] > g.MaxCostRatio {
		r := vals["canary_cost_tok"] / vals["stable_cost_tok"]
		chk.Failed = append(chk.Failed, fmt.Sprintf("cost/token %.2fx stable (max %.2f)", r, g.MaxCostRatio))
	}
	return chk
}

func (c *Controller) query(expr string) (float64, error) {
	resp, err := c.client.Get(c.prom + "/api/v1/query?query=" + url.QueryEscape(expr))
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	var out struct {
		Data struct {
			Result []struct {
				Value [2]any `json:"value"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return 0, err
	}
	if len(out.Data.Result) == 0 {
		return 0, nil
	}
	s, _ := out.Data.Result[0].Value[1].(string)
	v, err := strconv.ParseFloat(s, 64)
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return 0, nil // no samples (e.g. histogram_quantile on empty data); JSON can't carry NaN
	}
	return v, err
}

func (c *Controller) setWeight(w float64) error {
	mWeight.Set(w)
	body, _ := json.Marshal(map[string]float64{"weight": w})
	resp, err := c.client.Post(c.gateway+"/admin/canary", "application/json", bytes.NewReader(body))
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}

func (c *Controller) list(ctx *gin.Context) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ctx.JSON(200, c.history)
}

func (c *Controller) cur(ctx *gin.Context) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.current == nil {
		ctx.JSON(404, gin.H{"error": "no rollout yet"})
		return
	}
	ctx.JSON(200, c.current)
}

func (c *Controller) abort(ctx *gin.Context) {
	c.mu.Lock()
	ro := c.current
	c.mu.Unlock()
	if ro == nil || ro.Status != "running" {
		ctx.JSON(409, gin.H{"error": "nothing running"})
		return
	}
	close(ro.abort)
	ctx.JSON(200, gin.H{"aborting": ro.ID})
}

// ---------------------------------------------------------------- canary capacity

const saDir = "/var/run/secrets/kubernetes.io/serviceaccount/"

func (c *Controller) initK8s() {
	tok, err := os.ReadFile(saDir + "token")
	if err != nil {
		log.Printf("k8s: not in cluster, canary autoscaling disabled")
		return
	}
	ca, _ := os.ReadFile(saDir + "ca.crt")
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca)
	c.k8sToken = string(tok)
	c.k8s = &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}}
}

func (c *Controller) k8sDo(method, path string, body any) (map[string]any, error) {
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, "https://kubernetes.default.svc"+path, rd)
	req.Header.Set("Authorization", "Bearer "+c.k8sToken)
	req.Header.Set("Content-Type", "application/merge-patch+json")
	resp, err := c.k8s.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if resp.StatusCode >= 300 {
		return out, fmt.Errorf("k8s %s %s: %d", method, path, resp.StatusCode)
	}
	return out, nil
}

// scaleCanary sizes the canary pool to its traffic share of the stable pool
// (at least 1 pod) and waits until the pods are ready.
func (c *Controller) scaleCanary(weight float64) {
	if c.k8s == nil {
		return
	}
	const ns = "/apis/apps/v1/namespaces/battlefield/"
	st, err := c.k8sDo("GET", ns+"statefulsets/gpu/scale", nil)
	if err != nil {
		c.event("canary scale: %v", err)
		return
	}
	stable := 3.0
	if spec, ok := st["spec"].(map[string]any); ok {
		if r, ok := spec["replicas"].(float64); ok {
			stable = r
		}
	}
	want := int(math.Max(1, math.Ceil(weight*stable*1.2)))
	if _, err := c.k8sDo("PATCH", ns+"deployments/gpu-canary/scale", map[string]any{"spec": map[string]any{"replicas": want}}); err != nil {
		c.event("canary scale: %v", err)
		return
	}
	for i := 0; i < 60; i++ {
		d, err := c.k8sDo("GET", ns+"deployments/gpu-canary", nil)
		if err == nil {
			status, _ := d["status"].(map[string]any)
			if ready, _ := status["readyReplicas"].(float64); int(ready) >= want {
				c.event("canary pool scaled to %d replicas for %.0f%% traffic", want, weight*100)
				time.Sleep(3 * time.Second) // let the gateway discover the new pods
				return
			}
		}
		time.Sleep(2 * time.Second)
	}
	c.event("canary scale: timed out waiting for %d ready replicas", want)
}
