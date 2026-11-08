import { useEffect, useState } from "react"
import { getMultiAgent, runMultiAgent, reviewAgentFinding } from "../services/api"

/* ═══════════════════════════════════════════════════════════════════
   FEATURE 6 — Coordinated multi-agent audit
   Six specialist agents plus a lead coordinator, all reading the same
   project context. Each finding is evidence-linked and reviewable; the
   coordinator's conflicts and deduplication are shown explicitly.
   Findings never become confirmed conclusions automatically.
   ═══════════════════════════════════════════════════════════════════ */

const AGENT_LABELS = {
  financial: "Financial audit",
  execution: "Execution analysis",
  vendor: "Vendor intelligence",
  historical: "Historical analysis",
  reconciliation: "Data reconciliation",
  evidence: "Evidence review",
  coordinator: "Lead coordinator",
}

const CONFIDENCE_TONE = {
  high: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  medium: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  low: "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300",
}

const STATUS_TONE = {
  New: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  Accepted: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  Dismissed: "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300",
  "Flagged for Verification": "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
}

const REVIEW_ACTIONS = ["Accepted", "Dismissed", "Flagged for Verification"]

export default function MultiAgentPanel({ projectId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState("")
  const [open, setOpen] = useState({})

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await getMultiAgent(projectId)
        if (alive) { setData(res); setError("") }
      } catch (e) {
        if (alive) setError(e.message || "Could not load the multi-agent investigation.")
      }
    })()
    return () => { alive = false }
  }, [projectId])

  async function run() {
    setBusy("run")
    setError("")
    try {
      await runMultiAgent(projectId)
      const res = await getMultiAgent(projectId)
      setData(res)
    } catch (e) {
      setError(e.message || "The multi-agent run did not complete.")
    } finally {
      setBusy("")
    }
  }

  async function review(id, status) {
    setBusy(`f${id}`)
    try {
      await reviewAgentFinding(id, { status })
      const res = await getMultiAgent(projectId)
      setData(res)
    } catch (e) {
      setError(e.message || "Could not record the review.")
    } finally {
      setBusy("")
    }
  }

  const findings = data?.findings || []
  const runInfo = data?.run

  return (
    <div className="space-y-3">
      <section className="rounded-xl border border-gray-200 bg-white p-3.5 dark:border-gray-700 dark:bg-[#000000]">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h4 className="text-[0.72rem] font-bold uppercase tracking-wider text-gray-400">Multi-agent audit</h4>
            <p className="mt-0.5 text-[0.8rem] text-gray-500 dark:text-gray-400">
              Six specialist agents analyse the same stored records; the coordinator merges their findings, flags
              conflicts and removes duplicates.
            </p>
          </div>
          <button
            type="button"
            onClick={run}
            disabled={busy === "run"}
            className="rounded-lg bg-blue-600 px-3 py-1.5 text-[0.8rem] font-bold text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {busy === "run" ? "Running agents…" : runInfo?.available === false ? "Run multi-agent audit" : "Re-run audit"}
          </button>
        </div>

        {runInfo && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[0.72rem] text-gray-500 dark:text-gray-400">
            <span className="rounded bg-gray-100 px-1.5 py-0.5 font-bold text-gray-600 dark:bg-gray-800 dark:text-gray-300">{runInfo.status}</span>
            <span>{String(runInfo.created_at || "").replace("T", " ").slice(0, 16)}</span>
            {runInfo.run_by && <span>· {runInfo.run_by}</span>}
            {runInfo.duplicate_findings_removed > 0 && <span>· {runInfo.duplicate_findings_removed} duplicate(s) removed</span>}
          </div>
        )}

        {Array.isArray(runInfo?.agents) && runInfo.agents.length > 0 && (
          <div className="mt-2 grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
            {runInfo.agents.map((a, i) => {
              const key = a.agent || a.name || `agent-${i}`
              const failed = /fail|error/i.test(String(a.status || ""))
              return (
                <div key={key} className={`rounded-lg border p-2 ${failed ? "border-red-200 dark:border-red-900/40" : "border-gray-200 dark:border-gray-700"}`}>
                  <div className="flex items-center justify-between gap-1">
                    <p className="truncate text-[0.78rem] font-bold text-gray-800 dark:text-gray-200">{AGENT_LABELS[key] || key}</p>
                    <span className={`rounded px-1.5 py-0.5 text-[0.6rem] font-bold ${failed ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" : "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"}`}>
                      {a.status || "done"}
                    </span>
                  </div>
                  <p className="text-[0.68rem] text-gray-400">
                    {a.findings != null ? `${a.findings} finding(s)` : ""}
                    {a.duration_ms != null ? ` · ${Math.round(a.duration_ms)} ms` : ""}
                  </p>
                  {a.error && <p className="mt-0.5 text-[0.68rem] font-semibold text-red-600 dark:text-red-400">{a.error}</p>}
                </div>
              )
            })}
          </div>
        )}
      </section>

      {error && (
        <p className="rounded-lg border border-red-200 bg-red-50 p-2.5 text-[0.82rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">{error}</p>
      )}

      {data && data.available === false && (
        <p className="rounded-lg bg-gray-50 p-3 text-[0.85rem] text-gray-500 dark:bg-gray-900/40 dark:text-gray-400">{data.reason}</p>
      )}

      {Array.isArray(runInfo?.conflicts) && runInfo.conflicts.length > 0 && (
        <section className="rounded-xl border border-amber-200 bg-amber-50/60 p-3 dark:border-amber-900/40 dark:bg-amber-950/10">
          <p className="text-[0.7rem] font-bold uppercase tracking-wider text-amber-600 dark:text-amber-400">Conflicting conclusions</p>
          <ul className="mt-1 space-y-1 text-[0.8rem] text-gray-700 dark:text-gray-300">
            {runInfo.conflicts.map((c, i) => (
              <li key={i}>{typeof c === "string" ? c : c.summary || c.detail || JSON.stringify(c)}</li>
            ))}
          </ul>
        </section>
      )}

      {findings.length === 0 && data?.available !== false && (
        <p className="rounded-lg bg-gray-50 p-3 text-[0.85rem] text-gray-500 dark:bg-gray-900/40 dark:text-gray-400">
          No findings are stored for this project yet.
        </p>
      )}

      <div className="space-y-2">
        {findings.map((f) => (
          <section key={f.id} className="rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-[#000000]">
            <div className="flex flex-wrap items-start justify-between gap-1.5">
              <div className="min-w-0">
                <p className="text-[0.88rem] font-bold text-gray-900 dark:text-white">{f.title}</p>
                <p className="text-[0.68rem] font-semibold uppercase tracking-wider text-gray-400">
                  {AGENT_LABELS[f.agent] || f.agent}
                </p>
              </div>
              <div className="flex flex-wrap gap-1">
                <span className={`rounded px-1.5 py-0.5 text-[0.62rem] font-bold ${CONFIDENCE_TONE[f.confidence] || CONFIDENCE_TONE.low}`}>{f.confidence}</span>
                <span className={`rounded px-1.5 py-0.5 text-[0.62rem] font-bold ${STATUS_TONE[f.status] || STATUS_TONE.New}`}>{f.status}</span>
              </div>
            </div>

            <p className="mt-1 text-[0.82rem] leading-snug text-gray-600 dark:text-gray-300">{f.explanation}</p>

            {f.suggested_verification && (
              <p className="mt-1 rounded bg-blue-50 px-2 py-1 text-[0.75rem] text-blue-700 dark:bg-blue-950/30 dark:text-blue-300">
                <span className="font-bold">Verify:</span> {f.suggested_verification}
              </p>
            )}

            <button
              type="button"
              onClick={() => setOpen((o) => ({ ...o, [f.id]: !o[f.id] }))}
              className="mt-1.5 text-[0.72rem] font-bold text-blue-600 hover:underline dark:text-blue-400"
            >
              {open[f.id] ? "Hide evidence" : "Show evidence"}
            </button>

            {open[f.id] && (
              <div className="mt-1 space-y-1">
                {(f.evidence?.records || []).map((r, i) => (
                  <div key={i} className="rounded border border-gray-200 bg-gray-50 px-2 py-1 dark:border-gray-700 dark:bg-gray-900/40">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[0.6rem] font-bold uppercase text-gray-500 dark:bg-gray-700 dark:text-gray-300">{r.type || "record"}</span>
                      <code className="break-all text-[0.7rem] font-semibold text-blue-700 dark:text-blue-300">{r.reference}</code>
                    </div>
                    <div className="mt-0.5 flex flex-wrap gap-x-2.5 gap-y-0.5">
                      {Object.entries(r.fields || {}).map(([k, v]) => (
                        <span key={k} className="text-[0.7rem] text-gray-500 dark:text-gray-400"><span className="font-semibold">{k}:</span> {String(v ?? "—")}</span>
                      ))}
                    </div>
                  </div>
                ))}
                {(f.limitations || []).map((l) => (
                  <p key={l} className="text-[0.7rem] italic text-gray-400">{l}</p>
                ))}
              </div>
            )}

            <div className="mt-2 flex flex-wrap gap-1">
              {REVIEW_ACTIONS.map((action) => (
                <button
                  key={action}
                  type="button"
                  disabled={busy === `f${f.id}`}
                  onClick={() => review(f.id, action)}
                  className={`rounded-lg border px-2 py-1 text-[0.7rem] font-bold transition disabled:opacity-50 ${
                    f.status === action
                      ? "border-blue-500 bg-blue-50 text-blue-700 dark:border-blue-700 dark:bg-blue-950/40 dark:text-blue-300"
                      : "border-gray-300 text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                  }`}
                >
                  {action}
                </button>
              ))}
            </div>
            {f.reviewer_name && (
              <p className="mt-1 text-[0.7rem] text-gray-400">
                reviewed by {f.reviewer_name}{f.reviewer_note ? ` — ${f.reviewer_note}` : ""}
              </p>
            )}
          </section>
        ))}
      </div>

      {(data?.questions || []).length > 0 && (
        <section className="rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-[#000000]">
          <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Investigation questions</p>
          <ul className="mt-1 list-inside list-disc space-y-0.5 text-[0.8rem] text-gray-600 dark:text-gray-300">
            {data.questions.map((q, i) => (
              <li key={i}>{typeof q === "string" ? q : q.question || q.label || JSON.stringify(q)}</li>
            ))}
          </ul>
        </section>
      )}

      <div className="space-y-1">
        {(data?.disclaimers || []).map((d) => (
          <p key={d} className="text-[0.72rem] text-gray-500 dark:text-gray-400">• {d}</p>
        ))}
      </div>
    </div>
  )
}
