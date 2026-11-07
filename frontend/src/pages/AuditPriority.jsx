import { useEffect, useState, useCallback, memo } from "react"
import { getAuditPriority, getAuditPrioritySummary, getStates, getConstituencies, getProjectDetail } from "../services/api"
import { formatMoney } from "../utils/format"
import { MobileCardsSkeleton } from "../components/Skeletons"

/* ─── Reason chip helpers ─── */
const REASON_CHIPS = {
  "exceeds sanctioned": { label: "Cost Overrun", color: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" },
  "high expenditure": { label: "Exp-Progress Gap", color: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300" },
  "0% physical": { label: "Zero Progress", color: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" },
  "completed but": { label: "Status Mismatch", color: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300" },
  "high-value project": { label: "High-Value Delay", color: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300" },
  "high completion": { label: "Completion-Exp Gap", color: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300" },
  "very low fund": { label: "Low Utilization", color: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300" },
  "ml": { label: "ML Outlier", color: "bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300" },
  "disbursements": { label: "Zero Progress", color: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" },
}

function getReasonChip(reason) {
  const rl = (reason || "").toLowerCase()
  for (const [key, chip] of Object.entries(REASON_CHIPS)) {
    if (rl.includes(key)) return chip
  }
  return { label: reason || "Flagged", color: "bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300" }
}

function getReasonDetail(reason, p) {
  const rl = (reason || "").toLowerCase()
  const sanctioned = Number(p.sanctioned_amount || 0)
  const expenditure = Number(p.expenditure || 0)
  const completion = Number(p.completion_percentage || 0)
  const utilization = sanctioned > 0 ? (expenditure / sanctioned * 100) : 0

  if (rl.includes("exceeds sanctioned")) return `Expenditure (${formatMoney(expenditure)}) exceeds sanctioned amount (${formatMoney(sanctioned)}). Variance: ${formatMoney(expenditure - sanctioned)}.`
  if (rl.includes("high expenditure")) return `Fund utilization at ${utilization.toFixed(0)}% while physical progress is only ${completion}%. Financial and physical progress are significantly out of sync.`
  if (rl.includes("0% physical") || rl.includes("disbursements")) return `${formatMoney(expenditure)} has been disbursed but physical completion remains at 0%. Work may not have commenced or records may not be updated.`
  if (rl.includes("completed but")) return `Status is "${p.status}" but physical progress is ${completion}%, below the 90% completion threshold.`
  if (rl.includes("high-value project")) return `Sanctioned amount of ${formatMoney(sanctioned)} with only ${completion}% reported progress indicates severe execution delay.`
  if (rl.includes("high completion")) return `Progress at ${completion}% with zero expenditure. This may indicate a data recording gap.`
  if (rl.includes("very low fund")) return `Only ${utilization.toFixed(0)}% of ${formatMoney(sanctioned)} utilized with ${completion}% progress.`
  if (rl.includes("ml")) return `The ML model flagged this project as a statistical outlier compared to peer projects with similar attributes.`
  return reason
}

/* ─── Tier helpers ─── */
const TIER_STYLE = {
  P1: { chip: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300", bar: "bg-red-500", text: "text-red-600 dark:text-red-400" },
  P2: { chip: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300", bar: "bg-orange-500", text: "text-orange-600 dark:text-orange-400" },
  P3: { chip: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300", bar: "bg-amber-500", text: "text-amber-600 dark:text-amber-400" },
  P4: { chip: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300", bar: "bg-blue-500", text: "text-blue-600 dark:text-blue-400" },
}
const TIER_DEFAULT = { chip: "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300", bar: "bg-gray-400", text: "text-gray-600 dark:text-gray-400" }

function tierStyle(tier) {
  return TIER_STYLE[tier] || TIER_DEFAULT
}

function money(v) {
  const n = Number(v || 0)
  if (n >= 10000000) return `₹${(n / 10000000).toFixed(2)} Cr`
  if (n >= 100000) return `₹${(n / 100000).toFixed(2)} L`
  return `₹${n.toLocaleString("en-IN")}`
}

/* ═══════════════ MAIN COMPONENT ═══════════════ */
const AuditPriority = memo(function AuditPriority({ fy, drillDownParams, onClearDrillDown }) {
  const [priorities, setPriorities] = useState([])
  const [totalCount, setTotalCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [states, setStates] = useState([])
  const [constituencies, setConstituencies] = useState([])
  const [summary, setSummary] = useState({
    total_flagged: 0, high_risk: 0, medium_risk: 0, ml_anomalies: 0,
    tiers: { P1: { count: 0 }, P2: { count: 0 }, P3: { count: 0 }, P4: { count: 0 } },
    tier_labels: { P1: "Immediate Review", P2: "High Priority", P3: "Review", P4: "Monitor" },
    total_sanctioned_under_review: 0,
  })

  const [filterState, setFilterState] = useState("")
  const [filterConstituency, setFilterConstituency] = useState("")
  const [filterSeverity, setFilterSeverity] = useState("")
  const [filterTier, setFilterTier] = useState("")
  const [searchQuery, setSearchQuery] = useState("")
  const [sortBy, setSortBy] = useState("audit_priority")
  const [sortDir, setSortDir] = useState("asc")
  const [filterHouse, setFilterHouse] = useState("")
  const [currentPage, setCurrentPage] = useState(1)
  const [expanded, setExpanded] = useState(null)
  const rowsPerPage = 20

  const [selectedProject, setSelectedProject] = useState(null)
  const [detailRisk, setDetailRisk] = useState(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  /* ── Load static data ── */
  useEffect(() => {
    getStates().then((s) => { if (Array.isArray(s)) setStates(s) }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!filterState) { setConstituencies([]); setFilterConstituency(""); return }
    getConstituencies(filterState).then((d) => { if (Array.isArray(d)) setConstituencies(d) }).catch(() => {})
  }, [filterState])

  /* ── Load summary ── */
  useEffect(() => {
    const params = {}
    if (fy) params.fy = fy
    if (filterHouse) params.house = filterHouse
    getAuditPrioritySummary(params).then((s) => { if (s) setSummary(s) }).catch(() => {})
  }, [fy, filterHouse])

  /* ── Fetch priorities ── */
  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      setError("")
      const params = {
        skip: (currentPage - 1) * rowsPerPage,
        limit: rowsPerPage,
      }
      if (filterState) params.state = filterState
      if (filterConstituency) params.constituency = filterConstituency
      if (fy) params.fy = fy
      if (filterSeverity) params.risk_level = filterSeverity
      if (filterTier) params.tier = filterTier
      if (filterHouse) params.house = filterHouse
      if (searchQuery.trim()) params.q = searchQuery.trim()
      if (sortBy) { params.sort_by = sortBy; params.sort_dir = sortDir }

      const data = await getAuditPriority(params)
      setPriorities(data?.priorities || [])
      setTotalCount(data?.total || 0)
    } catch (err) {
      console.error("Audit priority error:", err)
      setError("Failed to load audit priority data.")
    } finally {
      setLoading(false)
    }
  }, [filterState, filterConstituency, fy, filterSeverity, filterTier, filterHouse, searchQuery, currentPage, sortBy, sortDir])

  useEffect(() => { fetchData() }, [fetchData])

  const handleReset = () => {
    setFilterState(""); setFilterConstituency(""); setFilterSeverity(""); setFilterTier(""); setFilterHouse("")
    setSearchQuery(""); setSortBy("audit_priority"); setSortDir("asc")
    setCurrentPage(1)
  }

  // Assistant control plane: tier/severity/state drill-down and clear-filters.
  useEffect(() => {
    if (drillDownParams) {
      if (drillDownParams.tier) setFilterTier(drillDownParams.tier)
      if (drillDownParams.severity) setFilterSeverity(drillDownParams.severity)
      if (drillDownParams.state) setFilterState(drillDownParams.state)
      setCurrentPage(1)
      if (onClearDrillDown) onClearDrillDown()
    }
  }, [drillDownParams, onClearDrillDown])

  useEffect(() => {
    const handler = () => handleReset()
    window.addEventListener("assistant:clear-filters", handler)
    return () => window.removeEventListener("assistant:clear-filters", handler)
  }, [])

  const handleOpenDetail = async (item) => {
    setSelectedProject(item)
    // Assistant project-context: the shared assistant hears which project is open.
    window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: item.project_id, projectName: item.project_name || null } }))
    setDetailRisk(null)
    setLoadingDetail(true)
    try {
      const detail = await getProjectDetail(item.project_id)
      if (detail?.risk) setDetailRisk(detail.risk)
    } catch (err) { console.error("Detail error:", err) }
    finally { setLoadingDetail(false) }
  }

  const totalPages = Math.max(1, Math.ceil(totalCount / rowsPerPage))

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 sm:p-6 text-[#292326] transition-colors duration-200 dark:bg-[#000000] dark:text-[#f5eff1]">
      <div className="mx-auto max-w-[1440px] space-y-4 sm:space-y-5">

        {/* ═══ HEADER ═══ */}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-2xl font-bold text-[#292326] dark:text-white">Audit Priority</h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400 max-w-3xl">
              Rank flagged projects by audit urgency, financial exposure, and risk — P1–P4 priority tiers built from the
              composite priority score (risk score, financial exposure, the financial/physical progress mismatch and the
              evidence gap — with a floor applied from the stored risk classification) so auditors know what to review first.
            </p>
          </div>
          <div className="rounded-xl border border-gray-200 bg-white px-4 py-2.5 text-right shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
            <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Total priority projects</p>
            <p className="font-mono text-2xl font-bold text-[#292326] dark:text-white">{(summary.total_flagged || 0).toLocaleString("en-IN")}</p>
            <p className="mt-0.5 text-[0.625rem] text-gray-400">
              Funds under review: <span className="font-mono font-bold">{money(summary.total_sanctioned_under_review)}</span>
            </p>
          </div>
        </div>

        {/* ═══ DISCLAIMER ═══ */}
        <div className="rounded-lg border border-gray-200 bg-white px-4 py-2.5 text-[0.6875rem] text-gray-500 dark:border-gray-700 dark:bg-[#0d090b] dark:text-gray-400">
          Projects are ranked using financial discrepancies, physical progress, expenditure patterns, and detected
          anomalies. A high priority score indicates that a project deserves review; it does not by itself establish
          wrongdoing. Priority scoring adds no new data — it only orders the works that existing detection has flagged.
        </div>

        {/* ═══ TIER SUMMARY CARDS ═══ */}
        <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-4">
          {["P1", "P2", "P3", "P4"].map((code) => {
            const tier = summary.tiers?.[code] || { count: 0, sanctioned_under_review: 0 }
            const style = tierStyle(code)
            return (
              <button
                key={code}
                onClick={() => { setFilterTier(filterTier === code ? "" : code); setCurrentPage(1) }}
                className={`rounded-xl border bg-white p-4 text-left shadow-sm transition dark:bg-[#0d090b] ${
                  filterTier === code ? "border-[#c8102e] ring-1 ring-[#c8102e]/20 dark:border-blue-500" : "border-gray-200 hover:border-gray-300 dark:border-gray-700"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className={`rounded px-1.5 py-0.5 text-[0.625rem] font-bold ${style.chip}`}>{code}</span>
                  <span className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">{summary.tier_labels?.[code] || ""}</span>
                </div>
                <p className={`mt-1.5 font-mono text-3xl font-bold ${style.text}`}>{(tier.count || 0).toLocaleString("en-IN")}</p>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                  <div className={`bar-fill h-full rounded-full ${style.bar}`} style={{ width: `${summary.total_flagged ? Math.max(2, (tier.count / summary.total_flagged) * 100) : 0}%` }} />
                </div>
                <p className="mt-1 text-[0.5625rem] text-gray-400">Sanctioned: <span className="font-mono">{money(tier.sanctioned_under_review)}</span></p>
              </button>
            )
          })}
        </div>

        {/* ═══ SECONDARY STATS ═══ */}
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded-lg border border-gray-200 bg-white px-4 py-2 text-[0.6875rem] text-gray-500 dark:border-gray-700 dark:bg-[#0d090b] dark:text-gray-400">
          <span>High risk level: <span className="font-mono font-bold text-red-600 dark:text-red-400">{(summary.high_risk || 0).toLocaleString("en-IN")}</span></span>
          <span>Medium risk level: <span className="font-mono font-bold text-amber-600 dark:text-amber-400">{(summary.medium_risk || 0).toLocaleString("en-IN")}</span></span>
          <span>ML outliers: <span className="font-mono font-bold text-purple-600 dark:text-purple-400">{(summary.ml_anomalies || 0).toLocaleString("en-IN")}</span></span>
          <span className="text-gray-400">Tier = composite priority score, floored by risk level (High → at least P2).</span>
        </div>

        {/* ═══ FILTERS ═══ */}
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-6">
            <div>
              <label className="block text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Search</label>
              <input value={searchQuery} onChange={(e) => { setSearchQuery(e.target.value); setCurrentPage(1) }}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); fetchData() } }}
                placeholder="Project name or ID..."
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#000000] dark:text-white" />
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
              <label className="block text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Priority Tier</label>
              <select value={filterTier} onChange={(e) => { setFilterTier(e.target.value); setCurrentPage(1) }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#000000] dark:text-white">
                <option value="">All Tiers</option>
                <option value="P1">P1 — Immediate Review</option>
                <option value="P2">P2 — High Priority</option>
                <option value="P3">P3 — Review</option>
                <option value="P4">P4 — Monitor</option>
              </select>
            </div>
            <div>
              <label className="block text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Risk Level</label>
              <select value={filterSeverity} onChange={(e) => { setFilterSeverity(e.target.value); setCurrentPage(1) }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#000000] dark:text-white">
                <option value="">All Levels</option>
                <option value="High">High Risk</option>
                <option value="Medium">Medium Risk</option>
                <option value="Low">Low Risk</option>
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
              <button onClick={handleReset} className="w-full rounded-lg border border-gray-300 bg-white px-4 py-2 text-xs font-bold text-gray-700 transition hover:bg-gray-50 dark:border-gray-600 dark:bg-[#000000] dark:text-gray-200">Reset Filters</button>
            </div>
          </div>
          {/* Sort */}
          <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-gray-100 pt-3 dark:border-gray-700/60">
            <span className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Sort:</span>
            {[
              { value: "audit_priority", label: "Audit Priority" },
              { value: "risk_score", label: "Risk Score" },
              { value: "sanctioned_amount", label: "Sanctioned" },
              { value: "expenditure", label: "Expenditure" },
              { value: "completion_percentage", label: "Progress" },
              { value: "state", label: "State" },
              { value: "id", label: "ID" },
            ].map((opt) => (
              <button key={opt.value} onClick={() => {
                if (sortBy === opt.value) setSortDir((d) => d === "asc" ? "desc" : "asc")
                else { setSortBy(opt.value); setSortDir(opt.value === "id" || opt.value === "state" || opt.value === "audit_priority" ? "asc" : "desc") }
                setCurrentPage(1)
              }}
                className={`rounded-lg px-3 py-1.5 text-[0.6875rem] font-bold transition ${sortBy === opt.value ? "bg-[#c8102e] text-white dark:bg-blue-600" : "border border-gray-200 bg-white text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:bg-[#0d090b] dark:text-gray-300"}`}>
                {opt.label}{sortBy === opt.value && <span className="ml-1">{sortDir === "asc" ? "↑" : "↓"}</span>}
              </button>
            ))}
          </div>
        </div>

        {/* ═══ ERROR ═══ */}
        {error && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm font-medium text-red-700 dark:border-red-800/60 dark:bg-red-950/40 dark:text-red-300">⚠ {error}</div>
        )}

        {/* ═══ PRIORITY LIST ═══ */}
        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
          {loading ? (
            <>
              <div className="hidden lg:block divide-y divide-gray-100 dark:divide-gray-700/60">
                {Array.from({ length: 6 }).map((_, i) => (
                  <div key={i} className="flex items-center gap-4 px-5 py-4">
                    <div className="animate-pulse rounded bg-gray-200/80 dark:bg-[#120b0e]/80 h-8 w-8 shrink-0" />
                    <div className="flex-1 space-y-2">
                      <div className="animate-pulse rounded bg-gray-200/80 dark:bg-[#120b0e]/80 h-4 w-2/3" />
                      <div className="animate-pulse rounded bg-gray-200/80 dark:bg-[#120b0e]/80 h-3 w-1/3" />
                    </div>
                    <div className="animate-pulse rounded-full bg-gray-200/80 dark:bg-[#120b0e]/80 h-6 w-16" />
                  </div>
                ))}
              </div>
              <div className="lg:hidden">
                <MobileCardsSkeleton rows={5} />
              </div>
            </>
          ) : priorities.length === 0 ? (
            <div className="p-16 text-center text-sm text-gray-500">No audit priorities match the current filters.</div>
          ) : (
            <>
              {/* ── DESKTOP CARDS ── */}
              <div className="hidden lg:block divide-y divide-gray-100 dark:divide-gray-700/60">
                {priorities.map((p) => {
                  const sanctioned = Number(p.sanctioned_amount || 0)
                  const expenditure = Number(p.expenditure || 0)
                  const completion = Number(p.completion_percentage || 0)
                  const utilization = sanctioned > 0 ? (expenditure / sanctioned * 100) : 0
                  const reasons = p.reasons || []
                  const visibleReasons = reasons.slice(0, 3)
                  const scoreColor = p.risk_score >= 60 ? "text-red-600 dark:text-red-400" : p.risk_score >= 30 ? "text-amber-600 dark:text-amber-400" : "text-blue-600 dark:text-blue-400"
                  const barColor = p.risk_score >= 60 ? "bg-red-500" : p.risk_score >= 30 ? "bg-amber-500" : "bg-blue-500"
                  const style = tierStyle(p.tier)
                  const isCritical = p.tier === "P1"
                  const isExpanded = expanded === p.project_id

                  return (
                    <div key={p.project_id} className="cursor-pointer transition hover:bg-blue-50/30 dark:hover:bg-[#120b0e]" onClick={() => handleOpenDetail(p)}>
                      <div className="flex items-stretch gap-0">
                      {/* Priority Rank */}
                      <div className={`flex w-16 flex-shrink-0 flex-col items-center justify-center border-r ${isCritical ? "border-red-200 bg-red-50 dark:border-red-900/40 dark:bg-red-950/20" : "border-gray-100 dark:border-gray-700/60"}`}>
                        <span className="text-[0.625rem] font-bold uppercase text-gray-400">Rank</span>
                        <span className={`font-mono text-xl font-bold ${isCritical ? "text-red-600 dark:text-red-400" : "text-gray-900 dark:text-white"}`}>{p.priority_rank}</span>
                      </div>

                      {/* Main Content */}
                      <div className="min-w-0 flex-1 p-4">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0 flex-1">
                            {/* Title Row */}
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className={`rounded px-2 py-0.5 text-[0.625rem] font-bold ${style.chip}`} title={(p.explanation || []).join(" ")}>
                                {p.tier} — {p.tier_label}
                              </span>
                              <span className="font-mono text-[0.625rem] font-bold text-gray-400">#{p.project_id}</span>
                              <span className={`rounded px-2 py-0.5 text-[0.625rem] font-bold ${
                                p.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                                : p.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                                : "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                              }`}>{p.risk_level}</span>
                              {p.ml_anomaly && <span className="rounded-full bg-purple-100 px-1.5 py-0.5 text-[0.5625rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">ML</span>}
                              {p.data_quality_flag === "POSSIBLY_STALE" && (
                                <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[0.5625rem] font-bold text-amber-700 dark:bg-amber-950 dark:text-amber-300" title={p.data_quality_reason}>
                                  ⚠ Data update required
                                </span>
                              )}
                            </div>
                            <h3 className="mt-1 truncate font-semibold text-sm text-gray-900 dark:text-white" title={p.project_name}>{p.project_name || "Unnamed"}</h3>
                            <p className="text-[0.6875rem] text-gray-500">{p.state || "N/A"}{p.constituency ? ` — ${p.constituency}` : ""}{p.house ? ` · ${p.house === "Lok Sabha" ? "Lok Sabha" : p.house === "Rajya Sabha" ? "Rajya Sabha" : p.house}` : ""}</p>

                            {/* Why Prioritized Chips */}
                            {visibleReasons.length > 0 && (
                              <div className="mt-2 flex flex-wrap gap-1.5">
                                {visibleReasons.map((r, i) => {
                                  const chip = getReasonChip(r)
                                  return <span key={i} className={`rounded-md px-2 py-0.5 text-[0.625rem] font-bold ${chip.color}`}>{chip.label}</span>
                                })}
                                {reasons.length > 3 && (
                                  <span className="rounded-md bg-gray-100 px-2 py-0.5 text-[0.625rem] font-bold text-gray-500 dark:bg-gray-700 dark:text-gray-400">+{reasons.length - 3} more</span>
                                )}
                              </div>
                            )}
                          </div>

                          {/* Risk Score */}
                          <div className="flex-shrink-0 text-right">
                            <div className="flex items-baseline gap-1">
                              <span className={`font-mono text-2xl font-bold ${scoreColor}`}>{p.risk_score}</span>
                              <span className="font-mono text-xs text-gray-400">/100</span>
                            </div>
                            <div className="mt-1.5 h-2 w-24 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                              <div className={`bar-fill h-full rounded-full ${barColor}`} style={{ width: `${Math.max(2, p.risk_score)}%` }} />
                            </div>
                          </div>
                        </div>

                        {/* Financial Mini-Row */}
                        <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-gray-600 dark:text-gray-400">
                          <span>Sanctioned: <strong className="font-mono text-gray-900 dark:text-white">{formatMoney(sanctioned)}</strong></span>
                          <span>Spent: <strong className="font-mono text-gray-900 dark:text-white">{formatMoney(expenditure)}</strong></span>
                          <span>Progress: <strong className="font-mono text-gray-900 dark:text-white">{completion}%</strong></span>
                          <span>Utilization: <strong className={`font-mono ${utilization > 100 ? "text-red-600 dark:text-red-400" : ""}`}>{utilization.toFixed(0)}%</strong></span>
                          <span>Funds under review: <strong className="font-mono text-gray-900 dark:text-white">{money(p.financial_exposure?.funds_under_review ?? sanctioned)}</strong></span>
                        </div>

                        {/* Audit intelligence row */}
                        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
                          <div className="rounded-lg bg-gray-50 px-2.5 py-1.5 dark:bg-[#120b0e]">
                            <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Main anomaly</p>
                            <p className="text-[0.625rem] font-semibold text-gray-700 dark:text-gray-200">{p.primary_anomaly}</p>
                          </div>
                          <div className="rounded-lg bg-gray-50 px-2.5 py-1.5 dark:bg-[#120b0e]">
                            <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Evidence gap</p>
                            <p className="text-[0.625rem] font-semibold text-gray-700 dark:text-gray-200" title={p.evidence_gap?.summary}>
                              {p.evidence_gap?.missing_count ?? 0} item(s) unavailable
                            </p>
                          </div>
                          <div className="rounded-lg bg-gray-50 px-2.5 py-1.5 dark:bg-[#120b0e]">
                            <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Recommended action</p>
                            <p className="text-[0.625rem] font-semibold text-gray-700 dark:text-gray-200">{p.recommended_action}</p>
                          </div>
                        </div>

                        {/* Why this priority (expandable) */}
                        <button
                          onClick={(e) => { e.stopPropagation(); setExpanded(isExpanded ? null : p.project_id) }}
                          className="mt-2 text-[0.625rem] font-bold text-blue-600 hover:underline dark:text-blue-400"
                        >
                          {isExpanded ? "Hide priority explanation" : "Why this priority?"}
                        </button>
                        {isExpanded && (
                          <div className="mt-2 rounded-lg border border-gray-200 bg-gray-50 p-2.5 dark:border-gray-700 dark:bg-[#120b0e]">
                            <ul className="space-y-1">
                              {(p.explanation || []).map((exp, i) => (
                                <li key={i} className="flex items-start gap-1.5 text-[0.625rem] text-gray-600 dark:text-gray-300">
                                  <span className="mt-0.5 text-gray-400">•</span><span>{exp}</span>
                                </li>
                              ))}
                            </ul>
                            <p className="mt-1.5 font-mono text-[0.5625rem] text-gray-400">
                              score {p.audit_priority_score}/100 · {p.formula || "risk(0-55) + exposure(0-20) + mismatch(0-15) + evidence_gap(0-10)"}
                            </p>
                            {p.evidence_gap?.missing_labels?.length > 0 && (
                              <p className="mt-1 text-[0.5625rem] text-gray-400">
                                Evidence not available: {p.evidence_gap.missing_labels.join("; ")}
                              </p>
                            )}
                            {(p.recommended_actions || []).length > 1 && (
                              <p className="mt-1 text-[0.5625rem] text-gray-400">
                                Further actions: {(p.recommended_actions || []).slice(1).join("; ")}
                              </p>
                            )}
                          </div>
                        )}
                      </div>
                      </div>
                    </div>
                  )
                })}
              </div>

              {/* ── MOBILE CARDS ── */}
              <div className="lg:hidden divide-y divide-gray-100 dark:divide-gray-700/60">
                {priorities.map((p) => {
                  const sanctioned = Number(p.sanctioned_amount || 0)
                  const expenditure = Number(p.expenditure || 0)
                  const reasons = p.reasons || []
                  const visibleReasons = reasons.slice(0, 2)
                  const scoreColor = p.risk_score >= 60 ? "text-red-600 dark:text-red-400" : p.risk_score >= 30 ? "text-amber-600 dark:text-amber-400" : "text-blue-600 dark:text-blue-400"
                  const isCritical = p.tier === "P1"

                  return (
                    <div key={p.project_id} className="p-4 cursor-pointer transition hover:bg-blue-50/30 dark:hover:bg-[#120b0e]" onClick={() => handleOpenDetail(p)}>
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className={`rounded px-1.5 py-0.5 text-[0.625rem] font-bold ${tierStyle(p.tier).chip}`}>
                              {p.tier} — {p.tier_label}
                            </span>
                            <span className={`rounded px-1.5 py-0.5 text-[0.625rem] font-bold ${isCritical ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" : "bg-gray-100 text-gray-600 dark:bg-gray-700"}`}>
                              #{p.priority_rank}
                            </span>
                            <span className={`rounded px-1.5 py-0.5 text-[0.625rem] font-bold ${
                              p.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                              : p.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                              : "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                            }`}>{p.risk_level}</span>
                            {p.ml_anomaly && <span className="rounded-full bg-purple-100 px-1.5 py-0.5 text-[0.5625rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">ML</span>}
                          </div>
                          <p className="mt-1 truncate font-semibold text-sm text-gray-900 dark:text-white" title={p.project_name}>{p.project_name || "Unnamed"}</p>
                          <p className="text-[0.6875rem] text-gray-500">{p.state || "N/A"}{p.constituency ? `, ${p.constituency}` : ""}</p>
                        </div>
                        <div className="text-right shrink-0">
                          <div className="flex items-baseline gap-1">
                            <span className={`font-mono text-xl font-bold ${scoreColor}`}>{p.risk_score}</span>
                            <span className="font-mono text-[0.625rem] text-gray-400">/100</span>
                          </div>
                        </div>
                      </div>
                      {/* Reason chips */}
                      {visibleReasons.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {visibleReasons.map((r, i) => {
                            const chip = getReasonChip(r)
                            return <span key={i} className={`rounded px-1.5 py-0.5 text-[0.5625rem] font-bold ${chip.color}`}>{chip.label}</span>
                          })}
                        </div>
                      )}
                      <div className="mt-2 grid grid-cols-3 gap-x-3 gap-y-1 text-xs">
                        <div className="flex justify-between"><span className="text-gray-500">Sanctioned</span><span className="font-mono font-semibold">{formatMoney(sanctioned)}</span></div>
                        <div className="flex justify-between"><span className="text-gray-500">Spent</span><span className="font-mono font-semibold">{formatMoney(expenditure)}</span></div>
                        <div className="flex justify-between"><span className="text-gray-500">Progress</span><span className="font-mono font-semibold">{p.completion_percentage}%</span></div>
                      </div>
                      <div className="mt-2 rounded-lg bg-gray-50 px-2.5 py-1.5 dark:bg-[#120b0e]">
                        <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Main anomaly · action</p>
                        <p className="text-[0.625rem] font-semibold text-gray-700 dark:text-gray-200">{p.primary_anomaly}</p>
                        <p className="mt-0.5 text-[0.625rem] text-gray-500 dark:text-gray-400">{p.recommended_action}</p>
                      </div>
                    </div>
                  )
                })}
              </div>

              {/* ═══ PAGINATION ═══ */}
              <div className="flex flex-wrap items-center justify-between gap-4 border-t border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#120b0e]">
                <span className="text-xs text-gray-600 dark:text-gray-400">
                  Showing {totalCount === 0 ? 0 : (currentPage - 1) * rowsPerPage + 1} to{" "}
                  {Math.min(currentPage * rowsPerPage, totalCount)} of{" "}
                  <strong>{totalCount.toLocaleString("en-IN")}</strong> priority projects
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
      {selectedProject && (() => {
        const p = selectedProject
        const sanctioned = Number(p.sanctioned_amount || 0)
        const expenditure = Number(p.expenditure || 0)
        const completion = Number(p.completion_percentage || 0)
        const utilization = sanctioned > 0 ? (expenditure / sanctioned * 100) : 0
        const reasons = p.reasons || []
        const scoreColor = p.risk_score >= 60 ? "text-red-600 dark:text-red-400" : p.risk_score >= 30 ? "text-amber-600 dark:text-amber-400" : "text-blue-600 dark:text-blue-400"
        const barColor = p.risk_score >= 60 ? "bg-red-500" : p.risk_score >= 30 ? "bg-amber-500" : "bg-blue-500"

        return (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/60 sm:p-4 backdrop-blur-2xs" onClick={() => { setSelectedProject(null); window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } })) }}>
          <div onClick={(e) => e.stopPropagation()} className="flex max-h-[90vh] sm:max-h-[85vh] w-full sm:max-w-2xl flex-col rounded-t-2xl sm:rounded-2xl border border-gray-200 bg-white shadow-soft-lg dark:border-gray-700 dark:bg-[#0d090b]">
            {/* Header */}
            <div className="flex items-start justify-between border-b border-gray-200 p-5 dark:border-gray-700">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`rounded px-2 py-0.5 text-xs font-bold ${tierStyle(p.tier).chip}`}>
                    {p.tier} — {p.tier_label}
                  </span>
                  <span className={`rounded px-2 py-0.5 text-xs font-bold ${
                    p.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                    : p.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                    : "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                  }`}>Priority #{p.priority_rank}</span>
                  <span className="font-mono text-xs font-bold text-gray-400">#{p.project_id}</span>
                  <span className={`rounded px-2 py-0.5 text-xs font-bold ${
                    p.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                    : p.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                    : "bg-gray-100 text-gray-600 dark:bg-gray-700"
                  }`}>{p.risk_level} Risk</span>
                  {p.ml_anomaly && <span className="rounded-full bg-purple-100 px-1.5 py-0.5 text-[0.625rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">ML Anomaly</span>}
                </div>
                <h3 className="mt-2 text-lg font-bold text-gray-900 dark:text-white leading-tight">{p.project_name}</h3>
                <p className="text-xs text-gray-500 mt-0.5">📍 {p.state || "N/A"} — {p.constituency || "N/A"}</p>
              </div>
              <button onClick={() => { setSelectedProject(null); window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } })) }} className="ml-3 flex-shrink-0 rounded p-1 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700">✕</button>
            </div>

            <div className="flex-1 overflow-y-auto p-5 space-y-4">
              {loadingDetail ? (
                <div className="p-8 text-center">
                  <div className="inline-block h-6 w-6 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent" />
                  <p className="mt-2 text-xs text-gray-500">Loading details...</p>
                </div>
              ) : (
                <>
                  {/* Risk Score */}
                  <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Risk Score</p>
                        <div className="flex items-baseline gap-2 mt-1">
                          <span className="font-mono text-3xl font-bold text-gray-900 dark:text-white">{p.risk_score}</span>
                          <span className="font-mono text-sm text-gray-400">/ 100</span>
                          <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${
                            p.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                            : p.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                            : "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                          }`}>{p.risk_level}</span>
                        </div>
                      </div>
                      <div className="text-right">
                        <p className="text-[0.625rem] font-bold uppercase text-gray-400">Priority</p>
                        <p className="font-mono text-2xl font-bold text-gray-900 dark:text-white">#{p.priority_rank}</p>
                      </div>
                    </div>
                    <div className="mt-3">
                      <div className="h-3 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                        <div className={`bar-fill h-full rounded-full ${barColor}`} style={{ width: `${Math.max(2, p.risk_score)}%` }} />
                      </div>
                    </div>
                  </div>

                  {/* Financial Overview */}
                  <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
                    <h4 className="mb-3 text-xs font-bold uppercase tracking-wider text-gray-400">Financial Overview</h4>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                      <div><p className="text-[0.625rem] font-bold uppercase text-gray-400">Sanctioned</p><p className="font-mono text-sm font-bold text-blue-700 dark:text-blue-400">{formatMoney(sanctioned)}</p></div>
                      <div><p className="text-[0.625rem] font-bold uppercase text-gray-400">Expenditure</p><p className="font-mono text-sm font-bold">{formatMoney(expenditure)}</p></div>
                      <div><p className="text-[0.625rem] font-bold uppercase text-gray-400">Progress</p><p className="font-mono text-sm font-bold">{completion}%</p></div>
                      <div><p className="text-[0.625rem] font-bold uppercase text-gray-400">Utilization</p><p className={`font-mono text-sm font-bold ${utilization > 100 ? "text-red-600 dark:text-red-400" : ""}`}>{utilization.toFixed(1)}%</p></div>
                    </div>
                    {utilization > 100 && (
                      <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs dark:border-red-900/40 dark:bg-red-950/20">
                        <span className="font-bold text-red-600 dark:text-red-400">Overspend:</span>{" "}
                        <span className="font-mono font-bold text-red-700 dark:text-red-300">+{formatMoney(expenditure - sanctioned)}</span>{" "}
                        <span className="text-red-500">(+{((expenditure - sanctioned) / sanctioned * 100).toFixed(1)}%)</span>
                      </div>
                    )}
                  </div>

                  {/* Why Prioritized */}
                  {reasons.length > 0 && (
                    <div className="rounded-xl border border-red-200 bg-red-50/50 p-4 dark:border-red-900/40 dark:bg-red-950/20">
                      <h4 className="mb-3 text-xs font-bold uppercase tracking-wider text-red-600 dark:text-red-400">Why This Project Was Prioritized</h4>
                      <div className="space-y-3">
                        {reasons.map((reason, i) => {
                          const chip = getReasonChip(reason)
                          return (
                            <div key={i} className="rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-[#0d090b]">
                              <div className="flex items-center gap-2 mb-1">
                                <span className={`rounded px-1.5 py-0.5 text-[0.5625rem] font-bold ${chip.color}`}>{chip.label}</span>
                                <span className="text-xs font-bold text-gray-800 dark:text-gray-200">{reason}</span>
                              </div>
                              <p className="text-[0.6875rem] text-gray-600 dark:text-gray-400 leading-relaxed pl-1">
                                {getReasonDetail(reason, p)}
                              </p>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )}

                  {/* Why this priority */}
                  {(p.explanation || []).length > 0 && (
                    <div className="rounded-xl border border-purple-200 bg-purple-50/50 p-4 dark:border-purple-900/40 dark:bg-purple-950/20">
                      <h4 className="mb-2 text-xs font-bold uppercase tracking-wider text-purple-700 dark:text-purple-300">
                        Why this priority ({p.tier} — {p.tier_label})
                      </h4>
                      <ul className="space-y-1">
                        {(p.explanation || []).map((exp, i) => (
                          <li key={i} className="flex items-start gap-1.5 text-[0.6875rem] text-gray-600 dark:text-gray-300">
                            <span className="mt-0.5 text-purple-500">•</span><span>{exp}</span>
                          </li>
                        ))}
                      </ul>
                      <p className="mt-2 font-mono text-[0.625rem] text-gray-400">
                        composite score {p.audit_priority_score}/100 · {p.formula || "risk(0-55) + exposure(0-20) + mismatch(0-15) + evidence_gap(0-10)"}
                      </p>
                    </div>
                  )}

                  {/* Evidence gap + recommended actions */}
                  {p.evidence_gap && (
                    <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
                      <h4 className="mb-2 text-xs font-bold uppercase tracking-wider text-gray-400">Evidence gap & recommended actions</h4>
                      <p className="text-[0.6875rem] text-gray-600 dark:text-gray-300">{p.evidence_gap.summary}</p>
                      {(p.evidence_gap.missing_labels || []).length > 0 && (
                        <p className="mt-1 text-[0.625rem] text-gray-500 dark:text-gray-400">
                          Not available: {(p.evidence_gap.missing_labels || []).join("; ")}
                        </p>
                      )}
                      {(p.recommended_actions || []).length > 0 && (
                        <ul className="mt-2 space-y-1">
                          {(p.recommended_actions || []).map((a, i) => (
                            <li key={i} className="flex items-start gap-1.5 text-[0.6875rem] text-gray-600 dark:text-gray-300">
                              <span className="mt-0.5 text-blue-600 dark:text-blue-400">→</span><span>{a}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}

                  {/* Detailed Risk (if loaded) */}
                  {detailRisk && (
                    <div className="rounded-xl border border-blue-200 bg-blue-50/50 p-4 dark:border-blue-900/40 dark:bg-blue-950/20">
                      <h4 className="mb-2 text-xs font-bold uppercase tracking-wider text-blue-700 dark:text-blue-300">Detailed Risk Assessment</h4>
                      <div className="flex gap-4 font-mono text-xs">
                        <span>Score: <strong>{detailRisk.risk_score}/100</strong></span>
                        <span>Level: <strong>{detailRisk.risk_level}</strong></span>
                        {detailRisk.ml_anomaly && <span>ML: <strong className="text-purple-600">Anomaly Detected</strong></span>}
                      </div>
                    </div>
                  )}

                  <p className="text-[0.625rem] leading-relaxed text-gray-400">
                    Priority indicates that a work deserves review based on its recorded values. It does not by itself
                    establish delay, irregularity or wrongdoing.
                  </p>
                </>
              )}
            </div>

            {/* Footer */}
            <div className="border-t border-gray-200 p-4 flex justify-between dark:border-gray-700">
              <button onClick={() => { setSelectedProject(null); window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } })) }} className="rounded-lg border border-gray-300 px-4 py-2 text-xs font-bold text-gray-700 dark:border-gray-600 dark:text-gray-200">Close</button>
              <button
                onClick={() => {
                  window.dispatchEvent(new CustomEvent("navigate-to-project", { detail: { query: String(p.project_id) } }))
                  setSelectedProject(null); window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } }))
                }}
                className="rounded-lg bg-[#c8102e] px-4 py-2 text-xs font-bold text-white dark:bg-blue-600">
                View Full Project →
              </button>
            </div>
          </div>
        </div>
        )
      })()}
    </div>
  )
})

export default AuditPriority
