import { lazy, Suspense, useEffect, useState, useCallback, useRef, memo } from "react"
import {
  getProjects,
  searchProjects,
  getStates,
  getConstituencies,
  getProjectDetail,
} from "../services/api"
import { useAuth } from "../context/AuthContext"
// Project-selection interface: the original full ProjectDetail view
// (presented as a centered rounded card), code-split so it only loads
// when a user actually selects a project.
const ProjectDetail = lazy(() => import("../components/ProjectDetail"))
import { formatMoney, formatNumber, formatDateHuman } from "../utils/format"
import { TableRowsSkeleton, MobileCardsSkeleton } from "../components/Skeletons"

function getRiskScoreBadge(project) {
  // Use the backend risk data (single source of truth from risk_scores table)
  const risk = project?.risk
  const score = risk?.risk_score ?? null
  const level = risk?.risk_level

  // If no backend risk data available, show as unknown
  if (score === null || score === undefined) {
    return {
      score: null,
      label: "N/A",
      badge: "border-gray-200 bg-gray-50 text-gray-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400",
      icon: "—",
    }
  }

  if (level === "High") {
    return {
      score,
      label: "High",
      badge: "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950/70 dark:text-red-300",
      icon: "⚠",
    }
  }
  if (level === "Medium") {
    return {
      score,
      label: "Medium",
      badge: "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/70 dark:text-amber-300",
      icon: "i",
    }
  }
  return {
    score,
    label: level || "Low",
    badge: "border-green-200 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-950/70 dark:text-green-300",
    icon: "✓",
  }
}

const STALE_THRESHOLD = 1000000 // ₹10 lakh
function getStaleProgressFlag(project) {
  const sanctioned = Number(project.sanctioned_amount || 0)
  const expenditure = Number(project.expenditure || 0)
  const completion = Number(project.completion_percentage || 0)
  if (sanctioned >= STALE_THRESHOLD && completion === 0 && expenditure === 0) {
    return "⚠ Update Required"
  }
  return null
}

const Projects = memo(function Projects({ projectSearchQuery, onClearProjectSearch, drillDownParams, onClearDrillDown, fy, onNavigate }) {
  const [projects, setProjects] = useState([])
  const [totalCount, setTotalCount] = useState(0)
  const [states, setStates] = useState([])
  const [constituencies, setConstituencies] = useState([])

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  const [keyword, setKeyword] = useState("")
  const [state, setState] = useState("")
  const [constituency, setConstituency] = useState("")
  const [house, setHouse] = useState("")
  const [status, setStatus] = useState("")
  const [sortBy, setSortBy] = useState("")
  const [sortDir, setSortDir] = useState("desc")
  const [currentPage, setCurrentPage] = useState(1)
  const [detailProjectId, setDetailProjectId] = useState(null)
  // Assistant tab deep-links: "open the forensic view" → open_project action
  // carrying {tab:'forensics'} — applied when the drawer opens.
  const [detailTab, setDetailTab] = useState(null)
  const [projectSearchApplied, setProjectSearchApplied] = useState(false)
  const rowsPerPage = 15

  // Open another project's drawer from inside an open drawer (e.g. peer comparison)
  // Single selection flow: any open-project request (deep link, assistant,
  // workspace) opens the project-details interface — nothing else.
  useEffect(() => {
    const handler = (e) => {
      const id = e.detail?.projectId
      if (id) {
        setDetailProjectId(Number(id))
        setDetailTab(e.detail?.tab || null)
      }
    }
    window.addEventListener("open-project", handler)
    return () => window.removeEventListener("open-project", handler)
  }, [])

  // Use a ref to track the previous projectSearchQuery to detect changes
  const prevProjectQueryRef = useRef(null)
  useEffect(() => {
    if (projectSearchQuery && projectSearchQuery.trim() && projectSearchQuery !== prevProjectQueryRef.current) {
      prevProjectQueryRef.current = projectSearchQuery
      setKeyword(projectSearchQuery.trim())
      setCurrentPage(1)
      setProjectSearchApplied(true)
      if (onClearProjectSearch) onClearProjectSearch()
    }
  }, [projectSearchQuery, onClearProjectSearch])

  // Handle drill-down params from Overview
  useEffect(() => {
    if (drillDownParams) {
      if (drillDownParams.state) setState(drillDownParams.state)
      if (drillDownParams.constituency) setConstituency(drillDownParams.constituency)
      if (drillDownParams.status) setStatus(drillDownParams.status)
      if (drillDownParams.keyword) setKeyword(drillDownParams.keyword)
      if (drillDownParams.house) setHouse(drillDownParams.house)
      setCurrentPage(1)
      if (onClearDrillDown) onClearDrillDown()
    }
  }, [drillDownParams, onClearDrillDown])

  // Assistant control plane: "clear all filters" resets THIS page's own
  // existing filter state — no parallel filter system.
  useEffect(() => {
    const handler = () => {
      setKeyword("")
      setState("")
      setConstituency("")
      setStatus("")
      setSortBy("")
      setSortDir("desc")
      setCurrentPage(1)
      setProjectSearchApplied(false)
    }
    window.addEventListener("assistant:clear-filters", handler)
    return () => window.removeEventListener("assistant:clear-filters", handler)
  }, [])

  useEffect(() => {
    async function loadStatesList() {
      try {
        const data = await getStates()
        if (Array.isArray(data)) setStates(data)
      } catch (err) {
        console.error("Failed to load states:", err)
      }
    }
    loadStatesList()
  }, [])

  useEffect(() => {
    async function loadConstituenciesList() {
      if (!state) {
        setConstituencies([])
        setConstituency("")
        return
      }
      try {
        const data = await getConstituencies(state)
        if (Array.isArray(data)) setConstituencies(data)
      } catch (err) {
        console.error("Failed to load constituencies:", err)
      }
    }
    loadConstituenciesList()
  }, [state])

  const fetchProjectsData = useCallback(async () => {
    try {
      setLoading(true)
      setError("")

      const skip = (currentPage - 1) * rowsPerPage
      let response

      if (keyword.trim()) {
        response = await searchProjects({
          q: keyword.trim(),
          state: state || undefined,
          constituency: constituency || undefined,
          house: house || undefined,
          fy: fy || undefined,
          status: status || undefined,
          sort_by: sortBy || undefined,
          sort_dir: sortBy ? sortDir : undefined,
          skip,
          limit: rowsPerPage,
        })
        if (response && response.results) {
          setProjects(response.results)
          setTotalCount(response.total || response.results.length)
        }
      } else {
        // /projects returns one page of rows; the same filters go to
        // /search/projects (no keyword) for the true filtered COUNT(*) so
        // "Showing X to Y of Z" always reflects the actual query size.
        const [projList, searchCount] = await Promise.all([
          getProjects({
            state: state || undefined,
            constituency: constituency || undefined,
            house: house || undefined,
            fy: fy || undefined,
            status: status || undefined,
            sort_by: sortBy || undefined,
            sort_dir: sortBy ? sortDir : undefined,
            skip,
            limit: rowsPerPage,
          }),
          searchProjects({
            state: state || undefined,
            constituency: constituency || undefined,
            house: house || undefined,
            fy: fy || undefined,
            status: status || undefined,
            limit: 1,
          }),
        ])
        setProjects(Array.isArray(projList) ? projList : [])
        setTotalCount(searchCount?.total || (Array.isArray(projList) ? projList.length : 0))
      }
    } catch (err) {
      console.error("Projects error:", err)
      setError("Failed to load projects from backend. Check connection.")
    } finally {
      setLoading(false)
    }
  }, [keyword, state, constituency, house, fy, status, currentPage, sortBy, sortDir])

  useEffect(() => {
    fetchProjectsData()
  }, [fetchProjectsData])

  const handleOpenDetail = (proj) => {
    // Project-selection interface: opens the shared AI Inspection &
    // Forensic Summary modal over the still-mounted Projects list, so
    // search/filter/pagination state is intact on close.
    setDetailProjectId(proj.id)
    window.dispatchEvent(new CustomEvent("open-project", { detail: { projectId: proj.id } }))
  }

  const totalPages = Math.max(1, Math.ceil(totalCount / rowsPerPage))

  // A filter change can shrink the result set below the current page — clamp
  // instead of showing an empty page with a stale page number.
  useEffect(() => {
    if (!loading && totalCount > 0 && currentPage > totalPages) {
      setCurrentPage(totalPages)
    }
  }, [loading, totalCount, currentPage, totalPages])

  const handleApplySearch = (e) => {
    e.preventDefault()
    setCurrentPage(1)
    fetchProjectsData()
  }

  const handleResetFilters = () => {
    setKeyword("")
    setState("")
    setConstituency("")
    setStatus("")
    setSortBy("")
    setSortDir("desc")
    setCurrentPage(1)
    setProjectSearchApplied(false)
  }

  return (
    <div className="min-h-full bg-[#f4f5f6] p-4 sm:p-6 text-[#30343A] transition-colors duration-200 dark:bg-[#171b20] dark:text-gray-100">
      <div className="mx-auto max-w-[1440px] space-y-4 sm:space-y-6">

        {/* PROJECT SEARCH CONTEXT BANNER */}
        {projectSearchApplied && keyword && (
          <div className="flex items-center justify-between rounded-xl border border-blue-200 bg-blue-50 p-4 dark:border-blue-800 dark:bg-blue-950/40">
            <div className="flex items-center gap-3">
              <span className="text-lg">🔍</span>
              <div>
                <p className="text-sm font-bold text-blue-900 dark:text-blue-200">
                  Search results for: <span className="font-mono">{keyword}</span>
                </p>
                <p className="text-xs text-blue-700 dark:text-blue-300">
                  Found {totalCount.toLocaleString("en-IN")} matching project{totalCount !== 1 ? "s" : ""}
                </p>
              </div>
            </div>
            <button
              onClick={() => {
                setKeyword("")
                setProjectSearchApplied(false)
                setCurrentPage(1)
              }}
              className="rounded-lg border border-blue-300 bg-white px-3 py-1.5 text-xs font-bold text-blue-700 transition hover:bg-blue-100 dark:border-blue-700 dark:bg-blue-900/60 dark:text-blue-200 dark:hover:bg-blue-800"
            >
              Clear Search
            </button>
          </div>
        )}

        <div>
          <h2 className="text-2xl font-bold text-[#30343A] dark:text-white">
            Projects Explorer
          </h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Live MPLADS projects database with real-time AI risk evaluation.
          </p>
        </div>

        <form onSubmit={handleApplySearch} className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm transition-colors dark:border-gray-700/80 dark:bg-[#222831]">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-gray-700 dark:text-gray-200">
                Search Work / Type
              </label>
              <input
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
                placeholder="Search by ID, name, state, constituency, MP name, or type..."
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-gray-700 dark:text-gray-200">
                State
              </label>
              <select
                value={state}
                onChange={(e) => {
                  setState(e.target.value)
                  setCurrentPage(1)
                }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white"
              >
                <option value="">All States</option>
                {states.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-gray-700 dark:text-gray-200">
                Constituency
              </label>
              <select
                value={constituency}
                disabled={!state}
                onChange={(e) => {
                  setConstituency(e.target.value)
                  setCurrentPage(1)
                }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 disabled:opacity-50 dark:border-gray-600 dark:bg-[#171b20] dark:text-white"
              >
                <option value="">{state ? "All Constituencies" : "Select State First"}</option>
                {constituencies.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-gray-700 dark:text-gray-200">
                House
              </label>
              <select
                value={house}
                onChange={(e) => {
                  setHouse(e.target.value)
                  setCurrentPage(1)
                }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white"
              >
                <option value="">All Houses</option>
                <option value="Lok Sabha">Lok Sabha</option>
                <option value="Rajya Sabha">Rajya Sabha</option>
              </select>
            </div>

            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-gray-700 dark:text-gray-200">
                Execution Status
              </label>
              <select
                value={status}
                onChange={(e) => {
                  setStatus(e.target.value)
                  setCurrentPage(1)
                }}
                className="mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white"
              >
                <option value="">All Statuses</option>
                <option value="Completed">Completed</option>
                <option value="Ongoing">Ongoing</option>
                <option value="Recommended">Recommended</option>
              </select>
            </div>
          </div>

          {/* SORT ROW */}
          <div className="mt-4 flex flex-wrap items-center gap-1.5 sm:gap-3 border-t border-gray-100 pt-3 dark:border-gray-700/60">
            <span className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Sort:</span>
            {[
              { value: "", label: "Default" },
              { value: "id", label: "Project ID" },
              { value: "project_name", label: "Name" },
              { value: "recommended_date", label: "Recommended Date" },
              { value: "sanctioned_amount", label: "Sanctioned" },
              { value: "expenditure", label: "Expenditure" },
              { value: "completion_percentage", label: "Progress" },
              { value: "state", label: "State" },
            ].map((opt) => (
              <button
                key={opt.value}
                type="button"
                title={opt.value === "recommended_date" ? "Newest recommendations first — undated projects at the end" : undefined}
                onClick={() => {
                  if (sortBy === opt.value) {
                    setSortDir((d) => d === "asc" ? "desc" : "asc")
                  } else {
                    setSortBy(opt.value)
                    setSortDir(opt.value === "id" || opt.value === "project_name" || opt.value === "state" ? "asc" : "desc")
                  }
                  setCurrentPage(1)
                }}
                className={`rounded-lg px-3 py-1.5 text-[0.6875rem] font-bold transition ${
                  sortBy === opt.value
                    ? "bg-[#46566a] text-white dark:bg-blue-600"
                    : "border border-gray-200 bg-white text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:bg-[#222831] dark:text-gray-300"
                }`}
              >
                {opt.label}
                {sortBy === opt.value && (
                  <span className="ml-1">{sortDir === "asc" ? "↑" : "↓"}</span>
                )}
              </button>
            ))}
            {sortBy && (
              <button
                type="button"
                onClick={() => { setSortBy(""); setCurrentPage(1) }}
                className="rounded-lg border border-gray-200 px-2 py-1.5 text-[0.625rem] font-bold text-gray-400 hover:text-gray-700 dark:border-gray-600 dark:hover:text-gray-200"
              >
                ✕ Clear
              </button>
            )}
          </div>

          <div className="mt-3 flex items-center justify-between">
            <div className="flex gap-2">
              <button
                type="submit"
                className="rounded-lg bg-[#46566a] px-4 sm:px-5 py-2 text-xs font-bold uppercase tracking-wider text-white shadow-2xs transition hover:bg-[#2c3540] dark:bg-blue-600 dark:hover:bg-blue-700"
              >
                Search
              </button>
              <button
                type="button"
                onClick={handleResetFilters}
                className="rounded-lg border border-gray-300 bg-white px-3 sm:px-4 py-2 text-xs font-bold uppercase tracking-wider text-gray-700 shadow-2xs transition hover:bg-gray-50 dark:border-gray-600 dark:bg-[#171b20] dark:text-gray-200 dark:hover:bg-gray-800"
              >
                Reset
              </button>
            </div>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              Page {currentPage} of {totalPages}
            </span>
          </div>
        </form>

        {error && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-xs font-semibold text-red-700 dark:border-red-800/60 dark:bg-red-950/40 dark:text-red-300">
            ⚠ {error}
          </div>
        )}

        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm transition-colors dark:border-gray-700/80 dark:bg-[#222831]">
          {loading ? (
            <>
              <TableRowsSkeleton rows={8} cols={6} className="hidden lg:block" />
              <MobileCardsSkeleton rows={5} />
            </>
          ) : (
            <>
              <div className="overflow-x-auto">
                {/* DESKTOP TABLE */}
                <div className="hidden lg:block overflow-x-auto">
                <table className="w-full min-w-[900px]">
                  <thead>
                    <tr className="border-b-2 border-gray-200 bg-gray-50 text-left text-[0.625rem] font-bold uppercase tracking-widest text-gray-500 dark:border-gray-700 dark:bg-[#1c2127] dark:text-gray-400">
                      <th className="px-3 py-3 w-20">ID</th>
                      <th className="px-3 py-3">Work Name & Category</th>
                      <th className="px-3 py-3 w-36">Location</th>
                      <th className="px-3 py-3 w-28">Recommended</th>
                      <th className="px-3 py-3 w-28 text-right">Sanctioned</th>
                      <th className="px-3 py-3 w-28 text-right">Expenditure</th>
                      <th className="px-3 py-3 w-24 text-center">Status</th>
                      <th className="px-3 py-3 w-24 text-center">Risk</th>
                      <th className="px-3 py-3 w-14 text-center"></th>
                    </tr>
                  </thead>

                  <tbody
                    key={`${keyword}|${state}|${constituency}|${status}|${sortBy}|${sortDir}|${currentPage}`}
                    className="rise-refresh"
                  >
                    {projects.map((proj) => {
                      const riskBadge = getRiskScoreBadge(proj)
                      return (
                        <tr
                          key={proj.id}
                          onClick={() => handleOpenDetail(proj)}
                          className="cursor-pointer transition-colors duration-100 hover:bg-blue-50/60 dark:hover:bg-[#2c3540]/80"
                        >
                          <td className="px-3 py-2.5 font-mono text-[0.6875rem] font-bold text-gray-500 dark:text-gray-400">
                            #{proj.id}
                          </td>
                          <td className="px-3 py-2.5 max-w-[280px]">
                            <p className="truncate font-semibold text-gray-900 dark:text-white" title={proj.project_name}>
                              {proj.project_name || "Unnamed Project"}
                            </p>
                            <p className="mt-0.5 truncate text-[0.6875rem] text-gray-400 dark:text-gray-500">
                              {proj.project_type || "General"}
                            </p>
                          </td>
                          <td className="px-3 py-2.5 text-xs">
                            <p className="font-medium text-gray-800 dark:text-gray-200 truncate">
                              {proj.constituency || proj.state || "N/A"}
                            </p>
                            <p className="mt-0.5 text-[0.6875rem] text-gray-400 dark:text-gray-500 truncate">{proj.state}</p>
                          </td>
                          <td className="px-3 py-2.5 text-xs whitespace-nowrap">
                            {formatDateHuman(proj.rec?.recommendation_date) ? (
                              <div>
                                <p className="font-mono text-[0.6875rem] font-semibold text-gray-700 dark:text-gray-300">
                                  {formatDateHuman(proj.rec.recommendation_date)}
                                </p>
                                {proj.rec.recommended_by && (
                                  <p className="mt-0.5 max-w-[110px] truncate text-[0.625rem] text-gray-400 dark:text-gray-500" title={`Recommended by ${proj.rec.recommended_by}`}>
                                    {proj.rec.recommended_by}
                                  </p>
                                )}
                              </div>
                            ) : (
                              <span className="text-[0.6875rem] italic text-gray-300 dark:text-gray-600" title="Recommendation date unavailable in the source dataset">—</span>
                            )}
                          </td>
                          <td className="px-3 py-2.5 text-right font-mono text-xs font-semibold tabular-nums text-gray-800 dark:text-gray-200">
                            {formatMoney(proj.sanctioned_amount)}
                          </td>
                          <td className="px-3 py-2.5 text-right font-mono text-xs font-semibold tabular-nums text-gray-800 dark:text-gray-200">
                            {formatMoney(proj.expenditure)}
                          </td>
                          <td className="p-4 text-center">
                            <span className="rounded-md border border-gray-200 bg-gray-100 px-2 py-0.5 text-xs font-medium dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200">
                              {proj.status || "Ongoing"}
                            </span>
                          </td>
                          <td className="p-4 text-center">
                            <span className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[0.625rem] font-bold uppercase tracking-wide ${riskBadge.badge}`}>
                              {riskBadge.icon} {riskBadge.label}
                            </span>
                            {getStaleProgressFlag(proj) && (
                              <span className="ml-1 inline-flex items-center gap-0.5 rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[0.5625rem] font-bold text-amber-600 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300" title="Data Update Notice: Reported progress or expenditure may not reflect the latest project status. A risk score indicates an anomaly based on available data and does not by itself confirm project delay or irregularity.">
                                ⚠
                              </span>
                            )}
                          </td>
                          <td className="p-4 text-center">
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                handleOpenDetail(proj)
                              }}
                              className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-gray-50 px-2.5 py-1 text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 transition-all duration-100 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-600 dark:border-gray-600 dark:bg-gray-700/50 dark:text-gray-400 dark:hover:border-blue-600 dark:hover:bg-blue-900/40 dark:hover:text-blue-400"
                              title="View project details"
                            >
                              View
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
                </div>

                {/* MOBILE CARD VIEW */}
                <div className="lg:hidden border-t border-gray-100 dark:border-gray-700/40">
                  {projects.map((proj) => {
                    const riskBadge = getRiskScoreBadge(proj)
                    return (
                      <div
                        key={proj.id}
                        onClick={() => handleOpenDetail(proj)}
                        className="px-4 py-3.5 cursor-pointer transition-colors duration-100 hover:bg-blue-50/60 dark:hover:bg-[#2c3540]/80"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="font-mono text-[0.625rem] font-bold text-gray-400">#{proj.id}</span>
                              <span className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[0.625rem] font-bold ${riskBadge.badge}`}>
                                {riskBadge.icon} {riskBadge.label}
                              </span>
                              {getStaleProgressFlag(proj) && (
                                <span className="inline-flex items-center gap-0.5 rounded-md border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[0.5625rem] font-bold text-amber-600 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300" title="Data Update Notice: Reported progress or expenditure may not reflect the latest project status.">
                                  ⚠
                                </span>
                              )}
                              <span className="rounded-md border border-gray-200 bg-gray-100 px-1.5 py-0.5 text-[0.625rem] font-medium dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200">
                                {proj.status || "Ongoing"}
                              </span>
                            </div>
                            <p className="mt-1 truncate font-semibold text-sm text-gray-900 dark:text-white" title={proj.project_name}>
                              {proj.project_name || "Unnamed Project"}
                            </p>
                            <p className="mt-0.5 text-[0.6875rem] text-gray-400 dark:text-gray-500">
                              {proj.constituency || ""}{proj.constituency && proj.state ? ", " : ""}{proj.state || "N/A"} • {proj.project_type || "General"}
                            </p>
                          </div>
                        </div>
                        <div className="mt-2.5 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                          <div className="flex items-center justify-between rounded-md bg-gray-50 dark:bg-gray-700/40 px-2.5 py-1.5">
                            <span className="text-gray-400 dark:text-gray-500">Sanctioned</span>
                            <span className="font-mono font-bold text-gray-800 dark:text-gray-200">{formatMoney(proj.sanctioned_amount)}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-gray-500 dark:text-gray-400">Spent</span>
                            <span className="font-mono font-bold text-gray-800 dark:text-gray-200">{formatMoney(proj.expenditure)}</span>
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>

                {projects.length === 0 && (
                  <div className="px-6 py-16 text-center">
                    <div className="text-3xl mb-3">📋</div>
                    <p className="text-sm font-semibold text-gray-500 dark:text-gray-400">No projects matched the search filters.</p>
                    <p className="mt-1 text-xs text-gray-400 dark:text-gray-500">Try adjusting your search criteria or resetting filters.</p>
                  </div>
                )}
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-gray-200 bg-gray-50/80 px-4 py-3 sm:px-5 sm:py-3.5 dark:border-gray-700 dark:bg-[#1c2127]">
                <div className="flex items-center gap-3">
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                  Showing <strong>{totalCount === 0 ? 0 : (currentPage - 1) * rowsPerPage + 1}</strong> to{" "}
                  <strong>{Math.min(currentPage * rowsPerPage, totalCount)}</strong> of <strong>{totalCount.toLocaleString("en-IN")}</strong> records
                  </span>
                </div>

                <div className="flex items-center gap-2">
                  <button
                    disabled={currentPage === 1}
                    onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                    className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-semibold text-gray-700 shadow-2xs transition hover:bg-gray-50 disabled:opacity-40 dark:border-gray-600 dark:bg-[#222831] dark:text-gray-200 dark:hover:bg-gray-700"
                  >
                    ← Previous
                  </button>

                  <span className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 font-mono text-xs font-bold text-gray-700 dark:border-gray-600 dark:bg-[#222831] dark:text-gray-200">
                    {currentPage} / {totalPages}
                  </span>

                  <button
                    disabled={currentPage >= totalPages}
                    onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
                    className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-semibold text-gray-700 shadow-2xs transition hover:bg-gray-50 disabled:opacity-40 dark:border-gray-600 dark:bg-[#222831] dark:text-gray-200 dark:hover:bg-gray-700"
                  >
                    Next →
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Project selection interface — the full ProjectDetail view in a
          centered rounded card. Rendered over the still-mounted list;
          closing restores the exact search/filter/scroll state. */}
      {detailProjectId && (
        <Suspense fallback={null}>
          <ProjectDetail
            projectId={detailProjectId}
            initialTab={detailTab}
            onClose={() => {
              setDetailProjectId(null)
              setDetailTab(null)
            }}
          />
        </Suspense>
      )}
    </div>
  )
})

export default Projects
