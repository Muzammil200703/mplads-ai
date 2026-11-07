import { useEffect, useMemo, useState } from "react"
import HistoricalProjectSnapshot from "./HistoricalProjectSnapshot"
import { getProjectChanges } from "../services/api"

/* Change History for one project.
   Every row comes from /changes/projects/{id}: a recorded import, a recorded
   before value and a recorded after value. Nothing here is derived by the
   client, and "missing from import" is never rendered as "cancelled". */

const TYPE_META = {
  new_project: { label: "New project", className: "bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950/40 dark:text-blue-300 dark:border-blue-900/60" },
  updated_project: { label: "Updated", className: "bg-gray-50 text-gray-700 border-gray-200 dark:bg-gray-900 dark:text-gray-300 dark:border-gray-700" },
  financial_change: { label: "Financial", className: "bg-amber-50 text-amber-800 border-amber-200 dark:bg-amber-950/30 dark:text-amber-300 dark:border-amber-900/50" },
  completion_change: { label: "Completion", className: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/30 dark:text-emerald-300 dark:border-emerald-900/50" },
  status_change: { label: "Status", className: "bg-purple-50 text-purple-700 border-purple-200 dark:bg-purple-950/30 dark:text-purple-300 dark:border-purple-900/50" },
  missing_from_import: { label: "Missing from import", className: "bg-red-50 text-red-700 border-red-200 dark:bg-red-950/30 dark:text-red-300 dark:border-red-900/50" },
  reappeared: { label: "Reappeared", className: "bg-teal-50 text-teal-700 border-teal-200 dark:bg-teal-950/30 dark:text-teal-300 dark:border-teal-900/50" },
}

const FIELD_OPTIONS = [
  ["", "All fields"],
  ["project_name", "Description"],
  ["sanctioned_amount", "Sanctioned amount"],
  ["expenditure", "Expenditure"],
  ["completion_percentage", "Completion"],
  ["status", "Status"],
  ["project_type", "Category"],
  ["state", "State"],
  ["district", "District"],
  ["constituency", "Constituency"],
  ["house", "House"],
]

const selectClass =
  "rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-xs text-gray-700 outline-none focus:border-blue-500 dark:border-gray-700 dark:bg-[#000000] dark:text-gray-200"

function formatDate(value) {
  if (!value) return "—"
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" })
}

export default function ChangeHistoryPanel({ projectId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [field, setField] = useState("")
  const [type, setType] = useState("")

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      await Promise.resolve()
      if (cancelled) return
      setLoading(true)
      setError(null)
      try {
        const payload = await getProjectChanges(projectId)
        if (!cancelled) setData(payload)
      } catch {
        if (!cancelled) {
          setData(null)
          setError("Change history could not be loaded right now.")
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [projectId])

  const events = data?.events || []
  const filtered = useMemo(
    () =>
      events.filter(
        (e) => (!field || e.field === field) && (!type || e.change_type === type)
      ),
    [events, field, type]
  )
  const types = useMemo(() => Array.from(new Set(events.map((e) => e.change_type))), [events])

  if (loading) {
    return (
      <div className="rounded-xl border border-gray-200 p-4 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
        Loading change history…
      </div>
    )
  }

  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300">
        {error}
      </div>
    )
  }

  if (!data?.has_history) {
    return (
      <div className="rounded-xl border border-gray-200 bg-gray-50/60 p-4 dark:border-gray-700 dark:bg-[#000000]">
        <p className="text-xs font-bold uppercase tracking-wider text-gray-400">No recorded changes yet</p>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
          History begins with the first dataset import captured by this system. Previous CSV
          loads were not recorded, so nothing is reconstructed for them.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">
            Recorded change history
          </p>
          <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
            {data.count} event{data.count === 1 ? "" : "s"} across {Object.keys(data.batches || {}).length} import
            {Object.keys(data.batches || {}).length === 1 ? "" : "s"} · every value below was recorded by an import.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <select className={selectClass} value={field} onChange={(e) => setField(e.target.value)} aria-label="Filter by field">
            {FIELD_OPTIONS.map(([value, label]) => (
              <option key={value || "all"} value={value}>{label}</option>
            ))}
          </select>
          <select className={selectClass} value={type} onChange={(e) => setType(e.target.value)} aria-label="Filter by change type">
            <option value="">All change types</option>
            {types.map((t) => (
              <option key={t} value={t}>{TYPE_META[t]?.label || t.replace(/_/g, " ")}</option>
            ))}
          </select>
        </div>
      </div>

      <HistoricalProjectSnapshot projectId={projectId} />

      {data.highlights?.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/70 p-3 dark:border-amber-900/50 dark:bg-amber-950/20">
          <p className="text-[0.625rem] font-bold uppercase tracking-wider text-amber-700 dark:text-amber-300">
            Requires attention
          </p>
          <ul className="mt-1 space-y-1 text-xs text-amber-900 dark:text-amber-200">
            {data.highlights.slice(0, 3).map((h) => (
              <li key={h.id}>• {h.impact_detail}</li>
            ))}
          </ul>
        </div>
      )}

      {filtered.length === 0 ? (
        <p className="rounded-xl border border-gray-200 p-3 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
          No changes match these filters.
        </p>
      ) : (
        <ol className="space-y-2">
          {filtered.map((event) => {
            const meta = TYPE_META[event.change_type] || {
              label: event.change_type.replace(/_/g, " "),
              className: "bg-gray-50 text-gray-700 border-gray-200 dark:bg-gray-900 dark:text-gray-300 dark:border-gray-700",
            }
            const batch = data.batches?.[event.batch_id]
            return (
              <li
                key={event.id}
                className="rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-[#000000]"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`rounded-full border px-2 py-0.5 text-[0.625rem] font-bold uppercase tracking-wide ${meta.className}`}>
                    {meta.label}
                  </span>
                  {event.field_label && (
                    <span className="text-xs font-semibold text-gray-700 dark:text-gray-200">{event.field_label}</span>
                  )}
                  {event.confidence === "review" && (
                    <span className="rounded-full border border-orange-200 bg-orange-50 px-2 py-0.5 text-[0.625rem] font-bold text-orange-700 dark:border-orange-900/50 dark:bg-orange-950/30 dark:text-orange-300">
                      Review identity
                    </span>
                  )}
                  <span className="ml-auto text-[0.6875rem] text-gray-400">{formatDate(event.changed_at)}</span>
                </div>

                {(event.previous_value || event.new_value) && (
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-600 line-through decoration-gray-400 dark:bg-gray-900 dark:text-gray-400">
                      {event.previous_value ?? "not recorded"}
                    </span>
                    <span aria-hidden="true" className="text-gray-400">→</span>
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 font-semibold text-gray-900 dark:bg-gray-900 dark:text-gray-100">
                      {event.new_value ?? "not recorded"}
                    </span>
                  </div>
                )}

                {event.impact_detail && (
                  <p className="mt-2 text-xs text-gray-600 dark:text-gray-300">{event.impact_detail}</p>
                )}

                <p className="mt-2 text-[0.6875rem] text-gray-400">
                  Import #{event.batch_id}
                  {batch?.filename ? ` · ${batch.filename}` : ""}
                  {batch?.dataset_version ? ` · dataset v${batch.dataset_version}` : ""}
                  {event.house ? ` · ${event.house}` : ""}
                </p>
              </li>
            )
          })}
        </ol>
      )}

      <p className="text-[0.6875rem] text-gray-400">
        A project missing from an import is a dataset gap, not a cancellation — it is reported
        that way and never as a deletion.
      </p>
    </div>
  )
}
