import { useCallback, useEffect, useState } from "react"
import {
  getAgentRun, runResearchAgent, reviewAgentFinding, getAgentReport,
} from "../services/api"

/* ═══════════════════════════════════════════════════════════════════
   AUTONOMOUS AUDIT RESEARCH AGENT — investigation workspace panel
   Lives inside the existing InvestigationWorkspace (Audit Center), not a
   separate application. One click has the agent work through project
   context, the existing evidence dossier, the payment trail, stored import
   history, peer works and vendor connections; it then presents
   connection-level findings with evidence, confidence, verification steps
   and limitations, plus investigation questions.

   Boundaries enforced by the backend (and reflected here):
     • the run is read-only over project/financial data;
     • findings are review indicators, never conclusions;
     • the auditor accepts / dismisses / flags each finding — the agent
       never closes an investigation or finalises a dossier finding.
   ═══════════════════════════════════════════════════════════════════ */

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

function Card({ children, className = "" }) {
  return (
    <section className={`rounded-xl border border-gray-200 bg-white p-3.5 dark:border-gray-700 dark:bg-[#000000] ${className}`}>
      {children}
    </section>
  )
}

function EvidenceRecords({ evidence }) {
  const records = Array.isArray(evidence?.records) ? evidence.records : []
  if (!records.length) return null
  return (
    <div className="mt-2 rounded-lg bg-gray-50 p-2 dark:bg-gray-900/40">
      <p className="text-[0.6875rem] font-bold uppercase tracking-wider text-gray-400">
        Evidence {evidence?.source ? `· ${evidence.source}` : ""}
      </p>
      <ul className="mt-1 space-y-1">
        {records.slice(0, 6).map((r, i) => (
          <li key={i} className="text-[0.78rem] leading-snug text-gray-600 dark:text-gray-300">
            <span className="font-mono text-[0.72rem] text-blue-700 dark:text-blue-300">
              {r.type || "record"}
            </span>
            {r.reference ? <> · {r.reference}</> : null}
            {r.fields && (
              <span className="text-gray-500 dark:text-gray-400">
                {" — "}
                {Object.entries(r.fields).slice(0, 5).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ")}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

export default function AuditResearchAgentPanel({ projectId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [notes, setNotes] = useState({})
  const [openEvidence, setOpenEvidence] = useState({})

  const load = useCallback(() => {
    setLoading(true)
    setError("")
    getAgentRun(projectId)
      .then(setData)
      .catch((e) => setError(e?.message || "Could not load the research agent."))
      .finally(() => setLoading(false))
  }, [projectId])

  // Fetch on mount/project change without a synchronous setState in the
  // effect body — state updates only land from the request callbacks.
  // The parent keys this panel by projectId, so no stale data survives a
  // project switch.
  useEffect(() => {
    let alive = true
    getAgentRun(projectId)
      .then((payload) => { if (alive) setData(payload) })
      .catch((e) => { if (alive) setError(e?.message || "Could not load the research agent.") })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [projectId])

  const run = async () => {
    setBusy(true); setError("")
    try {
      const payload = await runResearchAgent(projectId)
      setData(payload)
    } catch (e) {
      setError(e?.message || "The research agent could not complete this run.")
    } finally { setBusy(false) }
  }

  const review = async (finding, status) => {
    setBusy(true); setError("")
    try {
      await reviewAgentFinding(finding.id, { status, note: notes[finding.id] || undefined })
      load()
    } catch (e) {
      setError(e?.message || "Could not save the finding review.")
    } finally { setBusy(false) }
  }

  const download = async () => {
    setBusy(true); setError("")
    try {
      const report = await getAgentReport(projectId)
      const url = URL.createObjectURL(new Blob([report.body || ""], { type: "text/plain;charset=utf-8" }))
      const a = document.createElement("a")
      a.href = url
      a.download = report.filename || `MPLADS-agent-report-${projectId}.txt`
      document.body.appendChild(a); a.click(); a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(e?.message || "Could not generate the agent report.")
    } finally { setBusy(false) }
  }

  if (loading) {
    return (
      <Card>
        <div className="h-4 w-1/2 animate-pulse rounded-sm bg-gray-200 dark:bg-gray-700" />
        <div className="mt-2 h-20 animate-pulse rounded-lg bg-gray-100 dark:bg-gray-800" />
      </Card>
    )
  }

  const runData = data?.agent_run
  const findings = Array.isArray(data?.agent_findings) ? data.agent_findings : []
  const questions = Array.isArray(data?.questions) ? data.questions : []
  const checks = Array.isArray(data?.suggested_checks) ? data.suggested_checks : []
  const progress = data?.progress || {}

  return (
    <div className="space-y-3">
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-[0.875rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">
          {error}
        </div>
      )}

      <Card className="border-blue-200 bg-blue-50/40 dark:border-blue-900/40 dark:bg-blue-950/10">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="text-[0.7rem] font-bold uppercase tracking-wider text-blue-700 dark:text-blue-300">Autonomous audit research agent</p>
            <p className="mt-1 text-[0.875rem] leading-relaxed text-gray-600 dark:text-gray-300">
              The agent researches this project across the platform's own records — dossier evidence, payment trail,
              stored import history, peer works and vendor connections — then returns evidence-linked findings,
              questions and verification steps for your review. It reads data only and never closes an investigation.
            </p>
            {runData && (
              <p className="mt-1.5 text-[0.78rem] text-gray-500 dark:text-gray-400">
                Run #{runData.id} · {String(runData.created_at || "").replace("T", " ").slice(0, 16)} · by {runData.run_by || "unknown"} · {runData.steps?.length || 0} steps
                {runData.fields_followed?.length ? ` · followed: ${runData.fields_followed.join(", ")}` : ""}
              </p>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            <button
              onClick={run}
              disabled={busy}
              className="rounded-lg bg-[#c8102e] px-3 py-1.5 text-[0.8125rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
            >
              {busy ? "Researching…" : runData ? "Run research again" : "Run research agent"}
            </button>
            {runData && (
              <button
                onClick={download}
                disabled={busy}
                className="rounded-lg border border-gray-300 px-3 py-1.5 text-[0.8125rem] font-bold text-gray-700 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
              >
                Download agent report
              </button>
            )}
          </div>
        </div>

        {runData && (
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div className="rounded-lg bg-white/70 p-2 dark:bg-gray-900/50">
              <p className="text-[0.68rem] font-bold uppercase tracking-wider text-gray-400">Agent findings</p>
              <p className="font-mono text-[1.05rem] font-bold text-gray-900 dark:text-white">{progress.agent_findings ?? findings.length}</p>
            </div>
            <div className="rounded-lg bg-green-50 p-2 dark:bg-green-950/20">
              <p className="text-[0.68rem] font-bold uppercase tracking-wider text-green-600 dark:text-green-400">Accepted</p>
              <p className="font-mono text-[1.05rem] font-bold text-green-700 dark:text-green-300">{progress.agent_accepted ?? 0}</p>
            </div>
            <div className="rounded-lg bg-amber-50 p-2 dark:bg-amber-950/20">
              <p className="text-[0.68rem] font-bold uppercase tracking-wider text-amber-600 dark:text-amber-400">Flagged</p>
              <p className="font-mono text-[1.05rem] font-bold text-amber-700 dark:text-amber-300">{progress.agent_flagged ?? 0}</p>
            </div>
            <div className="rounded-lg bg-white/70 p-2 dark:bg-gray-900/50">
              <p className="text-[0.68rem] font-bold uppercase tracking-wider text-gray-400">Dossier unresolved</p>
              <p className="font-mono text-[1.05rem] font-bold text-gray-900 dark:text-white">{progress.dossier_unresolved ?? 0}</p>
            </div>
          </div>
        )}
      </Card>

      {!runData && (
        <Card>
          <p className="text-[0.9375rem] leading-relaxed text-gray-600 dark:text-gray-300">
            No research run exists for this project yet. Start one above — the agent will examine the recorded context,
            trace the payment and vendor connections, check stored history, and propose investigation questions backed
            by the records it used.
          </p>
        </Card>
      )}

      {runData && (
        <Card>
          <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Research trail</p>
          <ol className="mt-2 space-y-1.5">
            {(runData.steps || []).map((step, i) => (
              <li key={`${step.key}-${i}`} className="flex gap-2">
                <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-blue-100 text-[0.6rem] font-bold text-blue-700 dark:bg-blue-950 dark:text-blue-300">{i + 1}</span>
                <div className="min-w-0">
                  <p className="text-[0.84rem] font-semibold text-gray-800 dark:text-gray-100">{step.title}</p>
                  <p className="text-[0.78rem] leading-snug text-gray-500 dark:text-gray-400">{step.detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </Card>
      )}

      {findings.length > 0 && (
        <div className="space-y-2">
          <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Agent findings — connection level</p>
          {findings.map((f) => (
            <Card key={f.id}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <p className="min-w-0 flex-1 text-[0.9rem] font-bold text-gray-900 dark:text-white">{f.title}</p>
                <div className="flex shrink-0 gap-1">
                  <span className={`rounded-full px-1.5 py-0.5 text-[0.6rem] font-bold uppercase ${CONFIDENCE_TONE[f.confidence] || CONFIDENCE_TONE.low}`}>{f.confidence} confidence</span>
                  <span className={`rounded-full px-1.5 py-0.5 text-[0.6rem] font-bold ${STATUS_TONE[f.status] || STATUS_TONE.New}`}>{f.status}</span>
                </div>
              </div>
              <p className="mt-1 text-[0.84rem] leading-relaxed text-gray-700 dark:text-gray-300">{f.explanation}</p>

              {f.suggested_verification && (
                <p className="mt-1.5 rounded-sm bg-blue-50 p-2 text-[0.78rem] leading-snug text-blue-800 dark:bg-blue-950/20 dark:text-blue-200">
                  <span className="font-bold">Verify:</span> {f.suggested_verification}
                </p>
              )}

              <button
                onClick={() => setOpenEvidence((s) => ({ ...s, [f.id]: !s[f.id] }))}
                className="mt-1.5 text-[0.75rem] font-semibold text-blue-700 hover:underline dark:text-blue-300"
              >
                {openEvidence[f.id] ? "Hide evidence" : `Evidence (${(f.evidence?.records || []).length})`}
              </button>
              {openEvidence[f.id] && <EvidenceRecords evidence={f.evidence} />}

              {Array.isArray(f.limitations) && f.limitations.length > 0 && (
                <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-[0.72rem] leading-snug text-gray-500 dark:text-gray-400">
                  {f.limitations.map((l, i) => <li key={i}>{l}</li>)}
                </ul>
              )}

              <div className="mt-2 border-t border-gray-100 pt-2 dark:border-gray-800">
                <div className="flex flex-wrap items-center gap-1.5">
                  {REVIEW_ACTIONS.map((status) => (
                    <button
                      key={status}
                      onClick={() => review(f, status)}
                      disabled={busy}
                      className={`rounded-lg border px-2 py-1 text-[0.75rem] font-bold transition disabled:opacity-50 ${
                        f.status === status
                          ? "border-blue-500 bg-blue-50 text-blue-700 dark:border-blue-700 dark:bg-blue-950/40 dark:text-blue-300"
                          : "border-gray-200 text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
                      }`}
                    >
                      {status}
                    </button>
                  ))}
                  <input
                    value={notes[f.id] ?? f.reviewer_note ?? ""}
                    onChange={(e) => setNotes((n) => ({ ...n, [f.id]: e.target.value }))}
                    placeholder="Reviewer note"
                    className="min-w-0 flex-1 rounded-lg border border-gray-200 px-2 py-1 text-[0.75rem] outline-none focus:border-blue-500 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
                  />
                </div>
                {f.reviewer_name && (
                  <p className="mt-1 text-[0.7rem] text-gray-400">
                    Last reviewed by {f.reviewer_name}{f.reviewed_at ? ` · ${String(f.reviewed_at).replace("T", " ").slice(0, 16)}` : ""}
                  </p>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}

      {runData && questions.length > 0 && (
        <Card>
          <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Investigation questions</p>
          <ul className="mt-1.5 space-y-1.5">
            {questions.map((q, i) => (
              <li key={i} className="text-[0.84rem] leading-snug text-gray-700 dark:text-gray-300">
                <span className="font-semibold">{i + 1}. {q.question}</span>
                <span className="block text-[0.74rem] text-gray-500 dark:text-gray-400">Raised by: {q.raises_from}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {runData && checks.length > 0 && (
        <Card>
          <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Recommended verification steps</p>
          <ul className="mt-1.5 space-y-1">
            {checks.map((c, i) => (
              <li key={i} className="text-[0.84rem] leading-snug text-gray-700 dark:text-gray-300">
                • {c.check}
                {c.basis && <span className="text-[0.74rem] text-gray-500 dark:text-gray-400"> — {c.basis}</span>}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {runData && (data?.limitations?.length > 0 || data?.disclaimers?.length > 0) && (
        <Card className="bg-gray-50 dark:bg-gray-900/40">
          {data.limitations?.length > 0 && (
            <>
              <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Evidence limitations</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[0.76rem] leading-snug text-gray-500 dark:text-gray-400">
                {data.limitations.map((l, i) => <li key={i}>{l}</li>)}
              </ul>
            </>
          )}
          {data.disclaimers?.length > 0 && (
            <p className="mt-2 text-[0.72rem] leading-snug text-gray-400">{data.disclaimers.join(" ")}</p>
          )}
        </Card>
      )}
    </div>
  )
}
