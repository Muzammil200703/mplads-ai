import { useState, useCallback, useEffect, useRef } from "react"
import { searchProjects, getProjectDetail } from "../services/api"

const MAX_COMPARE = 4
const SEARCH_DEBOUNCE_MS = 280
const SEARCH_CACHE_MAX = 25

// Simple LRU cache: Map iterates in insertion order, delete+set moves to end
const searchCache = new Map()
function cacheGet(key) {
  if (searchCache.has(key)) {
    const v = searchCache.get(key)
    searchCache.delete(key)
    searchCache.set(key, v)
    return v
  }
  return undefined
}
function cacheSet(key, value) {
  if (searchCache.has(key)) searchCache.delete(key)
  if (searchCache.size >= SEARCH_CACHE_MAX) {
    // evict oldest (first entry)
    const oldest = searchCache.keys().next().value
    searchCache.delete(oldest)
  }
  searchCache.set(key, value)
}

function fmtMoney(v) {
  const n = Number(v || 0)
  if (n >= 10000000) return `₹${(n / 10000000).toFixed(2)} Cr`
  if (n >= 100000) return `₹${(n / 100000).toFixed(2)} L`
  return `₹${n.toLocaleString("en-IN")}`
}

function riskBadgeClass(level) {
  if (level === "High") return "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950/70 dark:text-red-300"
  if (level === "Medium") return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/70 dark:text-amber-300"
  return "border-green-200 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-950/70 dark:text-green-300"
}

function riskBg(level) {
  if (level === "High") return "bg-red-50 dark:bg-red-950/30"
  if (level === "Medium") return "bg-amber-50 dark:bg-amber-950/30"
  return "bg-green-50 dark:bg-green-950/30"
}

const METRICS = [
  { label: "State", fn: (p) => p.state || "N/A" },
  { label: "District", fn: (p) => p.district || "N/A" },
  { label: "Constituency", fn: (p) => p.constituency || "N/A" },
  { label: "MP", fn: (p) => p.mp_name || "N/A" },
  { label: "Financial Year", fn: (p) => p.fy || "N/A" },
  { label: "Project Type", fn: (p) => p.project_type || "N/A" },
  { label: "Status", fn: (p) => p.status || "Ongoing", badge: true },
  {
    label: "Sanctioned Amount",
    fn: (p) => fmtMoney(p.sanctioned_amount),
    raw: (p) => Number(p.sanctioned_amount || 0),
    highlight: "blue",
  },
  {
    label: "Expenditure",
    fn: (p) => fmtMoney(p.expenditure),
    raw: (p) => Number(p.expenditure || 0),
  },
  {
    label: "Expenditure %",
    fn: (p) => {
      const s = Number(p.sanctioned_amount || 0)
      const e = Number(p.expenditure || 0)
      return s > 0 ? `${((e / s) * 100).toFixed(1)}%` : "N/A"
    },
    raw: (p) => {
      const s = Number(p.sanctioned_amount || 0)
      return s > 0 ? (Number(p.expenditure || 0) / s) * 100 : 0
    },
  },
  {
    label: "Physical Progress",
    fn: (p) => `${Number(p.completion_percentage || 0).toFixed(1)}%`,
    raw: (p) => Number(p.completion_percentage || 0),
    bar: true,
  },
  {
    label: "Risk Score",
    fn: (p) => (p.risk ? `${p.risk.risk_score}/100` : "N/A"),
    raw: (p) => p.risk?.risk_score || 0,
    risk: true,
  },
  {
    label: "Risk Level",
    fn: (p) => p.risk?.risk_level || "None",
    riskBadge: true,
  },
  {
    label: "ML Anomaly",
    fn: (p) => (p.risk?.ml_anomaly ? "Yes" : "No"),
    anomalyBadge: true,
  },
  {
    label: "Anomaly Reasons",
    fn: (p) => {
      const reasons = p.risk?.reasons
      if (!reasons || !reasons.length) return "—"
      return reasons
    },
    reasons: true,
  },
]

export default function CompareProjects({ fy, drillDownParams, onClearDrillDown }) {
  const [query, setQuery] = useState("")
  const [results, setResults] = useState([])
  const [searching, setSearching] = useState(false)
  const [searchTotal, setSearchTotal] = useState(0)
  const [selected, setSelected] = useState([])
  const [details, setDetails] = useState({})
  const [loadingDetail, setLoadingDetail] = useState(null)
  const [searchFocused, setSearchFocused] = useState(false)
  const searchTimer = useRef(null)
  const searchInputRef = useRef(null)
  const abortRef = useRef(null)
  const latestQueryRef = useRef("")

  const fetchResults = useCallback(
    async (q) => {
      const trimmed = q.trim()
      if (!trimmed || trimmed.length < 1) {
        setResults([])
        setSearchTotal(0)
        setSearching(false)
        return
      }

      // Check cache first
      const cacheKey = `${trimmed}::${fy || "all"}`
      const cached = cacheGet(cacheKey)
      if (cached) {
        setResults(cached.results)
        setSearchTotal(cached.total)
        setSearching(false)
        return
      }

      // Cancel any in-flight request
      if (abortRef.current) abortRef.current.abort()
      const controller = new AbortController()
      abortRef.current = controller

      try {
        setSearching(true)
        const data = await searchProjects({
          q: trimmed,
          fy: fy || undefined,
          skip: 0,
          limit: 20,
          signal: controller.signal,
        })
        // Only apply if this is still the latest query
        if (!controller.signal.aborted && latestQueryRef.current === trimmed) {
          const results = data?.results || []
          // Prefer hasMore for accurate display; fall back to total count
          const total = data?.hasMore
            ? results.length + 1  // at least one more exists
            : (data?.total || results.length)
          setResults(results)
          setSearchTotal(total)
          cacheSet(cacheKey, { results, total })
        }
      } catch (err) {
        if (err?.name !== "AbortError" && latestQueryRef.current === trimmed) {
          setResults([])
          setSearchTotal(0)
        }
      } finally {
        if (!controller.signal.aborted && latestQueryRef.current === trimmed) {
          setSearching(false)
        }
      }
    },
    [fy]
  )

  const handleQueryChange = useCallback((val) => {
    setQuery(val)
    latestQueryRef.current = val.trim()
    if (searchTimer.current) clearTimeout(searchTimer.current)
    if (!val || val.trim().length < 1) {
      // Immediate clear — no debounce needed for empty
      setResults([])
      setSearchTotal(0)
      setSearching(false)
      if (abortRef.current) abortRef.current.abort()
      return
    }
    searchTimer.current = setTimeout(() => fetchResults(val), SEARCH_DEBOUNCE_MS)
  }, [fetchResults])

  const handleAdd = async (proj) => {
    if (selected.find((s) => s.id === proj.id)) return
    if (selected.length >= MAX_COMPARE) return
    const newSelected = [...selected, proj]
    setSelected(newSelected)
    // First selected project = the assistant's active project context.
    if (newSelected.length === 1) {
      window.dispatchEvent(new CustomEvent("assistant-project-context", {
        detail: { projectId: proj.id, projectName: proj.project_name || null },
      }))
    }
    if (!details[proj.id]) {
      setLoadingDetail(proj.id)
      try {
        const res = await getProjectDetail(proj.id)
        setDetails((prev) => ({
          ...prev,
          [proj.id]: { project: res.project, risk: res.risk },
        }))
      } catch {
        setDetails((prev) => ({
          ...prev,
          [proj.id]: { project: proj, risk: null },
        }))
      } finally {
        setLoadingDetail(null)
      }
    }
  }

  const handleRemove = (id) => {
    setSelected((prev) => prev.filter((s) => s.id !== id))
    // Removing the primary (first) project releases the assistant context —
    // never silently re-anchor it onto another comparison project.
    if (selected.length > 0 && selected[0].id === id) {
      window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } }))
    }
  }

  // Assistant preload: navigate action with {a, b} params seeds the
  // comparison with those project ids (validated numbers only).
  useEffect(() => {
    const ids = [drillDownParams?.a, drillDownParams?.b]
      .map((v) => parseInt(v, 10))
      .filter((v) => Number.isInteger(v) && String(v).length <= 10)
    if (ids.length === 0) return
    ;(async () => {
      for (const id of ids) {
        if (selected.find((s) => s.id === id)) continue
        try {
          const res = await getProjectDetail(id)
          handleAdd(res.project || { id })
        } catch {
          /* unknown id — skip silently, user can still search manually */
        }
      }
      onClearDrillDown?.()
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drillDownParams])

  const handleClearSearch = () => {
    setQuery("")
    latestQueryRef.current = ""
    setResults([])
    setSearchTotal(0)
    setSearchFocused(false)
    setSearching(false)
    if (abortRef.current) abortRef.current.abort()
    if (searchTimer.current) clearTimeout(searchTimer.current)
    if (searchInputRef.current) searchInputRef.current.blur()
  }

  const getFull = (id) => details[id] || null
  const getProject = (id) =>
    getFull(id)?.project || selected.find((s) => s.id === id) || {}
  const getRisk = (id) => getFull(id)?.risk || null

  useEffect(
    () => () => {
      if (searchTimer.current) clearTimeout(searchTimer.current)
      if (abortRef.current) abortRef.current.abort()
    },
    []
  )

  const enriched = selected.map((s) => ({
    ...getProject(s.id),
    risk: getRisk(s.id),
    _loading: loadingDetail === s.id,
  }))

  const hasComparison = enriched.length >= 2

  const showSearchResults =
    results.length > 0 &&
    searchFocused &&
    (!hasComparison || searchFocused)

  // Compute highlight colors per row
  function getHighlights(row) {
    if (!row.raw) return null
    const vals = enriched.map((p) => row.raw(p))
    const valid = vals.filter((v) => v !== null && v !== undefined && v > 0)
    if (valid.length < 2) return null
    const max = Math.max(...valid)
    const min = Math.min(...valid)
    return vals.map((v) => {
      if (v === max && max > 0) return "max"
      if (v === min && min > 0 && max !== min) return "min"
      return null
    })
  }

  // Col width per project column
  const colWidth = Math.max(260, Math.floor(1000 / Math.max(enriched.length, 1)))

  return (
    <div className="min-h-full bg-[#f9f9ff] p-4 sm:p-6 text-[#151c27] transition-colors duration-200 dark:bg-[#0a0a0c] dark:text-gray-100">
      <div className="mx-auto max-w-[1440px] space-y-5">

        {/* HEADER */}
        <div>
          <h2 className="text-2xl font-bold text-[#031632] dark:text-white">
            Compare Projects
          </h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Select 2–4 MPLADS projects to compare side by side. Search by project
            ID, name, state, constituency, or MP.
          </p>
        </div>

        {/* SEARCH */}
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm transition-colors dark:border-gray-700/80 dark:bg-[#17181c]">
          <div className="flex items-center justify-between">
            <label className="text-xs font-semibold uppercase tracking-wider text-gray-700 dark:text-gray-200">
              Search Projects
            </label>
            {hasComparison && (
              <button
                onClick={handleClearSearch}
                className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                {searchFocused ? "Done" : "+ Search More"}
              </button>
            )}
          </div>
          <div className="mt-2">
            <div className="relative">
              <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400">🔍</span>
              <input
                ref={searchInputRef}
                value={query}
                onChange={(e) => handleQueryChange(e.target.value)}
                onFocus={() => setSearchFocused(true)}
                onBlur={() => setTimeout(() => setSearchFocused(false), 200)}
                placeholder="Search by ID, name, state, constituency, MP..."
                className="w-full rounded-lg border border-gray-300 bg-white py-2.5 pl-9 pr-3 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#0a0a0c] dark:text-white"
              />
              {searching && (
                <span className="absolute right-3 top-1/2 -translate-y-1/2">
                  <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-r-transparent" />
                </span>
              )}
            </div>
          </div>

          {showSearchResults && (
            <div className="mt-3 max-h-72 overflow-y-auto rounded-lg border border-gray-200 dark:border-gray-700">
              <div className="bg-gray-50 px-4 py-2 text-[0.625rem] font-bold uppercase tracking-wider text-gray-400 dark:bg-[#141418]">
                {searchTotal.toLocaleString("en-IN")}+ results — showing first {results.length}
              </div>
              {results.map((proj) => {
                const isSelected = selected.some((s) => s.id === proj.id)
                const isFull = selected.length >= MAX_COMPARE
                return (
                  <div
                    key={proj.id}
                    className={`flex items-center justify-between gap-3 border-b border-gray-100 px-4 py-3 transition dark:border-gray-700/40 ${
                      isSelected ? "bg-blue-50 dark:bg-blue-950/30" : "hover:bg-gray-50 dark:hover:bg-[#141418]"
                    }`}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-[0.625rem] font-bold text-gray-400">#{proj.id}</span>
                        <span className="truncate text-sm font-semibold text-gray-900 dark:text-white">
                          {proj.project_name || "Unnamed Project"}
                        </span>
                      </div>
                      <p className="mt-0.5 text-[0.6875rem] text-gray-400 dark:text-gray-500">
                        {proj.state || "N/A"}{proj.constituency ? ` · ${proj.constituency}` : ""} · {fmtMoney(proj.sanctioned_amount)}
                      </p>
                    </div>
                    <button
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => isSelected ? handleRemove(proj.id) : handleAdd(proj)}
                      disabled={!isSelected && isFull}
                      className={`shrink-0 rounded-lg px-3 py-1.5 text-[0.625rem] font-bold uppercase tracking-wider transition ${
                        isSelected
                          ? "border border-red-200 bg-red-50 text-red-600 hover:bg-red-100 dark:border-red-800 dark:bg-red-950/50 dark:text-red-300"
                          : isFull
                            ? "border border-gray-200 bg-gray-100 text-gray-400 cursor-not-allowed dark:border-gray-700 dark:bg-gray-800 dark:text-gray-600"
                            : "border border-blue-200 bg-blue-50 text-blue-600 hover:bg-blue-100 dark:border-blue-800 dark:bg-blue-950/50 dark:text-blue-300"
                      }`}
                    >
                      {isSelected ? "Remove" : "Add"}
                    </button>
                  </div>
                )
              })}
            </div>
          )}
          {query.length > 0 && results.length === 0 && !searching && (
            <p className="mt-3 text-center text-xs text-gray-400 dark:text-gray-500">
              No projects found for "{query}"
            </p>
          )}
        </div>

        {/* SELECTED PROJECTS CHIPS */}
        {selected.length > 0 && (
          <div className="rounded-xl border border-blue-200 bg-blue-50/50 p-4 dark:border-blue-800 dark:bg-blue-950/30">
            <div className="mb-2 text-[0.625rem] font-bold uppercase tracking-wider text-blue-500 dark:text-blue-400">
              Comparing {selected.length}/{MAX_COMPARE} Projects
            </div>
            <div className="flex flex-wrap gap-2">
              {selected.map((s) => {
                const p = getProject(s.id)
                return (
                  <span
                    key={s.id}
                    className="inline-flex items-center gap-2 rounded-lg border border-blue-200 bg-white px-3 py-2 text-xs font-semibold text-blue-800 shadow-sm dark:border-blue-800 dark:bg-[#17181c] dark:text-blue-200"
                  >
                    <span className="font-mono text-[0.625rem] font-bold text-blue-400">#{s.id}</span>
                    <span className="max-w-[180px] truncate">{p.project_name || "Unnamed"}</span>
                    <button
                      onClick={() => handleRemove(s.id)}
                      className="ml-1 rounded p-0.5 text-blue-400 hover:bg-blue-200 hover:text-blue-700 dark:hover:bg-blue-800"
                    >✕</button>
                  </span>
                )
              })}
              {selected.length < MAX_COMPARE && (
                <button
                  onClick={() => { setSearchFocused(true); searchInputRef.current?.focus() }}
                  className="inline-flex items-center gap-1 rounded-lg border border-dashed border-blue-300 bg-transparent px-3 py-2 text-xs font-semibold text-blue-400 transition hover:border-blue-400 hover:text-blue-600 dark:border-blue-700 dark:hover:text-blue-300"
                >
                  + Add Project
                </button>
              )}
            </div>
          </div>
        )}

        {/* EMPTY STATE */}
        {!hasComparison && (
          <div className="rounded-xl border-2 border-dashed border-gray-200 bg-white/50 p-12 text-center transition-colors dark:border-gray-700 dark:bg-[#17181c]/50">
            <div className="text-4xl mb-3">⚖️</div>
            <p className="text-sm font-semibold text-gray-500 dark:text-gray-400">
              {selected.length === 0
                ? "Search and select at least 2 projects to compare"
                : `1 project selected — add ${2 - selected.length} more to start comparing`}
            </p>
            <p className="mt-1 text-xs text-gray-400 dark:text-gray-500">
              You can compare up to {MAX_COMPARE} projects side by side
            </p>
          </div>
        )}

        {/* ===== TRUE SIDE-BY-SIDE COMPARISON TABLE ===== */}
        {hasComparison && (
          <div className="space-y-5">

            <div className="rounded-xl border border-gray-200 bg-white shadow-sm transition-colors dark:border-gray-700/80 dark:bg-[#17181c]">
              <div className="border-b border-gray-200 px-6 py-4 dark:border-gray-700">
                <h3 className="text-base font-bold text-[#031632] dark:text-white">
                  Detailed Comparison
                </h3>
                <p className="mt-0.5 text-xs text-gray-400 dark:text-gray-500">
                  Scroll horizontally if needed. Highest values highlighted in red, lowest in green.
                </p>
              </div>

              {/* Scrollable table container — no page-level overflow */}
              <div className="overflow-x-auto" style={{ maxHeight: "none" }}>
                <table
                  className="w-full border-collapse"
                  style={{
                    tableLayout: "fixed",
                    minWidth: `${190 + enriched.length * colWidth}px`,
                  }}
                >
                  {/* Column widths */}
                  <colgroup>
                    <col style={{ width: "190px" }} />
                    {enriched.map((_, i) => (
                      <col key={i} style={{ width: `${colWidth}px` }} />
                    ))}
                  </colgroup>

                  {/* PROJECT HEADERS — sticky top */}
                  <thead>
                    <tr className="border-b border-gray-200 dark:border-gray-700">
                      <th className="sticky left-0 z-20 bg-gray-50 p-4 text-left align-top dark:bg-[#141418]">
                        <span className="text-[0.6875rem] font-bold uppercase tracking-wider text-gray-400">
                          Metric
                        </span>
                      </th>
                      {enriched.map((p) => (
                        <th
                          key={p.id}
                          className="border-l border-gray-100 p-4 text-left align-top dark:border-gray-700/60"
                          style={{ background: "inherit" }}
                        >
                          <div className="space-y-1">
                            <div className="flex items-start justify-between gap-2">
                              <span className="font-mono text-xs font-bold text-blue-500 dark:text-blue-400">
                                #{p.id}
                              </span>
                              <button
                                onClick={() => handleRemove(p.id)}
                                className="shrink-0 rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-500 dark:hover:bg-red-950/50"
                                title="Remove from comparison"
                              >
                                ✕
                              </button>
                            </div>
                            <p
                              className="text-sm font-bold leading-snug text-gray-900 dark:text-white"
                              style={{
                                display: "-webkit-box",
                                WebkitLineClamp: 3,
                                WebkitBoxOrient: "vertical",
                                overflow: "hidden",
                                wordBreak: "break-word",
                              }}
                              title={p.project_name}
                            >
                              {p.project_name || "Unnamed Project"}
                            </p>
                            <p className="text-[0.6875rem] text-gray-400 dark:text-gray-500">
                              {p.state || "N/A"}{p.constituency ? ` · ${p.constituency}` : ""}
                            </p>
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>

                  {/* METRIC ROWS — each row = one metric across all projects */}
                  <tbody>
                    {METRICS.map((row, rowIdx) => {
                      const highlights = getHighlights(row)
                      const isEven = rowIdx % 2 === 0

                      return (
                        <tr
                          key={row.label}
                          className={`border-b border-gray-100 transition dark:border-gray-700/40 ${
                            isEven
                              ? "bg-white dark:bg-[#17181c]"
                              : "bg-gray-50/80 dark:bg-[#141418]/50"
                          }`}
                        >
                          {/* Sticky metric label */}
                          <td
                            className={`sticky left-0 z-10 p-4 text-xs font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400 ${
                              isEven
                                ? "bg-white dark:bg-[#17181c]"
                                : "bg-gray-50/80 dark:bg-[#141418]/50"
                            }`}
                          >
                            {row.label}
                          </td>

                          {/* Value cells — one per project */}
                          {enriched.map((p, idx) => {
                            const val = row.fn(p)
                            const hl = highlights ? highlights[idx] : null

                            // --- Risk Score cell ---
                            if (row.risk) {
                              const score = p.risk?.risk_score ?? null
                              const level = p.risk?.risk_level || "None"
                              const scoreColor =
                                level === "High"
                                  ? "text-red-600 dark:text-red-400"
                                  : level === "Medium"
                                    ? "text-amber-600 dark:text-amber-400"
                                    : "text-green-600 dark:text-green-400"
                              return (
                                <td
                                  key={p.id}
                                  className="border-l border-gray-100 p-4 dark:border-gray-700/60"
                                >
                                  <span className={`font-mono text-sm font-bold ${scoreColor}`}>
                                    {val}
                                  </span>
                                </td>
                              )
                            }

                            // --- Risk Level badge ---
                            if (row.riskBadge) {
                              return (
                                <td
                                  key={p.id}
                                  className="border-l border-gray-100 p-4 dark:border-gray-700/60"
                                >
                                  <span className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[0.6875rem] font-bold uppercase ${riskBadgeClass(val)}`}>
                                    {val}
                                  </span>
                                </td>
                              )
                            }

                            // --- ML Anomaly badge ---
                            if (row.anomalyBadge) {
                              const isYes = val === "Yes"
                              return (
                                <td
                                  key={p.id}
                                  className="border-l border-gray-100 p-4 dark:border-gray-700/60"
                                >
                                  <span
                                    className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[0.6875rem] font-bold uppercase ${
                                      isYes
                                        ? "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/70 dark:text-amber-300"
                                        : "border-green-200 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-950/70 dark:text-green-300"
                                    }`}
                                  >
                                    {isYes ? "⚠" : "✓"} {val}
                                  </span>
                                </td>
                              )
                            }

                            // --- Status badge ---
                            if (row.badge) {
                              return (
                                <td
                                  key={p.id}
                                  className="border-l border-gray-100 p-4 dark:border-gray-700/60"
                                >
                                  <span className="inline-flex items-center rounded-md border border-gray-200 bg-gray-100 px-2.5 py-1 text-xs font-medium dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200">
                                    {val}
                                  </span>
                                </td>
                              )
                            }

                            // --- Physical Progress with mini bar ---
                            if (row.bar) {
                              const pct = Number(p.completion_percentage || 0)
                              const barColor =
                                pct >= 75 ? "bg-green-500" : pct >= 40 ? "bg-blue-500" : pct >= 15 ? "bg-amber-500" : "bg-red-500"
                              return (
                                <td
                                  key={p.id}
                                  className="border-l border-gray-100 p-4 dark:border-gray-700/60"
                                >
                                  <span className="font-mono text-sm font-bold text-gray-800 dark:text-gray-200">
                                    {val}
                                  </span>
                                  <div className="mt-1.5 h-2 w-full rounded-full bg-gray-100 dark:bg-gray-700">
                                    <div
                                      className={`bar-start h-full rounded-full transition-all duration-500 ${barColor}`}
                                      style={{ width: `${Math.min(100, pct)}%` }}
                                    />
                                  </div>
                                </td>
                              )
                            }

                            // --- Anomaly Reasons (wrapped text) ---
                            if (row.reasons) {
                              const reasons = Array.isArray(val) ? val : []
                              return (
                                <td
                                  key={p.id}
                                  className="border-l border-gray-100 p-4 dark:border-gray-700/60"
                                >
                                  {reasons.length === 0 ? (
                                    <span className="text-sm text-gray-400 dark:text-gray-500">—</span>
                                  ) : (
                                    <ul className="space-y-1">
                                      {reasons.slice(0, 5).map((r, i) => (
                                        <li
                                          key={i}
                                          className="text-xs leading-relaxed text-gray-600 dark:text-gray-300"
                                        >
                                          • {r}
                                        </li>
                                      ))}
                                      {reasons.length > 5 && (
                                        <li className="text-[0.625rem] font-semibold text-gray-400 dark:text-gray-500">
                                          +{reasons.length - 5} more
                                        </li>
                                      )}
                                    </ul>
                                  )}
                                </td>
                              )
                            }

                            // --- Sanctioned Amount (blue highlight) ---
                            if (row.highlight === "blue") {
                              return (
                                <td
                                  key={p.id}
                                  className="border-l border-gray-100 p-4 dark:border-gray-700/60"
                                >
                                  <span className="font-mono text-sm font-bold text-blue-600 dark:text-blue-400">
                                    {val}
                                  </span>
                                </td>
                              )
                            }

                            // --- Default text cell with highlight ---
                            let cellClass = "font-mono text-sm "
                            if (hl === "max") {
                              cellClass += "font-bold text-red-700 dark:text-red-300"
                            } else if (hl === "min") {
                              cellClass += "font-semibold text-green-700 dark:text-green-300"
                            } else if (row.text) {
                              cellClass += "font-sans font-semibold text-gray-900 dark:text-white"
                            } else {
                              cellClass += "text-gray-700 dark:text-gray-300"
                            }

                            return (
                              <td
                                key={p.id}
                                className={`border-l border-gray-100 p-4 dark:border-gray-700/60 ${cellClass}`}
                                style={row.text ? { wordBreak: "break-word" } : undefined}
                              >
                                {val}
                              </td>
                            )
                          })}
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            {/* ===== CHARTS BELOW TABLE ===== */}

            {/* EXPENDITURE VS SANCTIONED */}
            <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm transition-colors dark:border-gray-700/80 dark:bg-[#17181c]">
              <h3 className="mb-1 text-base font-bold text-[#031632] dark:text-white">
                Expenditure vs Sanctioned Amount
              </h3>
              <p className="mb-5 text-xs text-gray-400 dark:text-gray-500">
                Overlay shows spending relative to sanctioned budget. Red indicates over-budget.
              </p>
              <div className="space-y-5">
                {enriched.map((p) => {
                  const s = Number(p.sanctioned_amount || 0)
                  const e = Number(p.expenditure || 0)
                  const pct = s > 0 ? Math.min(150, (e / s) * 100) : 0
                  const overBudget = e > s && s > 0
                  return (
                    <div key={p.id}>
                      <div className="mb-1.5 flex items-center justify-between">
                        <span className="text-sm font-semibold text-gray-700 dark:text-gray-200">
                          <span className="font-mono text-gray-400">#{p.id}</span>{" "}
                          {(p.project_name || "Unnamed").substring(0, 50)}
                        </span>
                        <span className={`font-mono text-sm font-bold ${overBudget ? "text-red-600 dark:text-red-400" : "text-gray-600 dark:text-gray-300"}`}>
                          {pct.toFixed(1)}%
                        </span>
                      </div>
                      <div className="relative h-6 w-full overflow-hidden rounded bg-gray-100 dark:bg-gray-700">
                        <div className="absolute inset-y-0 left-0 bg-blue-200/60 dark:bg-blue-800/40" style={{ width: "100%" }} />
                        <div
                          className={`bar-start absolute inset-y-0 left-0 rounded ${overBudget ? "bg-red-500/80" : "bg-blue-500/80"}`}
                          style={{ width: `${Math.min(100, pct)}%` }}
                        />
                      </div>
                      <div className="mt-1.5 flex justify-between text-xs text-gray-400 dark:text-gray-500">
                        <span>Spent: {fmtMoney(e)}</span>
                        <span>Sanctioned: {fmtMoney(s)}</span>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* PHYSICAL PROGRESS */}
            <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm transition-colors dark:border-gray-700/80 dark:bg-[#17181c]">
              <h3 className="mb-1 text-base font-bold text-[#031632] dark:text-white">
                Physical Progress Comparison
              </h3>
              <p className="mb-5 text-xs text-gray-400 dark:text-gray-500">
                Color indicates progress level: green (75%+), blue (40%+), amber (15%+), red (&lt;15%)
              </p>
              <div className="space-y-5">
                {enriched.map((p) => {
                  const c = Number(p.completion_percentage || 0)
                  return (
                    <div key={p.id}>
                      <div className="mb-1.5 flex items-center justify-between">
                        <span className="text-sm font-semibold text-gray-700 dark:text-gray-200">
                          <span className="font-mono text-gray-400">#{p.id}</span>{" "}
                          {(p.project_name || "Unnamed").substring(0, 50)}
                        </span>
                        <span className="font-mono text-sm font-bold text-gray-600 dark:text-gray-300">
                          {c.toFixed(1)}%
                        </span>
                      </div>
                      <div className="h-3 w-full rounded-full bg-gray-100 dark:bg-gray-700">
                        <div
                          className={`h-full rounded-full transition-all duration-500 ${
                            c >= 75 ? "bg-green-500" : c >= 40 ? "bg-blue-500" : c >= 15 ? "bg-amber-500" : "bg-red-500"
                          }`}
                          style={{ width: `${Math.min(100, c)}%` }}
                        />
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
