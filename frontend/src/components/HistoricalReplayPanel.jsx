import { useEffect, useMemo, useState } from "react"
import {
  getHistoryComparison,
  getHistoryDashboard,
  getHistorySnapshots,
} from "../services/api"

/* Historical dataset replay.
   Every figure below comes from /history/*, which aggregates the snapshots
   written by committed imports. Nothing is recomputed from the live project
   table, and a checkpoint is labelled as an import state — not a daily one. */

const CARD = "rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-[#000000]"
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

function num(value) {
  if (value === null || value === undefined) return "—"
  const n = Number(value)
  return Number.isFinite(n) ? n.toLocaleString("en-IN") : "—"
}

function pct(value) {
  if (value === null || value === undefined) return "—"
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(1)}%` : "—"
}

function signedMoney(value) {
  if (value === null || value === undefined) return "—"
  if (value === 0) return money(0)
  return `${value > 0 ? "+" : "−"}${money(Math.abs(value))}`
}

function signedNum(value, digits = 0) {
  if (value === null || value === undefined) return "—"
  if (value === 0) return digits ? (0).toFixed(digits) : "0"
  const body = Math.abs(value).toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
  return `${value > 0 ? "+" : "−"}${body}`
}

function when(value) {
  if (!value) return "unknown time"
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })
}

function checkpointLabel(item) {
  return `Import #${item.batch_id} · ${when(item.imported_at)} · ${num(item.project_count)} projects`
}

export default function HistoricalReplayPanel({ house = "All", className = "mx-4 mt-4 sm:mx-6" }) {
  const [expanded, setExpanded] = useState(true)
  const [checkpoints, setCheckpoints] = useState(null)
  const [listError, setListError] = useState(null)
  const [selected, setSelected] = useState("")
  const [compare, setCompare] = useState(false)
  const [fromId, setFromId] = useState("")
  const [toId, setToId] = useState("")
  const [dashboard, setDashboard] = useState(null)
  const [dashLoading, setDashLoading] = useState(false)
  const [dashError, setDashError] = useState(null)
  const [comparison, setComparison] = useState(null)
  const [cmpLoading, setCmpLoading] = useState(false)
  const [cmpError, setCmpError] = useState(null)

  const scope = house === "Lok Sabha" || house === "Rajya Sabha" ? house : "All"
  const items = useMemo(() => checkpoints?.items || [], [checkpoints])

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        const payload = await getHistorySnapshots()
        if (cancelled) return
        setCheckpoints(payload)
        const available = payload?.items || []
        if (available.length) {
          setSelected(String(available[available.length - 1].batch_id))
          setFromId(String(available[0].batch_id))
          setToId(String(available[available.length - 1].batch_id))
        }
      } catch {
        if (!cancelled) {
          setCheckpoints(null)
          setListError("Historical checkpoints could not be loaded right now.")
        }
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!expanded || compare || !selected) return undefined
    let cancelled = false
    const run = async () => {
      await Promise.resolve()
      if (cancelled) return
      setDashLoading(true)
      setDashError(null)
      try {
        const payload = await getHistoryDashboard({
          batch_id: Number(selected),
          ...(scope === "All" ? {} : { house: scope }),
        })
        if (!cancelled) setDashboard(payload)
      } catch {
        if (!cancelled) {
          setDashboard(null)
          setDashError("Historical metrics could not be loaded for this checkpoint.")
        }
      } finally {
        if (!cancelled) setDashLoading(false)
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [expanded, compare, selected, scope])

  useEffect(() => {
    if (!expanded || !compare || !fromId || !toId) return undefined
    if (Number(fromId) >= Number(toId)) return undefined
    let cancelled = false
    const run = async () => {
      await Promise.resolve()
      if (cancelled) return
      setCmpLoading(true)
      setCmpError(null)
      try {
        const payload = await getHistoryComparison({
          from_batch: Number(fromId),
          to_batch: Number(toId),
          ...(scope === "All" ? {} : { house: scope }),
        })
        if (!cancelled) setComparison(payload)
      } catch {
        if (!cancelled) {
          setComparison(null)
          setCmpError("These checkpoints could not be compared.")
        }
      } finally {
        if (!cancelled) setCmpLoading(false)
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [expanded, compare, fromId, toId, scope])

  const selectedItem = useMemo(
    () => items.find((item) => String(item.batch_id) === String(selected)) || null,
    [items, selected]
  )
  const totals = dashboard?.totals
  const stateMax = useMemo(
    () => Math.max(1, ...(dashboard?.state_distribution || []).map((row) => row.projects || 0)),
    [dashboard]
  )

  const header = (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <p className={LABEL}>Historical dataset replay</p>
          <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[0.625rem] font-bold uppercase tracking-wide text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-300">
            Import checkpoint, not a daily state
          </span>
          <span className="rounded-full border border-gray-200 px-2 py-0.5 text-[0.625rem] font-bold uppercase tracking-wide text-gray-500 dark:border-gray-700 dark:text-gray-400">
            {scope === "All" ? "All houses" : scope}
          </span>
        </div>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
          Rebuilt from the snapshots each committed project import recorded. Figures are the
          dataset at that import, never today&apos;s values.
          {checkpoints?.earliest_available
            ? ` Replay starts ${when(checkpoints.earliest_available)}.`
            : ""}
        </p>
      </div>
      <button
        type="button"
        onClick={() => setExpanded((previous) => !previous)}
        className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs font-semibold text-gray-600 transition hover:border-blue-400 hover:text-blue-600 dark:border-gray-700 dark:text-gray-300"
        aria-expanded={expanded}
      >
        {expanded ? "Hide replay" : "Show replay"}
      </button>
    </div>
  )

  const shell = (body) => (
    <section className={className}>
      <div className={CARD}>
        {header}
        {expanded && <div className="mt-3">{body}</div>}
      </div>
    </section>
  )

  if (listError) {
    return shell(
      <p className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300">
        {listError}
      </p>
    )
  }

  if (!checkpoints) {
    return shell(<p className="text-xs text-gray-500 dark:text-gray-400">Loading checkpoints…</p>)
  }

  if (!items.length) {
    return shell(
      <div className="rounded-lg border border-gray-200 bg-gray-50/60 p-3 dark:border-gray-700 dark:bg-[#0b0b0b]">
        <p className="text-xs font-semibold text-gray-700 dark:text-gray-200">No replayable checkpoint yet</p>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
          Historical replay begins with the first project import this system records. Earlier CSV
          loads were never captured, so nothing is reconstructed for them — and an empty checkpoint
          is shown as unavailable rather than as zero projects.
        </p>
      </div>
    )
  }

  const singleBody = (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-gray-600 dark:text-gray-300">
          <span className={`${LABEL} block`}>Dataset checkpoint</span>
          <select
            className={`${SELECT} mt-1`}
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
            aria-label="Select a historical dataset checkpoint"
          >
            {items.map((item) => (
              <option key={item.batch_id} value={item.batch_id}>
                {checkpointLabel(item)}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => setSelected(String(checkpoints.latest_batch_id))}
          disabled={!checkpoints.latest_batch_id || String(selected) === String(checkpoints.latest_batch_id)}
          className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs font-semibold text-gray-600 transition enabled:hover:border-blue-400 enabled:hover:text-blue-600 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300"
        >
          Latest dataset
        </button>
        <button
          type="button"
          onClick={() => setCompare(true)}
          disabled={items.length < 2}
          className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs font-semibold text-gray-600 transition enabled:hover:border-blue-400 enabled:hover:text-blue-600 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300"
        >
          Compare two checkpoints
        </button>
        {selectedItem && (
          <p className="ml-auto text-[0.6875rem] text-gray-400">
            {selectedItem.filename || "dataset"}
            {selectedItem.dataset_version ? ` · dataset v${selectedItem.dataset_version}` : ""}
            {selectedItem.house_coverage ? ` · ${selectedItem.house_coverage}` : ""}
          </p>
        )}
      </div>

      {dashLoading && <p className="text-xs text-gray-500 dark:text-gray-400">Rebuilding metrics from the checkpoint…</p>}
      {dashError && (
        <p className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300">
          {dashError}
        </p>
      )}

      {!dashLoading && dashboard && !dashboard.available && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200">
          {dashboard.reason || "This checkpoint holds no records for the current house filter."}
        </p>
      )}

      {dashboard?.available && totals && (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
            {[
              { label: "Recorded projects", value: num(totals.projects) },
              { label: "Sanctioned", value: money(totals.sanctioned_amount), missing: totals.records_without_sanctioned },
              { label: "Expenditure", value: money(totals.expenditure), missing: totals.records_without_expenditure },
              { label: "Average completion", value: pct(totals.average_completion), missing: totals.records_without_completion },
              { label: "Completion ≥ 100%", value: num(totals.completed_projects) },
            ].map((tile) => (
              <div key={tile.label} className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                <p className={LABEL}>{tile.label}</p>
                <p className="mt-1 text-base font-semibold text-gray-900 dark:text-gray-100">{tile.value}</p>
                {tile.missing > 0 && (
                  <p className="mt-0.5 text-[0.625rem] text-gray-400">
                    {num(tile.missing)} not recorded
                  </p>
                )}
              </div>
            ))}
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <div>
              <p className={LABEL}>State distribution (checkpoint)</p>
              <ul className="mt-2 space-y-1.5">
                {(dashboard.state_distribution || []).slice(0, 8).map((row) => (
                  <li key={row.state} className="text-xs">
                    <div className="flex items-center justify-between gap-2 text-gray-600 dark:text-gray-300">
                      <span className="truncate">{row.state}</span>
                      <span className="font-semibold text-gray-900 dark:text-gray-100">{num(row.projects)}</span>
                    </div>
                    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
                      <div
                        className="h-full rounded-full bg-[#8b2233]"
                        style={{ width: `${Math.max(2, (row.projects / stateMax) * 100)}%` }}
                      />
                    </div>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <p className={LABEL}>Status distribution (checkpoint)</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {(dashboard.status_distribution || []).map((row) => (
                  <span
                    key={row.status}
                    className="rounded-full border border-gray-200 px-2 py-1 text-[0.6875rem] text-gray-600 dark:border-gray-700 dark:text-gray-300"
                  >
                    {row.status}: <strong className="text-gray-900 dark:text-gray-100">{num(row.projects)}</strong>
                  </span>
                ))}
              </div>
              <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50/70 p-2.5 dark:border-amber-900/50 dark:bg-amber-950/20">
                <p className="text-[0.625rem] font-bold uppercase tracking-wider text-amber-700 dark:text-amber-300">
                  Risk distribution unavailable
                </p>
                <p className="mt-1 text-[0.6875rem] text-amber-900 dark:text-amber-200">{dashboard.risk?.reason}</p>
              </div>
            </div>
          </div>

          <p className="text-[0.6875rem] text-gray-400">
            {(dashboard.limitations || []).join(" ")}
          </p>
        </>
      )}
    </div>
  )

  const compareBody = (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-gray-600 dark:text-gray-300">
          <span className={`${LABEL} block`}>Earlier checkpoint</span>
          <select
            className={`${SELECT} mt-1`}
            value={fromId}
            onChange={(event) => setFromId(event.target.value)}
            aria-label="Earlier checkpoint"
          >
            {items.map((item) => (
              <option key={item.batch_id} value={item.batch_id}>
                {checkpointLabel(item)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-gray-600 dark:text-gray-300">
          <span className={`${LABEL} block`}>Later checkpoint</span>
          <select
            className={`${SELECT} mt-1`}
            value={toId}
            onChange={(event) => setToId(event.target.value)}
            aria-label="Later checkpoint"
          >
            {items.map((item) => (
              <option key={item.batch_id} value={item.batch_id}>
                {checkpointLabel(item)}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => setCompare(false)}
          className="rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs font-semibold text-gray-600 transition hover:border-blue-400 hover:text-blue-600 dark:border-gray-700 dark:text-gray-300"
        >
          Back to one checkpoint
        </button>
      </div>

      {Number(fromId) >= Number(toId) && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200">
          Choose an earlier import first and a later import second.
        </p>
      )}
      {cmpLoading && <p className="text-xs text-gray-500 dark:text-gray-400">Comparing recorded snapshots…</p>}
      {cmpError && (
        <p className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300">
          {cmpError}
        </p>
      )}

      {!cmpLoading && comparison?.available && (
        <>
          <p className="text-xs text-gray-600 dark:text-gray-300">
            Import #{comparison.from.batch_id} ({when(comparison.from.imported_at)}) → import #
            {comparison.to.batch_id} ({when(comparison.to.imported_at)}) · {comparison.house} scope
          </p>

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            {[
              ["Added records", comparison.counts.added],
              ["Missing from later", comparison.counts.missing],
              ["Changed records", comparison.counts.changed],
              ["Expenditure changed", comparison.counts.expenditure_changed],
              ["Completion changed", comparison.counts.completion_changed],
              ["Status changed", comparison.counts.status_changed],
            ].map(([label, value]) => (
              <div key={label} className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                <p className={LABEL}>{label}</p>
                <p className="mt-1 text-base font-semibold text-gray-900 dark:text-gray-100">{num(value)}</p>
              </div>
            ))}
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="text-gray-400">
                  <th className="py-1 font-semibold">Aggregate metric</th>
                  <th className="py-1 text-right font-semibold">Earlier</th>
                  <th className="py-1 text-right font-semibold">Later</th>
                  <th className="py-1 text-right font-semibold">Difference</th>
                </tr>
              </thead>
              <tbody className="text-gray-700 dark:text-gray-200">
                {[
                  ["Recorded projects", comparison.from.projects, comparison.to.projects, comparison.metric_deltas.projects, (v) => num(v), (d) => signedNum(d)],
                  ["Sanctioned amount", null, null, comparison.metric_deltas.sanctioned_amount, money, signedMoney],
                  ["Expenditure", null, null, comparison.metric_deltas.expenditure, money, signedMoney],
                  ["Average completion", null, null, comparison.metric_deltas.average_completion, pct, (d) => `${signedNum(d, 1)} pts`],
                  ["Completed projects", null, null, comparison.metric_deltas.completed_projects, num, (d) => signedNum(d)],
                ].map(([label, fromValue, toValue, delta, fmt, fmtDelta]) => (
                  <tr key={label} className="border-t border-gray-100 dark:border-gray-800">
                    <td className="py-1.5">{label}</td>
                    <td className="py-1.5 text-right">{fromValue === null ? "—" : fmt(fromValue)}</td>
                    <td className="py-1.5 text-right">{toValue === null ? "—" : fmt(toValue)}</td>
                    <td className="py-1.5 text-right font-semibold">{fmtDelta(delta)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {comparison.samples?.length > 0 && (
            <div className="overflow-x-auto">
              <p className={`${LABEL} mb-1`}>Changed records (exact values)</p>
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="text-gray-400">
                    <th className="py-1 font-semibold">Project</th>
                    <th className="py-1 text-right font-semibold">Expenditure</th>
                    <th className="py-1 text-right font-semibold">Completion</th>
                    <th className="py-1 font-semibold">Status</th>
                  </tr>
                </thead>
                <tbody className="text-gray-700 dark:text-gray-200">
                  {comparison.samples.map((row) => (
                    <tr key={row.project_id} className="border-t border-gray-100 dark:border-gray-800">
                      <td className="max-w-[18rem] truncate py-1.5">
                        #{row.project_id} {row.project_name || ""}
                      </td>
                      <td className="py-1.5 text-right">
                        {money(row.expenditure.from)} → <strong>{money(row.expenditure.to)}</strong>
                      </td>
                      <td className="py-1.5 text-right">
                        {pct(row.completion.from)} → <strong>{pct(row.completion.to)}</strong>
                      </td>
                      <td className="py-1.5">
                        {row.status.from || "—"} → <strong>{row.status.to || "—"}</strong>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {comparison.sample_truncated && (
                <p className="mt-1 text-[0.6875rem] text-gray-400">
                  Showing the first {comparison.samples.length} changed records; counts above cover all of them.
                </p>
              )}
            </div>
          )}

          <p className="text-[0.6875rem] text-gray-400">{(comparison.limitations || []).join(" ")}</p>
        </>
      )}
    </div>
  )

  return shell(compare ? compareBody : singleBody)
}
