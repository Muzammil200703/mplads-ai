import { useCallback, useEffect, useState } from "react"
import { useAuth } from "../context/AuthContext"
import { getEvidenceQueue, reviewEvidence } from "../services/api"
import { RequireRole } from "./Workspace"

/* ═══════════════════════════════════════════════════════════════════
   CITIZEN EVIDENCE VERIFICATION QUEUE (SIH 26102)
   Field verifiers triage citizen submissions → Verified / Rejected
   (note required). Auditors additionally see verifier decisions and
   can escalate suspicious evidence. Server-side enforcement mirrors
   the UI: /ai/evidence/* requires the verification:review capability.
   ═══════════════════════════════════════════════════════════════════ */

const STATUS_BADGE = {
  pending: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  verified: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  rejected: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  escalated: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300",
}

const OBSERVED_BADGE = {
  Functional: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  "Non-Functional": "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  "Work Not Started": "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
}

export default function VerificationQueue({ onOpenProject }) {
  return (
    <RequireRole minRole="field_verifier">
      <QueueInner onOpenProject={onOpenProject} />
    </RequireRole>
  )
}

function QueueInner({ onOpenProject }) {
  const { user, can } = useAuth()
  const isAuditor = can("inquiry:review") // auditor/admin
  const [filter, setFilter] = useState("pending")
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [busyId, setBusyId] = useState(null)
  const [reviewPanel, setReviewPanel] = useState(null) // {reportId, decision}
  const [note, setNote] = useState("")
  const [flash, setFlash] = useState("")

  const load = useCallback(() => {
    setLoading(true); setError("")
    getEvidenceQueue(filter)
      .then((d) => setData(d))
      .catch((e) => setError(e.message || "Could not load the verification queue"))
      .finally(() => setLoading(false))
  }, [filter])

  useEffect(() => { load() }, [load])

  const decide = async (reportId, decision) => {
    setBusyId(reportId)
    setError("")
    try {
      const res = await reviewEvidence(reportId, decision, note.trim() || undefined)
      setFlash(res.message)
      setReviewPanel(null); setNote("")
      load()
    } catch (e) {
      setError(e.message || "Could not record the decision")
    } finally { setBusyId(null) }
  }

  const pills = [
    { key: "pending", label: "Pending" },
    { key: "escalated", label: "Escalated" },
    { key: "verified", label: "Verified" },
    { key: "rejected", label: "Rejected" },
    { key: "all", label: "All" },
  ]

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 text-[#292326] transition-colors duration-200 sm:p-6 dark:bg-[#000000] dark:text-gray-100">
      <div className="mx-auto max-w-[1440px] space-y-4">
        <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-2xl font-bold text-[#292326] dark:text-[#f5eff1]">Citizen Evidence Queue</h2>
            <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">
              Submissions from citizens ({isAuditor ? "and verifier decisions" : "awaiting your review"}).
              Verify only what the evidence supports — your decision is recorded with your identity and never
              changes official project data.
            </p>
          </div>
          {data?.counts && (
            <div className="flex flex-wrap gap-2 text-xs font-bold">
              <span className="rounded-lg bg-amber-100 px-3 py-1.5 text-amber-700 dark:bg-amber-950 dark:text-amber-300">{data.counts.pending || 0} pending</span>
              <span className="rounded-lg bg-green-100 px-3 py-1.5 text-green-700 dark:bg-green-950 dark:text-green-300">{data.counts.verified || 0} verified</span>
              <span className="rounded-lg bg-red-100 px-3 py-1.5 text-red-700 dark:bg-red-950 dark:text-red-300">{data.counts.rejected || 0} rejected</span>
              <span className="rounded-lg bg-violet-100 px-3 py-1.5 text-violet-700 dark:bg-violet-950 dark:text-violet-300">{data.counts.escalated || 0} escalated</span>
            </div>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          {pills.map((p) => (
            <button
              key={p.key}
              onClick={() => setFilter(p.key)}
              className={`rounded-full px-3.5 py-1.5 text-[0.8125rem] font-bold transition ${
                filter === p.key
                  ? "bg-[#c8102e] text-white dark:bg-blue-600"
                  : "border border-[#e8dadc] bg-white text-[#71666a] hover:bg-gray-50 dark:border-[#351923] dark:bg-[#000000] dark:text-[#b9aeb2] dark:hover:bg-[#0d090b]"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>

        {flash && <div className="rounded-xl border border-green-300 bg-green-50 p-3 text-sm font-semibold text-green-700 dark:border-green-900 dark:bg-green-950/40 dark:text-green-300">{flash}</div>}
        {error && (
          <div className="rounded-xl border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
            {error} <button onClick={load} className="ml-2 font-bold underline">Retry</button>
          </div>
        )}
        {loading && (
          <div className="rounded-xl border border-[#e8dadc] bg-white p-8 text-center text-sm text-[#71666a] dark:border-[#351923] dark:bg-[#000000] dark:text-[#b9aeb2]">Loading citizen evidence…</div>
        )}
        {!loading && !error && data && data.items.length === 0 && (
          <div className="rounded-xl border border-[#e8dadc] bg-white p-10 text-center dark:border-[#351923] dark:bg-[#000000]">
            <div className="text-4xl">📥</div>
            <p className="mt-3 font-semibold text-[#292326] dark:text-[#f5eff1]">No {filter === "all" ? "" : filter} citizen submissions</p>
            <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">Citizen evidence submitted through Ground Verification lands here for review.</p>
          </div>
        )}
        {!loading && !error && data && data.items.length > 0 && (
          <div className="space-y-3">
            {data.items.map((r) => (
              <div key={r.report_id} className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[0.6875rem] font-bold uppercase tracking-wide text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">Citizen Evidence</span>
                      <span className={`rounded px-2 py-0.5 text-xs font-bold uppercase ${STATUS_BADGE[r.review_status] || ""}`}>{r.review_status}</span>
                      <span className={`rounded px-2 py-0.5 text-xs font-bold ${OBSERVED_BADGE[r.citizen_status] || "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"}`}>{r.citizen_status}</span>
                    </div>
                    <p className="mt-1.5 truncate text-[0.9375rem] font-semibold text-[#292326] dark:text-[#f5eff1]">
                      #{r.project_id} {r.project_name || "Project"}
                    </p>
                    <p className="text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">
                      {r.project_state || "—"} · official status: {r.project_official_status || "—"}
                    </p>
                    {r.note && <p className="mt-1 rounded bg-gray-50 p-2 text-[0.8125rem] text-[#71666a] dark:bg-[#0d090b] dark:text-[#b9aeb2]">“{r.note}”</p>}
                    <p className="mt-1.5 text-xs text-[#71666a]/70 dark:text-[#b9aeb2]/70">
                      By {r.reporter} · {r.created_at?.replace("T", " ").slice(0, 16)} · GPS: {r.lat != null ? `${r.lat.toFixed(4)}, ${r.lon?.toFixed(4)} (${r.gps_source})` : "not captured"} {r.photo_hash_id ? "· 📷 photo stored" : ""}
                    </p>
                    {r.review_status !== "pending" && r.reviewed_by_name && (
                      <p className="mt-1 text-xs font-semibold text-[#292326] dark:text-[#f5eff1]">
                        {r.review_status} by {r.reviewed_by_name} · {r.reviewed_at?.replace("T", " ").slice(0, 16)}{r.review_note ? ` — “${r.review_note}”` : ""}
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-2">
                    <button
                      onClick={() => onOpenProject?.(r.project_id)}
                      className="rounded-lg border border-[#e8dadc] px-3 py-1.5 text-[0.8125rem] font-bold text-[#292326] transition hover:bg-[#f8f1f1] dark:border-[#351923] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]"
                    >
                      Open project
                    </button>
                    {(r.review_status === "pending" || r.review_status === "escalated") && (
                      <div className="flex gap-2">
                        <button
                          disabled={busyId === r.report_id}
                          onClick={() => { setReviewPanel({ reportId: r.report_id, decision: "verified" }); setNote("") }}
                          className="rounded-lg bg-green-600 px-3 py-1.5 text-[0.8125rem] font-bold text-white transition hover:bg-green-700 disabled:opacity-50"
                        >
                          ✓ Verify
                        </button>
                        <button
                          disabled={busyId === r.report_id}
                          onClick={() => { setReviewPanel({ reportId: r.report_id, decision: "rejected" }); setNote("") }}
                          className="rounded-lg bg-red-600 px-3 py-1.5 text-[0.8125rem] font-bold text-white transition hover:bg-red-700 disabled:opacity-50"
                        >
                          ✕ Reject
                        </button>
                        {isAuditor && r.review_status === "pending" && (
                          <button
                            disabled={busyId === r.report_id}
                            onClick={() => decide(r.report_id, "escalated")}
                            className="rounded-lg bg-violet-600 px-3 py-1.5 text-[0.8125rem] font-bold text-white transition hover:bg-violet-700 disabled:opacity-50"
                          >
                            ⬆ Escalate
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </div>
                {reviewPanel?.reportId === r.report_id && (
                  <div className="mt-3 rounded-lg border border-[#e8dadc] bg-[#faf7f6] p-3 dark:border-[#351923] dark:bg-[#120b0e]">
                    <label className="text-xs font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">
                      {reviewPanel.decision === "verified" ? "Verification note (optional)" : "Rejection note (required)"}
                    </label>
                    <textarea
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                      rows={2}
                      placeholder={reviewPanel.decision === "verified" ? "e.g. visited site, evidence consistent…" : "e.g. photo does not match the project location…"}
                      className="mt-1.5 w-full rounded-lg border border-[#e8dadc] bg-white p-2 text-sm dark:border-[#351923] dark:bg-[#000000] dark:text-gray-100"
                    />
                    <div className="mt-2 flex gap-2">
                      <button
                        disabled={busyId === r.report_id || (reviewPanel.decision === "rejected" && !note.trim())}
                        onClick={() => decide(r.report_id, reviewPanel.decision)}
                        className="rounded-lg bg-[#c8102e] px-3 py-1.5 text-xs font-bold text-white disabled:opacity-50 dark:bg-blue-600"
                      >
                        Confirm {reviewPanel.decision}
                      </button>
                      <button onClick={() => setReviewPanel(null)} className="rounded-lg border border-[#e8dadc] px-3 py-1.5 text-xs font-bold dark:border-[#351923]">Cancel</button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
