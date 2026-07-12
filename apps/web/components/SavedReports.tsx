"use client";

import { useEffect, useState } from "react";
import { EmptyState } from "@/components/ResearchUI";

type SavedReport = {
  id: string;
  createdAt: string;
  asset: string;
  strategy: string;
  verdict: string;
  confidence: string;
  summary: string;
  evidenceRefs: string[];
  markdown: string;
};

export function SavedReports() {
  const [reports, setReports] = useState<SavedReport[]>([]);

  useEffect(() => {
    try {
      setReports(JSON.parse(window.localStorage.getItem("keftrade-saved-reports") || "[]") as SavedReport[]);
    } catch {
      setReports([]);
    }
  }, []);

  function download(report: SavedReport) {
    const blob = new Blob([report.markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${report.asset.toLowerCase()}-research-report.md`;
    link.click();
    URL.revokeObjectURL(url);
  }

  function remove(id: string) {
    const next = reports.filter((report) => report.id !== id);
    setReports(next);
    window.localStorage.setItem("keftrade-saved-reports", JSON.stringify(next));
  }

  return (
    <section className="savedReportsSection">
      <header className="pageHeader simpleHeader">
        <div>
          <h1>Saved Browser Reports</h1>
          <p className="muted">Saved evidence reports from guided analysis. Reports are stored locally in this browser.</p>
        </div>
      </header>

      {reports.length ? (
        <div className="reportList">
          {reports.map((report) => (
            <article key={report.id} className="reportCard">
              <div>
                <span className="sectionLabel">{new Date(report.createdAt).toLocaleString()}</span>
                <h2>{report.asset}</h2>
                <p>{report.summary}</p>
              </div>
              <div className="answerSide">
                <span>Verdict <strong>{report.verdict}</strong></span>
                <span>Strategy <strong>{report.strategy}</strong></span>
                <span>Confidence <strong>{report.confidence}</strong></span>
              </div>
              <div className="resultActions">
                <button className="button secondary" type="button" onClick={() => download(report)}>Download Report</button>
                <button className="button ghost" type="button" onClick={() => remove(report.id)}>Dismiss</button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <EmptyState title="No saved reports yet." body="Run an analysis from Home, then save the report when the evidence answer is useful." />
      )}
    </section>
  );
}
