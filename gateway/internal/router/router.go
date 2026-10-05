// Package router picks a GPU for each request.
//
//	round_robin   blind rotation (what dies in Attack #2)
//	least_loaded  lowest load score
//	p2c           power of two choices on the load score
//	prefix        consistent hashing on the prompt prefix (KV/prefix-cache reuse)
//	              with bounded load so a hot key can't melt one GPU
package router

import (
	"errors"
	"hash/fnv"
	"math"
	"math/rand/v2"
	"sort"
	"strconv"
	"sync/atomic"

	"battlefield/gateway/internal/backend"
	"battlefield/gateway/internal/config"
)

var ErrNoBackend = errors.New("no eligible GPU")
var ErrSaturated = errors.New("all GPUs saturated")
var ErrKVFull = errors.New("no GPU has KV cache room for this request")

type Request struct {
	Pool       string
	Prompt     string
	KVTokens   int    // estimated prompt+output tokens
	HashKey    string // session/tenant affinity key; defaults to prompt prefix
	Exclude    map[string]bool
	LongPrompt bool
}

type Router struct {
	rr atomic.Uint64
}

// Score: lower is better. ~1.0 means "fully busy".
func Score(s backend.Snapshot, d config.Defenses) float64 {
	slots := float64(max(1, s.State.Slots))
	load := (float64(s.Inflight) + float64(s.State.QueueDepth)) / slots
	score := load
	if d.KVAware {
		score += 0.5 * s.State.KVUtil
	}
	// Latency term: observed TTFT relative to a 300ms target.
	ttft := math.Max(s.TTFTEWMAMs, s.State.TTFTEWMAMs)
	score += ttft / 300 * 0.25
	score += s.ErrorRate * 2
	w := s.Weight
	if w <= 0 {
		return math.Inf(1)
	}
	return score / w
}

// Eligible filters candidates according to the enabled defenses.
func Eligible(all []backend.Snapshot, req Request, d config.Defenses) []backend.Snapshot {
	var out, kvFit []backend.Snapshot
	for _, s := range all {
		// Membership, not a defense: a backend gone from the endpoint list is never eligible.
		if s.Pool != req.Pool || req.Exclude[s.Addr] || s.ManualEject || s.Gone || s.Weight <= 0 {
			continue
		}
		if d.HealthChecks && !s.Healthy {
			continue
		}
		if d.OutlierEjection && s.OutlierEject {
			continue
		}
		if d.CircuitBreaker && s.Breaker == "open" {
			continue
		}
		out = append(out, s)
		if KVFits(s, req.KVTokens) {
			kvFit = append(kvFit, s)
		}
	}
	if d.LongPromptPool {
		out = longPromptSplit(all, out, req, d)
	}
	if d.KVAware {
		fit := map[string]bool{}
		for _, s := range kvFit {
			fit[s.Addr] = true
		}
		var o []backend.Snapshot
		for _, s := range out {
			if fit[s.Addr] {
				o = append(o, s)
			}
		}
		return o
	}
	return out
}

// KVFits checks the request against the larger of the GPU's reported KV usage
// and the gateway's own in-flight KV accounting (polls are 500ms stale).
func KVFits(s backend.Snapshot, kv int) bool {
	if s.State.KVBudget == 0 {
		return true
	}
	used := max(int64(s.State.KVUsed), s.KVInflight)
	return used+int64(kv) <= int64(s.State.KVBudget)
}

// longPromptSplit reserves one fixed GPU per pool (the last by name among
// healthy pool members) for huge prompts, so a flood of long prompts can't
// evict everyone's KV cache. With fewer than 3 GPUs there is no reservation.
func longPromptSplit(all, out []backend.Snapshot, req Request, d config.Defenses) []backend.Snapshot {
	reserved := ""
	n := 0
	for _, s := range all {
		if s.Pool == req.Pool && (s.Healthy || !d.HealthChecks) && !s.ManualEject {
			n++
			if s.Name > reserved {
				reserved = s.Name
			}
		}
	}
	if n < 3 {
		return out
	}
	var long, short []backend.Snapshot
	for _, s := range out {
		if s.Name == reserved {
			long = append(long, s)
			// Spillover: short requests may borrow the reserved GPU while its
			// KV cache is mostly free (i.e. no long-prompt flood right now).
			if s.State.KVUtil < 0.3 {
				short = append(short, s)
			}
		} else {
			short = append(short, s)
		}
	}
	switch {
	case req.LongPrompt && len(long) > 0:
		return long
	case req.LongPrompt:
		return out // reserved GPU unavailable: degrade to sharing
	case len(short) > 0:
		return short
	}
	return out
}

func (r *Router) Pick(all []backend.Snapshot, req Request, d config.Defenses) (*backend.Backend, error) {
	c := Eligible(all, req, d)
	if len(c) == 0 {
		if d.KVAware && len(Eligible(all, req, config.Defenses{Routing: d.Routing, HealthChecks: d.HealthChecks,
			OutlierEjection: d.OutlierEjection, CircuitBreaker: d.CircuitBreaker, LongPromptPool: d.LongPromptPool})) > 0 {
			return nil, ErrKVFull
		}
		return nil, ErrNoBackend
	}
	if d.Backpressure {
		saturated := true
		for _, s := range c {
			slots := int64(max(1, s.State.Slots))
			if s.Inflight < slots*2 && s.State.QueueDepth < s.State.Slots {
				saturated = false
				break
			}
		}
		if saturated {
			return nil, ErrSaturated
		}
	}
	switch d.Routing {
	case "least_loaded":
		best := c[0]
		for _, s := range c[1:] {
			if Score(s, d) < Score(best, d) {
				best = s
			}
		}
		return best.B, nil
	case "p2c":
		if len(c) == 1 {
			return c[0].B, nil
		}
		i := rand.IntN(len(c))
		j := rand.IntN(len(c) - 1)
		if j >= i {
			j++
		}
		if Score(c[j], d) < Score(c[i], d) {
			return c[j].B, nil
		}
		return c[i].B, nil
	case "prefix":
		return prefixPick(c, req, d), nil
	default:
		// Weighted round robin: repeat each backend by weight*4.
		var ring []backend.Snapshot
		for _, s := range c {
			for k := 0; k < max(1, int(math.Round(s.Weight*4))); k++ {
				ring = append(ring, s)
			}
		}
		return ring[r.rr.Add(1)%uint64(len(ring))].B, nil
	}
}

func hash(s string) uint64 {
	h := fnv.New64a()
	h.Write([]byte(s))
	return h.Sum64()
}

// prefixPick: consistent hash ring with 64 virtual nodes per GPU. Walk the ring
// from the key's position and take the first GPU whose load is within 1.25x of
// the average (consistent hashing with bounded loads).
func prefixPick(c []backend.Snapshot, req Request, d config.Defenses) *backend.Backend {
	key := req.HashKey
	if key == "" {
		p := req.Prompt
		if len(p) > 256 {
			p = p[:256]
		}
		key = p
	}
	type vnode struct {
		h uint64
		i int
	}
	ring := make([]vnode, 0, len(c)*64)
	var total float64
	for i, s := range c {
		total += float64(s.Inflight + int64(s.State.QueueDepth))
		for v := 0; v < 64; v++ {
			ring = append(ring, vnode{hash(s.Name + "#" + strconv.Itoa(v)), i})
		}
	}
	sort.Slice(ring, func(a, b int) bool { return ring[a].h < ring[b].h })
	limit := math.Max(4, 1.25*(total+1)/float64(len(c)))
	h := hash(key)
	start := sort.Search(len(ring), func(k int) bool { return ring[k].h >= h })
	for k := 0; k < len(ring); k++ {
		s := c[ring[(start+k)%len(ring)].i]
		if float64(s.Inflight+int64(s.State.QueueDepth)) <= limit {
			return s.B
		}
	}
	return c[ring[start%len(ring)].i].B
}
