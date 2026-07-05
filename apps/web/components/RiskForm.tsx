"use client";

import { useEffect, useState } from "react";
import { getRiskSettings, updateRiskSettings, type RiskSettings } from "@/lib/api";

const defaults: RiskSettings = {
  account_size: "10000",
  max_risk_per_trade: "0.01",
  max_open_exposure: "0.03",
  daily_loss_limit: "0.02",
  weekly_loss_limit: "0.05",
  allow_leverage: false,
  allow_live_trading: false
};

export function RiskForm() {
  const [settings, setSettings] = useState<RiskSettings>(defaults);
  const [status, setStatus] = useState("Loading");

  useEffect(() => {
    getRiskSettings()
      .then((data) => {
        setSettings(data);
        setStatus("Loaded");
      })
      .catch(() => setStatus("Start the API and database to edit settings."));
  }, []);

  async function save() {
    const updated = await updateRiskSettings(settings);
    setSettings(updated);
    setStatus("Saved. Leverage and live trading remain locked.");
  }

  return (
    <div className="form">
      <div className="field">
        <label>Account size</label>
        <input value={settings.account_size} onChange={(event) => setSettings({ ...settings, account_size: event.target.value })} />
      </div>
      <div className="field">
        <label>Max risk per trade</label>
        <input value={settings.max_risk_per_trade} onChange={(event) => setSettings({ ...settings, max_risk_per_trade: event.target.value })} />
      </div>
      <div className="field">
        <label>Max open exposure</label>
        <input value={settings.max_open_exposure} onChange={(event) => setSettings({ ...settings, max_open_exposure: event.target.value })} />
      </div>
      <div className="field">
        <label>Daily loss limit</label>
        <input value={settings.daily_loss_limit} onChange={(event) => setSettings({ ...settings, daily_loss_limit: event.target.value })} />
      </div>
      <div className="field">
        <label>Weekly loss limit</label>
        <input value={settings.weekly_loss_limit} onChange={(event) => setSettings({ ...settings, weekly_loss_limit: event.target.value })} />
      </div>
      <div className="toolbar">
        <button className="button" onClick={save}>
          Save risk settings
        </button>
        <span className="muted">{status}</span>
      </div>
    </div>
  );
}

