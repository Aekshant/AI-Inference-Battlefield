package backend

import (
	"testing"
	"time"
)

func TestBreakerOpensAfterThresholdAndProbes(t *testing.T) {
	b := NewBreaker(nil)
	b.Threshold, b.Cooldown = 3, 20*time.Millisecond
	for i := 0; i < 3; i++ {
		if !b.Allow() {
			t.Fatalf("closed breaker refused request %d", i)
		}
		b.Failure()
	}
	if b.State() != Open || b.Allow() {
		t.Fatal("breaker should be open and refuse traffic")
	}
	time.Sleep(25 * time.Millisecond)
	if !b.Allow() {
		t.Fatal("after cooldown one probe should be allowed")
	}
	if b.Allow() {
		t.Fatal("only one half-open probe at a time")
	}
	b.Failure()
	if b.State() != Open {
		t.Fatal("failed probe must re-open the breaker")
	}
	time.Sleep(25 * time.Millisecond)
	b.Allow()
	b.Success()
	if b.State() != Closed {
		t.Fatal("successful probe must close the breaker")
	}
}

func TestBreakerCancelReleasesProbe(t *testing.T) {
	b := NewBreaker(nil)
	b.Threshold, b.Cooldown = 1, time.Millisecond
	b.Failure()
	time.Sleep(2 * time.Millisecond)
	if !b.Allow() {
		t.Fatal("probe expected")
	}
	b.Cancel()
	if !b.Allow() {
		t.Fatal("cancelled probe must free the slot")
	}
}
