import { useEffect, useMemo, useState } from "react"
import { getMPIntelligence, getMPIntelligenceDetail, getStates } from "../services/api"
import { formatCrore, formatNumber, formatDateHuman } from "../utils/format"

const HOUSES = ["All", "Lok Sabha", "Rajya Sabha"]

const COLUMNS = [
  { key: "mp_name", label: "MP Name", sortable: true },
  { key: "house", label: "House", sortable: true },
  { key: "state", label: "State", sortable: true },
  { key: "constituency", label: "Constituency", sortable: true },
  { key: "allocated_amount", label: "Sanctioned Amount", sortable: true, num: true },
  { key: "total_expenditure", label: "Expenditure", sortable: true, num: true },
  { key: "utilization_percentage", label: "Utilization %", sortable: true, num: true },
  { key: "completed_works", label: "Completed", sortable: true, num: true },
  { key: "recommended_works", label: "Recommended", sortable: true, num: true },
  { key: "pending_works", label: "Pending/Ongoing", sortable: true, num: true },
  { key: "completion_rate_percentage", label: "Completion Rate %", sortable: true, num: true },
]

export default function MPIntelligence({ darkMode, drillDownParams, onClearDrillDown, onContextChange }) {
  const [house, setHouse] = useState("All")
  const [state, setState] = useState("")
  const [search, setSearch] = useState("")
  const [sortBy, setSortBy] = useState("allocated_amount")
  const [sortDir, setSortDir] = useState("desc")
  const [data, setData] = useState(null)
  // Loading is derived: a fetch is in flight until its request key matches.
  const [loadedKey, setLoadedKey] = useState("")
  const [selectedMpId, setSelectedMpId] = useState(null)
  const [states, setStates] = useState([])
  const [processedDrill, setProcessedDrill] = useState(null)
  const [reload, setReload] = useState(0)

  // Report the open MP to the app shell so the AI Assistant's "this MP"
  // questions resolve against the detail view on screen (mp:<id> page context).
  useEffect(() => {
    onContextChange?.(selectedMpId || null)
    return () => onContextChange?.(null)
  }, [selectedMpId, onContextChange])

  useEffect(() => {
    getStates().then((s) => { if (Array.isArray(s)) setStates(s) }).catch(() => {})
  }, [])

  const reqKey = `${house}|${state}|${search}|${sortBy}|${sortDir}`
  const loading = loadedKey !== reqKey

  useEffect(() => {
    let active = true
    const params = { sort_by: sortBy, sort_dir: sortDir, limit: 300 }
    if (house !== "All") params.house = house
    if (state) params.state = state
    if (search.trim()) params.q = search.trim()
    getMPIntelligence(params)
      .then((d) => { if (active) { setData(d); setLoadedKey(reqKey) } })
      .catch(() => { if (active) { setData(null); setLoadedKey(reqKey) } })
    return () => { active = false }
  }, [house, state, search, sortBy, sortDir, reqKey, reload])

  // Drill-down (assistant navigation with an mp_id): adjust state during
  // render, exactly once per params object — the React-endorsed pattern.
  if (drillDownParams?.mp_id && drillDownParams !== processedDrill) {
    setProcessedDrill(drillDownParams)
    setSelectedMpId(drillDownParams.mp_id)
    if (onClearDrillDown) onClearDrillDown()
  }

  const pageBg = darkMode ? "text-[#f5eff1]" : "text-[#292326]"
  const cardBg = darkMode ? "bg-[#0d090b] border-[#351923]" : "bg-white border-[#e8dadc]"
  const mutedText = darkMode ? "text-[#b9aeb2]" : "text-[#71666a]"

  return (
    <div className={`min-h-full p-4 sm:p-6 ${pageBg}`}>
      <div className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl sm:text-2xl md:text-3xl font-bold">MP Intelligence</h1>
          <p className={`mt-1 text-xs sm:text-sm ${mutedText}`}>
            MP-level view of MPLADS works and finances, by house. Click an MP for the full work ledger.
          </p>
        </div>
        <div className="flex items-center gap-1 rounded-lg border border-gray-200 p-1 dark:border-gray-700">
          {HOUSES.map((h) => (
            <button
              key={h}
              onClick={() => setHouse(h)}
              className={`rounded-md px-3 py-1.5 text-xs font-bold transition ${
                house === h ? "bg-blue-600 text-white shadow-sm" : "text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
              }`}
            >
              {h}
            </button>
          ))}
        </div>
      </div>

      {selectedMpId ? (
        <MPDetail
          mpId={selectedMpId}
          darkMode={darkMode}
          onBack={() => setSelectedMpId(null)}
        />
      ) : (
        <>
          {/* SUMMARY CARDS — skeleton while loading, error notice on failure,
              never permanent "—/…" placeholder cards */}
            <div className="grid grid-cols-1 min-[400px]:grid-cols-2 gap-3 md:grid-cols-4">
            {data?.summary
              ? [
                  { label: "Total MPs", value: formatNumber(data.summary.total_mps) },
                  { label: "Total Sanctioned (MP entitlements)", value: `₹${formatCrore(data.summary.total_allocated)} Cr` },
                  { label: "Total Expenditure", value: `₹${formatCrore(data.summary.total_expenditure)} Cr` },
                  { label: "Unspent Balance", value: `₹${formatCrore(data.summary.total_unspent)} Cr` },
                  { label: "Recommended Works", value: formatNumber(data.summary.total_recommended_works) },
                  { label: "Completed Works", value: formatNumber(data.summary.total_completed_works) },
                  { label: "Pending/Ongoing Works", value: formatNumber(data.summary.total_pending_works) },
                  { label: "Avg Utilization", value: `${data.summary.average_utilization_percentage}%` },
                  { label: "Avg Completion Rate", value: `${data.summary.average_completion_rate_percentage}%` },
                ].map((c) => (
                  <div key={c.label} className={`rounded-xl border p-3 shadow-sm ${cardBg}`}>
                    <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">{c.label}</p>
                    <p className="value-nowrap mt-1 font-mono text-sm font-bold xl:text-base">{c.value}</p>
                    {c.label === "Total Sanctioned (MP entitlements)" && (
                      <p className="mt-1 text-[0.5625rem] leading-snug text-gray-400">
                        MP entitlements = each MP's allocated amount (sums higher than the work-catalog "Total Project Amount" on Overview, which counts works only). Expenditure reflects the recorded MP payment ledger.
                      </p>
                    )}
                  </div>
                ))
              : loading
                ? Array.from({ length: 8 }).map((_, i) => (
                    <div key={`sk-${i}`} className={`animate-pulse rounded-xl border p-3 shadow-sm ${cardBg}`}>
                      <div className="h-2 w-2/3 rounded-sm bg-gray-200 dark:bg-gray-700" />
                      <div className="mt-2.5 h-4 w-1/2 rounded-sm bg-gray-200 dark:bg-gray-700" />
                    </div>
                  ))
                : (
                  <div className={`col-span-2 rounded-xl border p-6 text-center text-sm md:col-span-4 ${cardBg}`}>
                    <p className="font-bold">MP summary could not be loaded</p>
                    <p className={`mt-1 text-xs ${mutedText}`}>
                      The backend did not return MP Intelligence summary data for this filter. Please retry — if it persists, the backend may be waking up or restarting.
                    </p>
                    <button
                      onClick={() => { setLoadedKey(""); setReload((r) => r + 1) }}
                      className="mt-3 rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-blue-700"
                    >
                      Retry
                    </button>
                  </div>
                )}
          </div>

          {/* FILTER BAR */}
          <div className="mt-5 flex flex-col gap-2 sm:flex-row">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search MP name, constituency, or state…"
              className={`w-full rounded-lg border px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500 ${cardBg}`}
            />
            <select
              value={state}
              onChange={(e) => setState(e.target.value)}
              className={`rounded-lg border px-3 py-2 text-sm outline-none ${cardBg}`}
            >
              <option value="">All States</option>
              {states.map((s) => (
                <option key={s.state ?? s} value={s.state ?? s}>{s.state ?? s}</option>
              ))}
            </select>
          </div>

          {/* MP TABLE */}
          <div className={`mt-4 overflow-hidden rounded-xl border shadow-sm ${cardBg}`}>
            {loading ? (
              <p className={`p-8 text-center text-sm ${mutedText}`}>Loading MPs…</p>
            ) : !data?.mps?.length ? (
              <p className={`p-8 text-center text-sm ${mutedText}`}>No MPs match the current filters.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-gray-100 dark:border-gray-700/60">
                      {COLUMNS.map((c) => (
                        <th
                          key={c.key}
                          onClick={() => {
                            if (!c.sortable) return
                            if (sortBy === c.key) setSortDir((d) => (d === "asc" ? "desc" : "asc"))
                            else { setSortBy(c.key); setSortDir(c.num ? "desc" : "asc") }
                          }}
                          className={`whitespace-nowrap px-3 py-2.5 font-bold uppercase tracking-wider text-gray-400 ${c.sortable ? "cursor-pointer select-none hover:text-gray-600 dark:hover:text-gray-200" : ""}`}
                        >
                          {c.label}{sortBy === c.key ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.mps.map((m) => (
                      <tr
                        key={m.id}
                        onClick={() => setSelectedMpId(m.id)}
                        className="cursor-pointer border-b border-gray-50 transition last:border-0 hover:bg-gray-50 dark:border-gray-800/60 dark:hover:bg-gray-800/50"
                      >
                        <td className="max-w-[220px] truncate px-3 py-2.5 font-bold">{m.mp_name}</td>
                        <td className="whitespace-nowrap px-3 py-2.5">
                          <span className={`rounded-full px-2 py-0.5 text-[0.5625rem] font-bold ${m.house === "Lok Sabha" ? "bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300" : "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300"}`}>{m.house}</span>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-gray-500 dark:text-gray-400">{m.state}</td>
                        <td className="max-w-[160px] truncate px-3 py-2.5 text-gray-500 dark:text-gray-400">{m.constituency}</td>
                        <td className="whitespace-nowrap px-3 py-2.5 font-mono">₹{formatCrore(m.allocated_amount)} Cr</td>
                        <td className="whitespace-nowrap px-3 py-2.5 font-mono">₹{formatCrore(m.total_expenditure)} Cr</td>
                        <td className="px-3 py-2.5 font-mono">{m.utilization_percentage}%</td>
                        <td className="px-3 py-2.5 font-mono">{formatNumber(m.completed_works)}</td>
                        <td className="px-3 py-2.5 font-mono">{formatNumber(m.recommended_works)}</td>
                        <td className="px-3 py-2.5 font-mono">{formatNumber(m.pending_works)}</td>
                        <td className="px-3 py-2.5 font-mono">{m.completion_rate_percentage}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {data && data.mps && data.mps.length < data.total && (
              <p className={`px-3 py-2 text-[0.625rem] ${mutedText}`}>
                Showing first {data.mps.length} of {formatNumber(data.total)} MPs — refine the search to narrow the list.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}

/* ═══════════════ MP DETAIL VIEW ═══════════════ */

function MPDetail({ mpId, darkMode, onBack }) {
  const [d, setD] = useState(null)
  const [loading, setLoading] = useState(true)
  const [workSort, setWorkSort] = useState({ key: "amount", dir: "desc" })
  const [category, setCategory] = useState("")

  useEffect(() => {
    let active = true
    getMPIntelligenceDetail(mpId)
      .then((x) => { if (active) setD(x) })
      .catch(() => { if (active) setD(null) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [mpId])

  const cardBg = darkMode ? "bg-[#0d090b] border-[#351923]" : "bg-white border-[#e8dadc]"
  const mutedText = darkMode ? "text-[#b9aeb2]" : "text-[#71666a]"

  const works = useMemo(() => {
    if (!d?.works) return []
    let list = category ? d.works.filter((w) => w.category === category) : d.works
    const { key, dir } = workSort
    const mul = dir === "asc" ? 1 : -1
    return [...list].sort((a, b) => {
      const av = a[key] ?? -Infinity
      const bv = b[key] ?? -Infinity
      return (typeof av === "string" ? av.localeCompare(bv) : (av - bv)) * mul
    })
  }, [d, category, workSort])

  if (loading) return <p className={`p-8 text-center text-sm ${mutedText}`}>Loading MP…</p>
  if (!d) return <p className={`p-8 text-center text-sm ${mutedText}`}>MP data could not be loaded.</p>

  const { mp, financial_summary, work_summary, completion, categories } = d

  return (
    <div className="space-y-5">
      <button onClick={onBack} className={`text-xs font-bold ${mutedText} hover:underline`}>
        ← Back to MP list
      </button>

      {/* PROFILE */}
      <div className={`rounded-xl border p-5 shadow-sm ${cardBg}`}>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-lg font-bold">{mp.mp_name}</h2>
            <p className={`mt-0.5 text-xs ${mutedText}`}>
              {mp.house} · {mp.constituency}, {mp.state}
            </p>
          </div>
          <span className={`self-start rounded-full px-3 py-1 text-xs font-bold ${mp.house === "Lok Sabha" ? "bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300" : "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300"}`}>{mp.house}</span>
        </div>
      </div>

      {/* FINANCIAL SUMMARY */}
      <div className="grid grid-cols-1 min-[400px]:grid-cols-2 gap-3 md:grid-cols-4">
        {[
          { label: "Sanctioned Amount", value: `₹${formatCrore(mp.allocated_amount)} Cr` },
          { label: "Expenditure", value: `₹${formatCrore(mp.total_expenditure)} Cr` },
          { label: "Remaining/Unspent", value: `₹${formatCrore(financial_summary.remaining_unspent)} Cr` },
          { label: "Utilization (spent ÷ sanctioned)", value: `${financial_summary.utilization_percentage}%` },
          { label: "Official Utilization", value: `${financial_summary.official_utilization_percentage}%` },
          { label: "Recommended Works", value: formatNumber(work_summary.recommended_works) },
          { label: "Completed Works", value: formatNumber(work_summary.completed_works) },
          { label: "Pending/Ongoing Works", value: formatNumber(work_summary.ongoing_pending_works) },
          { label: "Completion Rate", value: `${completion.completion_rate}%` },
        ].map((c) => (
          <div key={c.label} className={`rounded-xl border p-3 shadow-sm ${cardBg}`}>
            <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">{c.label}</p>
            <p className="value-nowrap mt-1 font-mono text-sm font-bold xl:text-base">{c.value}</p>
          </div>
        ))}
      </div>

      {/* VISUALS — two simple dashboard-style bars */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div className={`rounded-xl border p-4 shadow-sm ${cardBg}`}>
          <p className="text-xs font-bold uppercase tracking-wider">Sanctioned vs Expenditure</p>
          <div className="mt-3 space-y-2">
            <MiniBar darkMode={darkMode} label="Sanctioned" value={mp.allocated_amount} max={Math.max(mp.allocated_amount, mp.total_expenditure)} color="from-blue-500 to-blue-600" />
            <MiniBar darkMode={darkMode} label="Expenditure" value={mp.total_expenditure} max={Math.max(mp.allocated_amount, mp.total_expenditure)} color="from-purple-500 to-purple-600" />
          </div>
        </div>
        <div className={`rounded-xl border p-4 shadow-sm ${cardBg}`}>
          <p className="text-xs font-bold uppercase tracking-wider">Completed vs Pending</p>
          <div className="mt-3 space-y-2">
            <MiniBar darkMode={darkMode} unit="count" label={`Completed (${formatNumber(work_summary.completed_works)})`} value={work_summary.completed_works} max={Math.max(work_summary.completed_works, work_summary.ongoing_pending_works, 1)} color="from-green-500 to-green-600" />
            <MiniBar darkMode={darkMode} unit="count" label={`Pending/Ongoing (${formatNumber(work_summary.ongoing_pending_works)})`} value={work_summary.ongoing_pending_works} max={Math.max(work_summary.completed_works, work_summary.ongoing_pending_works, 1)} color="from-amber-500 to-amber-600" />
          </div>
        </div>
      </div>

      {/* WORK BREAKDOWN */}
      <div className={`rounded-xl border shadow-sm ${cardBg}`}>
        <div className="flex flex-col gap-2 border-b border-gray-100 p-4 sm:flex-row sm:items-center sm:justify-between dark:border-gray-700/60">
          <div>
            <h3 className="text-sm font-bold uppercase tracking-wider">Work Breakdown</h3>
            <p className={`mt-0.5 text-xs ${mutedText}`}>
              {formatNumber(works.length)} works{d.works_truncated ? " (first 400 shown)" : ""} · click a column to sort
            </p>
          </div>
          {categories.length > 0 && (
            <select value={category} onChange={(e) => setCategory(e.target.value)} className={`rounded-lg border px-2 py-1.5 text-xs ${cardBg}`}>
              <option value="">All categories</option>
              {categories.map((c) => <option key={c.category} value={c.category}>{c.category} ({c.count})</option>)}
            </select>
          )}
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-xs">
            <thead>
              <tr className="border-b border-gray-100 dark:border-gray-700/60">
                {[
                  { k: "work_id", l: "ID" },
                  { k: "work_description", l: "Work Description" },
                  { k: "category", l: "Category" },
                  { k: "amount", l: "Amount" },
                  { k: "expenditure", l: "Expenditure" },
                  { k: "status", l: "Status" },
                  { k: "progress", l: "Progress" },
                  { k: "recommendation_date", l: "Recommended" },
                  { k: "completion_date", l: "Completed" },
                ].map((c) => (
                  <th
                    key={c.k}
                    onClick={() => setWorkSort((s) => (s.key === c.k ? { key: c.k, dir: s.dir === "asc" ? "desc" : "asc" } : { key: c.k, dir: c.k === "work_description" || c.k === "category" || c.k === "status" ? "asc" : "desc" }))}
                    className="cursor-pointer select-none whitespace-nowrap px-3 py-2.5 font-bold uppercase tracking-wider text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
                  >
                    {c.l}{workSort.key === c.k ? (workSort.dir === "asc" ? " ↑" : " ↓") : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {works.map((w, i) => (
                <tr key={`${w.work_id ?? "x"}-${i}`} className="border-b border-gray-50 last:border-0 dark:border-gray-800/60">
                  <td className="px-3 py-2 font-mono">{w.work_id ?? "—"}</td>
                  <td className="max-w-[260px] truncate px-3 py-2 font-semibold" title={w.work_description}>{w.work_description}</td>
                  <td className="whitespace-nowrap px-3 py-2 text-gray-500 dark:text-gray-400">{w.category ?? "—"}</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono">₹{formatCrore(w.amount)} Cr</td>
                  <td className="whitespace-nowrap px-3 py-2 font-mono">
                    ₹{formatCrore(w.expenditure)} Cr
                    {w.expenditure_status === "no_linked_records" && <span className="ml-1 text-[0.5625rem] text-gray-400">(no linked payments)</span>}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">
                    <span className={`rounded-full px-2 py-0.5 text-[0.5625rem] font-bold ${w.status === "Completed" ? "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300" : "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"}`}>{w.status}</span>
                  </td>
                  <td className="px-3 py-2 font-mono">{w.progress != null ? `${w.progress}%` : "—"}</td>
                  <td className="whitespace-nowrap px-3 py-2 text-gray-500 dark:text-gray-400">{formatDateHuman(w.recommendation_date) ?? "—"}</td>
                  <td className="whitespace-nowrap px-3 py-2 text-gray-500 dark:text-gray-400">{formatDateHuman(w.completion_date) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {d.data_quality?.unlinked_payment_amount > 0 && (
          <p className={`border-t border-gray-100 px-4 py-2.5 text-[0.625rem] leading-snug dark:border-gray-700/60 ${mutedText}`}>
            Data quality: ₹{formatCrore(d.data_quality.unlinked_payment_amount)} Cr of recorded payments under this MP match no recommended work in the catalog and are reported here rather than assigned to any work.
          </p>
        )}
      </div>
    </div>
  )
}

function MiniBar({ label, value, max, color, darkMode, unit = "money" }) {
  const pct = max > 0 ? Math.max((value / max) * 100, 2) : 2
  return (
    <div>
      <div className="flex items-center justify-between text-[0.6875rem]">
        <span className={darkMode ? "text-gray-300" : "text-gray-600"}>{label}</span>
        <span className="font-mono font-bold">{unit === "money" ? `₹${formatCrore(value)} Cr` : formatNumber(value)}</span>
      </div>
      <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700/60">
        <div className={`h-full rounded-full bg-gradient-to-r ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}
