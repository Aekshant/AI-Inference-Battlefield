package main

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"

	"battlefield/gateway/internal/config"
)

// persisted is everything an operator (or the AI SRE) can change at runtime.
// It is written to STATE_FILE (a PersistentVolume in the cluster) after every
// admin change, so a gateway restart no longer silently drops all defenses.
type persisted struct {
	Defenses config.Defenses          `json:"defenses"`
	Tenants  map[string]config.Tenant `json:"tenants"`
	Canary   float64                  `json:"canary_weight"`
	Weights  map[string]float64       `json:"weights"`
	Ejects   map[string]bool          `json:"ejects"`
}

func (g *Gateway) save() {
	if g.stateFile == "" {
		return
	}
	w, e := g.reg.Overrides()
	st := persisted{Defenses: g.cfg.Defenses(), Tenants: g.cfg.Tenants(), Canary: g.canaryWeight(), Weights: w, Ejects: e}
	b, _ := json.MarshalIndent(st, "", "  ")
	tmp := g.stateFile + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		log.Printf("state: write: %v", err)
		return
	}
	if err := os.Rename(tmp, g.stateFile); err != nil { // atomic replace
		log.Printf("state: rename: %v", err)
	}
}

// restore loads persisted state; returns false when there is none.
func (g *Gateway) restore() bool {
	if g.stateFile == "" {
		return false
	}
	b, err := os.ReadFile(g.stateFile)
	if err != nil {
		return false
	}
	var st persisted
	if err := json.Unmarshal(b, &st); err != nil {
		log.Printf("state: ignoring corrupt %s: %v", g.stateFile, err)
		return false
	}
	g.cfg.Load(st.Defenses, st.Tenants)
	g.canary = st.Canary
	g.reg.LoadOverrides(st.Weights, st.Ejects)
	g.event("restored state from " + filepath.Base(g.stateFile))
	return true
}
