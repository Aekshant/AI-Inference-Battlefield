// Package stats keeps a 5-minute, per-second rolling window of gateway outcomes
// so the console can draw live charts without querying Prometheus.
package stats

import (
	"sort"
	"sync"
	"time"
)

const window = 300

type second struct {
	ts        int64
	ok, shed  int
	errors    int
	fallback  int
	latencies []float64 // ms, ok requests only
	ttfts     []float64 // ms
	byPool    map[string]int
	byNode    map[string]int
}

type Point struct {
	T        int64          `json:"t"`
	RPS      int            `json:"rps"`
	OK       int            `json:"ok"`
	Shed     int            `json:"shed"`
	Errors   int            `json:"errors"`
	Fallback int            `json:"fallback"`
	P50Ms    float64        `json:"p50_ms"`
	P99Ms    float64        `json:"p99_ms"`
	TTFTP99  float64        `json:"ttft_p99_ms"`
	ByPool   map[string]int `json:"by_pool"`
	ByNode   map[string]int `json:"by_node"`
}

type Recorder struct {
	mu   sync.Mutex
	ring [window]second
}

func New() *Recorder { return &Recorder{} }

func (r *Recorder) slot(now time.Time) *second {
	ts := now.Unix()
	s := &r.ring[ts%window]
	if s.ts != ts {
		*s = second{ts: ts, byPool: map[string]int{}, byNode: map[string]int{}}
	}
	return s
}

// Record one finished request. outcome: ok | shed | error | fallback.
func (r *Recorder) Record(outcome, pool, node string, latency, ttft time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	s := r.slot(time.Now())
	switch outcome {
	case "ok":
		s.ok++
		if len(s.latencies) < 4000 {
			s.latencies = append(s.latencies, float64(latency.Microseconds())/1000)
			if ttft > 0 {
				s.ttfts = append(s.ttfts, float64(ttft.Microseconds())/1000)
			}
		}
	case "shed":
		s.shed++
	case "fallback":
		s.fallback++
	default:
		s.errors++
	}
	if pool != "" {
		s.byPool[pool]++
	}
	if node != "" {
		s.byNode[node]++
	}
}

func pct(v []float64, p float64) float64 {
	if len(v) == 0 {
		return 0
	}
	c := append([]float64(nil), v...)
	sort.Float64s(c)
	return c[min(len(c)-1, int(float64(len(c))*p))]
}

// Series returns the last n complete seconds, oldest first.
func (r *Recorder) Series(n int) []Point {
	r.mu.Lock()
	defer r.mu.Unlock()
	if n > window-1 {
		n = window - 1
	}
	now := time.Now().Unix()
	out := make([]Point, 0, n)
	for ts := now - int64(n); ts < now; ts++ {
		s := &r.ring[ts%window]
		p := Point{T: ts}
		if s.ts == ts {
			p.OK, p.Shed, p.Errors, p.Fallback = s.ok, s.shed, s.errors, s.fallback
			p.RPS = s.ok + s.shed + s.errors + s.fallback
			p.P50Ms, p.P99Ms = pct(s.latencies, .5), pct(s.latencies, .99)
			p.TTFTP99 = pct(s.ttfts, .99)
			p.ByPool, p.ByNode = s.byPool, s.byNode
		}
		out = append(out, p)
	}
	return out
}
