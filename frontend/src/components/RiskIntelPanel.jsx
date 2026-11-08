import { useEffect, useState } from "react"
import {
  getEmergingRisk, getAnomalyEvolution, getBehaviourClusters,
  getClusterProjects, getStates,
} from "../services/api"
import { formatMoney, formatNumber } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════
   Risk Center analytical layer — Features 2, 3 and 7.
   A separate layer over the stored risk results: it never changes the
   existing scoring formulas. When import history is empty the panels say
   so instead of drawing a fabricated trend.
   ═══════════════════════════════════════════════════════════════════ */

const TABS = [
  ["emerging", "Emerging risk"],
  ["evolution", "Pattern evolution"],
  ["clusters", "Behaviour clusters"],
]

const QUALITY_TONE = {
  good: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  moderate: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  weak: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
}

function Field({ label, value }) {
  return (
    <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
      <p className="text-[0.65rem] font-bold uppercase tracking-wider text-gray-400">{label}</p>
      <p className="truncate text-[0.9rem] font-bold text-gray-900 dark:text-white" title={String(value)}>{value}</p>
    </div>
  )
}

function Note({ children }) {
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-[0.8rem] leading-snug text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-300">
      {children}
    </div>
  )
}

function labelize(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
}

function fmtMetric(key, value) {
  if (value == null) return "—"
  if (typeof value === "string") return value
  if (/expenditure|sanctioned|amount/.test(key)) return formatMoney(value)
  if (/pct|percentage|completion|utilization/.test(key)) return `${Number(value).toFixed(1)}%`
  return formatNumber(value)
}

/* ── Feature 2 ─────────────────────────────────────────────────── */

function EmergingRisk({ state, house }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState("")

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await getEmergingRisk({ state, house })
        if (alive) { setData(res); setError("") }
      } catch (e) {
        if (alive) setError(e.message || "Could not load emerging-risk indicators.")
      }
    })()
    return () => { alive = false }
  }, [state, house])

  if (error) return <p className="text-[0.85rem] font-semibold text-red-600 dark:text-red-400">{error}</p>
  if (!data) return <p className="text-[0.85rem] text-gray-400">Loading indicator baseline…</p>

  const current = data.current || {}
  const patterns = data.patterns || []
  const cmp = data.new_vs_persistent

  return (
    <div className="space-y-3">
      {data.mode === "current-baseline" ? (
        <Note>
          <strong>Point-in-time baseline, not a trend.</strong> {data.notes?.[0]}
        </Note>
      ) : (
        <div className="rounded-lg border border-green-200 bg-green-50 p-2.5 text-[0.8rem] font-semibold text-green-800 dark:border-green-900/40 dark:bg-green-950/20 dark:text-green-300">
          Comparing {data.history?.batches} committed import(s) — new and persistent patterns are distinguished below.
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        {Object.entries(current).map(([k, v]) => (
          <Field key={k} label={labelize(k)} value={fmtMetric(k, v)} />
        ))}
      </div>

      {patterns.length > 0 && (
        <div>
          <p className="mb-1 text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Recorded indicator patterns</p>
          <div className="space-y-1.5">
            {patterns.map((p, i) => (
              <div key={`${p.key || p.label || i}`} className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-[#0d090b]">
                <div className="flex flex-wrap items-center justify-between gap-1">
                  <p className="text-[0.85rem] font-bold text-gray-900 dark:text-white">{p.label || labelize(p.key || "")}</p>
                  {p.projects != null && (
                    <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[0.68rem] font-bold text-blue-700 dark:bg-blue-950 dark:text-blue-300">
                      {formatNumber(p.projects)} projects
                    </span>
                  )}
                </div>
                {p.detail && <p className="mt-0.5 text-[0.8rem] text-gray-600 dark:text-gray-300">{p.detail}</p>}
                {p.explanation && <p className="mt-0.5 text-[0.8rem] text-gray-600 dark:text-gray-300">{p.explanation}</p>}
              </div>
            ))}
          </div>
        </div>
      )}

      {cmp && (
        <div className="grid gap-2 sm:grid-cols-2">
          {[
            ["Newly emerging", cmp.new_patterns, "border-red-200 dark:border-red-900/40"],
            ["Persistent", cmp.persistent_patterns, "border-amber-200 dark:border-amber-900/40"],
            ["No longer present", cmp.resolved_patterns, "border-green-200 dark:border-green-900/40"],
          ].map(([label, list, tone]) => (
            <div key={label} className={`rounded-lg border bg-white p-2 dark:bg-[#0d090b] ${tone}`}>
              <p className="text-[0.68rem] font-bold uppercase tracking-wider text-gray-400">{label}</p>
              {(list || []).length === 0 ? (
                <p className="mt-1 text-[0.8rem] text-gray-400">None detected between the compared imports.</p>
              ) : (
                <ul className="mt-1 list-inside list-disc text-[0.82rem] text-gray-700 dark:text-gray-300">
                  {list.map((p, i) => <li key={i}>{typeof p === "string" ? p : p.label || p.key || JSON.stringify(p)}</li>)}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}

      {(data.notes || []).slice(1).map((n) => (
        <p key={n} className="text-[0.78rem] text-gray-500 dark:text-gray-400">{n}</p>
      ))}
    </div>
  )
}

/* ── Feature 7 ─────────────────────────────────────────────────── */

function PatternEvolution({ state, house }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState("")

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await getAnomalyEvolution({ state, house })
        if (alive) { setData(res); setError("") }
      } catch (e) {
        if (alive) setError(e.message || "Could not load anomaly evolution.")
      }
    })()
    return () => { alive = false }
  }, [state, house])

  if (error) return <p className="text-[0.85rem] font-semibold text-red-600 dark:text-red-400">{error}</p>
  if (!data) return <p className="text-[0.85rem] text-gray-400">Loading recorded import history…</p>

  const series = data.series || []
  return (
    <div className="space-y-3">
      {data.mode === "unavailable" && (
        <Note>
          <strong>No import history is stored.</strong> Anomaly patterns cannot be compared across imports yet. The
          current-record row below is recomputed now and labelled <em>reconstructed</em> — it is not a historical result.
        </Note>
      )}

      <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
        <table className="min-w-full text-[0.82rem]">
          <thead className="bg-gray-50 text-[0.68rem] uppercase tracking-wider text-gray-500 dark:bg-gray-900/60 dark:text-gray-400">
            <tr>
              <th className="px-2 py-1.5 text-left">Row</th>
              <th className="px-2 py-1.5 text-right">Projects</th>
              <th className="px-2 py-1.5 text-right">Avg completion</th>
              <th className="px-2 py-1.5 text-right">High risk</th>
              <th className="px-2 py-1.5 text-right">Anomalies</th>
              <th className="px-2 py-1.5 text-right">Expenditure</th>
              <th className="px-2 py-1.5 text-left">Provenance</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
            {series.map((row, i) => (
              <tr key={`${row.batch_id ?? "current"}-${i}`}>
                <td className="px-2 py-1.5 font-semibold text-gray-800 dark:text-gray-200">{row.label}</td>
                <td className="px-2 py-1.5 text-right font-mono">{formatNumber(row.projects ?? 0)}</td>
                <td className="px-2 py-1.5 text-right font-mono">{row.avg_completion != null ? `${Number(row.avg_completion).toFixed(1)}%` : "—"}</td>
                <td className="px-2 py-1.5 text-right font-mono">{row.high_risk != null ? formatNumber(row.high_risk) : "not recorded"}</td>
                <td className="px-2 py-1.5 text-right font-mono">{row.anomalies != null ? formatNumber(row.anomalies) : "not recorded"}</td>
                <td className="px-2 py-1.5 text-right font-mono">{row.expenditure != null ? formatMoney(row.expenditure) : "—"}</td>
                <td className="px-2 py-1.5">
                  <span className={`rounded px-1.5 py-0.5 text-[0.65rem] font-bold ${row.mode === "recorded" ? "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300" : "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"}`}>
                    {row.mode}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-[0.78rem] text-gray-500 dark:text-gray-400">
        Stored imports: {data.batches ?? 0}. Earlier anomaly levels were never recorded, so they are not reconstructed backwards.
      </p>
    </div>
  )
}

/* ── Feature 3 ─────────────────────────────────────────────────── */

function BehaviourClusters({ state, house, category, onSelectProject }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState("")
  const [openId, setOpenId] = useState(null)
  const [members, setMembers] = useState(null)
  const [memberError, setMemberError] = useState("")

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await getBehaviourClusters({ state, house, category })
        if (alive) { setData(res); setError(""); setOpenId(null); setMembers(null) }
      } catch (e) {
        if (alive) setError(e.message || "Could not cluster project behaviour.")
      }
    })()
    return () => { alive = false }
  }, [state, house, category])

  async function inspect(id) {
    if (openId === id) { setOpenId(null); return }
    setOpenId(id)
    setMembers(null)
    setMemberError("")
    try {
      const res = await getClusterProjects(id)
      setMembers(res)
    } catch (e) {
      setMemberError(e.message || "Could not load cluster members.")
    }
  }

  if (error) return <p className="text-[0.85rem] font-semibold text-red-600 dark:text-red-400">{error}</p>
  if (!data) return <p className="text-[0.85rem] text-gray-400">Clustering recorded execution behaviour…</p>
  if (!data.available) return <Note>{data.reason}</Note>

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[0.75rem] text-gray-500 dark:text-gray-400">
        <span className={`rounded px-1.5 py-0.5 font-bold ${QUALITY_TONE[data.quality] || QUALITY_TONE.moderate}`}>
          silhouette {data.silhouette} · {data.quality}
        </span>
        <span>{data.k} clusters over {formatNumber(data.projects_considered)} works</span>
        <span className="italic">{data.method}</span>
      </div>

      {Object.keys(data.imputed_values || {}).length > 0 && (
        <p className="text-[0.75rem] text-gray-500 dark:text-gray-400">
          Missing values were median-imputed for:{" "}
          {Object.entries(data.imputed_values).map(([k, v]) => `${labelize(k)} (${v})`).join(", ")}.
        </p>
      )}

      <div className="space-y-2">
        {(data.clusters || []).map((c) => (
          <div key={c.id} className="rounded-lg border border-gray-200 bg-white p-2.5 dark:border-gray-700 dark:bg-[#0d090b]">
            <div className="flex flex-wrap items-center justify-between gap-1.5">
              <div className="min-w-0">
                <p className="text-[0.9rem] font-bold text-gray-900 dark:text-white">{c.label}</p>
                <p className="text-[0.72rem] text-gray-400">
                  Cluster {c.id} · {formatNumber(c.size)} works · {c.share_pct}% of clustered sample
                </p>
              </div>
              <button
                type="button"
                onClick={() => inspect(c.id)}
                className="rounded-lg border border-gray-300 px-2.5 py-1 text-[0.75rem] font-bold text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
              >
                {openId === c.id ? "Hide projects" : "Inspect projects"}
              </button>
            </div>
            <p className="mt-1 text-[0.8rem] leading-snug text-gray-600 dark:text-gray-300">{c.description}</p>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {(c.top_states || []).map((s) => (
                <span key={s.state} className="rounded bg-gray-100 px-1.5 py-0.5 text-[0.68rem] font-semibold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                  {s.state} · {s.projects}
                </span>
              ))}
              {(c.top_categories || []).map((s) => (
                <span key={s.category} className="rounded bg-blue-50 px-1.5 py-0.5 text-[0.68rem] font-semibold text-blue-700 dark:bg-blue-950/40 dark:text-blue-300">
                  {s.category} · {s.projects}
                </span>
              ))}
            </div>

            {openId === c.id && (
              <div className="mt-2 rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
                {memberError && <p className="text-[0.78rem] font-semibold text-red-600 dark:text-red-400">{memberError}</p>}
                {!members && !memberError && <p className="text-[0.78rem] text-gray-400">Loading members…</p>}
                {members && (members.projects || []).length === 0 && (
                  <p className="text-[0.78rem] text-gray-400">{members.reason || "No project rows were returned for this cluster."}</p>
                )}
                {(members?.projects || []).map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => onSelectProject?.(p.id)}
                    disabled={!onSelectProject}
                    className="flex w-full items-center justify-between gap-2 border-b border-gray-200 py-1 text-left text-[0.8rem] last:border-0 disabled:cursor-default dark:border-gray-800"
                  >
                    <span className="min-w-0 truncate text-gray-700 dark:text-gray-200">{p.project_name || p.name || `Project #${p.id}`}</span>
                    <span className="shrink-0 font-mono text-[0.7rem] text-gray-400">#{p.id}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <p className="text-[0.75rem] italic text-gray-400">{data.features_used_note}</p>
      {(data.limitations || []).map((l) => <p key={l} className="text-[0.75rem] text-gray-500 dark:text-gray-400">{l}</p>)}
    </div>
  )
}

/* ── shell ─────────────────────────────────────────────────────── */

export default function RiskIntelPanel({ onSelectProject }) {
  const [tab, setTab] = useState("emerging")
  const [states, setStates] = useState([])
  const [scope, setScope] = useState({ state: "", house: "" })

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await getStates()
        const list = Array.isArray(res) ? res : res?.states || []
        if (alive) {
          setStates(list.map((s) => (typeof s === "string" ? s : s.state)).filter(Boolean))
        }
      } catch { /* the panel still works without the picker */ }
    })()
    return () => { alive = false }
  }, [])

  const selectCls =
    "rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 text-[0.8rem] text-gray-900 shadow-2xs outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white"

  return (
    <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400">Risk intelligence layer</h3>
          <p className="mt-0.5 text-[0.78rem] text-gray-400">
            Analytical layer over stored risk results — the scoring formulas are unchanged.
          </p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <select value={scope.state} onChange={(e) => setScope((s) => ({ ...s, state: e.target.value }))} className={selectCls} aria-label="Filter by state">
            <option value="">All states</option>
            {states.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select value={scope.house} onChange={(e) => setScope((s) => ({ ...s, house: e.target.value }))} className={selectCls} aria-label="Filter by House">
            <option value="">Both Houses</option>
            <option value="Lok Sabha">Lok Sabha</option>
            <option value="Rajya Sabha">Rajya Sabha</option>
          </select>
        </div>
      </div>

      <div className="mt-3 flex gap-1 overflow-x-auto border-b border-gray-200 pb-1 dark:border-gray-700">
        {TABS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`whitespace-nowrap rounded-t-lg px-2.5 py-1.5 text-[0.8rem] font-bold transition ${
              tab === key
                ? "bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300"
                : "text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-gray-800"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="mt-3">
        {tab === "emerging" && <EmergingRisk state={scope.state} house={scope.house} />}
        {tab === "evolution" && <PatternEvolution state={scope.state} house={scope.house} />}
        {tab === "clusters" && <BehaviourClusters state={scope.state} house={scope.house} category={scope.category} onSelectProject={onSelectProject} />}
      </div>
    </section>
  )
}
