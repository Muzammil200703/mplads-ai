import { useCallback, useEffect, useState } from "react"
import { getReconciliation, reviewReconciliation } from "../services/api"
import { useAuth } from "../context/AuthContext"

/* ═══════════════════════════════════════════════════════════════════
   FEATURE 4 — Cross-dataset reconciliation
   Shows each discrepancy with the original records side by side, a match
   confidence band and an auditor review action. Nothing is merged and no
   case is labelled as misconduct.
   ═══════════════════════════════════════════════════════════════════ */

const CONFIDENCE_TONE = {
  confirmed: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  probable: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  uncertain: "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300",
}

const SEVERITY_TONE = {
  high: "border-red-200 dark:border-red-900/40",
  medium: "border-amber-200 dark:border-amber-900/40",
  low: "border-gray-200 dark:border-gray-700",
}

const DECISIONS = [
  ["confirmed", "Confirm match"],
  ["needs_review", "Needs field review"],
  ["dismissed", "Dismiss"],
]

export default function ReconciliationPanel() {
  const { hasRole } = useAuth()
  const canReview = hasRole("auditor")

  const [data, setData] = useState(null)
  const [error, setError] = useState("")
  const [busyKey, setBusyKey] = useState("")
  const [note, setNote] = useState("")
  const [scope, setScope] = useState({ state: "", house: "" })
  const { state: scopeState, house: scopeHouse } = scope

  const load = useCallback(async () => {
    try {
      const res = await getReconciliation({ state: scopeState, house: scopeHouse, limit: 15 })
      setData(res)
      setError("")
    } catch (e) {
      setError(e.message || "Could not load cross-dataset reconciliation.")
    }
  }, [scopeState, scopeHouse])

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await getReconciliation({ state: scopeState, house: scopeHouse, limit: 15 })
        if (alive) { setData(res); setError("") }
      } catch (e) {
        if (alive) setError(e.message || "Could not load cross-dataset reconciliation.")
      }
    })()
    return () => { alive = false }
  }, [scopeState, scopeHouse])

  async function decide(caseKey, decision) {
    setBusyKey(caseKey)
    try {
      await reviewReconciliation(caseKey, { decision, note: note || null })
      setNote("")
      await load()
    } catch (e) {
      setError(e.message || "Could not save the review decision.")
    } finally {
      setBusyKey("")
    }
  }

  const selectCls =
    "rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 text-[0.8rem] text-gray-900 shadow-2xs outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white"

  return (
    <div className="space-y-3">
      <section className="rounded-xl border border-gray-200 bg-white p-3.5 dark:border-gray-700 dark:bg-[#000000]">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h4 className="text-[0.72rem] font-bold uppercase tracking-wider text-gray-400">Cross-dataset reconciliation</h4>
            <p className="mt-0.5 text-[0.8rem] text-gray-500 dark:text-gray-400">
              Projects, recommended works, expenditure, completed works and import records compared by identifier first,
              then by validated description matching. Source rows are shown unchanged.
            </p>
          </div>
          <div className="flex gap-1.5">
            <input
              value={scope.state}
              onChange={(e) => setScope((s) => ({ ...s, state: e.target.value }))}
              placeholder="State (optional)"
              className={selectCls}
              aria-label="Filter by state"
            />
            <select value={scope.house} onChange={(e) => setScope((s) => ({ ...s, house: e.target.value }))} className={selectCls} aria-label="Filter by House">
              <option value="">Both Houses</option>
              <option value="Lok Sabha">Lok Sabha</option>
              <option value="Rajya Sabha">Rajya Sabha</option>
            </select>
          </div>
        </div>

        {data?.counts && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {Object.entries(data.counts).map(([k, v]) => (
              <span key={k} className="rounded-sm bg-gray-100 px-2 py-0.5 text-[0.68rem] font-semibold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                {k.replace(/_/g, " ")}: {v}
              </span>
            ))}
          </div>
        )}
      </section>

      {error && (
        <p className="rounded-lg border border-red-200 bg-red-50 p-2.5 text-[0.82rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">{error}</p>
      )}

      {!data && !error && <p className="text-[0.85rem] text-gray-400">Comparing datasets…</p>}

      {data && (data.cases || []).length === 0 && (
        <p className="rounded-lg bg-gray-50 p-3 text-[0.85rem] text-gray-500 dark:bg-gray-900/40 dark:text-gray-400">
          No discrepancy of the supported checks was found in this scope.
        </p>
      )}

      {(data?.cases || []).map((c) => (
        <section key={c.case_key} className={`rounded-xl border bg-white p-3 dark:bg-[#000000] ${SEVERITY_TONE[c.severity] || SEVERITY_TONE.low}`}>
          <div className="flex flex-wrap items-start justify-between gap-1.5">
            <div className="min-w-0">
              <p className="text-[0.9rem] font-bold text-gray-900 dark:text-white">{c.title}</p>
              <p className="text-[0.7rem] text-gray-400">{c.check.replace(/_/g, " ")}</p>
            </div>
            <div className="flex flex-wrap gap-1">
              <span className={`rounded-sm px-1.5 py-0.5 text-[0.65rem] font-bold ${CONFIDENCE_TONE[c.confidence] || CONFIDENCE_TONE.uncertain}`}>
                {c.confidence} match
              </span>
              <span className="rounded-sm bg-gray-100 px-1.5 py-0.5 text-[0.65rem] font-bold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                {c.severity}
              </span>
            </div>
          </div>

          <p className="mt-1 text-[0.82rem] leading-snug text-gray-600 dark:text-gray-300">{c.explanation}</p>

          <div className="mt-2 space-y-1">
            {(c.records || []).map((r, i) => (
              <div key={`${r.dataset}-${i}`} className="rounded-sm border border-gray-200 bg-gray-50 px-2 py-1.5 dark:border-gray-700 dark:bg-gray-900/40">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="rounded-sm bg-gray-200 px-1.5 py-0.5 text-[0.62rem] font-bold uppercase text-gray-500 dark:bg-gray-700 dark:text-gray-300">{r.dataset}</span>
                  <code className="break-all text-[0.72rem] font-semibold text-blue-700 dark:text-blue-300">{r.reference}</code>
                </div>
                <div className="mt-0.5 flex flex-wrap gap-x-2.5 gap-y-0.5">
                  {Object.entries(r.fields || {}).map(([k, v]) => (
                    <span key={k} className="text-[0.72rem] text-gray-500 dark:text-gray-400">
                      <span className="font-semibold">{k}:</span> {String(v ?? "—")}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>

          {c.review ? (
            <p className="mt-2 rounded-sm bg-blue-50 px-2 py-1 text-[0.75rem] text-blue-700 dark:bg-blue-950/30 dark:text-blue-300">
              {c.review.decision} by {c.review.reviewer || "auditor"}
              {c.review.reviewed_at ? ` · ${String(c.review.reviewed_at).replace("T", " ").slice(0, 16)}` : ""}
              {c.review.note ? ` — ${c.review.note}` : ""}
            </p>
          ) : canReview ? (
            <div className="mt-2 flex flex-col gap-1.5 sm:flex-row sm:items-center">
              <input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Review note (optional)"
                className="flex-1 rounded-lg border border-gray-300 bg-white px-2 py-1 text-[0.78rem] text-gray-900 outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white"
              />
              <div className="flex gap-1">
                {DECISIONS.map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    disabled={busyKey === c.case_key}
                    onClick={() => decide(c.case_key, value)}
                    className="rounded-lg border border-gray-300 px-2 py-1 text-[0.72rem] font-bold text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <p className="mt-2 text-[0.72rem] italic text-gray-400">Auditor review required to record a decision.</p>
          )}
        </section>
      ))}

      {(data?.limitations || []).map((l) => (
        <p key={l} className="text-[0.75rem] text-gray-500 dark:text-gray-400">{l}</p>
      ))}
    </div>
  )
}
