import { lazy, Suspense, useEffect, useState, useMemo, memo, useCallback } from "react"
import {
  getStateIntelligence,
  getConstituencies,
  getBenchmarking,
  getDataFreshness,
} from "../services/api"
import { formatMoney, formatNumber } from "../utils/format"
import GeoIntelPanel from "../components/GeoIntelPanel"

// Heavy map (d3-geo + GeoJSON) loads only when this page is opened.
const GeographicDistribution = lazy(
  () => import("../components/GeographicDistribution")
)

const HOUSES = ["All", "Lok Sabha", "Rajya Sabha"]

function normalizeHouse(value) {
  return value === "Lok Sabha" || value === "Rajya Sabha" ? value : "All"
}

function completionRateOf(state) {
  if (Number.isFinite(state?.completion_rate_percentage)) {
    return Number(state.completion_rate_percentage)
  }
  const total = Number(state?.total_projects || 0)
  return total > 0 ? (Number(state.completed_projects || 0) / total) * 100 : null
}

function signedNumber(value, suffix = "") {
  const n = Number(value || 0)
  return `${n > 0 ? "+" : ""}${formatNumber(n)}${suffix}`
}

function signedPercent(value) {
  const n = Number(value || 0)
  return `${n > 0 ? "+" : ""}${n.toFixed(1)} pp`
}

function signedMoney(value) {
  const n = Number(value || 0)
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${formatMoney(Math.abs(n))}`
}

function CompareMetric({ label, selected, benchmark, formatter, delta }) {
  return (
    <div className="rounded-lg border border-gray-100 bg-white p-3 dark:border-gray-700/60 dark:bg-[#0d090b]">
      <p className="text-[0.625rem] font-bold uppercase tracking-wide text-gray-400">{label}</p>
      <div className="mt-1.5 flex items-end justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[0.5625rem] text-gray-400">Selected</p>
          <p className="font-mono text-sm font-bold">{formatter(selected)}</p>
        </div>
        <div className="min-w-0 text-right">
          <p className="truncate text-[0.5625rem] text-gray-400">Comparison</p>
          <p className="font-mono text-sm font-bold">{formatter(benchmark)}</p>
        </div>
      </div>
      <p className="mt-1.5 border-t border-gray-100 pt-1 text-[0.625rem] font-bold text-blue-600 dark:border-gray-700/60 dark:text-blue-400">
        {delta}
      </p>
    </div>
  )
}

const StateIntelligence = memo(function StateIntelligence({
  onNavigateToProjects,
  onContextChange,
  fy,
  darkMode,
  house: houseProp,
  onHouseChange,
  drillDownParams,
  onClearDrillDown,
}) {
  const [states, setStates] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [refreshKey, setRefreshKey] = useState(0)

  // House is shared with the app shell so choosing it on Overview or State
  // Intelligence survives navigation between the two pages. The local state
  // keeps this component usable on its own when no parent control is passed.
  const [localHouse, setLocalHouse] = useState(() => normalizeHouse(drillDownParams?.house))
  const house = normalizeHouse(houseProp ?? localHouse)
  const houseScope = house === "All" ? "" : house
  const changeHouse = useCallback(
    (next) => {
      const normalized = normalizeHouse(next)
      setLocalHouse(normalized)
      if (onHouseChange) onHouseChange(normalized)
    },
    [onHouseChange]
  )

  const [sortBy, setSortBy] = useState("total_projects")
  const [sortDir, setSortDir] = useState("desc")
  const [searchTerm, setSearchTerm] = useState(() => drillDownParams?.state || "")
  const [selectedMapCode, setSelectedMapCode] = useState("")
  const [compareCode, setCompareCode] = useState("")
  const [freshness, setFreshness] = useState(null)

  // Benchmarking state
  const [benchState, setBenchState] = useState(() => drillDownParams?.state || "")
  const [benchConstituency, setBenchConstituency] = useState("")
  const [benchConstituencies, setBenchConstituencies] = useState([])
  const [benchData, setBenchData] = useState(null)
  const [benchLoading, setBenchLoading] = useState(false)

  // Assistant context: the state currently drilled into ("this state").
  useEffect(() => {
    if (onContextChange) onContextChange(benchState || null)
    return () => {
      if (onContextChange) onContextChange(null)
    }
  }, [benchState, onContextChange])

  // Apply a state/House selection arriving from the Overview map or an
  // assistant action. The existing search box remains the page's state filter.
  useEffect(() => {
    if (!drillDownParams) return
    if (drillDownParams.state) {
      setSearchTerm(drillDownParams.state)
      setBenchState(drillDownParams.state)
      setBenchData(null)
    }
    if (drillDownParams.house !== undefined) {
      changeHouse(drillDownParams.house)
    }
    if (onClearDrillDown) onClearDrillDown()
  }, [drillDownParams, onClearDrillDown, changeHouse])

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        setLoading(true)
        setError("")
        const data = await getStateIntelligence({
          fy: fy || undefined,
          house: houseScope || undefined,
        })
        if (!cancelled && Array.isArray(data)) setStates(data)
      } catch (err) {
        console.error("State intelligence error:", err)
        if (!cancelled) setError("Failed to load state intelligence data.")
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [fy, houseScope, refreshKey])

  useEffect(() => {
    let cancelled = false
    getDataFreshness()
      .then((data) => {
        if (!cancelled) setFreshness(data)
      })
      .catch(() => {
        if (!cancelled) setFreshness(null)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const filteredStates = useMemo(() => {
    let result = [...states]
    if (searchTerm.trim()) {
      const q = searchTerm.trim().toLowerCase()
      result = result.filter((s) =>
        `${s.state || ""} ${s.canonical_state || ""} ${s.display_name || ""}`
          .toLowerCase()
          .includes(q)
      )
    }
    result.sort((a, b) => {
      const aVal = a[sortBy] || 0
      const bVal = b[sortBy] || 0
      if (typeof aVal === "string") {
        return sortDir === "asc" ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal)
      }
      return sortDir === "asc" ? aVal - bVal : bVal - aVal
    })
    return result
  }, [states, sortBy, sortDir, searchTerm])

  const handleSort = (col) => {
    if (sortBy === col) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"))
    } else {
      setSortBy(col)
      setSortDir(col === "state" ? "asc" : "desc")
    }
  }

  // Load constituencies when bench state changes
  useEffect(() => {
    if (!benchState) {
      setBenchConstituencies([])
      setBenchConstituency("")
      return
    }
    getConstituencies(benchState)
      .then((d) => {
        if (Array.isArray(d)) setBenchConstituencies(d)
      })
      .catch(() => {})
  }, [benchState])

  const loadBenchmarking = useCallback(async () => {
    if (!benchState && !benchConstituency) return
    setBenchLoading(true)
    try {
      const params = {}
      if (benchState) params.state = benchState
      if (benchConstituency) params.constituency = benchConstituency
      if (fy) params.fy = fy
      if (houseScope) params.house = houseScope
      const data = await getBenchmarking(params)
      setBenchData(data)
    } catch (err) {
      console.error("Benchmarking error:", err)
    } finally {
      setBenchLoading(false)
    }
  }, [benchState, benchConstituency, fy, houseScope])

  const stateByCode = useMemo(() => {
    const map = new Map()
    for (const row of states) {
      if (row.state_code) map.set(row.state_code, row)
    }
    return map
  }, [states])

  // The map is the source of the selected state; a drill-down or the existing
  // benchmark selector can also select one, so fall back to the recorded label.
  const selectedAggregate = useMemo(() => {
    return (
      (selectedMapCode && stateByCode.get(selectedMapCode)) ||
      states.find((s) => s.state === benchState) ||
      null
    )
  }, [benchState, selectedMapCode, stateByCode, states])

  const compareAggregate = compareCode ? stateByCode.get(compareCode) : null

  useEffect(() => {
    if (selectedMapCode || !benchState || states.length === 0) return
    const match = states.find(
      (s) => s.state === benchState || s.display_name === benchState || s.canonical_state === benchState
    )
    if (match?.state_code) setSelectedMapCode(match.state_code)
  }, [benchState, selectedMapCode, states])

  const scrollTo = (id) => {
    if (typeof document === "undefined") return
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" })
  }

  const handleMapSelect = useCallback((state) => {
    const code = state?.state_code || ""
    setSelectedMapCode(code)
    setCompareCode("")
    if (state?.drill_state) {
      setBenchState(state.drill_state)
      setBenchData(null)
    }
  }, [])

  const applyMapState = useCallback((state) => {
    const label = state?.drill_state || state?.state || state?.display_name || ""
    if (!label) return
    setSearchTerm(label)
    setBenchState(label)
    setSelectedMapCode(state?.state_code || "")
    setBenchData(null)
  }, [])

  const openMapState = useCallback((state) => {
    applyMapState(state)
    scrollTo("state-benchmarking")
  }, [applyMapState])

  const handleBenchStateChange = (value) => {
    setBenchState(value)
    setBenchData(null)
    setCompareCode("")
    const match = states.find(
      (s) => s.state === value || s.display_name === value || s.canonical_state === value
    )
    setSelectedMapCode(match?.state_code || "")
  }

  if (loading && states.length === 0) {
    return (
      <div className="min-h-full bg-[#faf7f6] p-4 sm:p-6 dark:bg-[#000000]">
        <h1 className="text-2xl font-bold text-[#292326] dark:text-white">State Intelligence</h1>
        <div className="mt-6 rounded-xl border border-gray-200 bg-white p-16 text-center dark:border-gray-700 dark:bg-[#0d090b]">
          <div className="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent" />
          <p className="mt-3 text-sm font-medium">Loading state intelligence...</p>
        </div>
      </div>
    )
  }

  const columns = [
    { key: "state", label: "State / UT", sortable: true },
    { key: "total_projects", label: "Works", sortable: true },
    { key: "completed_projects", label: "Completed", sortable: true },
    { key: "ongoing_projects", label: "Ongoing", sortable: true },
    { key: "total_sanctioned_amount", label: "Sanctioned", sortable: true, money: true },
    { key: "total_expenditure", label: "Expenditure", sortable: true, money: true },
    { key: "utilization_percentage", label: "Utilization", sortable: true, pct: true },
    { key: "average_completion_percentage", label: "Avg Progress", sortable: true, pct: true },
    { key: "high_risk_projects", label: "High Risk", sortable: true, alert: true },
    { key: "ml_anomaly_projects", label: "ML Anomalies", sortable: true, ml: true },
  ]

  const selectedCompletion = completionRateOf(selectedAggregate)
  const compareCompletion = completionRateOf(compareAggregate)
  const selectedName = selectedAggregate?.display_name || selectedAggregate?.state || "Selected state"
  const compareName = compareAggregate?.display_name || compareAggregate?.state || "Comparison state"

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 sm:p-6 text-[#292326] transition-colors duration-200 dark:bg-[#000000] dark:text-[#f5eff1]">
      <div className="mx-auto max-w-[1440px] space-y-4 sm:space-y-6">
        <div className="flex flex-col justify-between gap-4 md:flex-row md:items-center">
          <div>
            <h2 className="text-2xl font-bold text-[#292326] dark:text-white">State Intelligence</h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Comprehensive analytics across <strong>{states.length}</strong> states and union territories.
            </p>
          </div>
          <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
            <select
              value={house}
              onChange={(e) => changeHouse(e.target.value)}
              className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white"
              title="Filter states by House"
            >
              {HOUSES.map((h) => (
                <option key={h} value={h}>
                  {h === "All" ? "All Houses" : h}
                </option>
              ))}
            </select>
            <input
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="Search state..."
              className="w-full rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 sm:w-64 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white"
            />
          </div>
        </div>

        {error && (
          <div className="flex flex-col gap-2 rounded-xl border border-red-200 bg-red-50 p-4 text-sm font-medium text-red-700 dark:border-red-800/60 dark:bg-red-950/40 dark:text-red-300 sm:flex-row sm:items-center sm:justify-between">
            <span>⚠ {error}</span>
            <button
              type="button"
              onClick={() => setRefreshKey((key) => key + 1)}
              className="self-start rounded-md border border-red-300 px-2.5 py-1 text-xs font-bold hover:bg-red-100 dark:border-red-700 dark:hover:bg-red-950/60"
            >
              Retry
            </button>
          </div>
        )}

        {/* SHARED INDIA CHOROPLETH — same boundaries, metrics and palette as
            Overview. Selecting here does not navigate away; it opens the
            expanded state panel below the map. */}
        <Suspense
          fallback={
            <div
              className="h-72 animate-pulse rounded-xl border border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800/40"
              aria-label="Loading India map"
            />
          }
        >
          <GeographicDistribution
            darkMode={darkMode}
            house={house}
            fy={fy}
            title="India Geospatial Intelligence"
            subtitle="Explore the geographic distribution of MPLADS projects and compare financial and implementation indicators across Indian states."
            scopeNote="Follows the selected House filter"
            onSelectState={handleMapSelect}
            selectedStateCode={selectedMapCode}
            onApplyState={applyMapState}
            onOpenState={openMapState}
            dataFreshness={freshness}
          />
        </Suspense>

        {/* GEOSPATIAL INTELLIGENCE — coverage gaps, sub-state execution
            patterns, spatial relations, geographic outliers, progress map.
            Aggregation unit is disclosed by the backend. */}
        <GeoIntelPanel house={house === "All" ? "" : house} />

        {loading && (
          <div className="flex items-center gap-2 rounded-lg border border-blue-100 bg-blue-50/60 px-3 py-2 text-xs font-semibold text-blue-700 dark:border-blue-900/50 dark:bg-blue-950/20 dark:text-blue-300">
            <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-solid border-blue-600 border-r-transparent" />
            Updating state aggregates for {house === "All" ? "all Houses" : house}...
          </div>
        )}

        {/* Sort Controls */}
        <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
          <span className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Sort:</span>
          {[
            { value: "total_projects", label: "Works" },
            { value: "total_sanctioned_amount", label: "Sanctioned" },
            { value: "total_expenditure", label: "Expenditure" },
            { value: "utilization_percentage", label: "Utilization" },
            { value: "average_completion_percentage", label: "Avg Progress" },
            { value: "high_risk_projects", label: "High Risk" },
            { value: "state", label: "Name" },
          ].map((opt) => (
            <button
              key={opt.value}
              onClick={() => handleSort(opt.value)}
              className={`rounded-lg px-3 py-1.5 text-[0.6875rem] font-bold transition ${
                sortBy === opt.value
                  ? "bg-[#c8102e] text-white dark:bg-blue-600"
                  : "border border-gray-200 bg-white text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:bg-[#0d090b] dark:text-gray-300"
              }`}
            >
              {opt.label}
              {sortBy === opt.value && <span className="ml-1">{sortDir === "asc" ? "↑" : "↓"}</span>}
            </button>
          ))}
        </div>

        {/* EXISTING BENCHMARKING SECTION, extended with state-to-state comparison. */}
        <div id="state-benchmarking" className="scroll-mt-24 rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
          <div className="flex flex-col gap-1 sm:flex-row sm:items-baseline sm:justify-between">
            <h3 className="text-sm font-bold uppercase tracking-wider text-gray-400">State / Constituency Benchmarking</h3>
            <span className="font-mono text-[0.625rem] text-gray-400">
              {house === "All" ? "All Houses" : house} · {fy || "All FY"}
            </span>
          </div>
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <div className="flex-1 min-w-[150px]">
              <label className="mb-1 block text-[0.625rem] font-bold uppercase text-gray-400">State</label>
              <select
                value={benchState}
                onChange={(e) => handleBenchStateChange(e.target.value)}
                className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-[#000000] dark:text-white"
              >
                <option value="">Select State</option>
                {states.map((s) => (
                  <option key={s.state} value={s.state}>
                    {s.display_name || s.state}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex-1 min-w-[150px]">
              <label className="mb-1 block text-[0.625rem] font-bold uppercase text-gray-400">Constituency (optional)</label>
              <select
                value={benchConstituency}
                onChange={(e) => setBenchConstituency(e.target.value)}
                disabled={!benchState}
                className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm disabled:opacity-50 dark:border-gray-600 dark:bg-[#000000] dark:text-white"
              >
                <option value="">All Constituencies</option>
                {benchConstituencies.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </div>
            <button
              onClick={loadBenchmarking}
              disabled={(!benchState && !benchConstituency) || benchLoading}
              className="rounded-lg bg-[#c8102e] px-4 py-2 text-xs font-bold text-white transition hover:opacity-80 disabled:opacity-50 dark:bg-blue-600"
            >
              {benchLoading ? "Loading..." : "Compare with benchmark"}
            </button>
          </div>

          {/* Existing benchmark results */}
          {benchData && benchData.comparisons?.length > 0 && (
            <div className="mt-4 space-y-4">
              {benchData.comparisons.map((comp, ci) => (
                <div key={ci} className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
                  <h4 className="mb-3 text-xs font-bold text-gray-700 dark:text-gray-300">{comp.label}</h4>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    {comp.metrics.map((m, mi) => (
                      <div key={mi} className="min-w-0 rounded-lg bg-white p-3 dark:bg-[#0d090b]">
                        <p className="text-[0.625rem] font-bold uppercase text-gray-400">{m.metric}</p>
                        <p className="value-nowrap font-mono text-lg font-bold tabular-nums" title={`${m.selected}${m.unit}`}>{m.selected}{m.unit}</p>
                        <p className="text-[0.625rem] text-gray-400">Benchmark: {m.benchmark}{m.unit}</p>
                        <p className={`value-nowrap mt-1 text-xs font-bold ${m.better ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400"}`}>
                          {m.difference > 0 ? "+" : ""}{m.difference}{m.unit} {m.better ? "↑" : "↓"}
                        </p>
                      </div>
                    ))}
                  </div>
                  <div className="mt-3 rounded-lg border border-blue-100 bg-blue-50/50 p-3 dark:border-blue-900/40 dark:bg-blue-950/20">
                    {comp.metrics.map((m, mi) => (
                      <p key={mi} className="text-[0.6875rem] text-gray-600 dark:text-gray-400">
                        {comp.selected_name} {m.metric.toLowerCase()} is {Math.abs(m.difference)}{m.unit} {m.better ? "above" : "below"} {comp.benchmark_name.toLowerCase()} ({m.benchmark}{m.unit}).
                      </p>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
          {benchData && benchData.comparisons?.length === 0 && (
            <p className="mt-3 text-xs text-gray-400">No comparison data available for the selected filters.</p>
          )}

          {/* State-to-state comparison reuses the page's House/FY-scoped
              aggregate rows; it does not fetch project-level records. */}
          <div className="mt-5 border-t border-gray-200 pt-4 dark:border-gray-700/60">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <h4 className="text-xs font-bold uppercase tracking-wider text-gray-500 dark:text-gray-300">Compare selected state with another state</h4>
                <p className="mt-0.5 text-[0.6875rem] text-gray-400">
                  Differences use the same recorded aggregates and the current House/FY scope.
                </p>
              </div>
              <select
                value={compareCode}
                onChange={(e) => setCompareCode(e.target.value)}
                disabled={!selectedAggregate}
                className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm disabled:opacity-50 dark:border-gray-600 dark:bg-[#000000] dark:text-white sm:w-64"
              >
                <option value="">Select comparison state</option>
                {states
                  .filter((s) => s.state_code && s.state_code !== selectedAggregate?.state_code)
                  .map((s) => (
                    <option key={s.state_code} value={s.state_code}>
                      {s.display_name || s.state}
                    </option>
                  ))}
              </select>
            </div>

            {!selectedAggregate ? (
              <p className="mt-3 rounded-lg border border-dashed border-gray-200 p-3 text-xs text-gray-400 dark:border-gray-700">
                Select a state on the map, or choose one above, to begin a state-to-state comparison.
              </p>
            ) : !compareAggregate ? (
              <p className="mt-3 rounded-lg border border-dashed border-gray-200 p-3 text-xs text-gray-400 dark:border-gray-700">
                Selected state: <strong className="text-gray-600 dark:text-gray-300">{selectedName}</strong>. Choose a comparison state to see project, expenditure, utilization, completion and risk differences.
              </p>
            ) : (
              <div className="mt-3 space-y-3">
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
                  <CompareMetric
                    label="Total projects"
                    selected={selectedAggregate.total_projects}
                    benchmark={compareAggregate.total_projects}
                    formatter={(v) => formatNumber(v)}
                    delta={signedNumber(selectedAggregate.total_projects - compareAggregate.total_projects, " projects")}
                  />
                  <CompareMetric
                    label="Total expenditure"
                    selected={selectedAggregate.total_expenditure}
                    benchmark={compareAggregate.total_expenditure}
                    formatter={(v) => formatMoney(v)}
                    delta={signedMoney(selectedAggregate.total_expenditure - compareAggregate.total_expenditure)}
                  />
                  <CompareMetric
                    label="Fund utilization"
                    selected={selectedAggregate.utilization_percentage}
                    benchmark={compareAggregate.utilization_percentage}
                    formatter={(v) => `${Number(v || 0).toFixed(1)}%`}
                    delta={signedPercent(selectedAggregate.utilization_percentage - compareAggregate.utilization_percentage)}
                  />
                  <CompareMetric
                    label="Completion rate"
                    selected={selectedCompletion}
                    benchmark={compareCompletion}
                    formatter={(v) => (Number.isFinite(v) ? `${Number(v).toFixed(1)}%` : "—")}
                    delta={Number.isFinite(selectedCompletion) && Number.isFinite(compareCompletion) ? signedPercent(selectedCompletion - compareCompletion) : "Not available"}
                  />
                </div>

                <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700/60">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-gray-50 text-[0.625rem] uppercase tracking-wide text-gray-400 dark:bg-[#120b0e]">
                      <tr>
                        <th className="p-2.5 font-bold">Risk distribution</th>
                        <th className="p-2.5 text-right font-bold">{selectedName}</th>
                        <th className="p-2.5 text-right font-bold">{compareName}</th>
                        <th className="p-2.5 text-right font-bold">Difference</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100 dark:divide-gray-700/60">
                      {[
                        { label: "High risk", key: "high_risk_projects" },
                        { label: "Medium risk", key: "medium_risk_projects" },
                        { label: "ML anomalies", key: "ml_anomaly_projects" },
                      ].map((risk) => {
                        const riskSelected = selectedAggregate.risk_scored_projects > 0;
                        const riskCompare = compareAggregate.risk_scored_projects > 0;
                        const a = Number(selectedAggregate[risk.key] || 0);
                        const b = Number(compareAggregate[risk.key] || 0);
                        return (
                          <tr key={risk.key}>
                            <td className="p-2.5 font-semibold text-gray-600 dark:text-gray-300">{risk.label}</td>
                            <td className="p-2.5 text-right font-mono font-bold">
                              {riskSelected ? formatNumber(a) : "Not assessed"}
                            </td>
                            <td className="p-2.5 text-right font-mono font-bold">
                              {riskCompare ? formatNumber(b) : "Not assessed"}
                            </td>
                            <td className="p-2.5 text-right font-mono font-bold text-blue-600 dark:text-blue-400">
                              {riskSelected && riskCompare ? signedNumber(a - b) : "—"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <p className="text-[0.625rem] leading-relaxed text-gray-400">
                  Recorded aggregate differences for {house === "All" ? "all Houses" : house}{fy ? ` · FY ${fy}` : ""}. They describe the data; they are not a finding of wrongdoing. “Not assessed” means no stored risk score exists, not a zero.
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Table */}
        <div id="state-table" className="scroll-mt-24 overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
          <div className="hidden lg:block overflow-x-auto">
            <table className="w-full min-w-[1100px]">
              <thead>
                <tr className="border-b border-gray-200 bg-gray-50 text-left text-xs font-bold uppercase tracking-wider text-gray-700 dark:border-gray-700 dark:bg-[#120b0e] dark:text-gray-300">
                  {columns.map((col) => (
                    <th
                      key={col.key}
                      className={`p-3 ${col.sortable ? "cursor-pointer select-none hover:text-blue-600" : ""} ${col.money || col.pct || col.alert || col.ml ? "text-right" : ""}`}
                      onClick={() => col.sortable && handleSort(col.key)}
                    >
                      {col.label}
                      {sortBy === col.key && <span className="ml-1">{sortDir === "asc" ? "↑" : "↓"}</span>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-700/60">
                {filteredStates.map((s) => (
                  <tr
                    key={s.state}
                    className="cursor-pointer transition hover:bg-blue-50/40 dark:hover:bg-[#120b0e]"
                    onClick={() => onNavigateToProjects && onNavigateToProjects(s.drill_state || s.state)}
                  >
                    <td className="p-3 text-sm font-semibold">{s.display_name || s.state}</td>
                    <td className="p-3 text-right font-mono text-xs">{s.total_projects.toLocaleString("en-IN")}</td>
                    <td className="p-3 text-right font-mono text-xs text-green-600 dark:text-green-400">{s.completed_projects.toLocaleString("en-IN")}</td>
                    <td className="p-3 text-right font-mono text-xs text-blue-600 dark:text-blue-400">{s.ongoing_projects.toLocaleString("en-IN")}</td>
                    <td className="p-3 text-right font-mono text-xs">{formatMoney(s.total_sanctioned_amount)}</td>
                    <td className="p-3 text-right font-mono text-xs">{formatMoney(s.total_expenditure)}</td>
                    <td className="p-3 text-right font-mono text-xs font-bold">{s.utilization_percentage}%</td>
                    <td className="p-3 text-right font-mono text-xs">{s.average_completion_percentage}%</td>
                    <td className="p-3 text-right">
                      {s.high_risk_projects > 0 ? (
                        <span className="rounded-sm bg-red-100 px-1.5 py-0.5 font-mono text-xs font-bold text-red-700 dark:bg-red-950 dark:text-red-300">{s.high_risk_projects}</span>
                      ) : <span className="text-gray-300">0</span>}
                    </td>
                    <td className="p-3 text-right">
                      {s.ml_anomaly_projects > 0 ? (
                        <span className="rounded-sm bg-purple-100 px-1.5 py-0.5 font-mono text-xs font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">{s.ml_anomaly_projects}</span>
                      ) : <span className="text-gray-300">0</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* MOBILE CARD VIEW */}
          <div className="lg:hidden divide-y divide-gray-200 dark:divide-gray-700/60">
            {filteredStates.map((s) => (
              <div
                key={s.state}
                className="p-4 cursor-pointer transition hover:bg-blue-50/40 dark:hover:bg-[#120b0e]"
                onClick={() => onNavigateToProjects && onNavigateToProjects(s.drill_state || s.state)}
              >
                <div className="flex items-start justify-between gap-2">
                  <p className="min-w-0 font-semibold text-sm leading-snug">{s.display_name || s.state}</p>
                  <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
                    {s.high_risk_projects > 0 && <span className="whitespace-nowrap rounded-sm bg-red-100 px-1.5 py-0.5 text-[0.625rem] font-bold text-red-700 dark:bg-red-950 dark:text-red-300">{s.high_risk_projects} High</span>}
                    {s.ml_anomaly_projects > 0 && <span className="whitespace-nowrap rounded-sm bg-purple-100 px-1.5 py-0.5 text-[0.625rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">{s.ml_anomaly_projects} ML</span>}
                  </div>
                </div>
                {/* Three aligned pairs: works/completed, sanctioned/expenditure,
                    utilization/average progress. Each label sits above its own
                    value, so amounts and their Cr/L unit never wrap or collide. */}
                <div className="metric-grid mt-3">
                  <div className="metric-cell">
                    <span className="metric-label">Works</span>
                    <span className="metric-value">{s.total_projects.toLocaleString("en-IN")}</span>
                  </div>
                  <div className="metric-cell">
                    <span className="metric-label">Completed</span>
                    <span className="metric-value text-green-600 dark:text-green-400">{s.completed_projects.toLocaleString("en-IN")}</span>
                  </div>
                  <div className="metric-cell">
                    <span className="metric-label">Sanctioned</span>
                    <span className="metric-value">{formatMoney(s.total_sanctioned_amount)}</span>
                  </div>
                  <div className="metric-cell">
                    <span className="metric-label">Expenditure</span>
                    <span className="metric-value">{formatMoney(s.total_expenditure)}</span>
                  </div>
                  <div className="metric-cell">
                    <span className="metric-label">Utilization</span>
                    <span className="metric-value font-bold">{s.utilization_percentage}%</span>
                  </div>
                  <div className="metric-cell">
                    <span className="metric-label">Avg Progress</span>
                    <span className="metric-value">{s.average_completion_percentage}%</span>
                  </div>
                </div>
                {typeof s.work_verified_expenditure === "number" && s.work_verified_expenditure > 0 && (
                  <p className="mt-2 break-words text-[0.6875rem] leading-relaxed text-gray-400">
                    ₹{formatMoney(s.work_verified_expenditure).replace(/^[^\d]*/, "")} of this is verifiably matched to catalog works ({s.linked_work_keys ?? 0} keys), of which ₹{formatMoney(s.project_linked_expenditure ?? 0).replace(/^[^\d]*/, "")} is uniquely attributable; the rest cannot be matched to catalog records.
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>

        {filteredStates.length === 0 && !loading && (
          <div className="rounded-xl border border-gray-200 bg-white p-8 text-center dark:border-gray-700 dark:bg-[#0d090b]">
            <p className="text-sm text-gray-500">No states match the search term.</p>
          </div>
        )}
      </div>
    </div>
  )
})

export default StateIntelligence
