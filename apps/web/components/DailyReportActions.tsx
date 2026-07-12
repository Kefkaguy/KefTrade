"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { generateDailyResearchReport } from "@/lib/api";

export function GenerateDailyReportButton() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function generate() {
    setBusy(true);
    setMessage("");
    try {
      await generateDailyResearchReport();
      setMessage("Daily research report generated.");
      router.refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Report generation failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dailyReportActions">
      <button className="button" type="button" onClick={generate} disabled={busy}>
        {busy ? "Generating..." : "Generate today’s report"}
      </button>
      {message ? <span className="formHint" role="status">{message}</span> : null}
    </div>
  );
}
