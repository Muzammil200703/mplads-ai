import { useEffect, useRef, useState } from "react"
import { getProjectDetail, getSimilarProjects, getRiskExplanation, getAnomalyExplanation, getProjectTimeline, getProjectActivity, getInvestigation, saveProject as apiSaveProject, unsaveProject } from "../services/api"
import { useAuth } from "../context/AuthContext"
import {
  DigitalAuditCard,
  EvidenceGapPanel,
  InvestigationWorkspace,
  AnomalyExplorerPanel,
  WhatIfSimulator,
  PeerBenchmarkPanel,
  AuditCasePanel,
  PredictiveInsightsPanel,
} from "./AuditIntelligence"
import ForensicsPanel from "./ForensicsPanel"
import ForensicModeModal from "./ForensicModeModal"
import RecTimelineBlock from "./RecTimelineBlock"
import ProjectDNASection from "./ProjectDNASection"
import RiskStressTest from "./RiskStressTest"
import { parseReasons } from "../utils/reasons"

// ── Timeline helpers ─────────────────────────────────────────

const TIMELINE_STYLES = {
  recommendation: { dot: "bg-blue-500", label: "text-blue-600 dark:text-blue-400" },
  expenditure: { dot: "bg-purple-500", label: "text-purple-600 dark:text-purple-400" },
  completion: { dot: "bg-green-500", label: "text-green-600 dark:text-green-400" },
  status: { dot: "bg-gray-400", label: "text-gray-600 dark:text-gray-400" },
}

function fmtDate(d) {
  if (!d) return null
  try {
    return new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" })
  } catch { return d }
}

function TimelineSection({ timeline }) {
  const { events, delay_intelligence: di, meta } = timeline
  if (!events || events.length === 0) {
    return (
      <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4 text-center dark:border-gray-700 dark:bg-[#171b20]">
        <p className="text-xs font-semibold text-gray-500">No timeline events could be matched for this project.</p>
        <p className="mt-1 text-[0.625rem] text-gray-400">Lifecycle records for this work are not present in the available MPLADS datasets.</p>
      </div>
    )
  }

  const statusColor =
    di?.status === "AT RISK" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
    : di?.status === "COMPLETED" ? "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
    : di?.status === "ON TRACK" ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
    : "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300"

  return (
    <div className="space-y-4">
      {/* Lifecycle events */}
      <div className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-[#171b20]">
        <div className="mb-3 flex items-center justify-between">
          <h4 className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Project Lifecycle</h4>
          <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[0.5625rem] font-bold text-gray-500 dark:bg-gray-800">{meta?.data_period?.from ? `Data period: ${meta.data_period.from} → ${meta.data_period.to}` : ""}</span>
        </div>
        <div className="relative pl-5">
          <div className="absolute bottom-2 left-[5px] top-2 w-px bg-gray-200 dark:bg-gray-700" />
          {events.map((ev, i) => {
            const st = TIMELINE_STYLES[ev.event_type] || TIMELINE_STYLES.status
            return (
              <div key={i} className="relative pb-4 last:pb-0">
                <span className={`absolute -left-5 top-1 h-2.5 w-2.5 rounded-full ring-4 ring-white dark:ring-[#171b20] ${st.dot}`} />
                <div className="flex flex-wrap items-baseline justify-between gap-x-2">
                  <p className={`text-xs font-bold ${st.label}`}>{ev.title}</p>
                  <p className="font-mono text-[0.625rem] font-semibold text-gray-500">{fmtDate(ev.date)}</p>
                </div>
                <p className="mt-0.5 text-[0.6875rem] leading-relaxed text-gray-600 dark:text-gray-400">{ev.description}</p>
                <p className="mt-0.5 text-[0.5625rem] text-gray-400">
                  Source: {ev.source_dataset} · Match: {ev.match_confidence === "mp_verified" ? "MP-verified" : "exact"}
                </p>
              </div>
            )
          })}
        </div>
      </div>

      {/* Delay Intelligence */}
      {di && (
        <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
          <div className="mb-3 flex items-center justify-between">
            <h4 className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Delay Intelligence</h4>
            <span className={`rounded px-2 py-0.5 text-[0.625rem] font-bold ${statusColor}`}>{di.status}</span>
          </div>

          {/* Timeline metrics */}
          {di.timeline_metrics && Object.keys(di.timeline_metrics).length > 0 && (
            <div className="mb-3 grid grid-cols-2 gap-2">
              {Object.entries(di.timeline_metrics).map(([k, v]) => (
                <div key={k} className="rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700">
                  <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">
                    {k.replace(/_/g, " ").replace(/^days /, "Days ")}
                  </p>
                  <p className="font-mono text-sm font-bold">{v} days</p>
                </div>
              ))}
            </div>
          )}

          {/* Financial vs physical */}
          {di.financial_physical && (
            <div className="mb-3 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Financial Utilization</p>
                  <p className="font-mono text-base font-bold text-purple-600 dark:text-purple-400">{di.financial_physical.financial_utilization_pct ?? "N/A"}%</p>
                </div>
                <div>
                  <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Physical Completion</p>
                  <p className="font-mono text-base font-bold text-blue-600 dark:text-blue-400">{di.financial_physical.physical_completion_pct ?? "N/A"}%</p>
                </div>
              </div>
              <p className="mt-2 text-[0.625rem] font-semibold text-gray-500">{di.financial_physical.verdict}</p>
            </div>
          )}

          {/* Observed indicators */}
          {di.observed_indicators?.length > 0 ? (
            <div className="mb-3">
              <p className="mb-1.5 text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Observed Indicators</p>
              <div className="space-y-1.5">
                {di.observed_indicators.map((ind, i) => (
                  <div key={i} className={`flex items-start gap-2 rounded-lg p-2 ${
                    ind.severity === "high" ? "bg-red-50 dark:bg-red-950/20"
                    : ind.severity === "medium" ? "bg-amber-50 dark:bg-amber-950/20"
                    : "bg-gray-100 dark:bg-gray-800"
                  }`}>
                    <span className={`mt-0.5 h-1.5 w-1.5 flex-shrink-0 rounded-full ${ind.severity === "high" ? "bg-red-500" : ind.severity === "medium" ? "bg-amber-500" : "bg-gray-400"}`} />
                    <div>
                      <p className="text-[0.6875rem] font-bold text-gray-700 dark:text-gray-200">{ind.indicator}</p>
                      <p className="text-[0.625rem] text-gray-500 dark:text-gray-400">{ind.detail}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="mb-3 text-[0.625rem] text-gray-400">No adverse indicators observed from available data.</p>
          )}

          {/* Formal delay — explicitly not calculable */}
          <div className="mb-2 rounded-lg border border-gray-200 bg-white p-2.5 dark:border-gray-700">
            <p className="text-[0.625rem] text-gray-500 dark:text-gray-400">{di.delay_note}</p>
          </div>

          {/* Recorded reason / possible factors — only when data supports them */}
          {di.recorded_reason && (
            <div className="mb-2 rounded-lg border border-blue-200 bg-blue-50 p-2.5 dark:border-blue-900/40 dark:bg-blue-950/20">
              <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-blue-600 dark:text-blue-400">Recorded reason ({di.recorded_reason_source || "source dataset"})</p>
              <p className="text-[0.6875rem] text-gray-700 dark:text-gray-300">{di.recorded_reason}</p>
            </div>
          )}
          {di.possible_factors?.length > 0 && (
            <div className="rounded-lg border border-gray-200 bg-white p-2.5 dark:border-gray-700">
              <p className="text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Possible contributing factors (inference)</p>
              <ul className="mt-1 list-disc pl-4 text-[0.625rem] text-gray-600 dark:text-gray-400">
                {di.possible_factors.map((f, i) => <li key={i}>{f}</li>)}
              </ul>
            </div>
          )}
          {!di.possible_factors && (
            <p className="text-[0.5625rem] italic text-gray-400">{di.recorded_reason_note}</p>
          )}
        </div>
      )}

      {/* Data source / freshness disclaimer */}
      {meta?.disclaimer && (
        <div className="rounded-lg border border-gray-200 bg-gray-50 p-2.5 dark:border-gray-700 dark:bg-[#171b20]">
          <p className="text-[0.5625rem] leading-relaxed text-gray-400">
            <span className="font-bold text-gray-500">Historical dataset:</span> {meta.disclaimer}
          </p>
        </div>
      )}
    </div>
  )
}

function formatMoney(value) {
  const number = Number(value || 0)
  if (number >= 10000000) return `₹${(number / 10000000).toFixed(2)} Cr`
  if (number >= 100000) return `₹${(number / 100000).toFixed(2)} L`
  return `₹${number.toLocaleString("en-IN")}`
}

// Determine auditor-friendly badges from risk data
function getBadges(risk, project) {
  const badges = []
  if (!risk) return badges
  // reasons may be a comma-separated string (DB) or an array (predict_risk)
  const reasons = parseReasons(risk.reasons)
  const sanctioned = Number(project.sanctioned_amount || 0)
  const expenditure = Number(project.expenditure || 0)
  const completion = Number(project.completion_percentage || 0)

  if (risk.risk_level === "High") badges.push({ label: "HIGH RISK", color: "red" })
  else if (risk.risk_level === "Medium") badges.push({ label: "MEDIUM RISK", color: "amber" })

  if (reasons.some((r) => r.includes("exceeds sanctioned")))
    badges.push({ label: "COST OVERRUN", color: "red", detail: `Expenditure ₹${formatMoney(expenditure)} exceeds sanctioned ₹${formatMoney(sanctioned)} by ₹${formatMoney(Math.max(0, expenditure - sanctioned))}` })
  if (reasons.some((r) => r.includes("High expenditure")))
    badges.push({ label: "PROGRESS MISMATCH", color: "orange", detail: `Fund utilization at ${sanctioned > 0 ? ((expenditure / sanctioned) * 100).toFixed(0) : 0}% but progress only ${completion}%` })
  if (reasons.some((r) => r.includes("0% physical")))
    badges.push({ label: "ZERO PROGRESS", color: "red", detail: `₹${formatMoney(expenditure)} spent with 0% physical progress` })
  if (reasons.some((r) => r.includes("completed but")))
    badges.push({ label: "STATUS INCONSISTENCY", color: "amber", detail: `Marked Completed but progress is ${completion}%` })
  if (reasons.some((r) => r.includes("ML") || r.includes("ml")))
    badges.push({ label: "ML OUTLIER", color: "purple", detail: "Statistical outlier detected by ML model" })

  return badges
}

// Generate audit recommendation based on risk reasons
function getAuditRecommendation(rawReasons) {
  const reasons = parseReasons(rawReasons)
  if (reasons.length === 0) return null
  const recommendations = []
  for (const reason of reasons) {
    const r = reason.toLowerCase()
    if (r.includes("exceeds sanctioned"))
      recommendations.push("Review expenditure records and approved cost estimates. Verify whether additional sanctions were obtained for the excess amount.")
    else if (r.includes("high expenditure"))
      recommendations.push("Verify physical progress against expenditure. Request site inspection and progress documentation.")
    else if (r.includes("0% physical"))
      recommendations.push("Verify whether work has commenced. Review expenditure/disbursement records and contractor agreements.")
    else if (r.includes("completed but"))
      recommendations.push("Verify project status against actual completion. Review completion certificates and final inspection reports.")
    else if (r.includes("high-value project"))
      recommendations.push("Review execution timeline and identify causes of delay. Assess whether project requires administrative intervention.")
    else if (r.includes("high completion"))
      recommendations.push("Verify physical progress records against financial data. Check for data recording errors.")
    else if (r.includes("very low fund"))
      recommendations.push("Review fund utilization and project execution status. Assess whether project should be escalated.")
    else if (r.includes("ml"))
      recommendations.push("Review the project against comparable projects to determine whether the unusual pattern is legitimate.")
  }
  return recommendations
}

function SaveProjectButton({ projectId }) {
  const { user } = useAuth()
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState("")

  if (!user) return null

  const toggle = async () => {
    setBusy(true); setMsg("")
    try {
      if (saved) { await unsaveProject(projectId); setSaved(false) }
      else { await apiSaveProject(projectId); setSaved(true); setMsg("Saved") }
    } catch (e) { setMsg(e.message || "Failed") }
    finally { setBusy(false); setTimeout(() => setMsg(""), 2500) }
  }

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={toggle}
        disabled={busy}
        className={`rounded-lg px-3 py-2 text-xs font-bold transition disabled:opacity-50 ${
          saved
            ? "border border-amber-400 bg-amber-50 text-amber-700 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
            : "border border-gray-300 text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
        }`}
      >
        {busy ? "…" : saved ? "★ Saved" : "☆ Save project"}
      </button>
      {msg && <span className="text-[0.6875rem] font-semibold text-green-600 dark:text-green-400">{msg}</span>}
    </div>
  )
}

function ProjectDetail({ projectId, onClose, initialTab }) {
  const { user, hasRole, can } = useAuth()
  const [detail, setDetail] = useState(null)
  const [similar, setSimilar] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadingSimilar, setLoadingSimilar] = useState(false)
  const [activeTab, setActiveTab] = useState("overview")
  const [timeline, setTimeline] = useState(null)
  const [activity, setActivity] = useState(null)
  const [riskExplanation, setRiskExplanation] = useState(null)
  const [anomalyExplanation, setAnomalyExplanation] = useState(null)
  const [loadingExplanation, setLoadingExplanation] = useState(false)
  const [investigation, setInvestigation] = useState(null)
  const [auditSection, setAuditSection] = useState("evidence")
  // Forensic Mode / Risk Stress Test overlays — render ON TOP of the
  // existing detail interface without altering any of its structure.
  const [showForensicMode, setShowForensicMode] = useState(false)
  const [showStressTest, setShowStressTest] = useState(false)
  const [stressTab, setStressTab] = useState(false) // Risk & Audit tab hosts the inline stress panel
  // Closing animation: 150ms fade/dip, then unmount (reduced motion → instant)
  const [closing, setClosing] = useState(false)
  const closingRef = useRef(false)
  // Modal close → hand the assistant back to page-level context (matches the
  // Risk Center / Audit Priority modal lifecycle).
  const releaseAssistantContext = () => window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId: null } }))
  useEffect(() => () => releaseAssistantContext(), [])

  /* Side-by-side layout: while the floating assistant panel is open the
     overlay reserves room on the right (desktop only — CSS media query),
     centering the card in the remaining space so the two never overlap.
     Closing the assistant removes the reservation and the card re-centers. */
  const [assistantPanelOpen, setAssistantPanelOpen] = useState(
    () => document.documentElement.classList.contains("assistant-panel-open")
  )
  useEffect(() => {
    const h = (e) => setAssistantPanelOpen(!!e.detail?.open)
    window.addEventListener("assistant-panel", h)
    return () => window.removeEventListener("assistant-panel", h)
  }, [])
  const requestClose = () => {
    if (closingRef.current) return
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      onClose()
      return
    }
    closingRef.current = true
    setClosing(true)
    setTimeout(onClose, 150)
  }

  useEffect(() => {
    if (!projectId) return
    setLoading(true)
    setDetail(null)
    setSimilar(null)
    // Assistant tab deep-links: only allowlisted tab ids are honored; anything
    // else falls back to the default overview tab.
    const ALLOWED_TABS = ["overview", "financial", "risk", "forensics", "audit", "timeline", "similar", "dna"]
    setActiveTab(ALLOWED_TABS.includes(initialTab) ? initialTab : "overview")
    setTimeline(null)
    setActivity(null)
    // The open global assistant follows this modal: switching projects
    // (A → B) re-targets the assistant's project context immediately.
    window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId } }))
    setInvestigation(null)
    setAuditSection("evidence")
    Promise.all([
      getProjectDetail(projectId),
      getSimilarProjects(projectId, 5).catch(() => null),
      getInvestigation(projectId).catch(() => null),
    ]).then(([d, s, inv]) => {
      setInvestigation(inv)
      if (inv?.investigation) setAuditSection("investigation")
      setDetail(d)
      setSimilar(s)
      // Load risk + anomaly explanations in parallel
      if (d?.risk?.risk_level && d.risk.risk_level !== "None" && d.risk.risk_level !== "Low") {
        setLoadingExplanation(true)
        Promise.allSettled([
          getRiskExplanation(projectId).catch(() => null),
          getAnomalyExplanation(projectId).catch(() => null),
        ]).then(([re, ae]) => {
          if (re.status === "fulfilled" && re.value) setRiskExplanation(re.value)
          if (ae.status === "fulfilled" && ae.value) setAnomalyExplanation(ae.value)
        }).finally(() => setLoadingExplanation(false))
      }
    }).catch((err) => {
      console.error("Detail load error:", err)
    }).finally(() => {
      setLoading(false)
    })
  }, [projectId, initialTab])

  // Name for the assistant's context indicator (cosmetic; the id is what the
  // backend resolves). Fires whenever a different project's detail arrives.
  // NOTE: must stay before the early returns below (Rules of Hooks).
  const projName = detail?.project?.project_name
  const projId = detail?.project?.id
  useEffect(() => {
    if (projId && projName) {
      window.dispatchEvent(new CustomEvent("assistant-project-context", {
        detail: { projectId: projId, projectName: projName },
      }))
    }
  }, [projId, projName])

  if (loading) {
    return (
      <div className={`anim-overlay-in fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-2xs ${closing ? "is-closing" : ""}`} onClick={requestClose}>
        <div className="anim-card-in detail-loading-shimmer rounded-2xl bg-white p-12 text-center shadow-soft-lg dark:bg-[#222831]" onClick={(e) => e.stopPropagation()}>
          <div className="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent" />
          <p className="mt-3 text-sm font-medium">Loading project details...</p>
        </div>
      </div>
    )
  }

  if (!detail) {
    return (
      <div className={`anim-overlay-in fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-2xs ${closing ? "is-closing" : ""}`} onClick={requestClose}>
        <div className="anim-card-in rounded-2xl bg-white p-8 text-center shadow-soft-lg dark:bg-[#222831]" onClick={(e) => e.stopPropagation()}>
          <p className="text-sm text-gray-500">Project not found.</p>
          <button onClick={onClose} className="mt-4 rounded-lg bg-[#46566a] px-4 py-2 text-xs font-bold text-white">Close</button>
        </div>
      </div>
    )
  }

  const proj = detail.project
  const risk = detail.risk
  // Reasons may be a comma-separated string (risk_scores) or an array (predict_risk)
  const parsedReasons = parseReasons(risk?.reasons)
  const sanctioned = Number(proj.sanctioned_amount || 0)
  const expenditure = Number(proj.expenditure || 0)
  const completion = Number(proj.completion_percentage || 0)
  const utilization = sanctioned > 0 ? (expenditure / sanctioned * 100) : 0
  const remaining = Math.max(0, sanctioned - expenditure)
  const discrepancy = utilization - completion
  const badges = getBadges(risk, proj)
  const recommendations = getAuditRecommendation(parsedReasons)

  return (
    <div
      className={`anim-overlay-in fixed inset-0 z-[80] flex items-start justify-center overflow-y-auto bg-black/50 p-3 backdrop-blur-2xs transition-opacity modal-shift-transition sm:p-6 ${closing ? "is-closing" : ""} ${assistantPanelOpen ? "modal-share-with-assistant" : ""}`}
      onClick={requestClose}
    >
      <div data-mplads-modal-card className="anim-card-in relative my-auto w-full max-w-3xl rounded-2xl border border-[#dde1e5] bg-white p-5 text-gray-900 shadow-soft-lg sm:p-6 dark:border-[#39434E] dark:bg-[#222831] dark:text-white" onClick={(e) => e.stopPropagation()}>

        {/* Header */}
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-2">
            <span className="rounded bg-blue-100 px-2.5 py-1 font-mono text-xs font-bold text-blue-800 dark:bg-blue-900/60 dark:text-blue-200">#{proj.id}</span>
            {proj.status && (
              <span className={`rounded px-2 py-0.5 text-xs font-bold ${
                proj.status === "Completed" ? "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
                : proj.status === "Ongoing" ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                : "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
              }`}>{proj.status}</span>
            )}
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {/* Forensic + stress-test actions — hidden entirely for roles
                without permissions (never rendered disabled). Analysts
                (rank-based) and audit-capable roles get them. */}
            {user && (hasRole("analyst") || can("audit:action")) && (
              <>
                <button
                  onClick={() => setShowStressTest(true)}
                  title="Simulate risk-factor changes — read-only"
                  className="rounded-lg border border-gray-200 px-2.5 py-1 text-[0.625rem] font-bold text-gray-600 transition hover:border-purple-300 hover:bg-purple-50 dark:border-gray-600 dark:text-gray-300 dark:hover:border-purple-700 dark:hover:bg-purple-950/40"
                >
                  🧪 Stress Test
                </button>
                <button
                  onClick={() => setShowForensicMode(true)}
                  title="Consolidated forensic evidence for this project — read-only"
                  className="rounded-lg border border-gray-200 px-2.5 py-1 text-[0.625rem] font-bold text-gray-600 transition hover:border-blue-300 hover:bg-blue-50 dark:border-gray-600 dark:text-gray-300 dark:hover:border-blue-700 dark:hover:bg-blue-950/40"
                >
                  🔬 Investigate Project
                </button>
              </>
            )}
            <SaveProjectButton projectId={proj.id} />
            <button onClick={requestClose} className="rounded-lg p-1.5 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700">✕</button>
          </div>
        </div>

        <h2 className="mt-3 text-lg font-bold leading-snug">{proj.project_name || "Unnamed Project"}</h2>

        {/* Location */}
        <div className="mt-2 grid grid-cols-2 gap-2">
          <span className="text-xs text-gray-500">📍 {proj.state || "N/A"}</span>
          <span className="text-xs text-gray-500">🏛 {proj.constituency || "N/A"}</span>
        </div>

        {proj.data_quality_flag === "POSSIBLY_STALE" && (
          <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 dark:border-amber-900/60 dark:bg-amber-950/30">
            <div className="flex items-center gap-2"><span className="text-sm">⚠</span><span className="text-xs font-bold text-amber-700 dark:text-amber-300">Data Update Notice</span></div>
            <p className="mt-1.5 text-[0.6875rem] leading-relaxed text-amber-600 dark:text-amber-400">Reported progress or expenditure may not reflect the latest project status. A high-risk score indicates an anomaly based on available data and does not by itself confirm project delay or irregularity.</p>
          </div>
        )}

        {/* Badges */}
        {badges.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {badges.map((b, i) => (
              <span key={i} title={b.detail} className={`rounded px-2 py-0.5 text-[0.625rem] font-bold cursor-help ${
                b.color === "red" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                : b.color === "amber" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                : b.color === "orange" ? "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300"
                : b.color === "purple" ? "bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300"
                : "bg-gray-100 text-gray-600 dark:bg-gray-700"
              }`}>{b.label}</span>
            ))}
          </div>
        )}

        {/* Digital Audit Card — understand the project in seconds */}
        <div className="mt-4">
          <DigitalAuditCard
            detail={detail}
            investigation={investigation}
            onStartInvestigation={() => { setActiveTab("audit"); setAuditSection("investigation") }}
          />
        </div>

        {/* Tabs */}
        <div className="mt-5 flex flex-wrap gap-1 border-b border-gray-200 dark:border-gray-700">
          {[
            { id: "overview", label: "Overview" },
            { id: "financial", label: "Financial" },
            { id: "risk", label: "Risk & Audit" },
            { id: "forensics", label: "AI Forensics" },
            { id: "audit", label: "Investigate" },
            { id: "timeline", label: "Timeline" },
            ...(similar?.similar_projects?.length > 0 ? [{ id: "similar", label: "Similar" }] : []),
            { id: "dna", label: "🧬 DNA" },
          ].map((tab) => (
            <button key={tab.id} onClick={() => setActiveTab(tab.id)}
              className={`px-3 py-2 text-xs font-bold transition ${activeTab === tab.id ? "border-b-2 border-blue-600 text-blue-600 dark:text-blue-400" : "text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"}`}>
              {tab.label}
            </button>
          ))}
        </div>

        {/* Tab Content — keyed so each switch replays the subtle fade/slide-in */}
        <div key={activeTab} className="anim-panel-in mt-4 space-y-4">
          {activeTab === "overview" && (
            <>
              <InfoGrid items={[
                { label: "State", value: proj.state || "Not available" },
                { label: "Constituency", value: proj.constituency || "Not available" },
                { label: "Category", value: proj.project_type || "Not available" },
                { label: "Status", value: proj.status || "Not available" },
              ]} />
              {/* Recommendation & timeline facts — backend rec source of truth */}
              <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
                <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Recommendation & Timeline</p>
                <div className="mt-2.5">
                  <RecTimelineBlock rec={detail.rec} physical_completion_pct={detail?.financial_physical?.physical_completion_pct} />
                </div>
              </div>
              <ProgressCard completion={completion} />
              {/* Predictive estimates + payment-data
                  availability — derived from recorded values only. */}
              <PredictiveInsightsPanel detail={detail} />
            </>
          )}

          {activeTab === "financial" && (
            <>
              <div className="grid grid-cols-2 gap-3">
                <MetricCard label="Sanctioned" value={formatMoney(sanctioned)} color="blue" />
                <MetricCard label="Expenditure" value={formatMoney(expenditure)} color="purple" />
                <MetricCard label="Remaining" value={formatMoney(remaining)} color="green" />
                <MetricCard label="Utilization" value={`${utilization.toFixed(1)}%`} color={utilization > 100 ? "red" : "blue"} />
              </div>
              {/* Data-quality disclosures: unlinked/ambiguous expenditure is UNKNOWN, not a verified ₹0. */}
              {proj.expenditure_status === "no_linked_records" && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-300">
                  <span className="font-bold">No verifiably linked expenditure records.</span>{" "}
                  The payment ledger could not be reliably matched to this work (by normalized work key + MP + implementing-district authority), so the recorded spend is <span className="font-bold">unknown — not confirmed ₹0</span>. Utilization cannot be computed from the available data.
                </div>
              )}
              {proj.expenditure_status === "ambiguous_shared_work" && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-300">
                  <span className="font-bold">Shared work description — per-project spend unknown.</span>{" "}
                  Payments of ₹{Number(proj.work_key_expenditure || 0).toLocaleString("en-IN")} are recorded for this work description, but {proj.work_key_rows || "multiple"} catalog rows share it, so the total cannot be attributed to any single project. Per-project utilization is not computable without double-counting.
                </div>
              )}
              {/* Utilization Bar */}
              <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-bold text-gray-500">Fund Utilization</span>
                  <span className="font-mono font-bold">{utilization.toFixed(1)}%</span>
                </div>
                <div className="mt-2 h-3 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                  <div className={`h-full rounded-full transition-all duration-500 ${utilization > 100 ? "bg-red-500" : "bg-blue-600"}`}
                    style={{ width: `${Math.min(100, utilization)}%` }} />
                </div>
                <div className="mt-1.5 flex justify-between text-[0.625rem] text-gray-400">
                  <span>Spent: {formatMoney(expenditure)}</span>
                  <span>Remaining: {formatMoney(remaining)}</span>
                </div>
              </div>
              {/* Expenditure vs Progress */}
              <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
                <h4 className="mb-3 text-xs font-bold uppercase tracking-wider text-gray-400">Expenditure vs Physical Progress</h4>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <p className="text-[0.625rem] font-bold text-gray-400">Financial Utilization</p>
                    <p className="font-mono text-lg font-bold">{utilization.toFixed(1)}%</p>
                    <div className="mt-1 h-2 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                      <div className="h-full rounded-full bg-purple-600" style={{ width: `${Math.min(100, utilization)}%` }} />
                    </div>
                  </div>
                  <div>
                    <p className="text-[0.625rem] font-bold text-gray-400">Physical Progress</p>
                    <p className="font-mono text-lg font-bold">{completion}%</p>
                    <div className="mt-1 h-2 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                      <div className="h-full rounded-full bg-blue-600" style={{ width: `${Math.min(100, completion)}%` }} />
                    </div>
                  </div>
                </div>
                {Math.abs(discrepancy) > 10 && (
                  <div className={`mt-3 rounded-lg p-2.5 text-xs font-semibold ${
                    discrepancy > 0
                      ? "border border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300"
                      : "border border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900/60 dark:bg-blue-950/30 dark:text-blue-300"
                  }`}>
                    {discrepancy > 0
                      ? `⚠ Expenditure-progress mismatch: Financial utilization (${utilization.toFixed(0)}%) exceeds physical progress (${completion}%) by ${Math.abs(discrepancy).toFixed(0)} percentage points.`
                      : `Physical progress (${completion}%) exceeds financial utilization (${utilization.toFixed(0)}%) by ${Math.abs(discrepancy).toFixed(0)} percentage points.`}
                  </div>
                )}
              </div>
              {/* Expenditure Activity / history */}
              <LazyFetch
                store={[activity, setActivity]}
                loader={() => getProjectActivity(projectId)}
                loadingLabel="Loading expenditure activity..."
                render={(a) => <ActivitySection activity={a} />}
              />

              {/* Flag suspicious cases */}
              {expenditure > sanctioned && sanctioned > 0 && (
                <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">
                  ⚠ Cost overrun: Expenditure exceeds sanctioned amount by {formatMoney(expenditure - sanctioned)}
                </div>
              )}
              {expenditure > 0 && completion === 0 && (
                <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">
                  ⚠ Zero progress: {formatMoney(expenditure)} spent but physical progress is 0%
                </div>
              )}
            </>
          )}

          {activeTab === "risk" && (
            <>
              {/* Risk Score */}
              {risk && (
                <div className={`rounded-xl border p-4 ${
                  risk.risk_level === "High" ? "border-red-200 bg-red-50/60 dark:border-red-900/60 dark:bg-red-950/30"
                  : risk.risk_level === "Medium" ? "border-amber-200 bg-amber-50/60 dark:border-amber-900/60 dark:bg-amber-950/30"
                  : "border-green-200 bg-green-50/60 dark:border-green-900/60 dark:bg-green-950/30"
                }`}>
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-[0.625rem] font-bold uppercase text-gray-500">Risk Level</p>
                      <p className={`text-lg font-bold ${
                        risk.risk_level === "High" ? "text-red-600 dark:text-red-400"
                        : risk.risk_level === "Medium" ? "text-amber-600 dark:text-amber-400"
                        : "text-green-600 dark:text-green-400"
                      }`}>{risk.risk_level || "None"}</p>
                    </div>
                    <div className="text-right">
                      <p className="text-[0.625rem] font-bold uppercase text-gray-500">Score</p>
                      <p className="font-mono text-2xl font-bold">{risk.risk_score}<span className="text-sm text-gray-400">/100</span></p>
                    </div>
                  </div>
                  <div className="mt-3 h-2 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                    <div className={`h-full rounded-full ${risk.risk_level === "High" ? "bg-red-500" : risk.risk_level === "Medium" ? "bg-amber-500" : "bg-green-500"}`}
                      style={{ width: `${Math.min(100, risk.risk_score)}%` }} />
                  </div>
                </div>
              )}

              {/* Triggered Rules */}
              {parsedReasons && parsedReasons.length > 0 && (
                <div className="space-y-2">
                  <h4 className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Triggered Rules ({parsedReasons.length})</h4>
                  {parsedReasons.map((reason, i) => (
                    <div key={i} className="rounded-lg border border-red-100 bg-red-50/50 p-3 dark:border-red-900/40 dark:bg-red-950/20">
                      <p className="text-xs font-semibold text-red-700 dark:text-red-400">{reason}</p>
                    </div>
                  ))}
                </div>
              )}

              {/* Audit Insight Summary */}
              {risk && risk.risk_level !== "None" && risk.risk_level !== "Low" && (
                <div className="rounded-xl border border-blue-200 bg-blue-50/50 p-4 dark:border-blue-900/40 dark:bg-blue-950/20">
                  <h4 className="mb-2 text-xs font-bold uppercase tracking-wider text-blue-700 dark:text-blue-300">Audit Insight Summary</h4>
                  <div className="space-y-2 text-xs">
                    <p><span className="font-bold">Risk:</span> {risk.risk_level} (Score: {risk.risk_score}/100)</p>
                    <p><span className="font-bold">Key Finding:</span> {formatMoney(expenditure)} has been spent against {formatMoney(sanctioned)} sanctioned while physical progress is {completion}%.</p>
                    {discrepancy > 10 && (
                      <p><span className="font-bold">Primary Concern:</span> Expenditure is disproportionately high relative to reported physical progress (utilization {utilization.toFixed(0)}% vs progress {completion}%).</p>
                    )}
                    {expenditure > sanctioned && sanctioned > 0 && (
                      <p><span className="font-bold">Primary Concern:</span> Expenditure exceeds sanctioned amount by {formatMoney(expenditure - sanctioned)}.</p>
                    )}
                    {expenditure > 0 && completion === 0 && (
                      <p><span className="font-bold">Primary Concern:</span> Financial disbursements have been made but no physical progress is recorded.</p>
                    )}
                  </div>
                </div>
              )}

              {/* Recommended Audit Actions */}
              {recommendations && recommendations.length > 0 && (
                <div className="rounded-xl border border-amber-200 bg-amber-50/50 p-4 dark:border-amber-900/40 dark:bg-amber-950/20">
                  <h4 className="mb-2 text-xs font-bold uppercase tracking-wider text-amber-700 dark:text-amber-300">Recommended Audit Actions</h4>
                  <div className="space-y-1.5">
                    {recommendations.map((rec, i) => (
                      <div key={i} className="flex items-start gap-2 text-xs text-amber-800 dark:text-amber-200">
                        <span className="mt-0.5">→</span>
                        <span>{rec}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {risk?.ml_anomaly && (
                <div className="rounded-lg border border-purple-200 bg-purple-50 p-3 dark:border-purple-900/60 dark:bg-purple-950/30">
                  <p className="text-xs font-bold text-purple-700 dark:text-purple-300">✨ ML Statistical Outlier Detected</p>
                  <p className="mt-1 text-[0.625rem] text-purple-600 dark:text-purple-400">Isolation Forest model flagged this project as a multi-dimensional statistical anomaly.</p>
                </div>
              )}

              {/* AI Risk Explanation */}
              {riskExplanation && (
                <div className="rounded-xl border border-blue-200 bg-blue-50/40 p-4 dark:border-blue-900/40 dark:bg-blue-950/20">
                  <div className="flex items-center gap-2 mb-3">
                    <span className="flex h-5 w-5 items-center justify-center rounded-full bg-blue-600 text-[0.625rem] text-white">AI</span>
                    <h4 className="text-xs font-bold uppercase tracking-wider text-blue-700 dark:text-blue-300">Risk Explanation</h4>
                  </div>
                  <p className="text-xs text-gray-600 dark:text-gray-300 mb-3">{riskExplanation.summary}</p>
                  {riskExplanation.contributing_factors?.length > 0 && (
                    <div className="space-y-1.5">
                      <p className="text-[0.625rem] font-bold uppercase text-gray-400">Contributing Factors:</p>
                      {riskExplanation.contributing_factors.map((f, i) => (
                        <div key={i} className="flex items-start gap-2 text-xs">
                          <span className={`mt-0.5 flex-shrink-0 rounded px-1 py-0.5 text-[0.5625rem] font-bold ${
                            f.source === "ml" ? "bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300"
                            : f.source === "rule" ? "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300"
                            : "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                          }`}>{f.source}</span>
                          <span className="text-gray-700 dark:text-gray-300">{f.factor}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {riskExplanation.data_sufficiency === "limited" && (
                    <p className="mt-2 text-[0.625rem] italic text-gray-400">Note: Explanation is based on limited available data.</p>
                  )}
                </div>
              )}

              {riskExplanation?.data_quality?.flag === "POSSIBLY_STALE" && (
                <div className="rounded-xl border border-amber-200 bg-amber-50/40 p-4 dark:border-amber-900/40 dark:bg-amber-950/20">
                  <h4 className="mb-2 text-xs font-bold uppercase tracking-wider text-amber-700 dark:text-amber-300">Data Quality Check</h4>
                  <div className="flex items-start gap-2">
                    <span className="mt-0.5 text-sm">⚠</span>
                    <div>
                      <p className="text-xs font-semibold text-amber-700 dark:text-amber-300">Possible stale progress data</p>
                      <p className="mt-1 text-[0.6875rem] text-amber-600 dark:text-amber-400">{riskExplanation.data_quality.reason}</p>
                      <p className="mt-1.5 text-[0.625rem] italic text-amber-400 dark:text-amber-500">This indicator does not confirm project delay or irregularity.</p>
                    </div>
                  </div>
                </div>
              )}

              {/* Anomaly Explanation Panel */}
              {anomalyExplanation && anomalyExplanation.detected_anomalies?.length > 0 && (
                <div className="rounded-xl border border-red-200 bg-red-50/40 p-4 dark:border-red-900/40 dark:bg-red-950/20">
                  <h4 className="mb-3 text-xs font-bold uppercase tracking-wider text-red-700 dark:text-red-300">Why is this an anomaly?</h4>
                  {anomalyExplanation.detected_anomalies.map((anomaly, i) => (
                    <div key={i} className="mb-3 last:mb-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span className={`rounded px-1.5 py-0.5 text-[0.625rem] font-bold ${
                          anomaly.severity === "High" ? "bg-red-100 text-red-700"
                          : anomaly.severity === "Medium" ? "bg-amber-100 text-amber-700"
                          : "bg-gray-100 text-gray-600"
                        }`}>{anomaly.severity}</span>
                        <span className="text-xs font-bold">{anomaly.type}</span>
                        <span className="text-[0.5625rem] px-1 py-0.5 rounded bg-gray-100 text-gray-500">{anomaly.source}</span>
                      </div>
                      <p className="text-xs text-gray-600 dark:text-gray-300">{anomaly.what}</p>
                      {anomaly.factors?.length > 0 && (
                        <div className="mt-1.5 space-y-0.5">
                          {anomaly.factors.map((f, fi) => (
                            <p key={fi} className="text-[0.625rem] text-gray-500">• {f}</p>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}

              {loadingExplanation && !riskExplanation && !anomalyExplanation && (
                <div className="flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 p-3">
                  <div className="h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-r-transparent" />
                  <span className="text-xs text-gray-400">Loading AI explanations...</span>
                </div>
              )}
            </>
          )}

          {activeTab === "audit" && (
            <>
              {/* Sub-sections — keeps the drawer scannable */}
              <div className="flex flex-wrap gap-1.5">
                {[
                  { id: "evidence", label: "Evidence Gaps" },
                  { id: "investigation", label: investigation?.investigation ? `Investigation (${investigation.progress_pct}%)` : "Investigation" },
                  { id: "explorer", label: "Anomaly Explorer" },
                  { id: "simulate", label: "What-If" },
                  { id: "case", label: "Audit Case" },
                ].map((s) => (
                  <button
                    key={s.id}
                    onClick={() => setAuditSection(s.id)}
                    className={`rounded-lg px-3 py-2 text-[0.875rem] font-bold transition ${
                      auditSection === s.id
                        ? "bg-[#46566a] text-white dark:bg-blue-600"
                        : "border border-gray-200 text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                    }`}
                  >
                    {s.label}
                  </button>
                ))}
              </div>

              {auditSection === "evidence" && <EvidenceGapPanel projectId={projectId} />}
              {auditSection === "investigation" && (
                <InvestigationWorkspace projectId={projectId} onWorkspaceChange={setInvestigation} />
              )}
              {auditSection === "explorer" && <AnomalyExplorerPanel projectId={projectId} />}
              {auditSection === "simulate" && <WhatIfSimulator detail={detail} />}
              {auditSection === "case" && <AuditCasePanel projectId={projectId} />}
            </>
          )}

          {activeTab === "forensics" && (
            <ForensicsPanel projectId={projectId} onOpenProject={(id) => { if (id !== projectId) window.dispatchEvent(new CustomEvent("open-project", { detail: { projectId: id } })) }} />
          )}

          {activeTab === "dna" && (
            <ProjectDNASection projectId={projectId} onOpenProject={(id) => { if (id !== projectId) window.dispatchEvent(new CustomEvent("open-project", { detail: { projectId: id } })) }} />
          )}

          {activeTab === "timeline" && (
            <LazyFetch
              store={[timeline, setTimeline]}
              loader={() => getProjectTimeline(projectId)}
              loadingLabel="Loading timeline..."
              render={(t) => <TimelineSection timeline={t} />}
            />
          )}

          {activeTab === "similar" && similar && (
            <>
              {/* Peer benchmarking against comparable projects */}
              <PeerBenchmarkPanel
                projectId={projectId}
                onOpenProject={(id) => { if (id !== projectId) window.dispatchEvent(new CustomEvent("open-project", { detail: { projectId: id } })) }}
              />

              <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-[#171b20]">
                <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Comparison Criteria</p>
                <p className="mt-1 text-xs">
                  State: <strong>{similar.criteria.state}</strong> | Type: <strong>{similar.criteria.project_type}</strong> | Range: <strong>{similar.criteria.sanctioned_range}</strong>
                </p>
              </div>
              {similar.similar_projects.length > 0 ? (
                <div className="space-y-2">
                  {similar.similar_projects.map((sp) => (
                    <div key={sp.id} className="rounded-lg border border-gray-200 bg-gray-50/50 p-3 dark:border-gray-700 dark:bg-[#171b20]">
                      <div className="flex items-start justify-between">
                        <div>
                          <span className="font-mono text-[0.625rem] font-bold text-gray-400">#{sp.id}</span>
                          <p className="text-xs font-semibold">{sp.project_name}</p>
                          <p className="text-[0.625rem] text-gray-500">{sp.constituency || "N/A"}</p>
                        </div>
                        <span className={`rounded px-1.5 py-0.5 text-[0.625rem] font-bold ${
                          sp.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                          : sp.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                          : "bg-gray-100 text-gray-600 dark:bg-gray-700"
                        }`}>{sp.risk_level} ({sp.risk_score})</span>
                      </div>
                      <div className="mt-2 grid grid-cols-4 gap-2 text-[0.625rem]">
                        <div><p className="text-gray-400">Sanctioned</p><p className="font-bold">{formatMoney(sp.sanctioned_amount)}</p></div>
                        <div><p className="text-gray-400">Spent</p><p className="font-bold">{formatMoney(sp.expenditure)}</p></div>
                        <div><p className="text-gray-400">Utilization</p><p className="font-bold">{sp.sanctioned_amount > 0 ? ((sp.expenditure / sp.sanctioned_amount) * 100).toFixed(0) : 0}%</p></div>
                        <div><p className="text-gray-400">Progress</p><p className="font-bold">{sp.completion_percentage}%</p></div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-gray-500">No similar projects found matching the criteria.</p>
              )}
            </>
          )}

          {/* Risk Stress Test — also available inline inside Risk & Audit */}
          {activeTab === "risk" && (
            <div className="rounded-xl border border-purple-200 dark:border-purple-900/60">
              <button
                onClick={() => setStressTab((v) => !v)}
                className="flex w-full items-center justify-between px-4 py-2.5 text-left"
              >
                <span className="text-[0.6875rem] font-bold uppercase tracking-wider text-purple-700 dark:text-purple-300">🧪 Risk Stress Test</span>
                <span className="text-[0.625rem] text-gray-400">{stressTab ? "Hide" : "Expand"}</span>
              </button>
              {stressTab && (
                <div className="rise-in border-t border-purple-200 p-4 dark:border-purple-900/60">
                  <RiskStressTest
                    projectId={projectId}
                    baseline={{ riskScore: risk?.risk_score ?? 0, riskLevel: risk?.risk_level, sanctioned, expenditure, completion }}
                  />
                </div>
              )}
            </div>
          )}
        </div>

        {/* Forensic Mode / Stress Test overlays — stack above the detail UI */}
        {showForensicMode && (
          <ForensicModeModal projectId={projectId} onClose={() => setShowForensicMode(false)} />
        )}
        {showStressTest && (
          <RiskStressTestModal
            projectId={projectId}
            onClose={() => setShowStressTest(false)}
            baseline={{ riskScore: risk?.risk_score ?? 0, riskLevel: risk?.risk_level, sanctioned, expenditure, completion }}
          />
        )}
      </div>
    </div>
  )
}

// Modal wrapper so the stress tool can open over the detail drawer
function RiskStressTestModal({ projectId, onClose, baseline }) {
  return (
    <div className="anim-overlay-in fixed inset-0 z-[90] flex items-start justify-center overflow-y-auto bg-black/50 p-3 backdrop-blur-2xs transition-opacity sm:p-6" onClick={onClose}>
      <div className="anim-card-in my-auto w-full max-w-2xl rounded-2xl border border-[#dde1e5] bg-white p-5 shadow-soft-lg dark:border-[#39434E] dark:bg-[#222831] sm:p-6" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Risk Stress Test</p>
            <h3 className="mt-0.5 text-sm font-bold">Interactive What-If Simulation</h3>
          </div>
          <button onClick={onClose} className="rounded-lg p-1.5 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700">✕</button>
        </div>
        <div className="mt-4 max-h-[70vh] overflow-y-auto pr-1">
          <RiskStressTest projectId={projectId} baseline={baseline} />
        </div>
      </div>
    </div>
  )
}

// Sub-components

// Shared lazy loader used by both Timeline and Financial tabs so the
// fetch happens once per drawer regardless of which tab is opened first.
// Distinguishes a real empty result from a failed request (retry offered).
function LazyFetch({ store, loader, render, loadingLabel }) {
  const [data, setData] = store
  const [status, setStatus] = useState("idle") // idle | loading | done | error

  useEffect(() => {
    if (data || status === "loading") return
    setStatus("loading")
    loader()
      .then((d) => { setData(d); setStatus("done") })
      .catch(() => setStatus("error"))
  }, [data, status, loader])

  if (status === "loading") {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 p-3">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-r-transparent" />
        <span className="text-xs text-gray-400">{loadingLabel}</span>
      </div>
    )
  }
  if (status === "error" && !data) {
    return (
      <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-center">
        <p className="text-xs text-gray-500">Could not load data.</p>
        <button onClick={() => setStatus("idle")} className="mt-2 rounded bg-[#46566a] px-3 py-1 text-[0.625rem] font-bold text-white dark:bg-blue-600">Retry</button>
      </div>
    )
  }
  if (!data) return null
  return render(data)
}

// ── Expenditure Activity ─────────────────────────────────────

function fmtMonth(ym) {
  if (!ym) return ""
  try { return new Date(ym + "-01T00:00:00").toLocaleDateString("en-IN", { month: "short", year: "2-digit" }) } catch { return ym }
}

function fmtDay(d) {
  if (!d) return "N/A"
  try { return new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) } catch { return d }
}

function ActivitySection({ activity }) {
  const [showTx, setShowTx] = useState(false)
  const hasData = activity.transaction_count > 0

  if (!hasData) {
    return (
      <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
        <h4 className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Expenditure Activity</h4>
        <p className="mt-2 text-xs font-semibold text-gray-500">No expenditure activity recorded in the current dataset.</p>
        <p className="mt-1 text-[0.625rem] text-gray-400">The project is not automatically classified as delayed on this basis.</p>
      </div>
    )
  }

  const monthly = activity.monthly_activity || []
  const maxAmt = Math.max(...monthly.map(m => m.amount), 1)
  const statusChip =
    activity.activity_status === "extended_gap" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
    : activity.activity_status === "long_gap" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
    : "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
  const statusLabel =
    activity.activity_status === "extended_gap" ? "⚠ Extended activity gap"
    : activity.activity_status === "long_gap" ? "⚠ Long activity gap"
    : "✓ No significant gap detected"

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-[#171b20]">
      <div className="mb-3 flex items-center justify-between">
        <h4 className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Expenditure Activity</h4>
        <span className={`rounded px-2 py-0.5 text-[0.625rem] font-bold ${statusChip}`}>{statusLabel}</span>
      </div>

      {/* Summary card */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div><p className="text-[0.5625rem] font-bold uppercase text-gray-400">Total</p><p className="font-mono text-sm font-bold">{formatMoney(activity.total_expenditure)}</p></div>
        <div><p className="text-[0.5625rem] font-bold uppercase text-gray-400">Transactions</p><p className="font-mono text-sm font-bold">{activity.transaction_count}</p></div>
        <div><p className="text-[0.5625rem] font-bold uppercase text-gray-400">First activity</p><p className="text-[0.6875rem] font-semibold">{fmtDay(activity.first_activity_date)}</p></div>
        <div><p className="text-[0.5625rem] font-bold uppercase text-gray-400">Latest activity</p><p className="text-[0.6875rem] font-semibold">{fmtDay(activity.latest_activity_date)}</p></div>
      </div>
      {activity.days_since_latest_activity != null && (
        <p className="mt-2 text-[0.625rem] text-gray-500 dark:text-gray-400">{activity.days_since_latest_activity} days since latest recorded expenditure activity.</p>
      )}

      {/* Monthly chart — CSS bars, gaps visible as empty slots */}
      {monthly.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Monthly recorded expenditure</p>
          <div className="flex items-end gap-1 overflow-x-auto pb-1" style={{ height: 88 }}>
            {monthly.map((m) => (
              <div key={m.month} className="group relative flex min-w-[26px] flex-1 flex-col items-center justify-end" style={{ height: "100%" }}>
                <div
                  className="w-full rounded-t bg-purple-500/80 transition-all group-hover:bg-purple-600 dark:bg-purple-600/70"
                  style={{ height: `${Math.max(4, (m.amount / maxAmt) * 70)}px` }}
                />
                <span className="mt-0.5 text-[0.5rem] text-gray-400">{fmtMonth(m.month)}</span>
                <div className="pointer-events-none absolute bottom-full z-10 mb-1 hidden whitespace-nowrap rounded bg-gray-900 px-2 py-1 text-[0.5625rem] text-white shadow-soft group-hover:block dark:bg-gray-700">
                  <div className="font-bold">{fmtMonth(m.month)}</div>
                  <div>{formatMoney(m.amount)}</div>
                  <div>{m.transaction_count} record{m.transaction_count > 1 ? "s" : ""}</div>
                  {m.payment_statuses?.length > 0 && <div className="text-gray-300">{m.payment_statuses.join(", ")}</div>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Activity indicators (calculated) */}
      {activity.indicators?.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-[0.5625rem] font-bold uppercase tracking-wider text-gray-400">Activity Indicators</p>
          <div className="space-y-1.5">
            {activity.indicators.map((ind, i) => (
              <div key={i} className={`flex items-start gap-2 rounded-lg p-2 ${ind.severity === "high" ? "bg-red-50 dark:bg-red-950/20" : "bg-amber-50 dark:bg-amber-950/20"}`}>
                <span className={`mt-0.5 h-1.5 w-1.5 flex-shrink-0 rounded-full ${ind.severity === "high" ? "bg-red-500" : "bg-amber-500"}`} />
                <div>
                  <p className="text-[0.6875rem] font-bold text-gray-700 dark:text-gray-200">⚠ {ind.indicator}</p>
                  <p className="text-[0.625rem] text-gray-500 dark:text-gray-400">{ind.detail}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Largest gaps summary */}
      {activity.largest_gap && (
        <p className="mt-2 text-[0.625rem] text-gray-500 dark:text-gray-400">
          Largest recorded gap: {activity.largest_gap.gap_days} days ({fmtDay(activity.largest_gap.start_date)} → {fmtDay(activity.largest_gap.end_date)}).
        </p>
      )}

      {/* View expenditure history */}
      <button onClick={() => setShowTx(!showTx)} className="mt-3 text-[0.625rem] font-bold text-blue-600 hover:underline dark:text-blue-400">
        {showTx ? "Hide expenditure history" : `View expenditure history (${Math.min(activity.transaction_count, activity.transactions_returned)}${activity.transactions_truncated ? "+" : ""})`}
      </button>
          {showTx && (
        <div className="mt-2 max-h-52 space-y-1 overflow-y-auto rounded-lg border border-gray-200 p-2 dark:border-gray-700">
          {activity.transactions_truncated && <p className="text-[0.5625rem] italic text-gray-400">Showing the {activity.transactions_returned} most recent of {activity.transaction_count} recorded transactions.</p>}
          <ActivityTransactions transactions={activity.transactions} />
        </div>
      )}

      {/* Data note */}
      <p className="mt-3 rounded-lg bg-gray-50 p-2 text-[0.5625rem] leading-relaxed text-gray-400 dark:bg-gray-800/60">{activity.disclaimer}</p>
      <p className="mt-1 text-[0.5625rem] text-gray-400">Gap thresholds: ≥{activity.thresholds.long_gap_days}d long gap · ≥{activity.thresholds.extended_gap_days}d extended gap · Match: {activity.match_confidence === "mp_verified" ? "MP-verified" : activity.match_confidence === "key_exact" ? "exact key" : "n/a"}</p>
    </div>
  )
}

// Renders transaction rows from the activity payload.
function ActivityTransactions({ transactions }) {
  const txs = transactions || []
  if (txs.length === 0) return <p className="text-[0.625rem] text-gray-400">No transaction rows to display.</p>
  return (
    <>
      {txs.map((t, i) => (
        <div key={i} className="flex items-center justify-between rounded bg-gray-50 px-2 py-1 dark:bg-gray-800/60">
          <span className="text-[0.625rem] text-gray-500">{fmtDay(t.date)}</span>
          <span className="font-mono text-[0.625rem] font-bold">{formatMoney(t.amount)}</span>
          {t.payment_status && <span className="text-[0.5625rem] text-gray-400">{t.payment_status}</span>}
        </div>
      ))}
    </>
  )
}

function InfoGrid({ items }) {
  return (
    <div className="grid grid-cols-2 gap-2">
      {items.map((item) => (
        <div key={item.label} className="rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-[#171b20]">
          <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">{item.label}</p>
          <p className="mt-0.5 text-sm font-semibold">{item.value}</p>
        </div>
      ))}
    </div>
  )
}

function MetricCard({ label, value, color }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
      <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">{label}</p>
      <p className={`mt-1 font-mono text-lg font-bold text-${color}-600 dark:text-${color}-400`}>{value}</p>
    </div>
  )
}

function ProgressCard({ completion }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
      <div className="flex items-center justify-between">
        <span className="text-sm font-bold">Physical Completion</span>
        <span className="font-mono text-lg font-bold text-blue-600 dark:text-blue-400">{completion}%</span>
      </div>
      <div className="mt-2 h-4 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
        <div className={`h-full rounded-full transition-all duration-500 ${
          completion >= 80 ? "bg-green-500" : completion >= 40 ? "bg-blue-600" : completion > 0 ? "bg-amber-500" : "bg-red-500"
        }`} style={{ width: `${Math.min(100, completion)}%` }} />
      </div>
      <p className="mt-2 text-xs text-gray-500">
        {completion >= 80 ? "On track" : completion >= 40 ? "Moderate progress" : completion > 0 ? "Early stage" : "No progress reported"}
      </p>
    </div>
  )
}

export default ProjectDetail
