import { memo, useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react"
import { createPortal } from "react-dom"
import { geoMercator, geoPath } from "d3-geo"
import { getStateMapMetrics } from "../services/api"
import { formatCrore, formatNumber } from "../utils/format"

/*
 * Geographic Distribution of MPLADS Projects
 * ------------------------------------------
 * Shared state-level choropleth for Overview and State Intelligence.
 *
 * Data flow: /dashboard/state-map returns 36 aggregated state rows with ISO
 * 3166-2 codes and explicit availability flags. The boundary GeoJSON is
 * fetched separately (public/geo/india-states.geojson) and joined by code,
 * never by display name. No project-level rows reach the browser.
 *
 * Overview mode navigates on click. State Intelligence passes onSelectState
 * to select in place and show the expanded state panel (an inline card on
 * desktop, a viewport-level bottom sheet below 1024px); both modes use the
 * same boundaries, metric definitions, colours and aggregation response.
 *
 * Honesty rules implemented here:
 *   • null/absent metric -> hatched "no recorded value" fill
 *   • recorded zero       -> lightest sequential colour, tooltip shows 0
 *   • totals are state aggregates, not GPS project locations
 */

const MAP_WIDTH = 640
const MAP_HEIGHT = 640

/**
 * Selected-state details presentation.
 * - Below 1024px (phones and tablets): a viewport-level bottom sheet
 *   (portalled to <body>) so the card can never be clipped by the
 *   overflow-hidden map frame or hidden below the legend/ranking column. The
 *   sheet has a fixed header and one internal scroll area, so every statistic
 *   stays reachable on short screens.
 * - Desktop (>=1024px): the original inline panel below the map, unchanged.
 */
function useMediaQuery(query) {
  const subscribe = useCallback(
    (onChange) => {
      if (typeof window === "undefined") return () => {}
      const mq = window.matchMedia(query)
      mq.addEventListener("change", onChange)
      return () => mq.removeEventListener("change", onChange)
    },
    [query]
  )
  const getSnapshot = useCallback(
    () =>
      typeof window !== "undefined" && window.matchMedia(query).matches,
    [query]
  )
  return useSyncExternalStore(subscribe, getSnapshot, () => false)
}

const METRICS = [
  {
    id: "total_projects",
    label: "Total Projects",
    shortLabel: "Projects",
    unit: "projects",
  },
  {
    id: "total_expenditure",
    label: "Total Expenditure",
    shortLabel: "Expenditure",
    unit: "₹ crore",
  },
  {
    id: "fund_utilization",
    label: "Fund Utilization",
    shortLabel: "Utilization",
    unit: "% of sanctioned",
  },
  {
    id: "completed_projects",
    label: "Completed Projects",
    shortLabel: "Completed",
    unit: "projects",
  },
  {
    id: "high_risk_projects",
    label: "High-Risk Projects",
    shortLabel: "High risk",
    unit: "projects",
  },
]

const PALETTES = {
  light: {
    total_projects: ["#fcebec", "#f2b7bd", "#e78d96", "#d95d6b", "#c8102e"],
    total_expenditure: ["#f8f1f1", "#ead9da", "#d6b9bd", "#b7959a", "#8b7d81"],
    fund_utilization: ["#f9f3f3", "#eedcdd", "#d9bcbf", "#b89299", "#7f676c"],
    completed_projects: ["#e9f6f0", "#ceebdf", "#a3dac3", "#43ae87", "#269b71"],
    high_risk_projects: ["#fef7ea", "#fdebcb", "#f8be5f", "#f59e0b", "#e14b4b"],
  },
  dark: {
    total_projects: ["#210a12", "#3a0d1c", "#641329", "#a32346", "#d33b62"],
    total_expenditure: ["#241016", "#402b34", "#6b4a56", "#9d7c88", "#c9a9b3"],
    fund_utilization: ["#1c0810", "#2f0a14", "#641329", "#b25a72", "#d0a0ad"],
    completed_projects: ["#123b2c", "#175c45", "#1f8661", "#43ae87", "#74c4a5"],
    high_risk_projects: ["#2a2318", "#3a2f1a", "#b2720a", "#f59e0b", "#e46666"],
  },
}

let geoCache = null
let geoPromise = null

function loadIndiaGeo() {
  if (geoCache) return Promise.resolve(geoCache)
  if (!geoPromise) {
    geoPromise = fetch(`${import.meta.env.BASE_URL}geo/india-states.geojson`)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Boundary layer request failed (${response.status})`)
        }
        return response.json()
      })
      .then((data) => {
        if (!data || !Array.isArray(data.features) || data.features.length === 0) {
          throw new Error("Boundary layer is empty")
        }
        geoCache = data
        return data
      })
      .catch((error) => {
        geoPromise = null
        throw error
      })
  }
  return geoPromise
}

function metricValue(state, metric) {
  if (!state) return null
  switch (metric) {
    case "total_projects":
      return state.has_projects ? state.total_projects : null
    case "total_expenditure":
      return state.has_expenditure ? state.total_expenditure : null
    case "fund_utilization":
      return Number.isFinite(state.fund_utilization_percentage)
        ? state.fund_utilization_percentage
        : null
    case "completed_projects":
      return state.has_projects ? state.completed_projects : null
    case "high_risk_projects":
      return state.has_risk_scores ? state.high_risk_projects : null
    default:
      return null
  }
}

function formatMetricValue(metric, value) {
  if (!Number.isFinite(value)) return "—"
  if (metric === "total_expenditure") return `₹${formatCrore(value)} Cr`
  if (metric === "fund_utilization") return `${Number(value).toFixed(1)}%`
  return formatNumber(value)
}

function formatLegendValue(metric, value) {
  if (metric === "total_expenditure") return `₹${formatCrore(value)} Cr`
  if (metric === "fund_utilization") return `${value}%`
  return formatNumber(value)
}

function buildScale(metric, values) {
  const available = values.filter((value) => Number.isFinite(value))
  if (available.length === 0) {
    return { bins: [], indexOf: () => -1 }
  }

  // Utilization uses an interpretable fixed percentage scale. It deliberately
  // extends past 100% because ledger spend can exceed the catalog sanctioned
  // amount (unattributed payments) — capping at 100 would hide that.
  if (metric === "fund_utilization") {
    const bins = [
      { min: 0, max: 25, label: "0–<25%" },
      { min: 25, max: 50, label: "25–<50%" },
      { min: 50, max: 75, label: "50–<75%" },
      { min: 75, max: 100, label: "75–<100%" },
      { min: 100, max: null, label: "100%+" },
    ]
    return {
      bins,
      indexOf: (value) => {
        if (!Number.isFinite(value)) return -1
        if (value < 25) return 0
        if (value < 50) return 1
        if (value < 75) return 2
        if (value < 100) return 3
        return 4
      },
    }
  }

  // Small integer counts read better with explicit ranges than quantiles.
  if (metric === "high_risk_projects") {
    const max = Math.max(...available)
    const bins = [
      { min: 0, max: 0, label: "0" },
      { min: 1, max: 1, label: "1" },
      { min: 2, max: 3, label: "2–3" },
      { min: 4, max: 5, label: "4–5" },
      { min: 6, max: null, label: "6+" },
    ].filter((bin) => bin.min <= max)
    return {
      bins,
      indexOf: (value) => {
        if (!Number.isFinite(value)) return -1
        for (let i = 0; i < bins.length; i += 1) {
          if (bins[i].max === null || value <= bins[i].max) return i
        }
        return bins.length - 1
      },
    }
  }

  // Quantile classes keep the very skewed project/expenditure distributions
  // readable (a linear scale would paint almost every state the same shade).
  const unique = [...new Set(available)].sort((a, b) => a - b)
  if (unique.length === 1) {
    return {
      bins: [{ min: unique[0], max: unique[0], label: null }],
      indexOf: (value) => (Number.isFinite(value) ? 0 : -1),
    }
  }

  const binCount = Math.min(5, unique.length)
  const bins = []
  for (let i = 0; i < binCount; i += 1) {
    const start = Math.floor((i * unique.length) / binCount)
    const end = Math.floor(((i + 1) * unique.length) / binCount) - 1
    bins.push({ min: unique[start], max: unique[end], label: null })
  }
  return {
    bins,
    indexOf: (value) => {
      if (!Number.isFinite(value)) return -1
      for (let i = 0; i < bins.length; i += 1) {
        if (value <= bins[i].max) return i
      }
      return bins.length - 1
    },
  }
}

function colorForBin(index, binCount, palette) {
  if (index < 0 || binCount === 0) return null
  if (binCount === 1) return palette[0]
  const paletteIndex = Math.round((index * (palette.length - 1)) / (binCount - 1))
  return palette[paletteIndex]
}

function legendLabel(metric, bin) {
  if (bin.label) return bin.label
  if (bin.min === bin.max) return formatLegendValue(metric, bin.min)
  return `${formatLegendValue(metric, bin.min)} – ${formatLegendValue(metric, bin.max)}`
}

function freshnessMessage(freshness) {
  if (!freshness) return "Not reported"
  if (freshness.connected === false) {
    return freshness.message || "Unavailable"
  }
  if (freshness.message) return freshness.message
  const stamp = freshness.last_sync?.synced_at
  if (stamp) {
    const date = new Date(stamp)
    if (!Number.isNaN(date.getTime())) {
      return `Last synchronized ${date.toLocaleDateString("en-IN", {
        day: "numeric",
        month: "short",
        year: "numeric",
      })}`
    }
  }
  return freshness.freshness || "Not reported"
}

function noDataPatternStyle(darkMode) {
  return darkMode
    ? {
        backgroundColor: "#120b0e",
        backgroundImage:
          "repeating-linear-gradient(45deg, transparent, transparent 3px, #351923 3px, #351923 4px)",
      }
    : {
        backgroundColor: "#f8f1f1",
        backgroundImage:
          "repeating-linear-gradient(45deg, transparent, transparent 3px, #dfcbcd 3px, #dfcbcd 4px)",
      }
}

const GeographicDistribution = memo(function GeographicDistribution({
  darkMode,
  house,
  fy,
  onDrillDown,
  onSelectState,
  selectedStateCode,
  onApplyState,
  onOpenState,
  dataFreshness,
  title = "Geographic Distribution of MPLADS Projects",
  subtitle = "State-level view of recorded works and spending. The map summarises each state from its recorded state label — it is not a GPS-based map of individual project locations.",
  scopeNote = "Follows the House Dashboard selection",
}) {
  const [metric, setMetric] = useState("total_projects")
  const [data, setData] = useState(null)
  const [geo, setGeo] = useState(geoCache)
  const [geoError, setGeoError] = useState("")
  const [geoReloadKey, setGeoReloadKey] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [reloadKey, setReloadKey] = useState(0)
  const [hover, setHover] = useState(null)
  const [selectedCode, setSelectedCode] = useState(selectedStateCode || null)
  const mapRef = useRef(null)
  const titleId = useId()
  const sheetTitleId = useId()
  const noDataPatternId = useId().replace(/:/g, "")
  // Bottom sheet for phone and tablet widths; desktop keeps the existing
  // inline panel below the map.
  const isCompact = useMediaQuery("(max-width: 1023px)")

  const cardClasses = darkMode
    ? "bg-[#0d090b] border-[#351923]"
    : "bg-white border-[#e8dadc]"
  const mutedText = darkMode ? "text-[#b9aeb2]" : "text-[#71666a]"
  const palette = PALETTES[darkMode ? "dark" : "light"][metric]
  const selectedMetric = METRICS.find((item) => item.id === metric) || METRICS[0]
  const selectionEnabled = typeof onSelectState === "function"

  useEffect(() => {
    if (selectedStateCode !== undefined) {
      setSelectedCode(selectedStateCode || null)
    }
  }, [selectedStateCode])

  // Lazy boundary asset: fetched once per session, cached at module level so
  // switching House/FY never re-downloads the geometry.
  useEffect(() => {
    let cancelled = false
    if (geo) return undefined
    setGeoError("")
    loadIndiaGeo()
      .then((payload) => {
        if (!cancelled) setGeo(payload)
      })
      .catch((err) => {
        if (!cancelled) {
          console.error("India boundary layer error:", err)
          setGeoError("The India state boundary layer could not be loaded.")
        }
      })
    return () => {
      cancelled = true
    }
  }, [geo, geoReloadKey])

  // Aggregates only. This effect is intentionally scoped to fy/house: it does
  // not fire when unrelated Overview state changes (expanded panels, hover…).
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError("")
    setData(null)
    setHover(null)
    const params = {}
    if (fy) params.fy = fy
    if (house && house !== "All") params.house = house
    getStateMapMetrics(params)
      .then((payload) => {
        if (!cancelled) setData(payload)
      })
      .catch((err) => {
        if (!cancelled) {
          console.error("State map metrics error:", err)
          setError("State-level map metrics could not be loaded.")
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [fy, house, reloadKey])

  const statesByCode = useMemo(() => {
    const map = new Map()
    for (const state of data?.states || []) {
      map.set(state.state_code, state)
    }
    return map
  }, [data])

  const projection = useMemo(
    () => (geo ? geoMercator().fitExtent([[18, 18], [MAP_WIDTH - 18, MAP_HEIGHT - 18]], geo) : null),
    [geo]
  )
  const pathBuilder = useMemo(
    () => (projection ? geoPath(projection) : null),
    [projection]
  )
  const features = useMemo(() => {
    if (!geo || !pathBuilder) return []
    return geo.features
      .map((feature) => {
        const code = feature.properties?.code
        const name = feature.properties?.name || code || "Unknown state"
        return { code, name, path: pathBuilder(feature) }
      })
      .filter((feature) => feature.code && feature.path)
  }, [geo, pathBuilder])

  const values = useMemo(
    () =>
      (data?.states || [])
        .map((state) => metricValue(state, metric))
        .filter((value) => Number.isFinite(value)),
    [data, metric]
  )
  const scale = useMemo(() => buildScale(metric, values), [metric, values])

  const rankedStates = useMemo(() => {
    const rows = (data?.states || [])
      .map((state) => ({ state, value: metricValue(state, metric) }))
      .filter((row) => Number.isFinite(row.value))
    rows.sort((a, b) => b.value - a.value)
    if (metric === "high_risk_projects") {
      return rows.filter((row) => row.value > 0)
    }
    return rows
  }, [data, metric])

  const topStates = rankedStates.slice(0, 5)
  const topMax = topStates.length > 0 ? Math.max(topStates[0].value, 1) : 1
  const statesWithMetric = values.length
  const unmatchedLabels = data?.meta?.unmatched_labels || []

  const navigateToState = useCallback(
    (code, fallbackName) => {
      if (!onDrillDown) return
      const state = statesByCode.get(code)
      onDrillDown("State Intelligence", {
        // Prefer the recorded spelling so State Intelligence's existing state
        // filter matches its table rows exactly. House is always passed —
        // including "All" as an empty string — so the destination cannot keep
        // a stale House filter from a previous visit.
        state: state?.drill_state || state?.display_name || fallbackName,
        house: house && house !== "All" ? house : "",
      })
    },
    [house, onDrillDown, statesByCode]
  )

  const activateState = useCallback(
    (code, fallbackName) => {
      if (!selectionEnabled) {
        navigateToState(code, fallbackName)
        return
      }
      setHover(null)
      setSelectedCode(code)
      const state = statesByCode.get(code)
      onSelectState(
        state || {
          state_code: code,
          display_name: fallbackName,
          drill_state: fallbackName,
        },
        { house, fy }
      )
    },
    [fy, house, navigateToState, onSelectState, selectionEnabled, statesByCode]
  )

  const clearSelection = useCallback(() => {
    setHover(null)
    // Dismiss the map's own detail panel. Parent page filters stay untouched,
    // matching the pre-existing inline "Clear selection" behavior.
    setSelectedCode(null)
  }, [])

  // Esc dismisses the mobile sheet from anywhere in the dialog.
  useEffect(() => {
    if (!isCompact || !selectedCode) return undefined
    const handler = (event) => {
      if (event.key === "Escape") clearSelection()
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [clearSelection, isCompact, selectedCode])

  const showHover = useCallback((event, code) => {
    const rect = mapRef.current?.getBoundingClientRect()
    if (!rect) return
    const x = event.clientX - rect.left
    const y = event.clientY - rect.top
    // Measured worst-case tooltip footprint at the app's 125% root font size
    // (the card is ~301px tall). The tooltip now lives outside the clipped map
    // frame, and this keeps its initial placement on-screen as well.
    const tooltipWidth = 252
    const tooltipHeight = 320
    const left = x + 16 + tooltipWidth > rect.width
      ? Math.max(8, x - tooltipWidth - 16)
      : x + 16
    const top = y + 16 + tooltipHeight > rect.height
      ? Math.max(8, y - tooltipHeight - 16)
      : y + 16
    setHover({ code, left, top })
  }, [])

  const handlePathFocus = useCallback((code) => {
    const rect = mapRef.current?.getBoundingClientRect()
    setHover({ code, left: 12, top: Math.max(12, (rect?.height || 320) - 332) })
  }, [])

  const hoveredFeature = hover
    ? features.find((feature) => feature.code === hover.code)
    : null
  const hoveredState = hover ? statesByCode.get(hover.code) : null
  const hoveredName = hoveredState?.display_name || hoveredFeature?.name || "State / UT"
  const hoveredValue = hover ? metricValue(hoveredState, metric) : null
  const selectedState = selectedCode ? statesByCode.get(selectedCode) : null
  const selectedFeature = selectedCode
    ? features.find((feature) => feature.code === selectedCode)
    : null
  const selectedName = selectedState?.display_name || selectedFeature?.name || "State / UT"
  const selectedMetricValue = selectedState ? metricValue(selectedState, metric) : null
  const selectedCompletionRate = Number.isFinite(selectedState?.completion_rate_percentage)
    ? selectedState.completion_rate_percentage
    : selectedState?.total_projects
      ? (selectedState.completed_projects / selectedState.total_projects) * 100
      : null
  const selectedRiskCoverage = Number.isFinite(selectedState?.risk_coverage_percentage)
    ? selectedState.risk_coverage_percentage
    : selectedState?.total_projects
      ? (selectedState.risk_scored_projects / selectedState.total_projects) * 100
      : null

  const renderSelectionContent = (sheet) => (
    <>
      <div
        className={
          sheet
            ? "sticky top-0 z-10 -mx-4 mb-3 flex items-start justify-between gap-3 border-b border-gray-100 bg-white px-4 pb-3 pt-4 dark:border-gray-700/60 dark:bg-[#0d090b]"
            : "flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between"
        }
      >
        <div className="min-w-0">
          <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">
            Selected state
          </p>
          <h3
            id={sheet ? sheetTitleId : undefined}
            className="mt-0.5 text-lg font-bold"
          >
            {selectedName}
          </h3>
          <p className={`mt-0.5 text-[0.6875rem] ${mutedText}`}>
            {house && house !== "All" ? house : "All Houses"} · {fy || "All FY"} · State-level aggregate, not a project location
          </p>
          {sheet && (
            <p className={`mt-1 text-[0.625rem] ${mutedText}`}>
              Scroll inside this panel to see every statistic.
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={clearSelection}
          autoFocus={sheet}
          aria-label={sheet ? "Close state details" : undefined}
          className={
            sheet
              ? "shrink-0 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-[0.6875rem] font-bold text-gray-600 shadow-sm transition hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-700 dark:bg-[#0d090b] dark:text-gray-300 dark:hover:bg-gray-800"
              : "self-start rounded-md border border-gray-200 px-2.5 py-1 text-[0.6875rem] font-bold text-gray-500 transition hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
          }
        >
          {sheet ? "✕ Close" : "Clear selection"}
        </button>
      </div>

      {selectedState ? (
        <>
          {(!selectedState.has_projects && !selectedState.has_risk_scores) && (
            <p className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-2.5 text-[0.6875rem] text-amber-800 dark:text-amber-300">
              No state-level records are available for this House/FY scope. Figures are shown as unavailable, never as claimed zeroes.
            </p>
          )}
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-5">
            <div className="rounded-lg border border-blue-200 bg-blue-50/60 p-2.5 dark:border-blue-900/50 dark:bg-blue-950/20">
              <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-blue-600 dark:text-blue-300">
                {selectedMetric.shortLabel}
              </p>
              <p className="mt-1 font-mono text-base font-bold">
                {formatMetricValue(metric, selectedMetricValue)}
              </p>
              <p className={`text-[0.5625rem] ${mutedText}`}>{selectedMetric.unit}</p>
            </div>
            <div className="rounded-lg border border-gray-100 p-2.5 dark:border-gray-700/60">
              <p className="text-[0.5625rem] uppercase tracking-wide text-gray-400">Total projects</p>
              <p className="mt-1 font-mono text-sm font-bold">
                {selectedState.has_projects ? formatNumber(selectedState.total_projects) : "Not recorded"}
              </p>
            </div>
            <div className="rounded-lg border border-gray-100 p-2.5 dark:border-gray-700/60">
              <p className="text-[0.5625rem] uppercase tracking-wide text-gray-400">Total expenditure</p>
              <p className="mt-1 font-mono text-sm font-bold">
                {selectedState.has_expenditure ? `₹${formatCrore(selectedState.total_expenditure)} Cr` : "Not recorded"}
              </p>
            </div>
            <div className="rounded-lg border border-gray-100 p-2.5 dark:border-gray-700/60">
              <p className="text-[0.5625rem] uppercase tracking-wide text-gray-400">Fund utilization</p>
              <p className="mt-1 font-mono text-sm font-bold">
                {Number.isFinite(selectedState.fund_utilization_percentage)
                  ? `${Number(selectedState.fund_utilization_percentage).toFixed(1)}%`
                  : "Not available"}
              </p>
            </div>
            <div className="rounded-lg border border-gray-100 p-2.5 dark:border-gray-700/60">
              <p className="text-[0.5625rem] uppercase tracking-wide text-gray-400">Completed projects</p>
              <p className="mt-1 font-mono text-sm font-bold">
                {selectedState.has_projects ? formatNumber(selectedState.completed_projects) : "Not recorded"}
              </p>
            </div>
            <div className="rounded-lg border border-gray-100 p-2.5 dark:border-gray-700/60">
              <p className="text-[0.5625rem] uppercase tracking-wide text-gray-400">Ongoing projects</p>
              <p className="mt-1 font-mono text-sm font-bold">
                {selectedState.has_projects && Number.isFinite(selectedState.ongoing_projects)
                  ? formatNumber(selectedState.ongoing_projects)
                  : "Not available"}
              </p>
            </div>
            <div className="rounded-lg border border-red-100 p-2.5 dark:border-red-900/40">
              <p className="text-[0.5625rem] uppercase tracking-wide text-red-400">High-risk projects</p>
              <p className="mt-1 font-mono text-sm font-bold text-red-600 dark:text-red-400">
                {selectedState.has_risk_scores ? formatNumber(selectedState.high_risk_projects) : "Not assessed"}
              </p>
            </div>
            <div className="rounded-lg border border-gray-100 p-2.5 dark:border-gray-700/60">
              <p className="text-[0.5625rem] uppercase tracking-wide text-gray-400">Completion rate</p>
              <p className="mt-1 font-mono text-sm font-bold">
                {Number.isFinite(selectedCompletionRate) ? `${selectedCompletionRate.toFixed(1)}%` : "Not available"}
              </p>
              <p className={`text-[0.5625rem] ${mutedText}`}>Completed ÷ total projects</p>
            </div>
            <div className="rounded-lg border border-gray-100 p-2.5 dark:border-gray-700/60 col-span-2 sm:col-span-1">
              <p className="text-[0.5625rem] uppercase tracking-wide text-gray-400">Risk-score coverage</p>
              <p className="mt-1 font-mono text-sm font-bold">
                {Number.isFinite(selectedRiskCoverage) ? `${selectedRiskCoverage.toFixed(1)}%` : "Not available"}
              </p>
              <p className={`text-[0.5625rem] ${mutedText}`}>
                {selectedState.has_projects && Number.isFinite(selectedState.risk_scored_projects)
                  ? `${formatNumber(selectedState.risk_scored_projects)} of ${formatNumber(selectedState.total_projects)} projects scored`
                  : "No project coverage recorded"}
              </p>
            </div>
          </div>

          <div className={`mt-3 grid gap-2 rounded-lg border border-gray-100 bg-gray-50/70 p-3 text-[0.6875rem] dark:border-gray-700/60 dark:bg-white/5 sm:grid-cols-2 ${mutedText}`}>
            <p>
              <span className="font-bold text-gray-600 dark:text-gray-300">Payment coverage:</span>{" "}
              {selectedState.has_expenditure
                ? `${formatNumber(selectedState.transactions)} recorded ledger transactions.`
                : "No recorded ledger data for this House/FY scope."}
            </p>
            <p>
              <span className="font-bold text-gray-600 dark:text-gray-300">Data freshness:</span>{" "}
              {freshnessMessage(dataFreshness)}
            </p>
            <p className="sm:col-span-2">
              <span className="font-bold text-gray-600 dark:text-gray-300">Average recorded physical progress:</span>{" "}
              {Number.isFinite(selectedState.average_completion_percentage)
                ? `${Number(selectedState.average_completion_percentage).toFixed(1)}%`
                : "Not available"}
              {" "}<span className="ml-1 text-[0.625rem]">(completion rate above is completed ÷ total; the two definitions differ)</span>
            </p>
          </div>

          {(onApplyState || onOpenState) && (
            <div className="mt-3 flex flex-col gap-2 sm:flex-row">
              {onApplyState && (
                <button
                  type="button"
                  onClick={() => {
                    onApplyState(selectedState)
                    if (sheet) clearSelection()
                  }}
                  className="rounded-lg bg-blue-600 px-3 py-2 text-xs font-bold text-white transition hover:bg-blue-700"
                >
                  Apply state as page filter
                </button>
              )}
              {onOpenState && (
                <button
                  type="button"
                  onClick={() => {
                    onOpenState(selectedState)
                    if (sheet) clearSelection()
                  }}
                  className="rounded-lg border border-blue-200 px-3 py-2 text-xs font-bold text-blue-700 transition hover:bg-blue-50 dark:border-blue-800 dark:text-blue-300 dark:hover:bg-blue-950/30"
                >
                  Open detailed state view
                </button>
              )}
            </div>
          )}
        </>
      ) : (
        <p className={`mt-3 rounded-lg border border-dashed border-gray-200 p-3 text-xs dark:border-gray-700 ${mutedText}`}>
          No state-level aggregate is available for this selection. The boundary is known, but the data is not — it is not being shown as zero.
        </p>
      )}
    </>
  )

  return (
    <section
      aria-labelledby={titleId}
      className={`mt-6 sm:mt-7 rounded-xl border p-4 sm:p-5 shadow-sm ${cardClasses}`}
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <h2
            id={titleId}
            className="text-base sm:text-lg font-bold"
          >
            {title}
          </h2>
          <p className={`mt-1 max-w-3xl text-xs leading-relaxed ${mutedText}`}>
            {subtitle}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-lg bg-gray-100 px-2.5 py-1 font-mono text-[0.6875rem] font-bold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
            {house && house !== "All" ? house : "All Houses"} · {fy || "All FY"}
          </span>
          <span className={`text-[0.625rem] ${mutedText}`}>
            {scopeNote}
          </span>
        </div>
      </div>

      <div
        role="group"
        aria-label="Map metric"
        className="mt-4 flex flex-nowrap items-center gap-1 overflow-x-auto rounded-lg border border-gray-200 p-1 dark:border-gray-700"
        style={{ scrollbarWidth: "thin" }}
      >
        {METRICS.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setMetric(item.id)}
            aria-pressed={metric === item.id}
            className={`shrink-0 whitespace-nowrap rounded-md px-2.5 py-1.5 text-[0.6875rem] sm:text-xs font-bold transition ${
              metric === item.id
                ? "bg-blue-600 text-white shadow-sm"
                : "text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {(error || geoError) && (
        <div className="mt-3 flex flex-col gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-300 sm:flex-row sm:items-center sm:justify-between">
          <span>⚠ {error || geoError}</span>
          <button
            type="button"
            onClick={() => {
              if (geoError) setGeoReloadKey((key) => key + 1)
              setReloadKey((key) => key + 1)
            }}
            className="self-start rounded-md border border-amber-500/40 px-2.5 py-1 font-bold transition hover:bg-amber-500/10"
          >
            Retry
          </button>
        </div>
      )}

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_290px]">
        {/* MAP */}
        <div className="flex min-w-0 flex-col gap-1.5">
          <div className="relative">
          <div
            ref={mapRef}
            className="relative min-h-[280px] overflow-hidden rounded-lg border border-gray-200 bg-gradient-to-b from-slate-50 to-white dark:border-gray-700/60 dark:from-[#000000] dark:to-[#000000]"
          >
            <svg
            viewBox={`0 0 ${MAP_WIDTH} ${MAP_HEIGHT}`}
            className="mx-auto block h-auto max-h-[520px] w-full max-w-[560px]"
            role="group"
            aria-label={`India state map showing ${selectedMetric.label}${fy ? ` for ${fy}` : ""}${house && house !== "All" ? `, ${house}` : ""}`}
          >
            <defs>
              <pattern
                id={noDataPatternId}
                width="6"
                height="6"
                patternUnits="userSpaceOnUse"
                patternTransform="rotate(45)"
              >
                <rect
                  width="6"
                  height="6"
                  fill={darkMode ? "#120b0e" : "#f8f1f1"}
                />
                <line
                  x1="0"
                  y1="0"
                  x2="0"
                  y2="6"
                  stroke={darkMode ? "#351923" : "#dfcbcd"}
                  strokeWidth="2"
                />
              </pattern>
            </defs>

            {features.map((feature) => {
              const state = statesByCode.get(feature.code)
              const value = metricValue(state, metric)
              const binIndex = scale.indexOf(value)
              const fill = Number.isFinite(value)
                ? colorForBin(binIndex, scale.bins.length, palette)
                : `url(#${noDataPatternId})`
              const isHovered = hover?.code === feature.code
              const isSelected = selectedCode === feature.code
              const stateName = state?.display_name || feature.name
              return (
                <path
                  key={feature.code}
                  d={feature.path}
                  fill={fill}
                  stroke={
                    isHovered || isSelected
                      ? darkMode
                        ? "#c9a9b3"
                        : "#c8102e"
                      : darkMode
                        ? "#000000"
                        : "#ffffff"
                  }
                  strokeWidth={isHovered || isSelected ? 2 : 1}
                  strokeLinejoin="round"
                  vectorEffect="non-scaling-stroke"
                  className="cursor-pointer outline-none"
                  style={{ transition: "fill 180ms ease, stroke 120ms ease" }}
                  role="button"
                  tabIndex={0}
                  aria-pressed={isSelected}
                  aria-label={`${selectionEnabled ? "Select" : "Open"} ${stateName}: ${selectedMetric.label} ${formatMetricValue(metric, value)}`}
                  onClick={() => activateState(feature.code, feature.name)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault()
                      activateState(feature.code, feature.name)
                    }
                  }}
                  onPointerEnter={(event) => {
                    if (event.pointerType === "mouse" || event.pointerType === "pen") {
                      showHover(event, feature.code)
                    }
                  }}
                  onPointerMove={(event) => {
                    if (event.pointerType === "mouse" || event.pointerType === "pen") {
                      showHover(event, feature.code)
                    }
                  }}
                  onPointerLeave={() => setHover(null)}
                  onFocus={() => handlePathFocus(feature.code)}
                  onBlur={() => setHover(null)}
                >
                  <title>{`${stateName}: ${selectedMetric.label} ${formatMetricValue(metric, value)}`}</title>
                </path>
              )
            })}
          </svg>

          {loading && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/55 backdrop-blur-[1px] dark:bg-[#000000]/55">
              <div className="flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-semibold shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
                <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-solid border-blue-600 border-r-transparent" />
                Loading state aggregates…
              </div>
            </div>
          )}

          {!geo && !geoError && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/55 backdrop-blur-[1px] dark:bg-[#000000]/55">
              <div className="flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-semibold shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
                <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-solid border-blue-600 border-r-transparent" />
                Loading India boundaries…
              </div>
            </div>
          )}

          </div>

          {/* HOVER / FOCUS TOOLTIP */}
          {hover && (
            <div
              className="pointer-events-none absolute z-20 w-[240px] rounded-xl border border-gray-200 bg-white/95 p-3 text-[#292326] shadow-soft-lg backdrop-blur dark:border-gray-700 dark:bg-[#0d090b]/95 dark:text-[#f5eff1] sm:w-[252px]"
              style={{ left: hover.left, top: hover.top }}
            >
              <div className="flex items-start justify-between gap-2">
                <p className="text-sm font-bold leading-tight">{hoveredName}</p>
                <span className="shrink-0 rounded-sm bg-blue-50 px-1.5 py-0.5 text-[0.5625rem] font-bold uppercase tracking-wide text-blue-700 dark:bg-blue-950/60 dark:text-blue-300">
                  {selectedMetric.shortLabel}
                </span>
              </div>
              <p className="mt-1 font-mono text-xl font-bold">
                {formatMetricValue(metric, hoveredValue)}
              </p>
              <p className={`text-[0.625rem] ${mutedText}`}>
                {Number.isFinite(hoveredValue)
                  ? selectedMetric.unit
                  : "No recorded value for this scope"}
              </p>
              <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 border-t border-gray-100 pt-2 text-[0.625rem] dark:border-gray-700/60">
                <div>
                  <dt className="text-gray-400">Total projects</dt>
                  <dd className="font-mono font-bold">
                    {hoveredState?.has_projects
                      ? formatNumber(hoveredState.total_projects)
                      : "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-400">Expenditure</dt>
                  <dd className="font-mono font-bold">
                    {hoveredState?.has_expenditure
                      ? `₹${formatCrore(hoveredState.total_expenditure)} Cr`
                      : "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-400">Completed</dt>
                  <dd className="font-mono font-bold">
                    {hoveredState?.has_projects
                      ? formatNumber(hoveredState.completed_projects)
                      : "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-400">High risk</dt>
                  <dd className="font-mono font-bold">
                    {hoveredState?.has_risk_scores
                      ? formatNumber(hoveredState.high_risk_projects)
                      : "Not assessed"}
                  </dd>
                </div>
                <div className="col-span-2">
                  <dt className="text-gray-400">Fund utilization</dt>
                  <dd className="font-mono font-bold">
                    {Number.isFinite(hoveredState?.fund_utilization_percentage)
                      ? `${Number(hoveredState.fund_utilization_percentage).toFixed(1)}%`
                      : "—"}
                  </dd>
                </div>
              </dl>
              <p className={`mt-2 border-t border-gray-100 pt-1.5 text-[0.5625rem] leading-snug dark:border-gray-700/60 ${mutedText}`}>
                State-level aggregate · not a project GPS location
              </p>
            </div>
          )}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 px-1 text-[0.5625rem] leading-snug">
            <span className={mutedText}>
              {selectionEnabled
                ? "Click a state to select it · state aggregates only"
                : "Click a state to open State Intelligence · state aggregates only"}
            </span>
            <span className={mutedText}>
              Boundaries: DataMeet (CC BY 2.5 IN)
            </span>
          </div>
        </div>

        {/* LEGEND + RANKING + DATA NOTES */}
        <aside className="flex min-w-0 flex-col gap-3">
          <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700/60">
            <div className="flex items-baseline justify-between gap-2">
              <h3 className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                Legend · {selectedMetric.label}
              </h3>
              <span className="text-[0.5625rem] text-gray-400">{selectedMetric.unit}</span>
            </div>
            {scale.bins.length > 0 ? (
              <div className="mt-2 space-y-1.5">
                {scale.bins.map((bin, index) => (
                  <div key={`${bin.min}-${bin.max}-${index}`} className="flex items-center gap-2">
                    <span
                      className="h-3 w-6 shrink-0 rounded-sm border border-black/10 dark:border-white/10"
                      style={{
                        backgroundColor: colorForBin(index, scale.bins.length, palette),
                      }}
                    />
                    <span className="font-mono text-[0.625rem] text-gray-600 dark:text-gray-300">
                      {legendLabel(metric, bin)}
                    </span>
                  </div>
                ))}
                <div className="flex items-center gap-2 border-t border-gray-100 pt-1.5 dark:border-gray-700/60">
                  <span
                    className="h-3 w-6 shrink-0 rounded-sm border border-black/10 dark:border-white/10"
                    style={noDataPatternStyle(darkMode)}
                  />
                  <span className="text-[0.625rem] text-gray-500 dark:text-gray-400">
                    No recorded value
                  </span>
                </div>
              </div>
            ) : (
              <p className={`mt-2 text-[0.6875rem] ${mutedText}`}>
                No state-level value is available for this metric in the selected scope.
              </p>
            )}
          </div>

          <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700/60">
            <h3 className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">
              Top states · {selectedMetric.shortLabel}
            </h3>
            {topStates.length > 0 ? (
              <div className="mt-2 space-y-2">
                {topStates.map(({ state, value }) => (
                  <button
                    key={state.state_code}
                    type="button"
                    onClick={() => activateState(state.state_code, state.display_name)}
                    className="block w-full rounded-md text-left transition hover:bg-gray-50 dark:hover:bg-gray-800/60"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-[0.6875rem] font-semibold">
                        {state.display_name}
                      </span>
                      <span className="shrink-0 font-mono text-[0.625rem] font-bold">
                        {formatMetricValue(metric, value)}
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700/60">
                      <div
                        className="h-full rounded-full transition-all duration-500"
                        style={{
                          width: `${Math.max(4, (value / topMax) * 100)}%`,
                          backgroundColor: palette[3],
                        }}
                      />
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <p className={`mt-2 text-[0.6875rem] ${mutedText}`}>
                {metric === "high_risk_projects"
                  ? "No high-risk projects recorded in this scope."
                  : "No state-level value is available for this metric in the selected scope."}
              </p>
            )}
          </div>

          <div className={`rounded-lg border border-gray-100 bg-gray-50/70 p-3 text-[0.625rem] leading-relaxed dark:border-gray-700/60 dark:bg-white/5 ${mutedText}`}>
            <p>
              <span className="font-bold text-gray-600 dark:text-gray-300">
                {statesWithMetric} of {data?.meta?.states_total ?? 36}
              </span>{" "}
              states/UTs have a recorded value for this metric. A hatched state
              has no recorded value; a light shaded state can be a recorded zero.
            </p>
            {unmatchedLabels.length > 0 && (
              <p className="mt-1.5 text-amber-700 dark:text-amber-300">
                ⚠ {unmatchedLabels.length} recorded state label
                {unmatchedLabels.length === 1 ? "" : "s"} could not be matched
                to the map ({unmatchedLabels.slice(0, 3).join(", ")}
                {unmatchedLabels.length > 3 ? "…" : ""}). Those records are
                excluded from the choropleth, never reassigned to another state.
              </p>
            )}
            {data?.metric_definitions?.[metric] && (
              <p className="mt-1.5 border-t border-gray-200/70 pt-1.5 dark:border-gray-700/60">
                <span className="font-bold text-gray-600 dark:text-gray-300">
                  Definition:
                </span>{" "}
                {data.metric_definitions[metric]}
              </p>
            )}
          </div>
        </aside>
      </div>

      {selectionEnabled && selectedCode && !isCompact && (
        <div className={`mt-4 rounded-xl border p-4 shadow-sm ${cardClasses}`}>
          {renderSelectionContent(false)}
        </div>
      )}

      {selectionEnabled && selectedCode && isCompact && typeof document !== "undefined" &&
        createPortal(
          <div className="fixed inset-0 z-[100] flex items-end justify-center">
            <button
              type="button"
              aria-label="Close state details"
              onClick={clearSelection}
              className="absolute inset-0 h-full w-full cursor-default bg-black/40 backdrop-blur-[1px]"
            />
            <div
              role="dialog"
              aria-modal="true"
              aria-labelledby={sheetTitleId}
              className={`relative flex w-full max-w-lg flex-col overflow-hidden rounded-t-2xl border border-b-0 shadow-2xl ${cardClasses}`}
              style={{ maxHeight: "min(88dvh, 720px)" }}
            >
              <div
                className="mx-auto mt-2 h-1 w-10 shrink-0 rounded-full bg-gray-300 dark:bg-gray-600"
                aria-hidden="true"
              />
              <div
                className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4"
                style={{ paddingBottom: "max(1rem, env(safe-area-inset-bottom))" }}
              >
                {renderSelectionContent(true)}
              </div>
            </div>
          </div>,
          // Mount inside the app shell (not <body>) so the sheet inherits the
          // active light/dark theme without duplicating every dark: variant.
          document.getElementById("root")?.firstElementChild || document.body
        )}
    </section>
  )
})

export default GeographicDistribution
