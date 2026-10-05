import { useEffect, useState } from "react";
import Arsenal from "./pages/Arsenal";
import Battlefield from "./pages/Battlefield";
import Missions from "./pages/Missions";
import Rollouts from "./pages/Rollouts";
import Sre from "./pages/Sre";

const TABS: [string, string][] = [
  ["missions", "Missions"],
  ["battlefield", "Battlefield"],
  ["arsenal", "Arsenal"],
  ["rollouts", "Rollouts & Fleet"],
  ["sre", "AI SRE"],
];

function initialTab() {
  const h = window.location.hash.slice(1);
  if (TABS.some(([id]) => id === h)) return h;
  try {
    return localStorage.getItem("bf-tab") ?? "missions";
  } catch {
    return "missions";
  }
}

export default function App() {
  const [tab, setTab] = useState(initialTab);
  useEffect(() => {
    window.history.replaceState(null, "", `#${tab}`);
    try { localStorage.setItem("bf-tab", tab); } catch { /* storage unavailable */ }
  }, [tab]);
  return (
    <div className="app">
      <header className="top">
        <h1>⚔️ AI Inference Battlefield</h1>
        <nav className="tabs">
          {TABS.map(([id, label]) => (
            <button key={id} className={tab === id ? "on" : ""} onClick={() => setTab(id)}>{label}</button>
          ))}
        </nav>
        <span className="spacer" />
        <a className="muted" href="http://127.0.0.1:3000" target="_blank" rel="noreferrer">Grafana ↗</a>
      </header>
      {tab === "missions" && <Missions go={setTab} />}
      {tab === "battlefield" && <Battlefield />}
      {tab === "arsenal" && <Arsenal />}
      {tab === "rollouts" && <Rollouts />}
      {tab === "sre" && <Sre />}
    </div>
  );
}
