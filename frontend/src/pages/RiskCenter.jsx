import { useEffect, useState, useCallback, memo } from "react"
import { getAnomalies, getAnomaliesSummary, getStates, getConstituencies, getProjectDetail, getAnomalyAnalytics, getRiskExplanation, healthCheck, onBackendStatus, getBackendStatus } from "../services/api"
import { TableRowsSkeleton, CardsSkeleton, MobileCardsSkeleton } from "../components/Skeletons"
import { formatMoney, formatNumber } from "../utils/format"
import RecTimelineBlock from "../components/RecTimelineBlock"

/* ──────────── Expenditure-span display helpers (recorded activity only —
   NOT official start/delay dates; see tooltip below) ──────────── */
const DELAY_TOOLTIP =
  "Estimated from the first and latest recorded expenditure dates. This represents recorded financial activity duration, not an official contractual delay."

function formatDate(iso) {
  if (!iso) return "—"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })
}

function formatActivityDays(days) {
  if (days === null || days === undefined) return null
  if (days >= 365) {
    const yrs = (days / 365).toFixed(1).replace(/\.0$/, "")
    return `~${yrs} yr${yrs === "1" ? "" : "s"}`
  }
  if (days >= 60) {
    const mo = Math.round(days / 30.44)
    return `~${mo} mo`
  }
  return `~${days} days`
}

const DELAY_BADGE_STYLES = {
  high: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  medium: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  low: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
}

/* ──────────── Donut Chart (pure CSS) ──────────── */
const DonutChart = memo(function DonutChart({ high, medium, low, none }) {
  const total = high + medium + low + none
  if (total === 0) return null
  const highPct = (high / total) * 100
  const medPct = (medium / total) * 100
  const lowPct = (low / total) * 100
  const nonePct = (none / total) * 100

  const grad = `conic-gradient(
    var(--color-red-500) 0% ${highPct}%,
    var(--color-amber-500) ${highPct}% ${highPct + medPct}%,
    var(--color-teal-600) ${highPct + medPct}% ${highPct + medPct + lowPct}%,
    var(--color-gray-400) ${highPct + medPct + lowPct}% 100%
  )`

  const segments = [
    { label: "High Risk", count: high, pct: highPct, color: "bg-red-500" },
    { label: "Medium Risk", count: medium, pct: medPct, color: "bg-amber-500" },
    { label: "Low Risk", count: low, pct: lowPct, color: "bg-teal-600" },
    { label: "No Risk", count: none, pct: nonePct, color: "bg-gray-400" },
  ]

  return (
    <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="relative flex-shrink-0">
        <div
          className="h-44 w-44 rounded-full sm:h-52 sm:w-52"
          style={{ background: grad }}
        />
        <div className="absolute inset-0 flex flex-col items-center justify-center rounded-full bg-white dark:bg-[#0d090b]" style={{ margin: "22%" }}>
          <span className="font-mono text-2xl font-bold text-gray-900 dark:text-white">{total.toLocaleString("en-IN")}</span>
          <span className="text-xs text-gray-500">Projects</span>
        </div>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2 sm:flex-col sm:gap-3">
        {segments.map((s) => (
          <div key={s.label} className="flex items-center gap-2.5">
            <span className={`h-3.5 w-3.5 flex-shrink-0 rounded-sm ${s.color}`} />
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-semibold text-gray-700 dark:text-gray-300">{s.label}</span>
              <span className="font-mono text-sm font-bold text-gray-500">{s.count.toLocaleString("en-IN")}</span>
              <span className="font-mono text-xs text-gray-400">({s.pct.toFixed(1)}%)</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
})

/* ──────────── Horizontal Bar Chart ──────────── */
/* ═══════════════ FY HORIZONTAL BAR CHART ═══════════════ */
const FYearChart = memo(function FYearChart({ items }) {
  if (!items || items.length === 0) return null
  const maxCount = Math.max(...items.map((f) => f.count || 0))
  return (
    <div className="flex w-full flex-col gap-5 py-2">
      {items.map((f) => {
        const pct = maxCount > 0 ? (f.count / maxCount) * 100 : 0
        return (
          <div key={f.fy} className="flex w-full flex-col gap-1.5">
            <div className="flex items-baseline justify-between">
              <span className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                {f.fy}
              </span>
              <span className="text-sm font-bold tabular-nums text-gray-900 dark:text-white">
                {(f.count || 0).toLocaleString("en-IN")}
              </span>
            </div>
            <div className="h-7 w-full overflow-hidden rounded-md bg-gray-100 dark:bg-gray-700/60">
              <div
                className="bar-fill h-full rounded-md bg-emerald-500 transition-all duration-300"
                style={{ width: `${Math.max(2, pct)}%` }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
})

const HBarChart = memo(function HBarChart({ items, maxCount, barColor = "bg-blue-500", onClickItem, compact }) {
  if (!items || items.length === 0) return null
  const peak = maxCount || Math.max(...items.map((i) => i.count))
  return (
    <div className={compact ? "flex w-full flex-col justify-center gap-4" : "space-y-3"}>
      {items.map((item) => {
        const width = peak > 0 ? (item.count / peak) * 100 : 0
        return (
          <div
            key={item.label}
            className={`group flex items-center gap-3 ${onClickItem ? "cursor-pointer" : ""}`}
            onClick={() => onClickItem && onClickItem(item)}
          >
            <span className="w-44 flex-shrink-0 truncate text-right text-sm font-semibold text-gray-700 dark:text-gray-300" title={item.label}>
              {item.label}
            </span>
            <div className="min-w-0 flex-1">
              <div className="h-8 overflow-hidden rounded bg-gray-100 dark:bg-gray-700/60">
                <div
                  className={`bar-fill h-full rounded ${barColor} transition-all duration-300 group-hover:opacity-80`}
                  style={{ width: `${Math.max(2, width)}%` }}
                />
              </div>
            </div>
            <span className="w-24 flex-shrink-0 text-right font-mono text-sm font-bold text-gray-600 dark:text-gray-400">
              {item.count.toLocaleString("en-IN")}
            </span>
            {item.pct !== undefined && (
              <span className="w-12 flex-shrink-0 text-right font-mono text-xs text-gray-400">{item.pct}%</span>
            )}
          </div>
        )
      })}
    </div>
  )
})

/* ──────────── Data Update Notice (same as Projects.jsx) ──────────── */
const STALE_THRESHOLD = 1000000 // ₹10 lakh
function getStaleProgressFlag(project) {
  const sanctioned = Number(project.sanctioned_amount || 0)
  const expenditure = Number(project.expenditure || 0)
  const completion = Number(project.completion_percentage || 0)
  if (sanctioned >= STALE_THRESHOLD && completion === 0 && expenditure === 0) {
    return true
  }
  return false
}

/* ═══════════════ MAIN COMPONENT ═══════════════ */
const RiskCenter = memo(function RiskCenter({ drillDownParams, onClearDrillDown, fy }) {
  const [summary, setSummary] = useState({ total_projects_checked: 0, high_risk: 0, medium_risk: 0, low_risk: 0, total_anomalies: 0 })
  const [anomalies, setAnomalies] = useState([])
  const [totalCount, setTotalCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [dataReady, setDataReady] = useState(null)

  // Filters
  const [states, setStates] = useState([])
  const [constituencies, setConstituencies] = useState([])
  const [filterState, setFilterState] = useState("")
  const [filterConstituency, setFilterConstituency] = useState("")
  const [filterSeverity, setFilterSeverity] = useState("")
  const [filterHouse, setFilterHouse] = useState("")
  const [searchQuery, setSearchQuery] = useState("")
  const [sortBy, setSortBy] = useState("risk_score")
  const [sortDir, setSortDir] = useState("desc")
  const [currentPage, setCurrentPage] = useState(1)
  const rowsPerPage = 20

  // Detail panel
  const [selectedAnomaly, setSelectedAnomaly] = useState(null)
  const [detailRisk, setDetailRisk] = useState(null)
  const [detailRec, setDetailRec] = useState(null)
  const [riskExplanation, setRiskExplanation] = useState(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  // Analytics
  const [analytics, setAnalytics] = useState(null)

  // Handle drill-down params
  useEffect(() => {
    if (drillDownParams) {
      if (drillDownParams.risk_level) setFilterSeverity(drillDownParams.risk_level)
      if (drillDownParams.state) setFilterState(drillDownParams.state)
      if (drillDownParams.house) setFilterHouse(drillDownParams.house)
      setCurrentPage(1)
      if (onClearDrillDown) onClearDrillDown()
    }
  }, [drillDownParams, onClearDrillDown])

  // Load states on mount
  useEffect(() => {
    getStates().then((s) => { if (Array.isArray(s)) setStates(s) }).catch(() => {})
  }, [])

  // A reachable API with an empty SQLite schema must not read as “zero
  // anomalies”. This check makes the data-readiness contract visible in the
  // most judge-facing screen. Connectivity itself is owned by the shared
  // transport (services/api.js) and re-syncs the dataReady/error state when
  // the backend comes back — no page refresh needed.
  //
  // Race-safety: the health probe can settle `dataReady=false` while the
  // backend is waking up (Render cold-start 503s) or mid-redeploy, even
  // though the dataset is fully loaded. That verdict must never outlive the
  // backend's recovery — the onBackendStatus listener below clears it — and
  // any successful page fetch immediately re-probes /health (request-aware:
  // a newer success always wins, never an older failure).
  const assessHealth = useCallback((health, probeFailed = false) => {
    const ready = !probeFailed && health?.data_ready !== false && Number(health?.total_projects || 0) > 0
    setDataReady(ready)
    if (!ready) {
      setError(probeFailed
        ? "Audit data service is unavailable. No risk conclusion can be drawn until the dataset is restored."
        : "Audit data is not loaded in this deployment. No risk conclusion can be drawn from an empty dataset.")
    } else {
      // A successful probe clears BOTH stale verdicts — a banner that
      // survived the backend's recovery is stale by definition.
      setError((prev) => (
        prev === "Audit data is not loaded in this deployment. No risk conclusion can be drawn from an empty dataset." ||
        prev === "Audit data service is unavailable. No risk conclusion can be drawn until the dataset is restored." ||
        prev === "Unable to load anomaly data. Check backend connection."
          ? "" : prev
      ))
    }
    return ready
  }, [])

  // The page's own fetches prove backend reachability (shared transport).
  // After any success, re-run the health probe once: the fetches arriving
  // proves the instance is up, so a `dataReady=false` verdict from a probe
  // that ran against a waking instance is stale and must be re-assessed.
  useEffect(() => {
    if (getBackendStatus() !== true) return
    let cancelled = false
    const t = setTimeout(() => {
      healthCheck()
        .then((h) => { if (!cancelled) assessHealth(h, false) })
        .catch(() => { if (!cancelled) { /* another newer success wins; leave state */ } })
    }, 150)
    return () => { cancelled = true; clearTimeout(t) }
  }, [assessHealth])

  // Probe /health on mount to distinguish "unreachable" from "empty".
  useEffect(() => {
    let cancelled = false
    healthCheck()
      .then((health) => { if (!cancelled) assessHealth(health, false) })
      .catch(() => { if (!cancelled) assessHealth(null, true) })
    return () => { cancelled = true }
  }, [assessHealth])

  // Shared-transport subscription: a successful request anywhere in the app
  // means the backend is alive again, so a previous "service unavailable" OR
  // "not loaded" verdict from this page is stale and is cleared immediately
  // (both banners describe a backend state that recovery invalidates).
  useEffect(() => onBackendStatus((reachable) => {
    if (reachable) {
      setDataReady((prev) => (prev === false ? null : prev))
      setError((prev) => (
        prev === "Audit data is not loaded in this deployment. No risk conclusion can be drawn from an empty dataset." ||
        prev === "Audit data service is unavailable. No risk conclusion can be drawn until the dataset is restored." ||
        prev === "Unable to load anomaly data. Check backend connection."
          ? "" : prev
      ))
    }
  }), [])

  // Load constituencies when state changes
  useEffect(() => {
    if (!filterState) { setConstituencies([]); setFilterConstituency(""); return }
    getConstituencies(filterState).then((d) => { if (Array.isArray(d)) setConstituencies(d) }).catch(() => {})
  }, [filterState])

  // Load summary (house-aware: the header cards must agree with the table)
  useEffect(() => {
    const params = {}
    if (fy) params.fy = fy
    if (filterHouse) params.house = filterHouse
    getAnomaliesSummary(params).then((s) => { if (s) setSummary(s) }).catch(() => {})
  }, [fy, filterHouse])

  // Load analytics (filter-aware)
  useEffect(() => {
    const params = {}
    if (filterState) params.state = filterState
    if (filterConstituency) params.constituency = filterConstituency
    if (filterHouse) params.house = filterHouse
    if (fy) params.fy = fy
    getAnomalyAnalytics(params).then((a) => { if (a) setAnalytics(a) }).catch(() => {})
  }, [filterState, filterConstituency, filterHouse, fy])

  // Fetch anomalies with server-side filters
  const fetchAnomalies = useCallback(async () => {
    // While the mount probe is still settling, let the fetch decide: a
    // successful /anomalies response proves the dataset is live and the
    // post-success re-probe will confirm readiness. Only a settled
    // dataReady === false blocks the fetch (and even that is revisited by
    // the re-probe above once any request succeeds).
    if (dataReady === false) {
      setLoading(false)
      setError("Audit data is not loaded in this deployment. No risk conclusion can be drawn from an empty dataset.")
      return
    }
    try {
      setLoading(true)
      setError("")
      const params = {
        skip: (currentPage - 1) * rowsPerPage,
        limit: rowsPerPage,
      }
      if (filterState) params.state = filterState
      if (filterConstituency) params.constituency = filterConstituency
      if (filterHouse) params.house = filterHouse
      if (fy) params.fy = fy
      if (filterSeverity) params.risk_level = filterSeverity
      if (searchQuery.trim()) params.q = searchQuery.trim()
      if (sortBy) { params.sort_by = sortBy; params.sort_dir = sortDir }

      const data = await getAnomalies(params)
      setAnomalies(data?.anomalies || [])
      setTotalCount(data?.total_anomalies || 0)
      // A successful data fetch is authoritative: re-assess readiness so a
      // stale "not loaded" verdict from a cold-start probe cannot coexist
      // with rendered rows.
      healthCheck()
        .then((h) => assessHealth(h, false))
        .catch(() => {})
    } catch (err) {
      console.error("Risk center error:", err)
      setError("Unable to load anomaly data. Check backend connection.")
    } finally {
      setLoading(false)
    }
  }, [filterState, filterConstituency, filterHouse, fy, filterSeverity, searchQuery, currentPage, sortBy, sortDir, dataReady, assessHealth])

  useEffect(() => { fetchAnomalies() }, [fetchAnomalies])

  // Reset filters
  const handleReset = () => {
    setFilterState(""); setFilterConstituency(""); setFilterSeverity(""); setFilterHouse("")
    setSortBy("risk_score"); setSortDir("desc"); setSearchQuery(""); setCurrentPage(1)
  }

  // Assistant control plane: "clear filters" reuses the existing reset path.
  useEffect(() => {
    const handler = () => handleReset()
    window.addEventListener("assistant:clear-filters", handler)
    return () => window.removeEventListener("assistant:clear-filters", handler)
  }, [])

  // Open detail panel
  const handleOpenDetail = async (anomaly) => {
    setSelectedAnomaly(anomaly)
    // Assistant project-context: the shared assistant hears which project is open.
    window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: anomaly.project_id, projectName: anomaly.project_name || null } }))
    setDetailRisk(null)
    setDetailRec(null)
    setRiskExplanation(null)
    setLoadingDetail(true)
    try {
      const [detail, explanation] = await Promise.all([
        getProjectDetail(anomaly.project_id),
        getRiskExplanation(anomaly.project_id),
      ])
      if (detail) {
        setDetailRisk(detail.risk)
        setDetailRec(detail.rec)
      }
      if (explanation) setRiskExplanation(explanation)
    } catch (err) { console.error("Detail error:", err) }
    finally { setLoadingDetail(false) }
  }

  const totalPages = Math.max(1, Math.ceil(totalCount / rowsPerPage))
  const rd = analytics?.risk_distribution || { high: 0, medium: 0, low: 0, none: 0 }

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 sm:p-6 text-[#292326] transition-colors duration-200 dark:bg-[#000000] dark:text-[#f5eff1]">
      <div className="mx-auto max-w-[1440px] space-y-4 sm:space-y-6">

        {/* HEADER */}
        <div className="flex flex-col justify-between gap-4 md:flex-row md:items-end">
          <div>
            <h2 className="text-2xl font-bold text-[#292326] dark:text-white">AI Anomaly Detection Center</h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Server-side anomaly analysis across <strong>{summary.total_projects_checked.toLocaleString("en-IN")}</strong> MPLADS works.
              {analytics?.ml_anomaly_count > 0 && <> <span className="text-purple-600 dark:text-purple-400 font-semibold">{analytics.ml_anomaly_count.toLocaleString("en-IN")} ML anomalies</span> detected.</>}
            </p>
          </div>
        </div>

        {/* KPI CARDS */}
        <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-4">
          {[
            { label: "Total Checked", value: summary.total_projects_checked, icon: "🔍", text: "text-blue-600 dark:text-blue-400" },
            { label: "High Risk", value: summary.high_risk, icon: "🔴", text: "text-red-600 dark:text-red-400" },
            { label: "Medium Risk", value: summary.medium_risk, icon: "🟡", text: "text-amber-600 dark:text-amber-400" },
            { label: "Total Anomalies", value: summary.total_anomalies, icon: "📊", text: "text-purple-600 dark:text-purple-400" },
          ].map((card) => (
            <div key={card.label} className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
              <div className="flex items-center justify-between">
                <p className="text-[0.6875rem] font-bold uppercase tracking-wider text-gray-400">{card.label}</p>
                <span className="text-lg">{card.icon}</span>
              </div>
              <p className={`mt-1 font-mono text-3xl font-bold ${card.text}`}>{card.value.toLocaleString("en-IN")}</p>
            </div>
          ))}
        </div>

        {/* CHARTS ROW 1: Donut + Anomaly Types */}
        {analytics && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* Risk Distribution Donut */}
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
              <h3 className="mb-4 text-sm font-bold uppercase tracking-wider text-gray-400">Risk Distribution</h3>
              <DonutChart high={rd.high} medium={rd.medium} low={rd.low} none={rd.none} />
            </div>

            {/* Anomaly Type Distribution */}
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
              <h3 className="mb-4 text-sm font-bold uppercase tracking-wider text-gray-400">Anomaly Type Distribution</h3>
              <HBarChart
                items={analytics.anomaly_types.map((at) => ({ label: at.type, count: at.count, pct: at.percentage }))}
                barColor="bg-blue-500"
              />
            </div>
          </div>
        )}

        {/* CHARTS ROW 2: Top States + FY Distribution */}
        {analytics && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* Top States */}
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
              <h3 className="mb-4 text-sm font-bold uppercase tracking-wider text-gray-400">Top States by Anomaly Count</h3>
              <HBarChart
                items={analytics.state_distribution.map((sd, idx) => ({ label: sd.state, count: sd.count, rank: idx + 1 }))}
                barColor="bg-red-500"
                onClickItem={(item) => { setFilterState(item.label); setCurrentPage(1) }}
              />
              {analytics.state_distribution.length > 0 && (
                <p className="mt-3 text-xs text-gray-400 italic">Click a state to filter the project list below.</p>
              )}
            </div>

            {/* FY Distribution — compact vertical bar chart */}
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
              <h3 className="mb-1 text-sm font-bold text-gray-600 dark:text-gray-300">Anomalies by Financial Year</h3>
              <p className="mb-4 text-[0.6875rem] text-gray-400 dark:text-gray-500">Number of flagged projects per FY period</p>
              {analytics.fy_distribution && analytics.fy_distribution.length > 0 ? (
                <FYearChart items={analytics.fy_distribution} />
              ) : (
                <p className="py-8 w-full text-center text-sm text-gray-400">No FY distribution data available.</p>
              )}
            </div>
          </div>
        )}

        {/* PROJECTS BY RISK LEVEL */}
        {analytics && (
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
            <div className="mb-4">
              <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400">Projects by Risk Level</h3>
              <p className="mt-0.5 text-xs text-gray-400">Distribution of monitored projects across AI-assessed risk levels.</p>
            </div>
            {(() => {
              const rd = analytics.risk_distribution || {}
              const total = (rd.high || 0) + (rd.medium || 0) + (rd.low || 0) + (rd.none || 0)
              if (total === 0) return <p className="py-8 text-center text-sm text-gray-400">No risk data available.</p>
              const categories = [
                { label: "High Risk", count: rd.high || 0, color: "bg-red-500", text: "text-red-600 dark:text-red-400", border: "border-red-200 dark:border-red-900/40" },
                { label: "Medium Risk", count: rd.medium || 0, color: "bg-amber-500", text: "text-amber-600 dark:text-amber-400", border: "border-amber-200 dark:border-amber-900/40" },
                { label: "Low Risk", count: rd.low || 0, color: "bg-blue-500", text: "text-blue-600 dark:text-blue-400", border: "border-blue-200 dark:border-blue-900/40" },
                { label: "No Risk", count: rd.none || 0, color: "bg-gray-400", text: "text-gray-600 dark:text-gray-400", border: "border-gray-200 dark:border-gray-700" },
              ].sort((a, b) => b.count - a.count)
              const maxCount = categories[0]?.count || 1
              return (
                <div className="space-y-4">
                  {categories.map((cat) => {
                    const pct = total > 0 ? (cat.count / total * 100).toFixed(1) : 0
                    const barWidth = maxCount > 0 ? (cat.count / maxCount) * 100 : 0
                    return (
                      <div key={cat.label} className="group">
                        <div className="mb-1.5 flex items-baseline justify-between">
                          <span className="text-sm font-semibold text-gray-700 dark:text-gray-300">{cat.label}</span>
                          <div className="flex items-baseline gap-2">
                            <span className={`font-mono text-sm font-bold ${cat.text}`}>{cat.count.toLocaleString("en-IN")}</span>
                            <span className="font-mono text-xs text-gray-400">({pct}%)</span>
                          </div>
                        </div>
                        <div className="h-8 overflow-hidden rounded-md bg-gray-100 dark:bg-gray-700/60">
                          <div
                            className={`bar-fill h-full rounded-md ${cat.color} transition-all duration-500 group-hover:opacity-80`}
                            style={{ width: `${Math.max(1, barWidth)}%` }}
                          />
                        </div>
                      </div>
                    )
                  })}
                  <div className="mt-2 flex items-center justify-between border-t border-gray-100 pt-2 dark:border-gray-700/60">
                    <span className="text-xs text-gray-500">Total projects assessed</span>
                    <span className="font-mono text-sm font-bold text-gray-900 dark:text-white">{total.toLocaleString("en-IN")}</span>
                  </div>
                </div>
              )
            })()}
          </div>
        )}

        {/* FILTERS */}
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-5">
            <div>
              <label className="block text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Search</label>
              <input
                value={searchQuery}
                onChange={(e) => { setSearchQuery(e.target.value); setCurrentPage(1) }}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); setCurrentPage(1); fetchAnomalies() } }}
                placeholder="Project name or ID..."
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#000000] dark:text-white"
              />
            </div>
            <div>
              <label className="block text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Severity</label>
              <select value={filterSeverity} onChange={(e) => { setFilterSeverity(e.target.value); setCurrentPage(1) }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#000000] dark:text-white">
                <option value="">All Severity</option>
                <option value="High">High Risk</option>
                <option value="Medium">Medium Risk</option>
                <option value="Low">Low Risk</option>
                <option value="None">No Risk</option>
              </select>
            </div>
            <div>
              <label className="block text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">House</label>
              <select value={filterHouse} onChange={(e) => { setFilterHouse(e.target.value); setCurrentPage(1) }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#000000] dark:text-white">
                <option value="">All Houses</option>
                <option value="Lok Sabha">Lok Sabha</option>
                <option value="Rajya Sabha">Rajya Sabha</option>
              </select>
            </div>
            <div>
              <label className="block text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">State</label>
              <select value={filterState} onChange={(e) => { setFilterState(e.target.value); setCurrentPage(1) }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#000000] dark:text-white">
                <option value="">All States</option>
                {states.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Constituency</label>
              <select value={filterConstituency} disabled={!filterState}
                onChange={(e) => { setFilterConstituency(e.target.value); setCurrentPage(1) }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 disabled:opacity-50 dark:border-gray-600 dark:bg-[#000000] dark:text-white">
                <option value="">{filterState ? "All Constituencies" : "Select State"}</option>
                {constituencies.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <div className="flex items-end">
              <button onClick={handleReset}
                className="w-full rounded-lg border border-gray-300 bg-white px-4 py-2 text-xs font-bold text-gray-700 transition hover:bg-gray-50 dark:border-gray-600 dark:bg-[#000000] dark:text-gray-200">
                Reset Filters
              </button>
            </div>
          </div>
        </div>

        {/* ERROR */}
        {error && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm font-medium text-red-700 dark:border-red-800/60 dark:bg-red-950/40 dark:text-red-300">⚠ {error}</div>
        )}

        {/* SORT ROW */}
        <div className="flex flex-wrap items-center gap-1.5 sm:gap-3">
          <span className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Sort:</span>
          {[
            { value: "risk_score", label: "Risk Score" },
            { value: "sanctioned_amount", label: "Sanctioned" },
            { value: "expenditure", label: "Expenditure" },
            { value: "completion_percentage", label: "Progress" },
            { value: "project_name", label: "Name" },
            { value: "id", label: "Project ID" },
          ].map((opt) => (
            <button key={opt.value}
              onClick={() => {
                if (sortBy === opt.value) setSortDir((d) => d === "asc" ? "desc" : "asc")
                else { setSortBy(opt.value); setSortDir(opt.value === "project_name" || opt.value === "id" ? "asc" : "desc") }
                setCurrentPage(1)
              }}
              className={`rounded-lg px-3 py-1.5 text-[0.6875rem] font-bold transition ${sortBy === opt.value ? "bg-[#c8102e] text-white dark:bg-blue-600" : "border border-gray-200 bg-white text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:bg-[#0d090b] dark:text-gray-300"}`}>
              {opt.label}{sortBy === opt.value && <span className="ml-1">{sortDir === "asc" ? "↑" : "↓"}</span>}
            </button>
          ))}
        </div>

        {/* ANOMALY TABLE */}
        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
          {loading ? (
            <>
              <TableRowsSkeleton rows={8} cols={5} className="hidden lg:block" />
              <MobileCardsSkeleton rows={5} />
            </>
          ) : anomalies.length === 0 ? (
            <div className="p-16 text-center text-sm text-gray-500">No anomalies match the current filters.</div>
          ) : (
            <>
              {/* DESKTOP TABLE */}
              <div className="hidden lg:block overflow-x-auto">
                <table className="w-full min-w-[1020px]">
                  <thead>
                    <tr className="border-b-2 border-gray-200 bg-gray-50 text-left text-[0.625rem] font-bold uppercase tracking-widest text-gray-500 dark:border-gray-700 dark:bg-[#120b0e] dark:text-gray-400">
                      <th className="px-4 py-3">Project</th>
                      <th className="px-4 py-3">State / Constituency</th>
                      <th className="px-4 py-3" title={DELAY_TOOLTIP}>Delayed By ⓘ</th>
                      <th className="px-4 py-3 text-right">Sanctioned</th>
                      <th className="px-4 py-3 text-right">Expenditure</th>
                      <th className="px-4 py-3 text-center">Progress</th>
                      <th className="px-4 py-3 text-center">Risk Level</th>
                      <th className="px-4 py-3 text-center">Score</th>
                      <th className="px-4 py-3 text-center">ML</th>
                      <th className="px-4 py-3 text-center">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {anomalies.map((a) => (
                      <tr key={a.project_id} className="cursor-pointer border-b border-gray-100 transition hover:bg-blue-50/40 dark:border-gray-700/60 dark:hover:bg-[#120b0e]" onClick={() => handleOpenDetail(a)}>
                        <td className="px-4 py-2.5">
                          <span className="font-mono text-[0.625rem] font-bold text-gray-400">#{a.project_id}</span>
                          <p className="max-w-[250px] truncate text-sm font-semibold text-gray-900 dark:text-white" title={a.project_name}>{a.project_name || "Unnamed"}</p>
                          <p className="text-[0.6875rem] text-gray-400">{a.project_type || "General"}</p>
                        </td>
                        <td className="px-4 py-2.5 text-xs whitespace-nowrap">
                          {a.activity_days !== null && a.activity_days !== undefined ? (
                            <div title={DELAY_TOOLTIP}>
                              <span className="font-mono text-xs font-semibold text-gray-800 dark:text-gray-200">~{a.activity_days.toLocaleString("en-IN")} days</span>
                              {a.delay_indicator && (
                                <span className={`mt-0.5 block w-fit rounded-full px-1.5 py-px text-[0.5625rem] font-bold ${DELAY_BADGE_STYLES[a.delay_severity] || DELAY_BADGE_STYLES.low}`}>{a.delay_indicator}</span>
                              )}
                            </div>
                          ) : (
                            <span className="text-gray-300 dark:text-gray-600">—</span>
                          )}
                        </td>
                        <td className="px-4 py-2.5 text-xs">
                          <p className="font-semibold">{a.state || "N/A"}{a.house && <span className="ml-1.5 rounded bg-gray-100 px-1.5 py-px text-[0.5625rem] font-bold uppercase tracking-wide text-gray-500 dark:bg-[#120b0e] dark:text-gray-400" title={`House: ${a.house}`}>{a.house === "Lok Sabha" ? "LS" : a.house === "Rajya Sabha" ? "RS" : ""}</span>}</p>
                          <p className="text-gray-500">{a.constituency || "N/A"}</p>
                        </td>
                        <td className="px-4 py-2.5 text-right font-mono text-xs tabular-nums">{formatMoney(a.sanctioned_amount)}</td>
                        <td className="px-4 py-2.5 text-right font-mono text-xs tabular-nums">{formatMoney(a.expenditure)}</td>
                        <td className="px-4 py-2.5 text-center font-mono text-xs font-bold">{a.completion_percentage}%</td>
                        <td className="px-4 py-2.5 text-center">
                          <div className="flex items-center justify-center gap-1">
                            <span className={`rounded-full px-2 py-0.5 text-[0.625rem] font-bold ${a.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" : a.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300" : a.risk_level === "Low" ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300" : "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"}`}>{a.risk_level}</span>
                            {getStaleProgressFlag(a) && (
                              <span className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-1 py-0.5 text-[0.5625rem] font-bold text-amber-600 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300" title="Data Update Notice: Reported progress or expenditure may not reflect the latest project status. A risk score indicates an anomaly based on available data and does not by itself confirm project delay or irregularity.">⚠</span>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-2.5 text-center font-mono text-sm font-bold">{a.risk_score}</td>
                        <td className="px-4 py-2.5 text-center">{a.ml_anomaly ? <span className="rounded-full bg-purple-100 px-2 py-0.5 text-[0.625rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">ML</span> : <span className="text-gray-300">—</span>}</td>
                        <td className="px-4 py-2.5 text-center"><button onClick={(e) => { e.stopPropagation(); handleOpenDetail(a) }} className="rounded p-1 text-gray-600 hover:bg-gray-200 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-700">👁</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* MOBILE CARD VIEW */}
              <div className="lg:hidden divide-y divide-gray-100 dark:divide-gray-700/60">
                {anomalies.map((a) => (
                  <div key={a.project_id} className="p-4 cursor-pointer transition hover:bg-blue-50/40 dark:hover:bg-[#120b0e]" onClick={() => handleOpenDetail(a)}>
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="font-mono text-[0.625rem] font-bold text-gray-400">#{a.project_id}</span>
                          <span className={`rounded-full px-1.5 py-0.5 text-[0.625rem] font-bold ${a.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" : a.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300" : "bg-gray-100 text-gray-600 dark:bg-gray-700"}`}>{a.risk_level}</span>
                          {getStaleProgressFlag(a) && (
                            <span className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-1 py-0.5 text-[0.5625rem] font-bold text-amber-600 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300" title="Data Update Notice: Reported progress or expenditure may not reflect the latest project status.">⚠</span>
                          )}
                          {a.ml_anomaly && <span className="rounded-full bg-purple-100 px-1.5 py-0.5 text-[0.625rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">ML</span>}
                        </div>
                        <p className="mt-1 truncate font-semibold text-sm text-gray-900 dark:text-white" title={a.project_name}>{a.project_name || "Unnamed"}</p>
                        <p className="text-[0.6875rem] text-gray-500">{a.state || "N/A"}{a.constituency ? `, ${a.constituency}` : ""}</p>
                      </div>
                      <div className="text-right shrink-0">
                        <p className="font-mono text-sm font-bold">Score {a.risk_score}</p>
                      </div>
                    </div>
                    <div className="mt-2 grid grid-cols-3 gap-x-3 gap-y-1 text-xs">
                      <div className="flex justify-between"><span className="text-gray-500">Sanctioned</span><span className="font-mono font-semibold">{formatMoney(a.sanctioned_amount)}</span></div>
                      <div className="flex justify-between"><span className="text-gray-500">Spent</span><span className="font-mono font-semibold">{formatMoney(a.expenditure)}</span></div>
                      <div className="flex justify-between"><span className="text-gray-500">Progress</span><span className="font-mono font-semibold">{a.completion_percentage}%</span></div>
                    </div>
                    <div className="mt-1.5 flex items-center gap-2 text-[0.6875rem] text-gray-500 dark:text-gray-400">
                      <span title={DELAY_TOOLTIP}>Delayed By: <span className="font-mono font-semibold text-gray-700 dark:text-gray-300">{a.activity_days !== null && a.activity_days !== undefined ? `~${a.activity_days.toLocaleString("en-IN")} days` : "—"}</span> ⓘ</span>
                      {a.delay_indicator && (
                        <span className={`rounded-full px-1.5 py-px text-[0.5625rem] font-bold ${DELAY_BADGE_STYLES[a.delay_severity] || DELAY_BADGE_STYLES.low}`}>{a.delay_indicator}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              {/* PAGINATION */}
              <div className="flex flex-wrap items-center justify-between gap-4 border-t border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#120b0e]">
                <span className="text-xs text-gray-600 dark:text-gray-400">
                  Showing {totalCount === 0 ? 0 : (currentPage - 1) * rowsPerPage + 1} to{" "}
                  {Math.min(currentPage * rowsPerPage, totalCount)} of{" "}
                  <strong>{totalCount.toLocaleString("en-IN")}</strong> anomaly records
                </span>
                <div className="flex items-center gap-2">
                  <button disabled={currentPage === 1} onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                    className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-semibold text-gray-700 disabled:opacity-40 dark:border-gray-600 dark:bg-[#0d090b] dark:text-gray-200">← Prev</button>
                  <span className="font-mono text-xs font-bold">{currentPage} / {totalPages}</span>
                  <button disabled={currentPage >= totalPages} onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
                    className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-semibold text-gray-700 disabled:opacity-40 dark:border-gray-600 dark:bg-[#0d090b] dark:text-gray-200">Next →</button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {/* ═══════════ DETAIL MODAL ═══════════ */}
      {selectedAnomaly && (() => {
        const s = selectedAnomaly
        const re = riskExplanation
        const sanctioned = Number(s.sanctioned_amount || 0)
        const expenditure = Number(s.expenditure || 0)
        const completion = Number(s.completion_percentage || 0)
        const utilization = sanctioned > 0 ? (expenditure / sanctioned * 100) : 0
        const overspend = re?.overspend || (sanctioned > 0 && expenditure > sanctioned ? expenditure - sanctioned : null)
        const overspendPct = re?.overspend_pct || (overspend ? (overspend / sanctioned * 100) : null)
        const reasons = s.reasons || []
        const indicatorCount = re?.indicator_count ?? reasons.length
        const dataConfidence = re?.data_confidence || (sanctioned > 0 && expenditure === 0 && completion === 0 ? "Low" : (completion > 0 && expenditure > 0 ? "High" : "Medium"))
        const riskSignals = re?.risk_signals || []
        const aiRecommendation = re?.ai_recommendation || ""

        // Severity mapping for reasons
        const reasonSeverity = (r) => {
          const rl = r.toLowerCase()
          if (rl.includes("exceeds sanctioned") || rl.includes("0% physical") || rl.includes("disbursements made")) return "CRITICAL"
          if (rl.includes("high expenditure") || rl.includes("high-value") || rl.includes("ml") || rl.includes("anomaly")) return "HIGH"
          if (rl.includes("completed but") || rl.includes("very low fund") || rl.includes("high completion")) return "MEDIUM"
          return "LOW"
        }
        const severityColor = (sev) => {
          if (sev === "CRITICAL") return "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300 border-red-200 dark:border-red-800"
          if (sev === "HIGH") return "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300 border-orange-200 dark:border-orange-800"
          if (sev === "MEDIUM") return "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300 border-amber-200 dark:border-amber-800"
          return "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300 border-blue-200 dark:border-blue-800"
        }
        const reasonExplanation = (r) => {
          const rl = r.toLowerCase()
          if (rl.includes("exceeds sanctioned")) return `Expenditure (₹${expenditure.toLocaleString("en-IN")}) recorded against sanctioned amount (₹${sanctioned.toLocaleString("en-IN")}) — ${(utilization).toFixed(1)}% utilization. This may indicate costs beyond approved limits and requires verification.`
          if (rl.includes("high expenditure") && rl.includes("low physical completion")) return `₹${expenditure.toLocaleString("en-IN")} expenditure recorded while reported completion is only ${completion}%. The financial-physical gap may indicate premature payments or recording discrepancies.`
          if (rl.includes("disbursements made") || rl.includes("0% physical completion")) return `₹${expenditure.toLocaleString("en-IN")} spent with 0% reported physical progress. This may indicate that records have not been updated, or work has not commenced. Verification is recommended.`
          if (rl.includes("high-value project")) return `Sanctioned amount of ₹${sanctioned.toLocaleString("en-IN")} with only ${completion}% reported progress. Large-value projects with low progress may require prioritized field verification.`
          if (rl.includes("completed but")) return `Project status is recorded as "${s.status}" but physical progress is ${completion}%. This inconsistency may indicate a data recording issue.`
          if (rl.includes("high completion recorded")) return `${completion}% physical progress recorded but expenditure is ₹0. This may indicate a data recording gap.`
          if (rl.includes("very low fund")) return `Only ${utilization.toFixed(1)}% fund utilization with ${completion}% completion on a ₹${sanctioned.toLocaleString("en-IN")} project. Both financial and physical indicators suggest limited activity.`
          if (rl.includes("ml") || rl.includes("anomaly") || rl.includes("statistical")) return `The ML model identified this project as a statistical outlier compared to peer projects with similar attributes. This is an automated flag for further human review.`
          return r
        }

        return (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/60 sm:p-4 backdrop-blur-2xs" onClick={() => { setSelectedAnomaly(null); window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } })) }}>
          <div onClick={(e) => e.stopPropagation()} className="flex max-h-[92vh] sm:max-h-[88vh] w-full sm:max-w-3xl flex-col rounded-t-2xl sm:rounded-2xl border border-gray-200 bg-white shadow-soft-lg dark:border-gray-700 dark:bg-[#0d090b]">

            {/* ── HEADER ── */}
            <div className="flex items-start justify-between border-b border-gray-200 p-5 dark:border-gray-700">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-mono text-xs font-bold text-gray-400">#{s.project_id}</span>
                  <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${
                    s.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                    : s.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                    : s.risk_level === "Low" ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                    : "bg-gray-100 text-gray-600 dark:bg-gray-700"
                  }`}>
                    {s.risk_level} Risk — Score {s.risk_score}/100
                  </span>
                  {s.ml_anomaly && (
                    <span className="rounded-full bg-purple-100 px-1.5 py-0.5 text-[0.625rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">ML Anomaly</span>
                  )}
                  {getStaleProgressFlag(s) && (
                    <span className="inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[0.625rem] font-bold text-amber-600 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300">
                      <span>⚠</span><span>Data Update Notice</span>
                    </span>
                  )}
                  <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[0.625rem] font-bold ${
                    dataConfidence === "High" ? "border-green-200 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-950 dark:text-green-300"
                    : dataConfidence === "Medium" ? "border-yellow-200 bg-yellow-50 text-yellow-700 dark:border-yellow-800 dark:bg-yellow-950 dark:text-yellow-300"
                    : "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300"
                  }`}>Data: {dataConfidence}</span>
                </div>
                <h3 className="mt-2 text-lg font-bold text-gray-900 dark:text-white leading-tight">{s.project_name}</h3>
                <p className="text-xs text-gray-500 mt-0.5">📍 {s.state || "N/A"} — {s.constituency || "N/A"} {s.project_type ? `· ${s.project_type}` : ""}</p>
              </div>
              <button onClick={() => { setSelectedAnomaly(null); window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } })) }} className="ml-3 flex-shrink-0 rounded p-1 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700">✕</button>
            </div>

            {/* ── SCROLLABLE BODY ── */}
            <div className="flex-1 overflow-y-auto p-5 space-y-5">
              {loadingDetail ? (
                <div className="p-8 text-center">
                  <div className="inline-block h-6 w-6 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent" />
                  <p className="mt-2 text-xs text-gray-500">Loading detailed risk assessment...</p>
                </div>
              ) : (
                <>
                  {/* ── RISK SCORE GAUGE ── */}
                  <div className="rounded-xl border border-gray-200 bg-gray-50 p-5 dark:border-gray-700 dark:bg-[#000000]">
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Risk Score</p>
                        <div className="flex items-baseline gap-2 mt-1">
                          <span className="font-mono text-4xl font-bold text-gray-900 dark:text-white">{s.risk_score}</span>
                          <span className="font-mono text-lg text-gray-400">/ 100</span>
                        </div>
                        <div className="flex items-center gap-2 mt-2">
                          <span className={`rounded-full px-2.5 py-0.5 text-xs font-bold ${
                            s.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                            : s.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                            : s.risk_level === "Low" ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                            : "bg-gray-100 text-gray-600 dark:bg-gray-700"
                          }`}>{s.risk_level} Risk</span>
                          <span className="text-[0.6875rem] text-gray-500">{indicatorCount} indicator{indicatorCount !== 1 ? "s" : ""} triggered</span>
                          {s.ml_anomaly && <span className="text-[0.6875rem] font-semibold text-purple-600 dark:text-purple-400">· ML Outlier</span>}
                        </div>
                      </div>
                    </div>
                    {/* Gauge bar */}
                    <div className="mt-4">
                      <div className="h-3 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                        <div className={`bar-start h-full rounded-full transition-all duration-500 ${
                          s.risk_score >= 60 ? "bg-red-500" : s.risk_score >= 30 ? "bg-amber-500" : s.risk_score > 0 ? "bg-blue-500" : "bg-gray-300"
                        }`} style={{ width: `${Math.max(2, s.risk_score)}%` }} />
                      </div>
                      <div className="flex justify-between mt-1 text-[0.5625rem] text-gray-400">
                        <span>0</span><span>25</span><span>50</span><span>75</span><span>100</span>
                      </div>
                    </div>
                  </div>

                  {/* ── RECOMMENDATION & TIMELINE ── same backend rec source
                      of truth as the Projects page — identical values. */}
                  <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
                    <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Recommendation & Timeline</p>
                    <div className="mt-2.5">
                      <RecTimelineBlock rec={detailRec || selectedAnomaly?.rec} />
                    </div>
                  </div>

                  {/* ── DATA CONFIDENCE ── */}
                  <div className={`rounded-xl border p-4 ${
                    dataConfidence === "High" ? "border-green-200 bg-green-50/50 dark:border-green-900/40 dark:bg-green-950/20"
                    : dataConfidence === "Medium" ? "border-yellow-200 bg-yellow-50/50 dark:border-yellow-900/40 dark:bg-yellow-950/20"
                    : "border-red-200 bg-red-50/50 dark:border-red-900/40 dark:bg-red-950/20"
                  }`}>
                    <div className="flex items-center gap-2">
                      <span className={`text-xs font-bold uppercase tracking-wider ${
                        dataConfidence === "High" ? "text-green-600 dark:text-green-400"
                        : dataConfidence === "Medium" ? "text-yellow-600 dark:text-yellow-400"
                        : "text-red-600 dark:text-red-400"
                      }`}>Data Confidence: {dataConfidence}</span>
                    </div>
                    <p className="mt-1.5 text-xs text-gray-600 dark:text-gray-400">
                      {dataConfidence === "Low"
                        ? "Progress data may be missing or outdated. This assessment is based on incomplete project records and should be interpreted with caution."
                        : dataConfidence === "Medium"
                        ? "Some project fields are missing or zero. Assessment is based on partially available records."
                        : "Project data appears complete. Risk assessment is based on full financial and progress information."
                      }
                    </p>
                    {dataConfidence === "Low" && (expenditure === 0 && completion === 0 && sanctioned > 0) && (
                      <p className="mt-1.5 text-[0.625rem] text-gray-500 dark:text-gray-400 italic">This indicator does not confirm project delay or irregularity — it reflects potential data freshness gaps.</p>
                    )}
                  </div>

                  {/* ── FINANCIAL OVERVIEW ── */}
                  <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
                    <h4 className="mb-3 text-xs font-bold uppercase tracking-wider text-gray-400">Financial Overview</h4>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                      <div>
                        <p className="text-[0.625rem] font-bold uppercase text-gray-400">Sanctioned</p>
                        <p className="font-mono text-sm font-bold text-blue-700 dark:text-blue-400">{formatMoney(sanctioned)}</p>
                      </div>
                      <div>
                        <p className="text-[0.625rem] font-bold uppercase text-gray-400">Expenditure</p>
                        <p className="font-mono text-sm font-bold">{formatMoney(expenditure)}</p>
                      </div>
                      <div>
                        <p className="text-[0.625rem] font-bold uppercase text-gray-400">Progress</p>
                        <p className="font-mono text-sm font-bold">{completion}%</p>
                      </div>
                      <div>
                        <p className="text-[0.625rem] font-bold uppercase text-gray-400">Utilization</p>
                        <p className="font-mono text-sm font-bold">{utilization.toFixed(1)}%</p>
                      </div>
                    </div>
                    {overspend !== null && overspend > 0 && (
                      <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 dark:border-red-900/40 dark:bg-red-950/20">
                        <div className="flex items-center gap-3 text-xs">
                          <span className="font-bold text-red-600 dark:text-red-400">Overspend:</span>
                          <span className="font-mono font-bold text-red-700 dark:text-red-300">+{formatMoney(overspend)}</span>
                          <span className="text-red-500 dark:text-red-400">Over sanction: +{overspendPct.toFixed(1)}%</span>
                        </div>
                      </div>
                    )}
                    {sanctioned > 0 && (
                      <div className="mt-3">
                        <div className="h-2 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                          <div className={`bar-fill h-full rounded-full ${utilization > 100 ? "bg-red-500" : "bg-blue-600"}`} style={{ width: `${Math.min(100, utilization)}%` }} />
                        </div>
                      </div>
                    )}
                  </div>

                  {/* ── RECORDED EXPENDITURE ACTIVITY (delay estimate) ── */}
                  {s.activity_days !== null && s.activity_days !== undefined && (
                    <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <h4 className="text-xs font-bold uppercase tracking-wider text-gray-400" title={DELAY_TOOLTIP}>Recorded Expenditure Activity ⓘ</h4>
                        {s.delay_indicator && (
                          <span className={`rounded-full px-2 py-0.5 text-[0.625rem] font-bold ${DELAY_BADGE_STYLES[s.delay_severity] || DELAY_BADGE_STYLES.low}`} title={DELAY_TOOLTIP}>{s.delay_indicator}</span>
                        )}
                      </div>
                      <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-4">
                        <div>
                          <p className="text-[0.625rem] font-bold uppercase text-gray-400">Delayed By ⓘ</p>
                          <p className="font-mono text-sm font-bold text-gray-900 dark:text-white" title={DELAY_TOOLTIP}>~{s.activity_days.toLocaleString("en-IN")} days</p>
                        </div>
                        <div>
                          <p className="text-[0.625rem] font-bold uppercase text-gray-400">First Recorded Expenditure</p>
                          <p className="font-mono text-sm font-bold">{formatDate(s.expenditure_first_date)}</p>
                        </div>
                        <div>
                          <p className="text-[0.625rem] font-bold uppercase text-gray-400">Latest Recorded Expenditure</p>
                          <p className="font-mono text-sm font-bold">{formatDate(s.expenditure_latest_date)}</p>
                        </div>
                        <div>
                          <p className="text-[0.625rem] font-bold uppercase text-gray-400">Physical Completion</p>
                          <p className="font-mono text-sm font-bold">{completion}%</p>
                        </div>
                      </div>
                      <p className="mt-3 text-[0.625rem] italic text-gray-500 dark:text-gray-400">{DELAY_TOOLTIP}</p>
                    </div>
                  )}

                  {/* ── RISK DETECTION BREAKDOWN ── */}
                  {riskSignals.length > 0 && (
                    <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
                      <h4 className="mb-3 text-xs font-bold uppercase tracking-wider text-gray-400">Risk Detection Breakdown</h4>
                      <div className="overflow-x-auto">
                        <table className="w-full text-xs">
                          <thead>
                            <tr className="border-b border-gray-200 dark:border-gray-700">
                              <th className="pb-2 text-left font-bold text-gray-500">Signal</th>
                              <th className="pb-2 text-right font-bold text-gray-500">Value</th>
                              <th className="pb-2 text-right font-bold text-gray-500">Impact</th>
                            </tr>
                          </thead>
                          <tbody>
                            {riskSignals.map((sig, i) => (
                              <tr key={i} className="border-b border-gray-100 dark:border-gray-700/50 last:border-0">
                                <td className="py-2 font-semibold text-gray-700 dark:text-gray-300">{sig.signal}</td>
                                <td className="py-2 text-right font-mono font-bold text-gray-900 dark:text-white">{sig.value}</td>
                                <td className="py-2 text-right">
                                  <span className={`inline-block rounded px-1.5 py-0.5 text-[0.625rem] font-bold ${
                                    sig.impact === "HIGH" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                                    : sig.impact === "MEDIUM" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                                    : sig.impact === "LOW" ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                                    : "bg-gray-100 text-gray-500 dark:bg-gray-700"
                                  }`}>{sig.impact}</span>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  {/* ── WHY THIS RECORD WAS FLAGGED ── */}
                  {reasons.length > 0 && (
                    <div className="rounded-xl border border-red-200 bg-red-50/50 p-4 dark:border-red-900/40 dark:bg-red-950/20">
                      <h4 className="mb-3 text-xs font-bold uppercase tracking-wider text-red-600 dark:text-red-400">Why This Record Was Flagged</h4>
                      <div className="space-y-3">
                        {[...reasons].sort((a, b) => {
                          const order = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }
                          return (order[reasonSeverity(a)] ?? 4) - (order[reasonSeverity(b)] ?? 4)
                        }).map((reason, i) => {
                          const sev = reasonSeverity(reason)
                          return (
                            <div key={i} className="rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-[#0d090b]">
                              <div className="flex items-center gap-2 mb-1.5">
                                <span className={`rounded border px-1.5 py-0.5 text-[0.5625rem] font-bold ${severityColor(sev)}`}>{sev}</span>
                                <span className="text-xs font-bold text-gray-800 dark:text-gray-200">{reason}</span>
                              </div>
                              <p className="text-[0.6875rem] text-gray-600 dark:text-gray-400 leading-relaxed pl-1">
                                {reasonExplanation(reason)}
                              </p>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )}

                  {/* ── DATA UPDATE NOTICE ── */}
                  {getStaleProgressFlag(s) && (
                    <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 dark:border-amber-800/60 dark:bg-amber-950/40">
                      <h4 className="mb-2 text-xs font-bold uppercase tracking-wider text-amber-700 dark:text-amber-300">⚠ Data Update Notice</h4>
                      <p className="text-xs text-amber-800 dark:text-amber-200">
                        Reported progress or expenditure may not reflect the latest project status. A risk score indicates an anomaly based on available data and does not by itself confirm project delay or irregularity.
                      </p>
                      <div className="mt-2 grid grid-cols-3 gap-2 text-[0.625rem]">
                        <div><span className="font-bold text-amber-600 dark:text-amber-400">Sanctioned:</span> {formatMoney(sanctioned)}</div>
                        <div><span className="font-bold text-amber-600 dark:text-amber-400">Recorded Expenditure:</span> ₹0</div>
                        <div><span className="font-bold text-amber-600 dark:text-amber-400">Recorded Progress:</span> 0%</div>
                      </div>
                      <p className="mt-2 text-[0.625rem] italic text-amber-600/70 dark:text-amber-400/70">This indicator does not confirm project delay or irregularity.</p>
                    </div>
                  )}

                  {/* ── AI RECOMMENDATION ── */}
                  {aiRecommendation && (
                    <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-4 dark:border-indigo-900/40 dark:bg-indigo-950/20">
                      <h4 className="mb-2 text-xs font-bold uppercase tracking-wider text-indigo-600 dark:text-indigo-400">🤖 AI Recommendation</h4>
                      <p className="text-xs text-gray-700 dark:text-gray-300 leading-relaxed">{aiRecommendation}</p>
                    </div>
                  )}
                </>
              )}
            </div>

            {/* ── FOOTER ── */}
            <div className="border-t border-gray-200 p-4 flex flex-wrap items-center justify-between gap-2 dark:border-gray-700">
              <button onClick={() => { setSelectedAnomaly(null); window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } })) }} className="rounded-lg border border-gray-300 px-4 py-2 text-xs font-bold text-gray-700 dark:border-gray-600 dark:text-gray-200">Close</button>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => {
                    // Generate Audit Summary
                    const re2 = riskExplanation
                    const lines = [
                      `AUDIT SUMMARY — Project #${s.project_id}`,
                      `="=".repeat(40)`,
                      ``,
                      `Project: ${s.project_name}`,
                      `ID: ${s.project_id}`,
                      `State: ${s.state || "N/A"}`,
                      `Constituency: ${s.constituency || "N/A"}`,
                      `Type: ${s.project_type || "N/A"}`,
                      `Status: ${s.status || "N/A"}`,
                      ``,
                      `FINANCIAL DATA`,
                      `Sanctioned: ${formatMoney(sanctioned)}`,
                      `Expenditure: ${formatMoney(expenditure)}`,
                      `Utilization: ${utilization.toFixed(1)}%`,
                      overspend ? `Overspend: +${formatMoney(overspend)} (+${overspendPct?.toFixed(1)}%)` : null,
                      `Physical Progress: ${completion}%`,
                      ``,
                      `RISK ASSESSMENT`,
                      `Risk Score: ${s.risk_score}/100`,
                      `Risk Level: ${s.risk_level}`,
                      `ML Anomaly: ${s.ml_anomaly ? "Yes" : "No"}`,
                      `Data Confidence: ${dataConfidence}`,
                      `Indicators Triggered: ${indicatorCount}`,
                      ``,
                      `DETECTED ANOMALIES`,
                      ...reasons.map((r, i) => `  ${i + 1}. [${reasonSeverity(r)}] ${r}`),
                      ``,
                      `AI RECOMMENDATION`,
                      aiRecommendation || "No specific recommendation.",
                      ``,
                      `SUGGESTED ACTION`,
                      s.risk_level === "High" ? `Prioritize for verification. This project has ${indicatorCount} active risk indicators and a high-risk classification.`
                      : s.risk_level === "Medium" ? `Schedule for periodic review. ${indicatorCount} moderate indicators detected.`
                      : `Continue standard monitoring.`,
                      ``,
                      `This summary is generated from available project records and automated risk analysis.`,
                      `It is intended to support — not replace — human verification and contextual review.`,
                    ].filter(Boolean).join("\n")
                    const blob = new Blob([lines], { type: "text/plain" })
                    const url = URL.createObjectURL(blob)
                    const a = document.createElement("a")
                    a.href = url
                    a.download = `audit-summary-${s.project_id}.txt`
                    a.click()
                    URL.revokeObjectURL(url)
                  }}
                  className="rounded-lg border border-gray-300 px-3 py-2 text-[0.6875rem] font-bold text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700">
                  📄 Generate Audit Summary
                </button>
                <button
                  onClick={() => {
                    window.dispatchEvent(new CustomEvent("navigate-to-project", { detail: { query: String(s.project_id) } }))
                    setSelectedAnomaly(null); window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } }))
                  }}
                  className="rounded-lg bg-[#c8102e] px-4 py-2 text-xs font-bold text-white dark:bg-blue-600">
                  View Full Project →
                </button>
              </div>
            </div>
          </div>
        </div>
        )
      })()}
    </div>
  )
})

export default RiskCenter
