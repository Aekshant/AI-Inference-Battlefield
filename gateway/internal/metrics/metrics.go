package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	Requests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_requests_total", Help: "Requests by final outcome",
	}, []string{"outcome", "code", "pool", "tenant"})

	Shed = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_shed_total", Help: "Requests shed by the gateway",
	}, []string{"reason", "tenant"})

	Latency = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "gw_request_seconds", Help: "End-to-end latency of successful requests",
		Buckets: []float64{.1, .25, .5, .75, 1, 1.5, 2, 3, 5, 8, 13, 20, 30},
	}, []string{"pool", "tenant"})

	TTFT = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name: "gw_ttft_seconds", Help: "Time to first token seen by the gateway",
		Buckets: []float64{.05, .1, .2, .3, .5, .75, 1, 1.5, 2, 3, 5, 8, 13},
	}, []string{"pool", "backend"})

	BackendRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_backend_requests_total", Help: "Attempts sent to each GPU",
	}, []string{"backend", "pool", "result"})

	Retries = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_retries_total", Help: "Retries by reason",
	}, []string{"reason"})

	Fallbacks = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_fallbacks_total", Help: "Fallback responses",
	}, []string{"kind"})

	Inflight = promauto.NewGauge(prometheus.GaugeOpts{Name: "gw_inflight", Help: "Admitted requests in flight"})
	Queued   = promauto.NewGauge(prometheus.GaugeOpts{Name: "gw_queue_depth", Help: "Requests waiting in the gateway queue"})

	BackendHealthy = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "gw_backend_healthy", Help: "1 if /state polls succeed"}, []string{"backend", "pool"})
	BackendBreaker = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "gw_backend_breaker_state", Help: "0 closed, 1 open, 2 half-open"}, []string{"backend", "pool"})
	BackendWeight  = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "gw_backend_weight", Help: "Operator routing weight"}, []string{"backend", "pool"})
	BackendEjected = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "gw_backend_ejected", Help: "1 if manually or automatically ejected"}, []string{"backend", "pool"})
	BackendInfl    = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "gw_backend_inflight", Help: "Gateway in-flight per GPU"}, []string{"backend", "pool"})
	BackendTTFT    = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "gw_backend_ttft_ewma_seconds", Help: "Gateway-observed TTFT EWMA"}, []string{"backend", "pool"})
	CanaryWeight   = promauto.NewGauge(prometheus.GaugeOpts{Name: "gw_canary_weight", Help: "Fraction of traffic sent to the canary pool"})
	Defense        = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "gw_defense_enabled", Help: "1 if a defense is enabled"}, []string{"defense"})
)
