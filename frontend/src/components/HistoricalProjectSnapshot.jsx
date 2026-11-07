import { useEffect, useState } from "react"
import { getHistoricalProject, getProjectChangeHistory } from "../services/api"

/* "As recorded in this import" block for one project.
   Values come from the snapshot row that import wrote; the current column comes
   from the live record, and every difference is shown with exact numbers. */

const SELECT =
  "rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-xs text-gray-700 outline-none focus:border-blue-500 dark:border-gray-700 dark:bg-[#000000] dark:text-gray-200"
const LABEL = "text-[0.625rem] font-bold uppercase tracking-wider text-gray-400"

function money(value) {
  if (value === null || value === undefined || value === "") return "—"
  const n = Number(value)
  if (!Number.isFinite(n)) return "—"
  if (Math.abs(n) >= 1e7) return `₹${(n / 1e7).toFixed(2)} Cr`
  if (Math.abs(n) >= 1e5) return `₹${(n / 1e5).toFixed(2)} L`
  return `₹${n.toLocaleString("en-IN")}`
}

function pct(value) {
  if (value === null || value === undefined || value === "") return "—"
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(1)}%` : "—"
}

function when(value) {
  if (!value) return "unknown time"
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit" })
}

function deltaText(diff) {
  if (diff.delta === undefined || diff.delta === null) return null
  if (diff.field === "sanctioned_amount" || diff.field === "expenditure") {
    const sign = diff.delta > 0 ? "+" : diff.delta < 0 ? "−" : ""
    return `${sign}${money(Math.abs(diff.delta))}`
  }
  if (diff.field === "completion_percentage") {
    const sign = diff.delta > 0 ? "+" : diff.delta < 0 ? "−" : ""
    return `${sign}${Math.abs(diff.delta).toFixed(1)} pts`
  }
  return null
}

function fieldText(field, value) {
  if (value === null || value === undefined || value === "") return "not recorded"
  if (field === "sanctioned_amount" || field === "expenditure") return money(value)
  if (field === "completion_percentage") return pct(value)
  if (field === "rec_date" || field === "completion_date") return when(value)
  return String(value)
}

export default function HistoricalProjectSnapshot({ projectId }) {
  const [snapshots, setSnapshots] = useState([])
  const [batchId, setBatchId] = useState("")
  const [payload, setPayload] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        const data = await getProjectChangeHistory(projectId)
        if (cancelled) return
        const seen = new Set()
        const unique = (data?.snapshots || []).filter((snapshot) => {
          const key = snapshot.batch_id
          if (key === null || key === undefined || seen.has(key)) return false
          seen.add(key)
          return true
        })
        setSnapshots(unique)
      } catch {
        if (!cancelled) setSnapshots([])
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [projectId])

  useEffect(() => {
    if (!batchId) return undefined
    let cancelled = false
    const run = async () => {
      setLoading(true)
      setError(null)
      try {
        const data = await getHistoricalProject(projectId, Number(batchId))
        if (!cancelled) setPayload(data)
      } catch {
        if (!cancelled) {
          setPayload(null)
          setError("This checkpoint could not be read for this project.")
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [projectId, batchId])

  if (!snapshots.length) return null

  const historical = payload?.historical
  const values = historical
    ? [
        ["Sanctioned amount", fieldText("sanctioned_amount", historical.sanctioned_amount)],
        ["Expenditure", fieldText("expenditure", historical.expenditure)],
        ["Completion", fieldText("completion_percentage", historical.completion_percentage)],
        ["Status", fieldText("status", historical.status)],
        ["Category", fieldText("project_type", historical.project_type)],
        ["House", fieldText("house", historical.house)],
        ["Recommendation date", fieldText("rec_date", historical.rec_date)],
        ["Completion date", fieldText("completion_date", historical.completion_date)],
      ]
    : []

  return (
    <div className="rounded-xl border border-gray-200 bg-gray-50/60 p-3 dark:border-gray-700 dark:bg-[#0b0b0b]">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <p className={LABEL}>Reconstruct at an import</p>
          <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
            The values this project had when a dataset import recorded it — compared with the
            current record. A checkpoint is an import state, not that day&apos;s state.
          </p>
        </div>
        <select
          className={SELECT}
          value={batchId}
          onChange={(event) => {
            const next = event.target.value
            setBatchId(next)
            if (!next) {
              setPayload(null)
              setError(null)
            }
          }}
          aria-label="Select an import to reconstruct this project"
        >
          <option value="">Select an import…</option>
          {snapshots.map((snapshot) => (
            <option key={snapshot.batch_id} value={snapshot.batch_id}>
              Import #{snapshot.batch_id} · {when(snapshot.captured_at)}
            </option>
          ))}
        </select>
      </div>

      {loading && <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">Reading the recorded snapshot…</p>}
      {error && <p className="mt-2 text-xs text-red-600 dark:text-red-400">{error}</p>}

      {!loading && payload && !payload.available && (
        <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-xs text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200">
          {payload.reason}
        </p>
      )}

      {!loading && payload?.available && (
        <div className="mt-3 space-y-3">
          <p className="text-[0.6875rem] text-gray-500 dark:text-gray-400">
            Import #{payload.batch_id}
            {payload.imported_at ? ` · committed ${when(payload.imported_at)}` : ""}
            {payload.filename ? ` · ${payload.filename}` : ""}
          </p>

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {values.map(([label, value]) => (
              <div key={label} className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-[#000000]">
                <p className={LABEL}>{label}</p>
                <p className="mt-0.5 text-xs font-semibold text-gray-900 dark:text-gray-100">{value}</p>
              </div>
            ))}
          </div>

          {payload.differences?.length ? (
            <div className="overflow-x-auto">
              <p className={`${LABEL} mb-1`}>Historical vs current</p>
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="text-gray-400">
                    <th className="py-1 font-semibold">Field</th>
                    <th className="py-1 font-semibold">In this import</th>
                    <th className="py-1 font-semibold">Current</th>
                    <th className="py-1 text-right font-semibold">Difference</th>
                  </tr>
                </thead>
                <tbody className="text-gray-700 dark:text-gray-200">
                  {payload.differences.map((diff) => (
                    <tr key={diff.field} className="border-t border-gray-100 dark:border-gray-800">
                      <td className="py-1.5">{diff.label}</td>
                      <td className="py-1.5">{fieldText(diff.field, diff.historical)}</td>
                      <td className="py-1.5">{fieldText(diff.field, diff.current)}</td>
                      <td className="py-1.5 text-right font-semibold">{deltaText(diff) || "changed"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-[0.6875rem] text-gray-400">
              No tracked field differs between this import and the current record.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
