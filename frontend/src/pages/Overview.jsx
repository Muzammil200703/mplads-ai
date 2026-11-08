import { lazy, Suspense, useEffect, useState, memo } from "react"
import {
  getDashboardOverview,
  getAINarrativeInsights,
  getDashboardStates,
  getAnomaliesSummary,
  getEarlyWarning,
  getDashboardHouse,
  healthCheck,
  onBackendStatus,
  getBackendStatus,
} from "../services/api"
import { formatCrore, formatNumber } from "../utils/format"
// The choropleth (d3-geo + GeoJSON) is not needed for first paint. Route-level
// splitting already keeps other pages out of the initial bundle, but this
// component was still imported eagerly by the landing page — load it on demand
// so initial JS/network work excludes the map unless the user reaches it.
const GeographicDistribution = lazy(
  () => import("../components/GeographicDistribution")
)

const Overview = memo(function Overview({
  darkMode,
  onDrillDown,
  fy,
  house: houseProp,
  onHouseChange,
}) {
  const [overview, setOverview] = useState(null)
  const [narratives, setNarratives] = useState([])
  const [stateData, setStateData] = useState([])
  const [anomaliesSummary, setAnomaliesSummary] = useState(null)
  const [earlyWarning, setEarlyWarning] = useState(null)
  const [ewExpanded, setEwExpanded] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  // Backend connectivity is owned by the shared transport (services/api.js):
  // every request reports success/failure there, so this page's indicator can
  // never contradict other pages, and a success anywhere instantly clears a
  // stale "unreachable" state without a page refresh.
  const [backendConnected, setBackendConnected] = useState(getBackendStatus())
  useEffect(() => onBackendStatus(setBackendConnected), [])
  const [dataReady, setDataReady] = useState(true)
  const [overviewMissing, setOverviewMissing] = useState(false)

  // Official eSAKSHI-style per-house dashboard (metric definitions mirror the
  // official MPLADS dashboard; backend derives everything from the ledgers).
  // `house` also scopes the page-level portfolio cards, anomalies summary and
  // early-warning panels — one selector, one consistent snapshot everywhere.
  // "All" is the unfiltered portfolio (no house constraint anywhere).
  const [localHouse, setLocalHouse] = useState("All")
  const house = houseProp || localHouse || "All"
  const setHouse = (next) => {
    setLocalHouse(next)
    if (onHouseChange) onHouseChange(next)
  }
  const [houseStats, setHouseStats] = useState(null)
  useEffect(() => {
    let cancelled = false
    // "All" resolves too: the backend combines the two per-house reference
    // snapshots (amounts summed — houses are disjoint — rates recomputed
    // allocation-weighted), so every scope gets the same metric grid.
    getDashboardHouse(house, fy || undefined)
      .then((d) => { if (!cancelled) setHouseStats(d) })
      .catch(() => { if (!cancelled) setHouseStats(null) })
    return () => { cancelled = true }
  }, [house, fy])

  useEffect(() => {
    async function loadData() {
      try {
        setLoading(true)
        setError("")
        setOverviewMissing(false)

        const fyParams = fy ? { fy } : {}
        const houseParam = house !== "All" ? { house } : {}
        const [healthRes, ovRes, narrRes, statesRes, anomRes, ewRes] = await Promise.allSettled([
          healthCheck(),
          getDashboardOverview({ ...fyParams, ...houseParam }),
          getAINarrativeInsights({ ...fyParams, ...houseParam }),
          getDashboardStates({ ...fyParams, ...houseParam }),
          getAnomaliesSummary({ ...fyParams, ...houseParam }),
          getEarlyWarning({ ...fyParams, ...houseParam }),
        ])

        const projectCount = healthRes.status === "fulfilled"
          ? Number(healthRes.value?.total_projects || 0)
          : 0
        const hasData = healthRes.status === "fulfilled" && healthRes.value?.data_ready !== false && projectCount > 0
        const datasetMissing = healthRes.status === "fulfilled" && !hasData

        if (hasData) {
          // A successful health check is authoritative proof of reachability —
          // clear any stale unreachable banner immediately. Ongoing
          // connectivity continues to be tracked by the shared transport
          // (onBackendStatus above).
          setBackendConnected(true)
          setDataReady(true)
        } else if (datasetMissing) {
          setDataReady(false)
          setOverview(null)
          setError(
            "Audit data is not loaded in this deployment. Results are intentionally withheld so an empty database is never mistaken for a clean audit outcome."
          )
        } else if (healthRes.status === "fulfilled") {
          // Health answered — the backend is reachable and its dataset is
          // loaded. Connectivity itself is tracked by the shared transport
          // (onBackendStatus above), so no local flag is set here.
          setDataReady(true)
        }

        // The overview payload is consumed independently of /health so a
        // transient failure of this single request cannot silently zero the
        // portfolio cards while sibling sections (states, anomalies, early
        // warning) keep rendering. An empty payload is treated exactly like a
        // rejected request and is surfaced by the summary banner below.
        const ovPayload = ovRes.status === "fulfilled" ? ovRes.value : null
        if (ovPayload && ovPayload.total_projects != null) {
          setOverview(ovPayload)
          setOverviewMissing(false)
        } else if (!datasetMissing && healthRes.status === "fulfilled") {
          // Rejected request, null body, or a payload missing the one field
          // every card consumes — surface it, never render it as zero.
          // The previous payload is cleared too: keeping FY-A values visible
          // while the user asked for FY-B would present stale figures as if
          // they were current. Zero + banner is the honest state.
          setOverview(null)
          setOverviewMissing(true)
        }

        if (narrRes.status === "fulfilled" && narrRes.value?.insights) {
          setNarratives(narrRes.value.insights)
        }

        if (statesRes.status === "fulfilled" && Array.isArray(statesRes.value)) {
          setStateData(statesRes.value)
        }

        if (anomRes.status === "fulfilled") {
          setAnomaliesSummary(anomRes.value)
        }

        if (ewRes.status === "fulfilled") {
          setEarlyWarning(ewRes.value)
        }

        // Connectivity is NOT decided here anymore. The old logic flipped a
        // page-local "unreachable" flag whenever health (or overview+states)
        // failed — even while sibling requests succeeded — and never cleared
        // it after the backend woke up, leaving a stale banner over live data.
        // Now: successful requests mark the backend reachable in the shared
        // transport; exhausted failures mark it degraded only if no newer
        // success exists (race-guarded). The red banner below renders purely
        // from that shared state, so a wake-up clears it automatically.
      } catch (err) {
        console.error("Overview error:", err)
        setError("Error loading dashboard metrics.")
      } finally {
        setLoading(false)
      }
    }

    loadData()
  }, [fy, house])

  // Assistant control plane: "refresh data" replays the page's own load
  // cycle — no parallel fetch path.
  useEffect(() => {
    const handler = () => {
      async function reload() {
        try {
          setLoading(true)
          const fyParams = fy ? { fy } : {}
          const houseParam = house !== "All" ? { house } : {}
          const ovRes = await getDashboardOverview({ ...fyParams, ...houseParam })
          if (ovRes && ovRes.total_projects != null) {
            setOverview(ovRes)
            setOverviewMissing(false)
          }
          const narrRes = await getAINarrativeInsights({ ...fyParams, ...houseParam }).catch(() => null)
          if (narrRes?.insights) setNarratives(narrRes.insights)
          const statesRes = await getDashboardStates({ ...fyParams, ...houseParam })
          if (Array.isArray(statesRes)) setStateData(statesRes)
          const anomRes = await getAnomaliesSummary({ ...fyParams, ...houseParam })
          if (anomRes) setAnomaliesSummary(anomRes)
          const ewRes = await getEarlyWarning({ ...fyParams, ...houseParam })
          if (ewRes) setEarlyWarning(ewRes)
        } catch {
          // Failures surface through the normal error banners on next load.
        } finally {
          setLoading(false)
        }
      }
      reload()
    }
    window.addEventListener("assistant:refresh-data", handler)
    return () => window.removeEventListener("assistant:refresh-data", handler)
  }, [fy, house])

  const pageClasses = darkMode
    ? "bg-[#000000] text-[#f5eff1]"
    : "bg-[#faf7f6] text-[#292326]"

  const cardClasses = darkMode
    ? "bg-[#0d090b] border-[#351923]"
    : "bg-white border-[#e8dadc]"

  const mutedText = darkMode ? "text-[#b9aeb2]" : "text-[#71666a]"

  const topStates = stateData.slice(0, 6)
  const maxProjectsInState = topStates.length > 0
    ? Math.max(...topStates.map((s) => s.total_projects))
    : 1

  const totalWorks = overview?.total_projects || 0
  const sanctionedAmount = overview?.total_sanctioned_amount || overview?.total_allocated_amount || 0
  const expenditure = overview?.total_expenditure || 0
  const highRiskWorks = anomaliesSummary?.high_risk || 0
  const completedWorks = overview?.completed_projects || 0
  const utilization = overview?.utilization_percentage || 0

  if (loading) {
    return (
      <div className={`min-h-full p-4 sm:p-6 ${pageClasses}`}>
        <h1 className="text-xl sm:text-2xl md:text-3xl font-bold">Executive Overview</h1>
        <div className={`mt-6 rounded-xl border p-12 text-center ${cardClasses}`}>
          <div className="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent" />
          <p className={`mt-3 font-medium ${mutedText}`}>Loading live MPLADS intelligence from backend...</p>
        </div>
      </div>
    )
  }

  if (!dataReady) {
    return (
      <div className={`min-h-full p-4 sm:p-6 ${pageClasses}`}>
        <h1 className="text-xl sm:text-2xl md:text-3xl font-bold">Executive Overview</h1>
        <section className={`mt-6 max-w-3xl rounded-xl border p-6 sm:p-8 ${cardClasses}`}>
          <p className="text-2xl" aria-hidden>⚠️</p>
          <h2 className="mt-3 text-lg font-bold">Audit dataset unavailable</h2>
          <p className={`mt-2 text-sm leading-6 ${mutedText}`}>
            No project-level audit results are displayed because this deployment has not loaded its MPLADS dataset.
            This safeguard prevents an empty database from being presented as “zero risk.”
          </p>
          <p className={`mt-3 text-xs leading-5 ${mutedText}`}>
            For deployment: ensure the Git-LFS dataset is fetched before the API starts, then reload this page.
          </p>
        </section>
      </div>
    )
  }

  return (
    <div className={`min-h-full p-4 sm:p-6 transition-colors duration-200 ${pageClasses}`}>
      {/* HEADER */}
      <div className="mb-4 sm:mb-6">
        <h1 className="text-xl sm:text-2xl md:text-3xl font-bold">Executive Overview</h1>
        <p className={`mt-1 sm:mt-2 text-sm sm:text-base ${mutedText}`}>
          AI-driven monitoring of MPLADS allocations, physical progress, and financial anomalies.
        </p>
      </div>

      {/* HOUSE DASHBOARD — official eSAKSHI metric set (allocated,
          expenditure, utilization, expenditure rate, MPs, completed/pending
          works, ongoing-work payments). Backend-derived from the ledgers;
          the metric definitions match the official MPLADS dashboard. */}
      <div className={`mb-7 rounded-xl border p-5 shadow-sm ${cardClasses}`}>
        <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="flex items-center gap-2 text-sm font-bold uppercase tracking-wider">
              🏛 House Dashboard
            </h3>
            <p className={`mt-0.5 text-xs ${mutedText}`}>
              Official eSAKSHI metric set, computed from the imported dataset snapshot.
            </p>
          </div>
          <div className="flex max-w-full items-center gap-1 self-start rounded-lg border border-gray-200 p-1 dark:border-gray-700">
            {["All", "Rajya Sabha", "Lok Sabha"].map((h) => (
              <button
                key={h}
                onClick={() => setHouse(h)}
                className={`ctrl-nowrap min-w-0 rounded-md px-3 py-1.5 text-xs font-bold transition ${
                  house === h
                    ? "bg-blue-600 text-white shadow-sm"
                    : "text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
                }`}
              >
                {h}
              </button>
            ))}
          </div>
        </div>
        {houseStats ? (
          <>
            {/* Responsive metric grid: single column on very narrow phones so
                values keep usable width, two columns from 400px, four ≥md. */}
            <div className="grid grid-cols-1 min-[400px]:grid-cols-2 gap-3 md:grid-cols-4">
              {[
                { label: "Total Allocated", num: `₹${formatCrore(houseStats.total_allocated)}`, unit: "Cr", sub: "Total funds allocated to MPs" },
                { label: "Total Expenditure", num: `₹${formatCrore(houseStats.total_expenditure)}`, unit: "Cr", sub: houseStats.expenditure_transactions != null ? `Vendor payments · ${formatNumber(houseStats.expenditure_transactions)} transactions` : "Vendor payments recorded" },
                { label: "Fund Utilization", num: `${houseStats.fund_utilization_percentage}`, unit: "%", sub: "Share of allocation recommended by MPs" },
                { label: "Expenditure Rate", num: `${houseStats.expenditure_rate_percentage}`, unit: "%", sub: "Vendor expenditure as a share of allocation" },
                { label: "Total MPs", num: formatNumber(houseStats.total_mps), unit: "", sub: houseStats.data_snapshot?.reference ? "Official eSAKSHI roster" : " MPs in the system" },
                { label: "Works Completed", num: formatNumber(houseStats.works_completed), unit: "", sub: houseStats.completed_work_value != null ? `Completed works · ₹${formatCrore(houseStats.completed_work_value)} Cr value` : "Completed works and their value" },
                { label: "Works Pending", num: formatNumber(houseStats.works_pending), unit: "", sub: "Recommended works not yet in the completions ledger" },
                { label: "Ongoing-Work Payments", num: `₹${formatCrore(houseStats.ongoing_work_payments)}`, unit: "Cr", sub: "Vendor payments linked to works not yet marked complete" },
                { label: "Total Project Amount", num: houseStats.total_project_amount != null ? `₹${formatCrore(houseStats.total_project_amount)}` : "—", unit: houseStats.total_project_amount != null ? "Cr" : "", sub: "Sanctioned amounts across monitored works" },
              ].map((m) => (
                <div key={m.label} className="flex min-w-0 flex-col rounded-lg border border-gray-100 bg-white/60 p-3 dark:border-gray-700/60 dark:bg-white/5">
                  <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">{m.label}</p>
                  {/* Value line: currency/percentage figures stay on one line
                      (monospaced, tabular numerals); the scale unit renders as
                      a small suffix instead of a wrapped fragment. */}
                  <p className="value-nowrap mt-1 font-mono text-sm font-bold text-gray-900 xl:text-base dark:text-white">
                    {m.num}
                    {m.unit && (
                      <span className="ml-1 font-sans text-[0.625rem] font-semibold text-gray-400">
                        {m.unit}
                      </span>
                    )}
                  </p>
                  <p className="mt-0.5 text-[0.625rem] leading-snug text-gray-400">{m.sub}</p>
                </div>
              ))}
            </div>
            {houseStats.data_snapshot && (
              <p className={`mt-3 text-[0.625rem] leading-snug ${mutedText}`}>
                {houseStats.data_snapshot.reference
                  ? <>
                      Reference: {houseStats.data_snapshot.label}. Metric definitions match the official eSAKSHI dashboard; underlying data is a batch import, not a live feed.
                    </>
                  : <>
                      Data snapshot: batch import through{" "}
                      {houseStats.data_snapshot.last_payment_date
                        ? new Date(houseStats.data_snapshot.last_payment_date).toLocaleDateString("en-IN", { month: "long", year: "numeric" })
                        : "—"}
                      . Batch snapshot, not a live feed — the official eSAKSHI portal updates in real time, so figures drift as new payments are recorded there.
                    </>
                }
              </p>
            )}
          </>
        ) : (
          <p className={`text-xs ${mutedText}`}>House metrics could not be loaded.</p>
        )}
      </div>

      {/* ERROR NOTICE — two distinct failure classes, never conflated:
          1) GLOBAL BACKEND UNREACHABLE — the shared transport confirms every
             request is failing (no success since the failure). Red banner.
             A later success anywhere clears it without a page refresh.
          2) REQUEST-LEVEL failures (one endpoint rejected while others
             succeeded) — amber partial notice, page stays usable. */}
      {backendConnected === false && (
        <div className="mb-6 rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-sm font-semibold text-red-600 dark:text-red-400">
          ⚠ Backend is unreachable right now. On the free hosting tier the server sleeps when idle and takes about a
          minute to wake — please retry in a moment. If this keeps happening, the backend URL may be down or
          misconfigured.
        </div>
      )}
      {backendConnected !== false && error && (
        <div className="mb-6 rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-sm font-semibold text-red-600 dark:text-red-400">
          ⚠ {error}
        </div>
      )}

      {/* PARTIAL-LOAD NOTICE — overview request failed but the backend is up.
          The cards must never quietly present zero as the portfolio truth. */}
      {overviewMissing && (
        <div className="mb-6 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-amber-800 dark:text-amber-300">
          <p className="text-sm font-semibold">Portfolio summary could not be loaded.</p>
          <p className={`mt-1 text-xs leading-relaxed ${mutedText}`}>
            The backend is reachable, but the portfolio aggregation did not complete, so the summary cards below are
            showing zero rather than a misleading partial figure. Other sections on this page are served by separate
            requests and may still show live data. Retry in a moment and the cards will repopulate from the backend.
          </p>
        </div>
      )}

      {/* EARLY WARNING SUMMARY — derived from the risk engine + recorded fields */}
      {earlyWarning && (
        <div className={`mb-7 rounded-xl border p-5 shadow-sm ${cardClasses}`}>
          <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h3 className="flex items-center gap-2 text-sm font-bold uppercase tracking-wider">
                <span>🚨</span> Early Warning Summary
              </h3>
              <p className={`mt-0.5 text-xs ${mutedText}`}>
                Portfolio health bands derived from the existing AI risk engine and recorded progress data.
              </p>
            </div>
            <button
              onClick={() => setEwExpanded((v) => !v)}
              className="self-start whitespace-nowrap rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-semibold transition hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
            >
              {ewExpanded ? "Hide details ↑" : "Show details ↓"}
            </button>
          </div>
          {/* Balanced responsive band grid: 1 column on very narrow phones,
              2 columns from 400px, 4 columns ≥md; every card follows the same
              internal layout (label row → count → description) and stretches
              to an identical height within its row. */}
          <div className="grid grid-cols-1 min-[400px]:grid-cols-2 gap-3 md:grid-cols-4">
            {[
              { key: "normal", label: "Normal", icon: "🟢", count: earlyWarning.bands?.normal, color: "text-green-600 dark:text-green-400", ring: "border-green-200 dark:border-green-900/60", desc: "No active risk indicators" },
              { key: "watch", label: "Watch", icon: "🟡", count: earlyWarning.bands?.watch, color: "text-amber-600 dark:text-amber-400", ring: "border-amber-200 dark:border-amber-900/60", desc: "Leading indicators — spend with no progress, stalled starts" },
              { key: "early", label: "Early Warning", icon: "🟠", count: earlyWarning.bands?.early_warning, color: "text-orange-600 dark:text-orange-400", ring: "border-orange-200 dark:border-orange-900/60", desc: "Medium risk score from the AI engine" },
              { key: "critical", label: "Critical", icon: "🔴", count: earlyWarning.bands?.critical, color: "text-red-600 dark:text-red-400", ring: "border-red-200 dark:border-red-900/60", desc: "High risk score — review soon" },
            ].map((b) => (
              <div key={b.key} className={`flex h-full min-w-0 flex-col rounded-xl border-2 ${b.ring} p-3.5 transition-all duration-200 hover:shadow-md`}>
                {/* Header: dot + label on one aligned row; the label wraps at
                    word boundaries only, never splitting the dot alignment. */}
                <p className="flex min-w-0 items-center gap-1.5 text-xs font-bold uppercase tracking-wide">
                  <span className="shrink-0 text-[0.625rem] leading-none">{b.icon}</span>
                  <span className="min-w-0">{b.label}</span>
                </p>
                <p className={`value-nowrap mt-2 font-mono text-xl font-bold tabular-nums md:text-2xl ${b.color}`}>
                  {(b.count || 0).toLocaleString("en-IN")}
                </p>
                <p className={`mt-1 text-[0.625rem] leading-snug ${mutedText}`}>{b.desc}</p>
              </div>
            ))}
          </div>
          {ewExpanded && (
            <div className="mt-4 space-y-4 border-t border-gray-100 pt-4 dark:border-gray-700/60">
              {/* FY trends — sanctioned vs expenditure + completed + avg risk */}
              {earlyWarning.fy_trends?.length > 0 && (
                <div>
                  <p className="mb-2 text-[0.6875rem] font-bold uppercase tracking-wider text-gray-400">Financial Year Trends (recorded data)</p>
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[540px] text-left text-xs">
                      <thead>
                        <tr className={`border-b ${"border-gray-200 dark:border-gray-700"}`}>
                          <th className="py-1.5 pr-3 font-bold uppercase tracking-wide text-gray-400">FY</th>
                          <th className="py-1.5 pr-3 text-right font-bold uppercase tracking-wide text-gray-400">Projects</th>
                          <th className="py-1.5 pr-3 text-right font-bold uppercase tracking-wide text-gray-400">Sanctioned</th>
                          <th className="py-1.5 pr-3 text-right font-bold uppercase tracking-wide text-gray-400">Expenditure</th>
                          <th className="py-1.5 pr-3 text-right font-bold uppercase tracking-wide text-gray-400">Utilization</th>
                          <th className="py-1.5 pr-3 text-right font-bold uppercase tracking-wide text-gray-400">Completed</th>
                          <th className="py-1.5 text-right font-bold uppercase tracking-wide text-gray-400">Avg Risk</th>
                        </tr>
                      </thead>
                      <tbody className="font-mono">
                        {earlyWarning.fy_trends.map((t) => (
                          <tr key={t.fy} className="border-b border-gray-50 last:border-0 dark:border-gray-800">
                            <td className="py-1.5 pr-3 font-bold">{t.fy}</td>
                            <td className="py-1.5 pr-3 text-right">{t.projects.toLocaleString("en-IN")}</td>
                            <td className="py-1.5 pr-3 text-right">₹{formatCrore(t.sanctioned)} Cr</td>
                            <td className="py-1.5 pr-3 text-right">₹{formatCrore(t.expenditure)} Cr</td>
                            <td className={`py-1.5 pr-3 text-right font-bold ${t.utilization_percentage != null && t.utilization_percentage > 90 ? "text-amber-600 dark:text-amber-400" : "text-blue-600 dark:text-blue-400"}`}>
                              {t.utilization_percentage != null ? `${Number(t.utilization_percentage).toFixed(1)}%` : "—"}
                            </td>
                            <td className="py-1.5 pr-3 text-right">{t.completed.toLocaleString("en-IN")}</td>
                            <td className={`py-1.5 text-right font-bold ${t.avg_risk >= 60 ? "text-red-500" : t.avg_risk >= 30 ? "text-amber-500" : "text-green-600"}`}>
                              {t.avg_risk > 0 ? t.avg_risk : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
              {/* Monthly expenditure sparkline-bars (compact) */}
              {earlyWarning.monthly_expenditure?.length > 0 && (
                <div>
                  <p className="mb-2 text-[0.6875rem] font-bold uppercase tracking-wider text-gray-400">Expenditure trend (monthly, from the payment ledger)</p>
                  <div className="flex h-16 items-end gap-1">
                    {(() => {
                      const months = earlyWarning.monthly_expenditure.slice(-18)
                      const max = Math.max(...months.map((m) => m.amount), 1)
                      return months.map((m) => (
                        <div key={m.month} className="group relative flex-1">
                          <div
                            className="w-full rounded-t bg-blue-500/70 transition-all duration-300 hover:bg-blue-600 dark:bg-blue-400/70"
                            style={{ height: `${Math.max(3, (m.amount / max) * 60)}px` }}
                            title={`${m.month}: ₹${formatCrore(m.amount)} Cr (${m.transactions.toLocaleString("en-IN")} tx)`}
                          />
                        </div>
                      ))
                    })()}
                  </div>
                  <p className={`mt-1 text-[0.5625rem] ${mutedText}`}>Last {Math.min(18, earlyWarning.monthly_expenditure.length)} months with recorded expenditure · hover for values</p>
                </div>
              )}
              {/* Hot states */}
              {earlyWarning.hot_states?.length > 0 && (
                <div>
                  <p className="mb-2 text-[0.6875rem] font-bold uppercase tracking-wider text-gray-400">States with most flagged projects</p>
                  <div className="flex flex-wrap gap-1.5">
                    {earlyWarning.hot_states.filter((s) => s.flagged > 0).slice(0, 8).map((s) => (
                      <button
                        key={s.state}
                        onClick={() => onDrillDown && onDrillDown("Risk Center", { state: s.state })}
                        className="rounded-full border border-gray-200 px-2.5 py-1 text-[0.6875rem] font-semibold transition hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
                      >
                        {s.state} <span className="ml-1 font-mono font-bold text-red-500">{s.flagged}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* AI NARRATIVE INSIGHTS BANNER */}
      {narratives.length > 0 && (
        <div className="mb-7 rounded-xl border border-blue-200 bg-gradient-to-r from-blue-50 via-white to-blue-50/50 p-5 shadow-sm dark:border-blue-900/60 dark:from-[#210a12] dark:via-[#210a12] dark:to-[#210a12]">
          <div className="mb-3 flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-blue-600 text-xs text-white">✨</span>
            <h3 className="text-xs font-bold uppercase tracking-wider text-blue-950 dark:text-blue-200">
              AI Audit Insights & Discrepancy Findings
            </h3>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
            {narratives.map((item, idx) => (
              <div
                key={idx}
                className="rounded-lg border border-blue-100 bg-white/80 p-3 shadow-2xs dark:border-gray-700/60 dark:bg-[#000000]/70"
              >
                <div className="flex items-center gap-2">
                  <span className="text-sm">
                    {item.type === "risk" ? "⚠️" : item.type === "financial" ? "💰" : item.type === "completion" ? "🏗️" : "📍"}
                  </span>
                  <span className="text-xs font-bold text-gray-900 dark:text-white">
                    {item.title}
                  </span>
                </div>
                <p className="mt-1.5 text-xs leading-relaxed text-gray-600 dark:text-gray-300">
                  {item.message}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* CORE STAT CARDS */}
      <div className="grid grid-cols-1 gap-4 sm:gap-5 md:grid-cols-2 xl:grid-cols-4">
        {/* TOTAL WORKS */}
        <div
          className={`rounded-xl border p-3 sm:p-4 shadow-sm cursor-pointer transition-all duration-200 hover:shadow-md hover:-translate-y-0.5 ${cardClasses}`}
          onClick={() => onDrillDown && onDrillDown("Projects")}
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p className={`text-[0.6875rem] font-bold uppercase tracking-wider ${mutedText}`}>
                Total Monitored Works
              </p>
              <h2 className="mt-2.5 text-2xl sm:text-3xl font-bold font-mono tracking-tight">
                {totalWorks.toLocaleString("en-IN")}
              </h2>
            </div>
            <div className="flex-shrink-0 flex items-center justify-center h-11 w-11 rounded-xl bg-blue-500/10 text-xl">
              🏗️
            </div>
          </div>
          <div className="mt-4 pt-3 border-t border-gray-100 dark:border-gray-700/50">
            <p className={`text-xs ${mutedText}`}>
              <span className="font-bold text-green-600 dark:text-green-400">✓ {(overview?.completed_projects || 0).toLocaleString("en-IN")}</span>{" "}
              Completed works
            </p>
          </div>
        </div>

        {/* SANCTIONED AMOUNT */}
        <div className={`rounded-xl border p-3 sm:p-4 shadow-sm transition-all duration-200 hover:shadow-md hover:-translate-y-0.5 ${cardClasses}`}>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p className={`text-[0.6875rem] font-bold uppercase tracking-wider ${mutedText}`}>
                Total Project Amount
              </p>
              <h2 className="mt-2.5 text-2xl sm:text-3xl font-bold font-mono tracking-tight">
                ₹{formatCrore(sanctionedAmount)} <span className="text-sm sm:text-base font-sans text-gray-400 font-normal">Cr</span>
              </h2>
            </div>
            <div className="flex-shrink-0 flex items-center justify-center h-11 w-11 rounded-xl bg-amber-500/10 text-xl">
              💰
            </div>
          </div>
          <div className="mt-4 pt-3 border-t border-gray-100 dark:border-gray-700/50">
            <p className={`text-xs ${mutedText}`}>
              Across <span className="font-bold text-gray-800 dark:text-gray-200">{overview?.total_states || stateData.length}</span> States & UTs
            </p>
          </div>
        </div>

        {/* TOTAL EXPENDITURE */}
        <div className={`rounded-xl border p-3 sm:p-4 shadow-sm transition-all duration-200 hover:shadow-md hover:-translate-y-0.5 ${cardClasses}`}>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p className={`text-[0.6875rem] font-bold uppercase tracking-wider ${mutedText}`}>
                Cumulative Expenditure
              </p>
              <h2 className="mt-2.5 text-2xl sm:text-3xl font-bold font-mono tracking-tight">
                ₹{formatCrore(expenditure)} <span className="text-sm sm:text-base font-sans text-gray-400 font-normal">Cr</span>
              </h2>
            </div>
            <div className="flex-shrink-0 flex items-center justify-center h-11 w-11 rounded-xl bg-purple-500/10 text-xl">
              💳
            </div>
          </div>
          <div className="mt-4 pt-3 border-t border-gray-100 dark:border-gray-700/50">
            <p className={`text-xs ${mutedText}`}>
              <span className="font-bold text-blue-600 dark:text-blue-400">{Number(utilization).toFixed(1)}%</span> Fund utilization
            </p>
          </div>
        </div>

        {/* HIGH RISK WORKS */}
        <div
          className={`rounded-xl border-2 border-l-4 border-l-red-500 border-t-red-200 border-r-red-200 border-b-red-200 dark:border-l-red-500 dark:border-t-red-900/50 dark:border-r-red-900/50 dark:border-b-red-900/50 p-3 sm:p-4 shadow-sm cursor-pointer transition-all duration-200 hover:shadow-soft-lg hover:-translate-y-0.5 ${cardClasses}`}
          onClick={() => onDrillDown && onDrillDown("Risk Center", { risk_level: "High" })}
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p className="text-[0.6875rem] font-bold uppercase tracking-wider text-red-400 dark:text-red-400">
                High-Risk Anomalies
              </p>
              <h2 className="mt-2.5 text-2xl sm:text-3xl font-bold font-mono text-red-500 dark:text-red-400 tracking-tight">
                {highRiskWorks.toLocaleString("en-IN")}
              </h2>
            </div>
            <div className="flex-shrink-0 flex items-center justify-center h-11 w-11 rounded-xl bg-red-500/10 text-xl">
              ⚠️
            </div>
          </div>
          <div className="mt-4 pt-3 border-t border-red-100 dark:border-red-900/50">
            <p className="text-xs text-red-400 dark:text-red-400">
              Flagged by <span className="font-bold">AI Anomaly Model</span>
            </p>
          </div>
        </div>
      </div>

      {/* GEOGRAPHIC DISTRIBUTION — state-level choropleth. Placed directly
          below the KPI cards and above the existing regional charts. It
          reuses the House Dashboard's existing house selection (no duplicate
          House control) and its own aggregation request fires only when the
          House or FY scope changes. */}
      <Suspense
        fallback={
          <div
            className={`mt-6 h-64 animate-pulse rounded-xl border ${cardClasses}`}
            aria-label="Loading geographic distribution"
          />
        }
      >
        <GeographicDistribution
          darkMode={darkMode}
          house={house}
          fy={fy}
          onDrillDown={onDrillDown}
        />
      </Suspense>

      {/* STATE INTENSITY & REGIONAL ANALYSIS */}
      <div className="mt-6 grid grid-cols-1 gap-4 sm:gap-5 xl:grid-cols-12">
        {/* REGIONAL BREAKDOWN */}
        <div className={`rounded-xl border p-5 sm:p-6 shadow-sm xl:col-span-8 ${cardClasses}`}>
          <div className="mb-5 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 border-b border-gray-100 pb-3 dark:border-gray-700/60">
            <div>
              <h2 className="text-base sm:text-lg font-bold">Top States by Work Allocation</h2>
              <p className={`mt-0.5 text-xs ${mutedText}`}>
                Live project distribution, fund allocation, and physical completion rates.
              </p>
            </div>
            <span className="flex-shrink-0 rounded-lg bg-gray-100 px-2.5 py-1 font-mono text-[0.6875rem] font-bold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
              {stateData.length} States · {house}
            </span>
          </div>

          <div className="space-y-3">
            {topStates.length === 0 && (
              <div className="rounded-lg border border-dashed border-gray-200 bg-gray-50/70 p-4 dark:border-gray-700 dark:bg-gray-800/40">
                <p className="text-xs font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  Portfolio snapshot
                </p>
                <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
                  <div className="rounded-md bg-white px-3 py-2 dark:bg-gray-800">
                    <p className="text-[0.625rem] uppercase tracking-wide text-gray-400">Monitored works</p>
                    <p className="mt-1 text-sm font-bold text-gray-800 dark:text-gray-100">{totalWorks.toLocaleString("en-IN")}</p>
                  </div>
                  <div className="rounded-md bg-white px-3 py-2 dark:bg-gray-800">
                    <p className="text-[0.625rem] uppercase tracking-wide text-gray-400">States & UTs</p>
                    <p className="mt-1 text-sm font-bold text-gray-800 dark:text-gray-100">{(overview?.total_states || 0).toLocaleString("en-IN")}</p>
                  </div>
                  <div className="rounded-md bg-white px-3 py-2 dark:bg-gray-800">
                    <p className="text-[0.625rem] uppercase tracking-wide text-gray-400">Data connection</p>
                    <p className={`mt-1 text-sm font-bold ${backendConnected ? "text-green-600 dark:text-green-400" : "text-red-500 dark:text-red-400"}`}>
                      {backendConnected ? "Connected" : "Unavailable"}
                    </p>
                  </div>
                </div>
                <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
                  Regional allocation details will appear here when state-level data is available.
                </p>
              </div>
            )}
            {topStates.map((st, idx) => {
              const pct = (st.total_projects / maxProjectsInState) * 100
              return (
                <div
                  key={st.state}
                  className="rounded-lg px-2 py-2.5 transition-all duration-150 hover:bg-gray-50 dark:hover:bg-gray-800/50 cursor-pointer"
                  onClick={() => onDrillDown && onDrillDown("Projects", { state: st.state })}
                >
                  <div className="mb-2 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-1 text-xs">
                    <div className="flex items-center gap-2">
                      <span className="flex-shrink-0 inline-flex items-center justify-center h-5 w-5 rounded-full bg-gray-100 dark:bg-gray-700 text-[0.625rem] font-bold text-gray-500 dark:text-gray-400">
                        {idx + 1}
                      </span>
                      <span className="font-bold text-gray-900 dark:text-white">
                        {st.state}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 sm:gap-3 font-mono text-[0.6875rem] flex-wrap pl-7 sm:pl-0">
                      <span className="text-gray-600 dark:text-gray-400">₹{formatCrore(st.total_sanctioned_amount)} Cr</span>
                      <span className="font-bold text-blue-600 dark:text-blue-400">
                        {st.total_projects.toLocaleString("en-IN")} works
                      </span>
                      <span className="text-green-600 dark:text-green-400">
                        {st.utilization_percentage}%
                      </span>
                    </div>
                  </div>
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700/60">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-blue-500 to-blue-600 transition-all duration-500 dark:from-blue-400 dark:to-blue-500"
                      style={{ width: `${Math.max(4, pct)}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* QUICK METRICS & SYSTEM STATUS */}
        <div className="flex flex-col gap-4 sm:gap-5 xl:col-span-4">
          <div className={`rounded-xl border p-5 sm:p-6 shadow-sm ${cardClasses}`}>
            <h3 className="mb-4 text-xs font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Execution Health</h3>
            <div className="space-y-0">
              {[
                { label: "Completed Works", value: completedWorks.toLocaleString("en-IN"), color: "text-green-600 dark:text-green-400" },
                { label: "Ongoing Works", value: (overview?.ongoing_projects || 0).toLocaleString("en-IN"), color: "text-blue-600 dark:text-blue-400" },
                { label: "Avg Physical Completion", value: `${overview?.average_completion_percentage || 0}%`, color: "text-purple-600 dark:text-purple-400" },
                { label: "Total MPs Registered", value: (overview?.total_mps || 0).toLocaleString("en-IN"), color: "text-gray-900 dark:text-white" },
              ].map((item, i) => (
                <div key={i} className="flex items-center justify-between py-2.5 border-b border-gray-50 dark:border-gray-700/40 last:border-0 last:pb-0">
                  <span className="text-gray-500 dark:text-gray-400 text-[0.6875rem] uppercase tracking-wide font-sans">{item.label}</span>
                  <span className={`font-mono font-bold text-xs ${item.color}`}>{item.value}</span>
                </div>
              ))}
            </div>
          </div>

          {/* BACKEND STATUS BADGE */}
          <div className={`rounded-xl border p-4 sm:p-5 shadow-sm ${cardClasses}`}>
            <div className="flex items-center gap-3">
              <span className={`flex-shrink-0 h-3 w-3 rounded-full ${backendConnected ? "bg-green-500 shadow-sm shadow-green-500/50 animate-pulse" : "bg-red-500"}`} />
              <div className="min-w-0 flex-1">
                <h4 className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400 dark:text-gray-500">
                  Backend Link
                </h4>
                <p className="mt-0.5 text-xs font-semibold truncate">
                  {backendConnected ? "Connected & Synchronized" : "Disconnected"}
                </p>
              </div>
            </div>
          </div>

          {/* DATA SOURCES & PROVENANCE */}
          <div className={`rounded-xl border p-4 sm:p-5 shadow-sm ${cardClasses}`}>
            <div className="mb-3 flex items-center gap-2">
              <span className="text-sm">🏛</span>
              <h4 className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                Data Sources & Provenance
              </h4>
              <span className="ml-auto rounded-full border border-green-200 bg-green-50 px-2 py-0.5 text-[0.5625rem] font-bold text-green-700 dark:border-green-800 dark:bg-green-950/40 dark:text-green-300">
                Official Government Data
              </span>
            </div>
            <div className="space-y-0">
              {[
                { source: "MPLADS Official Government Portal", data: "Project, constituency, MP and allocation records" },
                { source: "MPLADS Works / Expenditure Data", data: "Sanctioned amount, expenditure and project progress" },
                { source: "Project Status Records", data: "Ongoing/completed work information" },
                { source: "AI Analysis Layer", data: "Risk scoring and anomaly detection derived from available project data" },
              ].map((row, i) => (
                <div key={i} className="flex flex-col gap-0.5 border-b border-gray-50 py-2 dark:border-gray-700/40 last:border-0 last:pb-0">
                  <span className="text-[0.625rem] font-bold text-gray-700 dark:text-gray-300">{row.source}</span>
                  <span className="text-[0.625rem] text-gray-500 dark:text-gray-400">{row.data}</span>
                </div>
              ))}
            </div>
            <p className="mt-2.5 border-t border-gray-50 pt-2 text-[0.5625rem] leading-relaxed text-gray-400 dark:border-gray-700/40 dark:text-gray-500">
              Data is sourced from official MPLADS government records and processed by the platform for monitoring and analytical purposes.
            </p>
          </div>
        </div>
      </div>

    </div>
  )
})

export default Overview
