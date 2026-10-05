package backend

import (
	"sync"
	"time"
)

type BreakerState int

const (
	Closed BreakerState = iota
	Open
	HalfOpen
)

func (s BreakerState) String() string {
	return [...]string{"closed", "open", "half_open"}[s]
}

// Breaker is a consecutive-failure circuit breaker:
// closed --N failures--> open --cooldown--> half_open --success--> closed
//
//	\--failure--> open
type Breaker struct {
	mu        sync.Mutex
	state     BreakerState
	failures  int
	openedAt  time.Time
	probing   bool
	Threshold int
	Cooldown  time.Duration
	onChange  func(from, to BreakerState)
}

func NewBreaker(onChange func(from, to BreakerState)) *Breaker {
	return &Breaker{Threshold: 5, Cooldown: 10 * time.Second, onChange: onChange}
}

func (b *Breaker) State() BreakerState {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.state == Open && time.Since(b.openedAt) >= b.Cooldown {
		b.set(HalfOpen)
	}
	return b.state
}

// Allow reports whether a request may go to this backend. In half-open only a
// single probe is let through at a time.
func (b *Breaker) Allow() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	switch b.state {
	case Closed:
		return true
	case Open:
		if time.Since(b.openedAt) < b.Cooldown {
			return false
		}
		b.set(HalfOpen)
		fallthrough
	default:
		if b.probing {
			return false
		}
		b.probing = true
		return true
	}
}

func (b *Breaker) Success() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.failures = 0
	b.probing = false
	if b.state != Closed {
		b.set(Closed)
	}
}

func (b *Breaker) Failure() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.failures++
	b.probing = false
	if b.state == HalfOpen || (b.state == Closed && b.failures >= b.Threshold) {
		b.openedAt = time.Now()
		b.set(Open)
	}
}

func (b *Breaker) set(to BreakerState) {
	from := b.state
	b.state = to
	if b.onChange != nil && from != to {
		go b.onChange(from, to)
	}
}

// Cancel releases a half-open probe slot without judging the backend
// (e.g. the client went away mid-request).
func (b *Breaker) Cancel() {
	b.mu.Lock()
	b.probing = false
	b.mu.Unlock()
}
