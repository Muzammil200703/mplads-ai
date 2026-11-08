import { useEffect, useState } from "react"
import {
  getGeoCoverage, getGeoAreas, getGeoSpatial, getGeoOutliers, getGeoProgress, getStates,
} from "../services/api"
import { formatMoney, formatNumber } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════
   PHASE 2 — Geospatial intelligence (Features 14–18)
   Lives inside the existing State Intelligence page. Every payload states
   its aggregation unit, because this dataset records district for only a
   handful of projects: constituency is the finest available geography.
   No project coordinates are invented; only field-verification GPS is exact.
   ═══════════════════════════════════════════════════════════════════ */

const TABS = [
  ["coverage", "Coverage gaps"],
  ["areas", "Execution patterns"],
  ["spatial", "Spatial relations"],
  ["outliers", "Geographic outliers"],
  ["progress", "Progress map"],
]

const fetchers = {
  coverage: (scope) => getGeoCoverage(scope.state || undefined),
  areas: (scope) => getGeoAreas(scope),
  spatial: (scope) => getGeoSpatial(scope),
  outliers: (scope) => getGeoOutliers(scope),
  progress: (scope) => getGeoProgress(scope),
}

function Banner({ text, tone = "blue" }) {
  const tones = {
    blue: "border-blue-200 bg-blue-50 text-blue-800 dark:border-blue-900/40 dark:bg-blue-950/20 dark:text-blue-300",
    amber: "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-300",
  }
  return <div className={`rounded-lg border p-2.5 text-[0.8rem] leading-snug ${tones[tone]}`}>{text}</div>
}

function Bar({ value, max, tone = "bg-blue-500" }) {
  const pct = max > 0 && value != null ? Math.max(0, Math.min(100, (Number(value) / max) * 100)) : 0
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-sm bg-gray-200 dark:bg-gray-700">
      <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  )
}

function AreaTable({ rows, unit, showIndex }) {
  if (!rows?.length) {
    return <p className="rounded-lg bg-gray-50 p-3 text-[0.82rem] text-gray-500 dark:bg-gray-900/40 dark:text-gray-400">No areas match this scope.</p>
  }
  const maxProjects = Math.max(...rows.map((r) => Number(r.projects) || 0), 1)
  return (
    <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
      <table className="min-w-full text-[0.8rem]">
        <thead className="bg-gray-50 text-[0.66rem] uppercase tracking-wider text-gray-500 dark:bg-gray-900/60 dark:text-gray-400">
          <tr>
            <th className="px-2 py-1.5 text-left">{unit}</th>
            <th className="px-2 py-1.5 text-right">Projects</th>
            <th className="px-2 py-1.5 text-right">Completion</th>
            <th className="px-2 py-1.5 text-right">Utilization</th>
            <th className="px-2 py-1.5 text-right">High risk</th>
            {showIndex && <th className="px-2 py-1.5 text-right">Coverage index</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {rows.map((r) => (
            <tr key={r.area} className={r.low_coverage ? "bg-amber-50/60 dark:bg-amber-950/10" : ""}>
              <td className="max-w-[16rem] px-2 py-1.5">
                <div className="flex items-center gap-1.5">
                  <span className="truncate font-semibold text-gray-800 dark:text-gray-200" title={r.area}>{r.area}</span>
                  {r.low_coverage && <span className="shrink-0 rounded-sm bg-amber-100 px-1 py-0.5 text-[0.6rem] font-bold text-amber-700 dark:bg-amber-950 dark:text-amber-300">low</span>}
                  {r.small_sample && <span className="shrink-0 rounded-sm bg-gray-100 px-1 py-0.5 text-[0.6rem] font-bold text-gray-500 dark:bg-gray-800 dark:text-gray-300">small n</span>}
                </div>
                <Bar value={r.projects} max={maxProjects} tone="bg-gray-400 dark:bg-gray-500" />
              </td>
              <td className="px-2 py-1.5 text-right font-mono">{formatNumber(r.projects)}</td>
              <td className="px-2 py-1.5 text-right font-mono">{r.avg_completion != null ? `${Number(r.avg_completion).toFixed(1)}%` : "—"}</td>
              <td className="px-2 py-1.5 text-right font-mono">{r.utilization_pct != null ? `${Number(r.utilization_pct).toFixed(1)}%` : "—"}</td>
              <td className="px-2 py-1.5 text-right font-mono">{r.high_risk ?? 0}</td>
              {showIndex && <td className="px-2 py-1.5 text-right font-mono">{r.coverage_index != null ? r.coverage_index.toFixed(2) : "—"}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Disclosure({ data }) {
  const unit = data?.geo_unit
  return (
    <div className="space-y-1.5">
      {unit?.fallback && <Banner tone="amber" text={unit.explanation} />}
      {(data?.notes || []).map((n) => (
        <p key={n} className="text-[0.78rem] text-gray-500 dark:text-gray-400">• {n}</p>
      ))}
      {(data?.limitations || []).map((l) => (
        <p key={l} className="text-[0.74rem] italic text-gray-400">• {l}</p>
      ))}
    </div>
  )
}

function Coverage({ data }) {
  if (!data?.available) return <Banner tone="amber" text={data?.reason || "Coverage analysis is unavailable for this scope."} />
  if (data.states) {
    return (
      <div className="space-y-2">
        <AreaTable rows={data.states.map((s) => ({ ...s, area: s.state }))} unit="State" showIndex />
        <p className="text-[0.78rem] text-gray-500 dark:text-gray-400">
          Median recorded projects per state: {formatNumber(data.median_projects_per_state || 0)}. Select a state above to compare
          its {data.geo_unit?.label?.toLowerCase() || "area"} coverage.
        </p>
        <Disclosure data={data} />
      </div>
    )
  }
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          [data.unit_label || "Areas", data.areas?.length ?? 0],
          ["Median projects", formatNumber(data.median_projects_per_area || 0)],
          ["Low-coverage areas", data.low_coverage_areas?.length ?? 0],
          ["Projects without area", formatNumber(data.projects_without_area || 0)],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
            <p className="text-[0.64rem] font-bold uppercase tracking-wider text-gray-400">{label}</p>
            <p className="font-mono text-[1rem] font-bold text-gray-900 dark:text-white">{value}</p>
          </div>
        ))}
      </div>
      <AreaTable rows={data.areas} unit={data.unit_label || "Area"} showIndex />
      {(data.low_coverage_areas || []).length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-2 dark:border-amber-900/40 dark:bg-amber-950/10">
          <p className="text-[0.68rem] font-bold uppercase tracking-wider text-amber-600 dark:text-amber-400">Areas below the 25th percentile of recorded projects</p>
          <p className="mt-1 flex flex-wrap gap-1">
            {data.low_coverage_areas.map((a) => (
              <span key={a.area} className="rounded-sm bg-white px-1.5 py-0.5 text-[0.72rem] font-semibold text-gray-700 dark:bg-black/30 dark:text-gray-200">
                {a.area} · {a.projects}
              </span>
            ))}
          </p>
        </div>
      )}
      <Disclosure data={data} />
    </div>
  )
}

function Areas({ data }) {
  if (!data?.available) return <Banner tone="amber" text={data?.reason || "Execution patterns are unavailable for this scope."} />
  const unit = data.unit_label || "Area"
  return (
    <div className="space-y-3">
      <div className="grid gap-2 lg:grid-cols-2">
        <div>
          <p className="mb-1 text-[0.68rem] font-bold uppercase tracking-wider text-gray-400">Lowest average completion (≥10 projects)</p>
          <AreaTable rows={data.ranked_by_completion} unit={unit} />
        </div>
        <div>
          <p className="mb-1 text-[0.68rem] font-bold uppercase tracking-wider text-gray-400">Highest recorded utilization</p>
          <AreaTable rows={data.ranked_by_utilization} unit={unit} />
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <span className="rounded-sm bg-gray-100 px-2 py-0.5 text-[0.7rem] font-semibold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
          Median completion: {data.state_medians?.completion != null ? `${Number(data.state_medians.completion).toFixed(1)}%` : "—"}
        </span>
        <span className="rounded-sm bg-gray-100 px-2 py-0.5 text-[0.7rem] font-semibold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
          Median utilization: {data.state_medians?.utilization != null ? `${Number(data.state_medians.utilization).toFixed(1)}%` : "—"}
        </span>
      </div>
      <details className="rounded-lg border border-gray-200 p-2 dark:border-gray-700">
        <summary className="cursor-pointer text-[0.75rem] font-bold text-gray-600 dark:text-gray-300">Metric definitions</summary>
        <ul className="mt-1 space-y-0.5 text-[0.75rem] text-gray-500 dark:text-gray-400">
          {Object.entries(data.metric_definitions || {}).map(([k, v]) => (
            <li key={k}><span className="font-semibold">{k}</span>: {v}</li>
          ))}
        </ul>
      </details>
      <Disclosure data={data} />
    </div>
  )
}

function Spatial({ data }) {
  if (!data?.available) return <Banner tone="amber" text={data?.reason || "Spatial analysis is unavailable for this scope."} />
  return (
    <div className="space-y-3">
      <Banner text={`Coordinate basis: ${data.coordinate_basis}. ${data.distance_available ? "" : "No project pair currently has two GPS-verified points, so distance cannot be measured."}`} />
      <div>
        <p className="mb-1 text-[0.68rem] font-bold uppercase tracking-wider text-gray-400">Review candidates (never merged automatically)</p>
        {(data.groups || []).length === 0 ? (
          <p className="rounded-lg bg-gray-50 p-2 text-[0.8rem] text-gray-500 dark:bg-gray-900/40 dark:text-gray-400">No repeated names or shared linkage keys in this scope.</p>
        ) : (
          <div className="space-y-1.5">
            {data.groups.map((g, i) => (
              <div key={i} className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-[#0d090b]">
                <div className="flex flex-wrap items-center justify-between gap-1">
                  <p className="truncate text-[0.82rem] font-bold text-gray-900 dark:text-white">{g.name || g.work_key}</p>
                  <span className="rounded-sm bg-gray-100 px-1.5 py-0.5 text-[0.64rem] font-bold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                    {g.kind} · {g.project_count}
                  </span>
                </div>
                <p className="mt-0.5 text-[0.76rem] text-gray-600 dark:text-gray-300">{g.explanation}</p>
                <p className="text-[0.7rem] text-gray-400">Projects: {(g.sample_project_ids || []).map((id) => `#${id}`).join(", ")}</p>
              </div>
            ))}
          </div>
        )}
      </div>
      <div>
        <p className="mb-1 text-[0.68rem] font-bold uppercase tracking-wider text-gray-400">Recorded concentration</p>
        <div className="flex flex-wrap gap-1.5">
          {(data.concentration || []).map((a) => (
            <span key={a.area} className="rounded-sm bg-gray-100 px-2 py-0.5 text-[0.72rem] font-semibold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
              {a.area} · {a.projects}
            </span>
          ))}
        </div>
      </div>
      <p className="text-[0.76rem] text-gray-500 dark:text-gray-400">GPS-verified points available: {data.gps_points_available ?? 0}.</p>
      <Disclosure data={data} />
    </div>
  )
}

function Outliers({ data }) {
  if (!data?.available) return <Banner tone="amber" text={data?.reason || "Outlier detection is unavailable for this scope."} />
  return (
    <div className="space-y-3">
      <Banner text={data.method} />
      {(data.outliers || []).length === 0 ? (
        <p className="rounded-lg bg-gray-50 p-2 text-[0.8rem] text-gray-500 dark:bg-gray-900/40 dark:text-gray-400">
          No area deviates by 3 robust standard deviations on the compared metrics ({data.areas_compared} areas compared).
        </p>
      ) : (
        <div className="space-y-1.5">
          {data.outliers.map((o, i) => (
            <div key={`${o.area}-${o.metric}-${i}`} className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-[#0d090b]">
              <div className="flex flex-wrap items-center justify-between gap-1">
                <p className="text-[0.82rem] font-bold text-gray-900 dark:text-white">{o.area}</p>
                <span className={`rounded-sm px-1.5 py-0.5 text-[0.64rem] font-bold ${o.direction === "above" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" : "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"}`}>
                  {o.label} · z {o.robust_z}
                </span>
              </div>
              <p className="mt-0.5 text-[0.76rem] leading-snug text-gray-600 dark:text-gray-300">{o.explanation}</p>
            </div>
          ))}
        </div>
      )}
      {(data.category_concentration || []).length > 0 && (
        <div>
          <p className="mb-1 text-[0.68rem] font-bold uppercase tracking-wider text-gray-400">Category concentration</p>
          <div className="space-y-1">
            {data.category_concentration.map((c) => (
              <p key={c.area} className="text-[0.76rem] text-gray-600 dark:text-gray-300">
                <span className="font-semibold">{c.area}</span> — {c.top_category} ({c.share_pct}% of {c.projects})
              </p>
            ))}
          </div>
        </div>
      )}
      <Disclosure data={data} />
    </div>
  )
}

function Progress({ data }) {
  if (!data?.available) return <Banner tone="amber" text={data?.reason || "Progress mapping is unavailable for this scope."} />
  const maxCompletion = Math.max(...(data.areas || []).map((a) => Number(a.avg_completion) || 0), 1)
  return (
    <div className="space-y-3">
      <Banner text={`Mapped at ${data.precision} level. ${data.gps_point_count ? `${data.gps_point_count} GPS-verified point(s) are exact; all other areas are aggregates.` : "No GPS-verified project point exists in this scope."}`} />
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {(data.areas || []).map((a) => (
          <div key={a.area} className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-[#0d090b]">
            <div className="flex items-center justify-between gap-1">
              <p className="truncate text-[0.8rem] font-bold text-gray-900 dark:text-white" title={a.area}>{a.area}</p>
              <span className="shrink-0 font-mono text-[0.68rem] text-gray-400">{a.projects}</span>
            </div>
            <div className="mt-1">
              <div className="flex justify-between text-[0.66rem] text-gray-400">
                <span>Completion</span>
                <span>{a.avg_completion != null ? `${Number(a.avg_completion).toFixed(1)}%` : "—"}</span>
              </div>
              <Bar value={a.avg_completion} max={maxCompletion} tone="bg-blue-500" />
            </div>
            <div className="mt-1 flex justify-between text-[0.68rem] text-gray-500 dark:text-gray-400">
              <span>Utilization {a.utilization_pct != null ? `${Number(a.utilization_pct).toFixed(1)}%` : "—"}</span>
              <span>High risk {a.high_risk ?? 0}</span>
            </div>
            {a.expenditure != null && (
              <p className="text-[0.68rem] text-gray-400">{formatMoney(a.expenditure)} linked payments</p>
            )}
          </div>
        ))}
      </div>
      <Disclosure data={data} />
    </div>
  )
}

export default function GeoIntelPanel({ house = "", initialState = "" }) {
  const [tab, setTab] = useState("coverage")
  const [state, setState] = useState(initialState)
  const [states, setStates] = useState([])
  const [data, setData] = useState(null)
  const [error, setError] = useState("")

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await getStates()
        const list = Array.isArray(res) ? res : res?.states || []
        if (alive) setStates(list.map((s) => (typeof s === "string" ? s : s.state)).filter(Boolean))
      } catch { /* selector is optional */ }
    })()
    return () => { alive = false }
  }, [])

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await fetchers[tab]({ state, house })
        if (alive) { setData(res); setError("") }
      } catch (e) {
        if (alive) setError(e.message || "Could not load geospatial intelligence.")
      }
    })()
    return () => { alive = false }
  }, [tab, state, house])

  const selectCls =
    "rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 text-[0.8rem] text-gray-900 shadow-2xs outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white"

  return (
    <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400">Geospatial intelligence</h3>
          <p className="mt-0.5 text-[0.78rem] text-gray-400">
            Recorded-project coverage and execution geography. Findings describe the MPLADS dataset, not proven infrastructure deficits.
          </p>
        </div>
        <select value={state} onChange={(e) => setState(e.target.value)} className={`${selectCls} w-full sm:w-56`} aria-label="Select state for geospatial analysis">
          <option value="">All states</option>
          {states.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      {/* Horizontally scrollable on phones: each tab keeps its own box and
          spacing instead of compressing into merged text. */}
      <div
        className="geo-tab-scroll -mx-1 mt-3 flex gap-1.5 overflow-x-auto border-b border-gray-200 px-1 pb-1 dark:border-gray-700"
        role="tablist"
        aria-label="Geospatial intelligence views"
      >
        {TABS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={`shrink-0 whitespace-nowrap rounded-t-lg border-b-2 px-3 py-2 text-[0.8rem] font-bold transition ${
              tab === key
                ? "border-blue-500 bg-blue-50 text-blue-700 dark:border-blue-500 dark:bg-blue-950/40 dark:text-blue-300"
                : "border-transparent text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-gray-800"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="mt-3">
        {error && <p className="rounded-lg border border-red-200 bg-red-50 p-2.5 text-[0.82rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">{error}</p>}
        {!error && !data && <p className="text-[0.82rem] text-gray-400">Loading geospatial aggregates…</p>}
        {!error && data && tab === "coverage" && <Coverage data={data} />}
        {!error && data && tab === "areas" && <Areas data={data} />}
        {!error && data && tab === "spatial" && <Spatial data={data} />}
        {!error && data && tab === "outliers" && <Outliers data={data} />}
        {!error && data && tab === "progress" && <Progress data={data} />}
      </div>
    </section>
  )
}
