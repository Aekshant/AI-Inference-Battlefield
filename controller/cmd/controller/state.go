package main

import (
	"bytes"
	"encoding/json"
	"log"
	"os"
	"time"
)

// persisted is the controller state worth surviving a restart: the model
// registry (with eval results, so `make eval` isn't needed again) and the
// version each edge ring should run.
type persisted struct {
	Models  map[string]*Model `json:"models"`
	Desired map[string]string `json:"fleet_desired"`
}

func (c *Controller) snapshot(f *Fleet) []byte {
	c.mu.Lock()
	f.mu.Lock()
	b, _ := json.MarshalIndent(persisted{Models: c.models, Desired: f.desired}, "", "  ")
	f.mu.Unlock()
	c.mu.Unlock()
	return b
}

// persistLoop writes the state file whenever it changes (atomic rename).
func (c *Controller) persistLoop(path string, f *Fleet) {
	var last []byte
	for range time.Tick(2 * time.Second) {
		b := c.snapshot(f)
		if bytes.Equal(b, last) {
			continue
		}
		if err := os.WriteFile(path+".tmp", b, 0o644); err != nil {
			log.Printf("state: %v", err)
			continue
		}
		if err := os.Rename(path+".tmp", path); err != nil {
			log.Printf("state: %v", err)
			continue
		}
		last = b
	}
}

func (c *Controller) restore(path string, f *Fleet) {
	b, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var st persisted
	if err := json.Unmarshal(b, &st); err != nil {
		log.Printf("state: ignoring corrupt %s: %v", path, err)
		return
	}
	c.mu.Lock()
	for k, m := range st.Models {
		if m.Status == "running" {
			m.Status = "candidate" // a rollout can't survive a restart; it must be restarted
		}
		c.models[k] = m
	}
	c.mu.Unlock()
	f.mu.Lock()
	for k, v := range st.Desired {
		f.desired[k] = v
	}
	f.mu.Unlock()
	c.event("restored registry (%d models) and fleet desired versions from %s", len(st.Models), path)
}
