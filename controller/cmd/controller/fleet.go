package main

// Fleet hub: edge devices report telemetry here (including telemetry buffered
// while partitioned) and pull the model version desired for their ring.
//
// Ring rollout: canary (10%) -> early (30%) -> broad (60%). After each ring,
// devices that are online and already running the new version are checked;
// offline devices don't block. If the error rate is over the gate, the rollout
// halts and every ring is pointed back at the previous version.

import (
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var rings = []string{"canary", "early", "broad"}

type Telemetry struct {
	TS        float64 `json:"ts"`
	LatencyMs float64 `json:"latency_ms"`
	OK        bool    `json:"ok"`
	Version   string  `json:"version"`
}

type Device struct {
	ID          string    `json:"id"`
	Ring        string    `json:"ring"`
	Version     string    `json:"version"`
	LastSeen    time.Time `json:"last_seen"`
	Online      bool      `json:"online"`
	Buffered    int       `json:"last_batch_buffered"` // records that arrived late (sent after a partition)
	Dropped     int       `json:"dropped_total"`
	recent      []Telemetry
	ErrorRate   float64 `json:"error_rate"`
	P50Latency  float64 `json:"p50_latency_ms"`
	TotalEvents int     `json:"total_events"`
}

type FleetRollout struct {
	ID        string    `json:"id"`
	Version   string    `json:"version"`
	Previous  string    `json:"previous"`
	HoldS     int       `json:"hold_s"`
	MaxErr    float64   `json:"max_error_rate"`
	MinDev    int       `json:"min_devices"`
	Ring      string    `json:"ring"`
	Status    string    `json:"status"` // running | completed | halted
	Reason    string    `json:"reason,omitempty"`
	Started   time.Time `json:"started"`
	RingStats []gin.H   `json:"ring_stats"`
}

type Fleet struct {
	mu      sync.Mutex
	devices map[string]*Device
	desired map[string]string // ring -> version
	rollout *FleetRollout
	history []*FleetRollout
	event   func(string, ...any)
}

var (
	mFleetOnline  = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "fleet_devices", Help: "Edge devices by ring/version/online"}, []string{"ring", "version", "online"})
	mFleetLate    = promauto.NewCounter(prometheus.CounterOpts{Name: "fleet_telemetry_late_total", Help: "Telemetry records delivered late (buffered during a partition)"})
	mFleetRecords = promauto.NewCounter(prometheus.CounterOpts{Name: "fleet_telemetry_records_total", Help: "Telemetry records received"})
	mFleetErr     = promauto.NewGaugeVec(prometheus.GaugeOpts{Name: "fleet_ring_error_rate", Help: "Error rate of online devices per ring and version"}, []string{"ring", "version"})
)

func NewFleet(event func(string, ...any)) *Fleet {
	f := &Fleet{devices: map[string]*Device{}, event: event,
		desired: map[string]string{"canary": "edge-v1", "early": "edge-v1", "broad": "edge-v1"}}
	go f.loop()
	return f
}

func (f *Fleet) Routes(r *gin.Engine) {
	g := r.Group("/fleet")
	g.POST("/telemetry", f.telemetry)
	g.GET("/desired", func(c *gin.Context) {
		f.mu.Lock()
		defer f.mu.Unlock()
		c.JSON(200, gin.H{"version": f.desired[c.Query("ring")]})
	})
	g.GET("", f.summary)
	g.GET("/devices", f.list)
	g.POST("/rollouts", f.start)
	g.GET("/rollouts/current", func(c *gin.Context) {
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.rollout == nil {
			c.JSON(404, gin.H{"error": "no fleet rollout yet"})
			return
		}
		c.JSON(200, f.rollout)
	})
	g.POST("/rollback", func(c *gin.Context) {
		var b struct {
			Version string `json:"version"`
		}
		_ = c.BindJSON(&b)
		if b.Version == "" {
			b.Version = "edge-v1"
		}
		f.mu.Lock()
		for _, r := range rings {
			f.desired[r] = b.Version
		}
		if f.rollout != nil && f.rollout.Status == "running" {
			f.rollout.Status, f.rollout.Reason = "halted", "manual rollback"
		}
		f.mu.Unlock()
		f.event("fleet: manual rollback of all rings to %s", b.Version)
		c.JSON(200, gin.H{"desired": b.Version})
	})
}

func (f *Fleet) telemetry(c *gin.Context) {
	var b struct {
		Device   string      `json:"device"`
		Ring     string      `json:"ring"`
		Version  string      `json:"version"`
		Dropped  int         `json:"dropped"`
		Records  []Telemetry `json:"records"`
		Buffered int         `json:"buffered"`
	}
	if err := c.BindJSON(&b); err != nil {
		return
	}
	now := time.Now()
	f.mu.Lock()
	d := f.devices[b.Device]
	if d == nil {
		d = &Device{ID: b.Device}
		f.devices[b.Device] = d
	}
	d.Ring, d.Version, d.LastSeen, d.Dropped, d.Buffered = b.Ring, b.Version, now, b.Dropped, b.Buffered
	d.TotalEvents += len(b.Records)
	d.recent = append(d.recent, b.Records...)
	if len(d.recent) > 60 {
		d.recent = d.recent[len(d.recent)-60:]
	}
	desired := f.desired[b.Ring]
	f.mu.Unlock()
	mFleetRecords.Add(float64(len(b.Records)))
	late := 0
	for _, r := range b.Records {
		if float64(now.Unix())-r.TS > 15 {
			late++
		}
	}
	mFleetLate.Add(float64(late))
	c.JSON(200, gin.H{"accepted": len(b.Records), "desired": desired})
}

// stats recomputes per-device health from records of the current version.
func (d *Device) stats() {
	var lat []float64
	errs, n := 0, 0
	for _, r := range d.recent {
		if r.Version != d.Version {
			continue
		}
		n++
		if !r.OK {
			errs++
		}
		lat = append(lat, r.LatencyMs)
	}
	d.ErrorRate, d.P50Latency = 0, 0
	if n > 0 {
		d.ErrorRate = float64(errs) / float64(n)
		sort.Float64s(lat)
		d.P50Latency = lat[len(lat)/2]
	}
}

func (f *Fleet) loop() {
	for range time.Tick(2 * time.Second) {
		f.mu.Lock()
		type key struct{ ring, version, online string }
		counts := map[key]int{}
		errs := map[[2]string][]float64{}
		for _, d := range f.devices {
			d.Online = time.Since(d.LastSeen) < 12*time.Second
			d.stats()
			on := "false"
			if d.Online {
				on = "true"
				errs[[2]string{d.Ring, d.Version}] = append(errs[[2]string{d.Ring, d.Version}], d.ErrorRate)
			}
			counts[key{d.Ring, d.Version, on}]++
		}
		f.mu.Unlock()
		mFleetOnline.Reset()
		for k, v := range counts {
			mFleetOnline.WithLabelValues(k.ring, k.version, k.online).Set(float64(v))
		}
		mFleetErr.Reset()
		for k, v := range errs {
			s := 0.0
			for _, x := range v {
				s += x
			}
			mFleetErr.WithLabelValues(k[0], k[1]).Set(s / float64(len(v)))
		}
	}
}

func (f *Fleet) summary(c *gin.Context) {
	f.mu.Lock()
	defer f.mu.Unlock()
	type agg struct {
		Devices  int            `json:"devices"`
		Online   int            `json:"online"`
		Versions map[string]int `json:"versions"`
		ErrRate  float64        `json:"online_error_rate"`
	}
	out := map[string]*agg{}
	for _, r := range rings {
		out[r] = &agg{Versions: map[string]int{}}
	}
	for _, d := range f.devices {
		a := out[d.Ring]
		if a == nil {
			continue
		}
		a.Devices++
		a.Versions[d.Version]++
		if d.Online {
			a.Online++
			a.ErrRate += d.ErrorRate
		}
	}
	for _, a := range out {
		if a.Online > 0 {
			a.ErrRate /= float64(a.Online)
		}
	}
	c.JSON(200, gin.H{"rings": out, "desired": f.desired, "rollout": f.rollout})
}

func (f *Fleet) list(c *gin.Context) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]*Device, 0, len(f.devices))
	for _, d := range f.devices {
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	c.JSON(200, out)
}

func (f *Fleet) start(c *gin.Context) {
	ro := &FleetRollout{HoldS: 20, MaxErr: 0.05, MinDev: 2}
	if err := c.BindJSON(ro); err != nil || ro.Version == "" {
		c.JSON(400, gin.H{"error": "need version"})
		return
	}
	f.mu.Lock()
	if f.rollout != nil && f.rollout.Status == "running" {
		f.mu.Unlock()
		c.JSON(409, gin.H{"error": "fleet rollout already running"})
		return
	}
	ro.ID = fmt.Sprintf("f%d", time.Now().Unix()%100000)
	ro.Previous = f.desired["broad"]
	ro.Status, ro.Started = "running", time.Now()
	f.rollout = ro
	f.history = append(f.history, ro)
	f.mu.Unlock()
	f.event("fleet rollout %s: %s -> %s rings=%v hold=%ds max_err=%.0f%%", ro.ID, ro.Previous, ro.Version, rings, ro.HoldS, ro.MaxErr*100)
	go f.run(ro)
	c.JSON(202, ro)
}

func (f *Fleet) run(ro *FleetRollout) {
	for _, ring := range rings {
		f.mu.Lock()
		if ro.Status != "running" {
			f.mu.Unlock()
			return
		}
		ro.Ring = ring
		f.desired[ring] = ro.Version
		f.mu.Unlock()
		f.event("fleet rollout %s: ring %s -> %s", ro.ID, ring, ro.Version)

		deadline := time.Now().Add(time.Duration(ro.HoldS) * time.Second)
		for {
			time.Sleep(3 * time.Second)
			f.mu.Lock()
			if ro.Status != "running" {
				f.mu.Unlock()
				return
			}
			// Health of every ring that already runs the new version.
			var bad []string
			judged := 0
			for _, r := range rings {
				if f.desired[r] != ro.Version {
					continue
				}
				on, upd, offline := 0, 0, 0
				errSum := 0.0
				for _, d := range f.devices {
					if d.Ring != r {
						continue
					}
					if !d.Online {
						offline++
						continue
					}
					on++
					if d.Version == ro.Version {
						upd++
						errSum += d.ErrorRate
					}
				}
				er := 0.0
				if upd > 0 {
					er = errSum / float64(upd)
				}
				if r == ring {
					judged = upd
				}
				ro.RingStats = append(ro.RingStats, gin.H{"ts": time.Now(), "ring": r, "online": on, "updated": upd, "offline": offline, "error_rate": er})
				if len(ro.RingStats) > 200 {
					ro.RingStats = ro.RingStats[len(ro.RingStats)-200:]
				}
				if upd >= ro.MinDev && er > ro.MaxErr {
					bad = append(bad, fmt.Sprintf("ring %s error rate %.0f%% on %d updated devices", r, er*100, upd))
				}
			}
			if len(bad) > 0 {
				for _, r := range rings {
					f.desired[r] = ro.Previous
				}
				ro.Status, ro.Reason = "halted", fmt.Sprintf("health gate: %v -> all rings rolled back to %s", bad, ro.Previous)
				f.mu.Unlock()
				f.event("fleet rollout %s HALTED: %s", ro.ID, ro.Reason)
				return
			}
			done := time.Now().After(deadline) && judged >= ro.MinDev
			f.mu.Unlock()
			if done {
				break
			}
			if time.Since(ro.Started) > 20*time.Minute {
				f.mu.Lock()
				ro.Status, ro.Reason = "halted", "not enough updated devices to judge ring "+ring
				f.mu.Unlock()
				f.event("fleet rollout %s halted: %s", ro.ID, ro.Reason)
				return
			}
		}
	}
	f.mu.Lock()
	ro.Status, ro.Ring = "completed", ""
	f.mu.Unlock()
	f.event("fleet rollout %s completed: all rings on %s", ro.ID, ro.Version)
}
