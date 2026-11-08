import { useCallback, useEffect, useState } from "react"
import {
  getInvestigationMemory, addInvestigationMemory, updateInvestigationMemory,
} from "../services/api"
import { useAuth } from "../context/AuthContext"

/* ═══════════════════════════════════════════════════════════════════
   FEATURE 5 — AI investigation memory
   Persistent investigation context, kept strictly separate by origin:
   auditor-confirmed findings, auditor-dismissed findings with reasons,
   and AI suggestions that remain suggestions. Auditors can confirm or
   remove memory; analysts can add context but not confirm it.
   ═══════════════════════════════════════════════════════════════════ */

const ORIGIN_TONE = {
  "auditor-confirmed": "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  "auditor-dismissed": "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300",
  "ai-suggested": "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
}

function FindingList({ title, items, empty }) {
  return (
    <div>
      <p className="mb-1 text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">{title}</p>
      {(items || []).length === 0 ? (
        <p className="rounded-lg bg-gray-50 px-2 py-1.5 text-[0.78rem] text-gray-400 dark:bg-gray-900/40">{empty}</p>
      ) : (
        <div className="space-y-1.5">
          {items.map((f) => (
            <div key={`${f.key || f.id}-${f.origin}`} className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-[#0d090b]">
              <div className="flex flex-wrap items-center justify-between gap-1">
                <p className="text-[0.83rem] font-bold text-gray-900 dark:text-white">{f.title}</p>
                <span className={`rounded-sm px-1.5 py-0.5 text-[0.62rem] font-bold ${ORIGIN_TONE[f.origin] || ORIGIN_TONE["ai-suggested"]}`}>
                  {f.origin}
                </span>
              </div>
              {f.explanation && <p className="mt-0.5 text-[0.78rem] leading-snug text-gray-600 dark:text-gray-300">{f.explanation}</p>}
              {f.reviewer && (
                <p className="mt-0.5 text-[0.7rem] text-gray-400">
                  reviewed by {f.reviewer}{f.reviewer_note ? ` — ${f.reviewer_note}` : ""}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function InvestigationMemoryPanel({ projectId }) {
  const { hasRole } = useAuth()
  const canConfirm = hasRole("auditor")

  const [data, setData] = useState(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState({ kind: "note", title: "", body: "" })

  const load = useCallback(async () => {
    try {
      const res = await getInvestigationMemory(projectId)
      setData(res)
      setError("")
    } catch (e) {
      setError(e.message || "Could not load investigation memory.")
    }
  }, [projectId])

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await getInvestigationMemory(projectId)
        if (alive) { setData(res); setError("") }
      } catch (e) {
        if (alive) setError(e.message || "Could not load investigation memory.")
      }
    })()
    return () => { alive = false }
  }, [projectId])

  async function submit(e) {
    e.preventDefault()
    if (!draft.body.trim() && !draft.title.trim()) return
    setBusy(true)
    try {
      await addInvestigationMemory(projectId, {
        kind: draft.kind,
        title: draft.title.trim() || null,
        body: draft.body.trim() || null,
        source: "investigation workspace",
      })
      setDraft({ kind: "note", title: "", body: "" })
      await load()
    } catch (err) {
      setError(err.message || "Could not save the memory entry.")
    } finally {
      setBusy(false)
    }
  }

  async function patch(entryId, payload) {
    setBusy(true)
    try {
      await updateInvestigationMemory(entryId, payload)
      await load()
    } catch (err) {
      setError(err.message || "Could not update the memory entry.")
    } finally {
      setBusy(false)
    }
  }

  if (error) {
    return <p className="rounded-lg border border-red-200 bg-red-50 p-2.5 text-[0.82rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">{error}</p>
  }
  if (!data) return <p className="text-[0.85rem] text-gray-400">Loading stored investigation context…</p>

  const counts = data.counts || {}
  const inputCls =
    "w-full rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 text-[0.82rem] text-gray-900 outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white"

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          ["Confirmed", counts.confirmed],
          ["Dismissed", counts.dismissed],
          ["AI suggestions", counts.ai_suggested],
          ["Open questions", counts.unresolved_questions],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
            <p className="text-[0.65rem] font-bold uppercase tracking-wider text-gray-400">{label}</p>
            <p className="font-mono text-[1rem] font-bold text-gray-900 dark:text-white">{value ?? 0}</p>
          </div>
        ))}
      </div>

      <FindingList title="Auditor-confirmed findings" items={data.confirmed_findings} empty="No finding has been accepted by an auditor yet." />
      <FindingList title="Dismissed findings and reasons" items={data.dismissed_findings} empty="No finding has been dismissed." />
      <FindingList title="AI suggestions — not verified" items={data.ai_suggested} empty="No unreviewed AI suggestions are stored." />

      {(data.unresolved_questions || []).length > 0 && (
        <div>
          <p className="mb-1 text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Unresolved investigation questions</p>
          <ul className="list-inside list-disc space-y-0.5 text-[0.8rem] text-gray-600 dark:text-gray-300">
            {data.unresolved_questions.map((q, i) => (
              <li key={i}>{typeof q === "string" ? q : q.question || q.label || JSON.stringify(q)}</li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <p className="mb-1 text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Stored notes and context</p>
        {(data.entries || []).length === 0 ? (
          <p className="rounded-lg bg-gray-50 px-2 py-1.5 text-[0.78rem] text-gray-400 dark:bg-gray-900/40">No stored memory entries for this project.</p>
        ) : (
          <div className="space-y-1.5">
            {data.entries.map((e) => (
              <div key={e.id} className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-[#0d090b]">
                <div className="flex flex-wrap items-center justify-between gap-1">
                  <p className="text-[0.83rem] font-bold text-gray-900 dark:text-white">{e.title || e.kind}</p>
                  <div className="flex flex-wrap gap-1">
                    <span className={`rounded-sm px-1.5 py-0.5 text-[0.62rem] font-bold ${e.confirmed ? ORIGIN_TONE["auditor-confirmed"] : ORIGIN_TONE["ai-suggested"]}`}>
                      {e.confirmed ? "auditor-confirmed" : `${e.origin} · unconfirmed`}
                    </span>
                    <span className="rounded-sm bg-gray-100 px-1.5 py-0.5 text-[0.62rem] font-bold text-gray-500 dark:bg-gray-800 dark:text-gray-300">{e.kind}</span>
                  </div>
                </div>
                {e.body && <p className="mt-0.5 whitespace-pre-line text-[0.8rem] text-gray-600 dark:text-gray-300">{e.body}</p>}
                <div className="mt-1 flex flex-wrap items-center justify-between gap-1">
                  <p className="text-[0.68rem] text-gray-400">
                    {e.created_by || "unknown"} · {String(e.created_at || "").replace("T", " ").slice(0, 16)}
                    {e.source ? ` · ${e.source}` : ""}
                  </p>
                  {canConfirm && (
                    <div className="flex gap-1">
                      {!e.confirmed && (
                        <button type="button" disabled={busy} onClick={() => patch(e.id, { confirmed: true })}
                          className="rounded-sm border border-green-300 px-1.5 py-0.5 text-[0.68rem] font-bold text-green-700 hover:bg-green-50 disabled:opacity-50 dark:border-green-800 dark:text-green-300 dark:hover:bg-green-950/30">
                          Confirm
                        </button>
                      )}
                      <button type="button" disabled={busy} onClick={() => patch(e.id, { status: "removed" })}
                        className="rounded-sm border border-gray-300 px-1.5 py-0.5 text-[0.68rem] font-bold text-gray-600 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                        Remove
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <form onSubmit={submit} className="rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-[#000000]">
        <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Add investigation context</p>
        <div className="mt-1.5 flex flex-col gap-1.5 sm:flex-row">
          <select value={draft.kind} onChange={(e) => setDraft((d) => ({ ...d, kind: e.target.value }))} className={`${inputCls} sm:w-36`} aria-label="Entry kind">
            <option value="note">Note</option>
            <option value="question">Open question</option>
            <option value="pattern">Pattern</option>
            <option value="evidence">Evidence reference</option>
            <option value="finding">Finding context</option>
          </select>
          <input value={draft.title} onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))} placeholder="Title" className={`${inputCls} sm:w-56`} />
        </div>
        <textarea
          value={draft.body}
          onChange={(e) => setDraft((d) => ({ ...d, body: e.target.value }))}
          rows={2}
          placeholder="What should a future reviewer know about this investigation?"
          className={`${inputCls} mt-1.5`}
        />
        <div className="mt-1.5 flex items-center justify-between gap-2">
          <p className="text-[0.7rem] text-gray-400">
            Added as {canConfirm ? "auditor" : "analyst"} context; it stays unconfirmed until an auditor confirms it.
          </p>
          <button type="submit" disabled={busy}
            className="rounded-lg bg-blue-600 px-3 py-1.5 text-[0.78rem] font-bold text-white hover:bg-blue-700 disabled:opacity-50">
            {busy ? "Saving…" : "Save context"}
          </button>
        </div>
      </form>

      <div className="space-y-1">
        {(data.rules || []).map((r) => (
          <p key={r} className="text-[0.72rem] text-gray-500 dark:text-gray-400">• {r}</p>
        ))}
      </div>

      {(data.related_investigations || []).length > 0 && (
        <div>
          <p className="mb-1 text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Related investigations (same state and category)</p>
          <div className="flex flex-wrap gap-1.5">
            {data.related_investigations.map((r) => (
              <span key={r.project_id} className="rounded-lg border border-gray-200 px-2 py-1 text-[0.72rem] text-gray-600 dark:border-gray-700 dark:text-gray-300">
                #{r.project_id} · {r.name} · {r.investigation_status}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
