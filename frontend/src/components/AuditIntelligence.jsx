import { useEffect, useState } from "react"
import {
  getEvidenceGaps,
  getAnomalyExplorer,
  getPeerBenchmark,
  simulateRisk,
  getAuditCase,
  getInvestigation,
  startInvestigation,
  updateInvestigation,
  updateEvidenceItem,
  updateFindingReview,
  addFindingAction,
  getInvestigationReport,
} from "../services/api"
import { formatMoney } from "../utils/format"
import { parseReasons } from "../utils/reasons"
import { useAuth } from "../context/AuthContext"
import { saveAuditCase, createMyInvestigation } from "../services/api"

/* ═══════════════════════════════════════════════════════════
   MPLADS Audit Intelligence components
   Every value rendered here comes from the backend, which reads it
   from the project record / risk results. Nothing is estimated locally.
   ═══════════════════════════════════════════════════════════ */

const TIER_STYLE = {
  P1: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  P2: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300",
  P3: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  P4: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
}

const SEVERITY_STYLE = {
  high: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  medium: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  low: "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300",
  unknown: "bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400",
}

const STATUS_STYLE = {
  available: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  reported_zero: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  partial: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  not_available: "bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400",
}

function Card({ children, className = "" }) {
  return (
    <div className={`rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-[#000000] ${className}`}>
      {children}
    </div>
  )
}

function SectionTitle({ children, right }) {
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <h4 className="text-[0.875rem] font-bold uppercase tracking-wider text-gray-400">{children}</h4>
      {right}
    </div>
  )
}

function Chip({ children, className = "" }) {
  return <span className={`rounded px-2 py-0.5 text-[0.8125rem] font-bold ${className}`}>{children}</span>
}

function Spinner({ label }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-[#000000]">
      <div className="h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-r-transparent" />
      <span className="text-[0.9375rem] text-gray-400">{label}</span>
    </div>
  )
}

/* ═══════════════ 9. Digital Audit Card ═══════════════ */

export function DigitalAuditCard({ detail, onStartInvestigation, investigation }) {
  const proj = detail?.project
  const risk = detail?.risk
  const audit = detail?.audit_intelligence
  if (!proj || !risk) return null

  const priority = audit?.priority
  const exposure = audit?.financial_exposure
  const evidence = audit?.evidence_gap
  const actions = audit?.recommended_actions || []

  const reasons = parseReasons(risk.reasons)

  const sanctioned = Number(proj.sanctioned_amount || 0)
  const expenditure = Number(proj.expenditure || 0)
  const completion = Number(proj.completion_percentage || 0)
  const utilization = sanctioned > 0 ? (expenditure / sanctioned) * 100 : null
  const overspend = utilization !== null && utilization > 100

  const scoreColor =
    risk.risk_level === "High" ? "text-red-600 dark:text-red-400"
    : risk.risk_level === "Medium" ? "text-amber-600 dark:text-amber-400"
    : "text-green-600 dark:text-green-400"

  return (
    <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
      {/* RISK */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Risk</p>
          <div className="flex items-baseline gap-1.5">
            <span className={`font-mono text-3xl font-bold ${scoreColor}`}>{risk.risk_score}</span>
            <span className="font-mono text-sm text-gray-400">/ 100</span>
          </div>
          <span className={`mt-1 inline-block rounded px-2 py-0.5 text-[0.8125rem] font-bold ${
            risk.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
            : risk.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
            : "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
          }`}>{(risk.risk_level || "None").toUpperCase()} RISK</span>
        </div>
        <div className="text-right">
          <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">How urgent?</p>
          {priority ? (
            <>
              <span className={`mt-0.5 inline-block rounded px-2 py-1 text-[0.8125rem] font-bold ${TIER_STYLE[priority.tier] || TIER_STYLE.P4}`} title={(priority.explanation || []).join(" ")}>
                {priority.tier} — {priority.tier_label}
              </span>
              <p className="mt-1 font-mono text-[0.8125rem] text-gray-400">
                Audit priority {priority.audit_priority_score}/100
              </p>
            </>
          ) : (
            <span className="text-[0.9375rem] text-gray-400">Not available</span>
          )}
        </div>
      </div>

      <div className="mt-3 h-2 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
        <div className={`h-full rounded-full ${
          risk.risk_level === "High" ? "bg-red-500" : risk.risk_level === "Medium" ? "bg-amber-500" : "bg-green-500"
        }`} style={{ width: `${Math.min(100, Number(risk.risk_score) || 0)}%` }} />
      </div>

      {/* WHY */}
      <div className="mt-4">
        <SectionTitle>Why?</SectionTitle>
        {reasons.length > 0 ? (
          <ul className="space-y-1">
            {reasons.slice(0, 4).map((r, i) => (
              <li key={i} className="flex items-start gap-1.5 text-[1rem] leading-relaxed text-gray-700 dark:text-gray-300">
                <span className="mt-0.5 text-red-500">→</span><span>{r}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[0.9375rem] text-gray-400">No risk conditions are currently triggered for this project.</p>
        )}
        {utilization !== null && (
          <p className="mt-1.5 font-mono text-[0.875rem] text-gray-500 dark:text-gray-400">
            Sanctioned {formatMoney(sanctioned)} · Spent {formatMoney(expenditure)} · Utilization {utilization.toFixed(1)}% · Progress {completion}%
          </p>
        )}
      </div>

      {/* WHAT IS MISSING */}
      <div className="mt-4">
        <SectionTitle>What is missing?</SectionTitle>
        {evidence ? (
          <>
            <p className="text-[1rem] leading-relaxed text-gray-600 dark:text-gray-300">{evidence.summary}</p>
            {evidence.missing_labels?.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {evidence.missing_labels.slice(0, 5).map((l) => (
                  <Chip key={l} className="bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">{l}</Chip>
                ))}
                {evidence.missing_labels.length > 5 && (
                  <Chip className="bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400">
                    +{evidence.missing_labels.length - 5} more
                  </Chip>
                )}
              </div>
            )}
          </>
        ) : (
          <p className="text-[0.9375rem] text-gray-400">Evidence summary unavailable.</p>
        )}
      </div>

      {/* WHAT SHOULD I DO */}
      <div className="mt-4">
        <SectionTitle>What should I do?</SectionTitle>
        {actions.length > 0 ? (
          <ul className="space-y-1">
            {actions.slice(0, 4).map((a, i) => (
              <li key={i} className="flex items-start gap-1.5 text-[1rem] leading-relaxed text-gray-700 dark:text-gray-300">
                <span className="mt-0.5 text-blue-600 dark:text-blue-400">•</span><span>{a}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[0.9375rem] text-gray-400">No specific verification action was generated.</p>
        )}
      </div>

      {/* Exposure + review level */}
      <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2">
        {exposure && (
          <div className="rounded-lg border border-gray-200 bg-white p-2.5 dark:border-gray-700 dark:bg-[#0d090b]">
            <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Funds under review</p>
            <p className="font-mono text-sm font-bold">{exposure.funds_under_review_display}</p>
            {overspend && exposure.overspend_display && (
              <p className="mt-0.5 text-[0.875rem] font-semibold text-red-600 dark:text-red-400">
                Overspend {exposure.overspend_display}
              </p>
            )}
          </div>
        )}
        {audit?.review_level && (
          <div className="rounded-lg border border-gray-200 bg-white p-2.5 dark:border-gray-700 dark:bg-[#0d090b]">
            <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Suggested review level</p>
            <p className="text-[1rem] font-semibold text-gray-800 dark:text-gray-200">{audit.review_level.level}</p>
            <p className="mt-0.5 text-[0.8125rem] text-gray-400">{audit.review_level.basis}</p>
          </div>
        )}
      </div>

      {/* START INVESTIGATION */}
      <button
        onClick={onStartInvestigation}
        className="mt-4 w-full rounded-lg bg-[#c8102e] px-4 py-2.5 text-[0.9375rem] font-bold text-white transition hover:bg-[#a50f29] dark:bg-blue-600 dark:hover:bg-blue-500"
      >
        {investigation?.investigation
          ? `Open Investigation (${investigation.progress_pct}% complete)`
          : "START INVESTIGATION"}
      </button>

      <p className="mt-2 text-[0.8125rem] leading-relaxed text-gray-400">
        Priority, exposure and actions are system-generated indicators derived from the recorded project values.
        They identify work for review and do not establish delay, irregularity or wrongdoing.
      </p>
    </div>
  )
}

/* ═══════════════ 2. Evidence gap panel ═══════════════ */

export function EvidenceGapPanel({ projectId }) {
  const [gaps, setGaps] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  useEffect(() => {
    let alive = true
    setLoading(true); setError("")
    getEvidenceGaps(projectId)
      .then((d) => alive && setGaps(d))
      .catch(() => alive && setError("Could not load the evidence check."))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [projectId])

  if (loading) return <Spinner label="Checking evidence availability..." />
  if (error) return <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-[0.9375rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">{error}</div>
  if (!gaps) return null
  const conf = gaps.confidence
  return (
    <div className="space-y-3">
      <Card>
        <SectionTitle right={<Chip className={
          conf === "High" ? "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
          : conf === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
          : "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
        }>DATA CONFIDENCE: {conf}</Chip>}>
          What evidence is missing?
        </SectionTitle>
        <p className="text-[1rem] leading-relaxed text-gray-600 dark:text-gray-300">{gaps.summary}</p>
        {gaps.confidence_basis?.length > 0 && (
          <ul className="mt-2 space-y-1">
            {gaps.confidence_basis.map((b, i) => (
              <li key={i} className="text-[0.9375rem] leading-relaxed text-gray-500 dark:text-gray-400">• {b}</li>
            ))}
          </ul>
        )}
        <p className="mt-2 text-[0.8125rem] text-gray-400">
          Risk score is calculated from the available data and is not reduced when evidence is missing.
        </p>
      </Card>

      <Card>
        <SectionTitle>Evidence availability</SectionTitle>
        <div className="divide-y divide-gray-100 dark:divide-gray-700/60">
          {gaps.items.map((item) => (
            <div key={item.key} className="py-2.5 first:pt-0 last:pb-0">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-[1rem] font-semibold text-gray-800 dark:text-gray-200">{item.label}</p>
                  {item.value && <p className="font-mono text-[1rem] text-gray-500 dark:text-gray-400">{item.value}</p>}
                  {item.note && <p className="mt-0.5 text-[0.8125rem] text-gray-400">{item.note}</p>}
                </div>
                <Chip className={`flex-shrink-0 ${STATUS_STYLE[item.status] || STATUS_STYLE.not_available}`}>
                  {item.status_label}
                </Chip>
              </div>
              {item.source && <p className="mt-0.5 text-[0.8125rem] text-gray-400">source: {item.source}</p>}
            </div>
          ))}
        </div>
      </Card>

      {gaps.limitations?.length > 0 && (
        <Card className="border-amber-200 bg-amber-50/50 dark:border-amber-900/40 dark:bg-amber-950/20">
          <SectionTitle>Assessment limitations</SectionTitle>
          <ul className="space-y-1.5">
            {gaps.limitations.map((l, i) => (
              <li key={i} className="flex items-start gap-1.5 text-[0.9375rem] leading-relaxed text-amber-700 dark:text-amber-300">
                <span className="mt-0.5">⚠</span><span>{l}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}

/* ═══════════════ 1. Investigation workspace ═══════════════ */

const ITEM_STATUSES = ["Pending", "Verified", "Discrepancy Found", "Not Applicable"]
const ITEM_STATUS_SHORT = {
  "Pending": "Pending",
  "Verified": "Verified",
  "Discrepancy Found": "Discrepancy",
  "Not Applicable": "N/A",
}
const ITEM_STATUS_STYLE = {
  "Pending": "border-gray-300 text-gray-600 dark:border-gray-600 dark:text-gray-300",
  "Verified": "border-green-400 bg-green-50 text-green-700 dark:border-green-700 dark:bg-green-950/30 dark:text-green-300",
  "Discrepancy Found": "border-red-400 bg-red-50 text-red-700 dark:border-red-700 dark:bg-red-950/30 dark:text-red-300",
  "Not Applicable": "border-gray-400 bg-gray-100 text-gray-600 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300",
}

const FINDING_STATUS_STYLE = {
  "Open": "border-gray-300 text-gray-600 dark:border-gray-600 dark:text-gray-300",
  "Under review": "border-blue-400 bg-blue-50 text-blue-700 dark:border-blue-700 dark:bg-blue-950/30 dark:text-blue-300",
  "Clarification requested": "border-amber-400 bg-amber-50 text-amber-700 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-300",
  "Evidence received": "border-cyan-400 bg-cyan-50 text-cyan-700 dark:border-cyan-700 dark:bg-cyan-950/30 dark:text-cyan-300",
  "Verified": "border-green-400 bg-green-50 text-green-700 dark:border-green-700 dark:bg-green-950/30 dark:text-green-300",
  "Not substantiated": "border-slate-400 bg-slate-50 text-slate-600 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-300",
  "Resolved": "border-emerald-400 bg-emerald-50 text-emerald-700 dark:border-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300",
}

const FINDING_CLASS_STYLE = {
  confirmed_data_inconsistency: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  potential_concern: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  missing_information: "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300",
}

const WORKSPACE_TABS = [
  ["overview", "Overview / final review"],
  ["dossier", "Project dossier"],
  ["findings", "Findings"],
  ["evidence", "Evidence"],
  ["actions", "Actions"],
  ["history", "History"],
]

const dateShort = (value) => value ? String(value).replace("T", " ").slice(0, 16) : "—"
const safeUrl = (value) => /^https?:\/\//i.test(String(value || "")) ? value : null

function DossierField({ label, value }) {
  const shown = value === 0 ? "0" : (value ?? "Not available in the source dataset")
  return (
    <div>
      <p className="text-[0.75rem] font-bold uppercase tracking-wider text-gray-400">{label}</p>
      <p className="mt-0.5 text-[0.9375rem] text-gray-800 dark:text-gray-200">{shown}</p>
    </div>
  )
}

function EvidenceRecords({ records = [] }) {
  if (!records.length) return <p className="text-[0.875rem] text-gray-400">No record references were returned.</p>
  return (
    <div className="space-y-1.5">
      {records.map((record, index) => (
        <div key={`${record.reference || "record"}-${index}`} className="rounded border border-gray-200 bg-gray-50 px-2.5 py-2 dark:border-gray-700 dark:bg-gray-900/40">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[0.7rem] font-bold uppercase text-gray-500 dark:bg-gray-700 dark:text-gray-300">{record.type || "record"}</span>
            <code className="text-[0.8rem] font-semibold text-blue-700 dark:text-blue-300">{record.reference || "reference unavailable"}</code>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
            {Object.entries(record.fields || {}).map(([key, value]) => (
              <span key={key} className="text-[0.8rem] text-gray-500 dark:text-gray-400">
                <span className="font-semibold">{key}:</span> {Array.isArray(value) ? value.join("; ") : String(value ?? "—")}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

export function InvestigationWorkspace({ projectId, onWorkspaceChange }) {
  const { user, loading: authLoading, hasRole } = useAuth()
  const canInvestigate = !!user && hasRole("analyst")
  const [ws, setWs] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [note, setNote] = useState("")
  const [savedNote, setSavedNote] = useState(false)
  const [tab, setTab] = useState("overview")
  const [editingKey, setEditingKey] = useState(null)
  const [drafts, setDrafts] = useState({})
  const [reportBusy, setReportBusy] = useState(false)

  const apply = (payload) => {
    setWs(payload)
    if (onWorkspaceChange) onWorkspaceChange(payload)
  }

  useEffect(() => {
    if (!canInvestigate) {
      setLoading(false)
      setWs(null)
      return undefined
    }
    let alive = true
    setLoading(true)
    setError("")
    getInvestigation(projectId)
      .then((data) => {
        if (!alive) return
        apply(data)
        setNote(data?.investigation?.note || "")
      })
      .catch((e) => alive && setError(e?.message || "Could not load the investigation workspace."))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, canInvestigate])

  const handleStart = async () => {
    setBusy(true); setError("")
    try {
      const data = await startInvestigation(projectId)
      apply(data)
      setNote(data?.investigation?.note || "")
    } catch (e) { setError(e?.message || "Could not start the investigation.") }
    finally { setBusy(false) }
  }

  const handleItem = async (itemKey, status) => {
    setBusy(true); setError("")
    try {
      const data = await updateEvidenceItem(projectId, itemKey, status)
      apply(data)
    } catch (e) { setError(e?.message || "Could not update the checklist item.") }
    finally { setBusy(false) }
  }

  const handleStatus = async (status) => {
    setBusy(true); setError("")
    try {
      const data = await updateInvestigation(projectId, { status })
      apply(data)
    } catch (e) { setError(e?.message || "Could not update the investigation status.") }
    finally { setBusy(false) }
  }

  const handleSaveNote = async () => {
    setBusy(true); setError(""); setSavedNote(false)
    try {
      const data = await updateInvestigation(projectId, { note })
      apply(data)
      setSavedNote(true)
    } catch (e) { setError(e?.message || "Could not save the note.") }
    finally { setBusy(false) }
  }

  const openReview = (finding) => {
    setEditingKey(finding.key)
    setDrafts((prev) => ({
      ...prev,
      [finding.key]: {
        status: finding.review?.status || finding.status || "Open",
        comment: finding.review?.comment || "",
        evidence_reference: finding.review?.evidence_reference || "",
        document_name: finding.review?.document_name || "",
        document_url: finding.review?.document_url || "",
        resolution: finding.review?.resolution || "",
      },
    }))
  }

  const setDraft = (findingKey, field, value) => {
    setDrafts((prev) => ({
      ...prev,
      [findingKey]: { ...(prev[findingKey] || {}), [field]: value },
    }))
  }

  const saveReview = async (findingKey) => {
    setBusy(true); setError("")
    try {
      const draft = drafts[findingKey] || {}
      const data = await updateFindingReview(projectId, findingKey, {
        status: draft.status || undefined,
        comment: draft.comment || undefined,
        evidence_reference: draft.evidence_reference || undefined,
        document_name: draft.document_name || undefined,
        document_url: draft.document_url || undefined,
        resolution: draft.resolution || undefined,
      })
      apply(data)
      setEditingKey(null)
    } catch (e) { setError(e?.message || "Could not save the finding review.") }
    finally { setBusy(false) }
  }

  const addAction = async (findingKey) => {
    setBusy(true); setError("")
    try {
      const data = await addFindingAction(projectId, findingKey)
      apply(data)
    } catch (e) { setError(e?.message || "Could not add the action item.") }
    finally { setBusy(false) }
  }

  const askAssistant = () => {
    window.dispatchEvent(new CustomEvent("assistant-project-context", { detail: { projectId } }))
    window.dispatchEvent(new CustomEvent("assistant:open"))
  }

  const downloadReport = async () => {
    setReportBusy(true); setError("")
    try {
      const report = await getInvestigationReport(projectId)
      const blob = new Blob([report.body || ""], { type: "text/plain;charset=utf-8" })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = report.filename || `MPLADS-investigation-${projectId}.txt`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
    } catch (e) { setError(e?.message || "Could not generate the investigation report.") }
    finally { setReportBusy(false) }
  }

  if (authLoading || loading) return <Spinner label="Loading investigation workspace..." />

  if (!canInvestigate) {
    return (
      <Card>
        <SectionTitle>Investigation workspace</SectionTitle>
        <p className="text-[1rem] leading-relaxed text-gray-600 dark:text-gray-300">
          Investigation records are restricted to signed-in Analysts, Auditors and Administrators.
          Sign in with an authorized account to open the project dossier, evidence findings and review workflow.
        </p>
      </Card>
    )
  }

  const inv = ws?.investigation

  if (!inv) {
    return (
      <Card>
        <SectionTitle>AI Audit Investigation Workspace</SectionTitle>
        <p className="text-[1rem] leading-relaxed text-gray-600 dark:text-gray-300">
          An investigation converts this project's detected anomalies into an evidence-linked dossier that an auditor can
          review. The findings are generated from the recorded project, payment, risk and verification records — they are
          not allegations.
        </p>
        {error && <p className="mt-2 text-[0.9375rem] font-semibold text-red-600 dark:text-red-400">{error}</p>}
        <button
          onClick={handleStart}
          disabled={busy}
          className="mt-3 w-full rounded-lg bg-[#c8102e] px-4 py-2.5 text-[0.9375rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
        >
          {busy ? "Starting..." : "START INVESTIGATION"}
        </button>
      </Card>
    )
  }

  const dossier = ws.dossier || {}
  const project = dossier.project || {}
  const financial = dossier.financial || {}
  const timeline = dossier.timeline || {}
  const risk = dossier.risk || {}
  const evidence = dossier.evidence || {}
  const dq = dossier.data_quality || {}
  const similar = dossier.similar_projects || {}
  const findings = ws.findings || []
  const unresolved = findings.filter((finding) => !["Verified", "Not substantiated", "Resolved"].includes(finding.status))
  const workflow = ws.workflow || []
  const progress = ws.progress_pct || 0
  const transactions = financial.transactions || []

  return (
    <div className="space-y-3">
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-[0.9375rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">
          {error}
        </div>
      )}

      <Card>
        <SectionTitle
          right={<Chip className="bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300">{inv.status}</Chip>}
        >
          Investigation workspace
        </SectionTitle>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-[1.05rem] font-bold text-gray-900 dark:text-white">{project.project_name || `Project #${projectId}`}</p>
            <p className="mt-0.5 text-[0.875rem] text-gray-500 dark:text-gray-400">
              {[project.house, project.mp, project.constituency, project.state].filter(Boolean).join(" · ") || "Project location not recorded"}
            </p>
            <p className="mt-1 text-[0.8rem] text-gray-400">Investigation #{inv.id} · opened {dateShort(inv.created_at)}</p>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <button
              onClick={askAssistant}
              className="rounded-lg border border-blue-300 bg-blue-50 px-3 py-1.5 text-[0.8125rem] font-bold text-blue-700 transition hover:bg-blue-100 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-300"
            >
              Ask AI about this investigation
            </button>
            <button
              onClick={downloadReport}
              disabled={reportBusy}
              className="rounded-lg border border-gray-300 px-3 py-1.5 text-[0.8125rem] font-bold text-gray-700 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
            >
              {reportBusy ? "Preparing..." : "Download preliminary report"}
            </button>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
            <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Findings</p>
            <p className="font-mono text-[1.1rem] font-bold text-gray-900 dark:text-white">{findings.length}</p>
          </div>
          <div className="rounded-lg bg-amber-50 p-2 dark:bg-amber-950/20">
            <p className="text-[0.7rem] font-bold uppercase tracking-wider text-amber-600 dark:text-amber-400">Unresolved</p>
            <p className="font-mono text-[1.1rem] font-bold text-amber-700 dark:text-amber-300">{unresolved.length}</p>
          </div>
          <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
            <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Evidence items</p>
            <p className="font-mono text-[1.1rem] font-bold text-gray-900 dark:text-white">{ws.pending_items ?? 0} pending</p>
          </div>
          <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
            <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">Checklist progress</p>
            <p className="font-mono text-[1.1rem] font-bold text-gray-900 dark:text-white">{progress}%</p>
          </div>
        </div>
      </Card>

      <div className="flex gap-1 overflow-x-auto border-b border-gray-200 pb-1 dark:border-gray-700">
        {WORKSPACE_TABS.map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`whitespace-nowrap rounded-t-lg px-2.5 py-2 text-[0.8125rem] font-bold transition ${
              tab === key
                ? "bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300"
                : "text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-gray-800"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <div className="space-y-3">
          <Card className="border-blue-200 bg-blue-50/40 dark:border-blue-900/40 dark:bg-blue-950/10">
            <SectionTitle right={<Chip className="bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300">PRELIMINARY</Chip>}>
              Investigation summary
            </SectionTitle>
            <p className="whitespace-pre-line text-[0.9375rem] leading-relaxed text-gray-700 dark:text-gray-300">{ws.summary?.text || "Summary unavailable."}</p>
          </Card>

          <Card>
            <SectionTitle>Final review / investigation status</SectionTitle>
            <div className="flex flex-wrap items-center gap-1.5">
              {workflow.map((status, index) => (
                <div key={status} className="flex items-center gap-1.5">
                  <button
                    onClick={() => handleStatus(status)}
                    disabled={busy}
                    className={`rounded-lg border px-2.5 py-1.5 text-[0.8125rem] font-bold transition disabled:opacity-60 ${
                      inv.status === status
                        ? "border-blue-500 bg-blue-50 text-blue-700 dark:border-blue-700 dark:bg-blue-950/40 dark:text-blue-300"
                        : "border-gray-200 text-gray-500 hover:border-gray-300 dark:border-gray-700 dark:text-gray-400"
                    }`}
                  >
                    {status}
                  </button>
                  {index < workflow.length - 1 && <span className="text-[0.8125rem] text-gray-300 dark:text-gray-600">→</span>}
                </div>
              ))}
            </div>
            <div className="mt-3">
              <div className="flex items-center justify-between text-[0.8125rem] font-bold text-gray-400">
                <span>Evidence checklist progress</span>
                <span className="font-mono">{progress}%</span>
              </div>
              <div className="mt-1 h-2 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                <div className="h-full rounded-full bg-blue-600 transition-all duration-300" style={{ width: `${progress}%` }} />
              </div>
              <p className="mt-1 text-[0.875rem] text-gray-500 dark:text-gray-400">
                {ws.resolved_items} of {ws.total_items} checklist items resolved · {ws.discrepancies} discrepancy note(s)
              </p>
            </div>
            <label className="mt-3 block text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400" htmlFor="investigation-note">Investigator note</label>
            <textarea
              id="investigation-note"
              value={note}
              onChange={(e) => { setNote(e.target.value); setSavedNote(false) }}
              rows={3}
              placeholder="Record verification steps, clarifications and the basis for the final review."
              className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-[0.9375rem] text-gray-900 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#000000] dark:text-white"
            />
            <div className="mt-2 flex items-center gap-2">
              <button
                onClick={handleSaveNote}
                disabled={busy}
                className="rounded-lg border border-gray-300 px-3 py-1.5 text-[0.8125rem] font-bold text-gray-700 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
              >
                {busy ? "Saving..." : "Save note"}
              </button>
              {savedNote && <span className="text-[0.8125rem] font-semibold text-green-600 dark:text-green-400">Saved</span>}
              <span className="ml-auto font-mono text-[0.75rem] text-gray-400">Risk at start {inv.risk_score_at_start ?? "—"}</span>
            </div>
          </Card>
        </div>
      )}

      {tab === "dossier" && (
        <div className="space-y-3">
          <Card>
            <SectionTitle>Project and location</SectionTitle>
            <div className="grid gap-3 sm:grid-cols-2">
              <DossierField label="Work" value={project.project_name} />
              <DossierField label="Project ID" value={`#${project.project_id ?? projectId}`} />
              <DossierField label="Description" value={project.description} />
              <DossierField label="MP / House" value={[project.mp, project.house].filter(Boolean).join(" · ")} />
              <DossierField label="State / district / constituency" value={[project.state, project.district, project.constituency].filter(Boolean).join(" · ")} />
              <DossierField label="Category / financial year" value={[project.project_type, project.fy].filter(Boolean).join(" · ")} />
              <DossierField label="Status / completion" value={`${project.status || "Not recorded"} · ${project.completion_percentage ?? "Not recorded"}%`} />
              <DossierField label="Recommendation date" value={timeline.recommendation_date_display} />
              <DossierField label="Approximate start" value={timeline.approx_start_date_display} />
              <DossierField label="Sanction reference" value={dossier.sanction?.sanction_reference || dossier.sanction?.note} />
            </div>
            <p className="mt-2 text-[0.8rem] leading-relaxed text-gray-400">{project.description_note || dossier.sanction?.note}</p>
          </Card>

          <Card>
            <SectionTitle right={<Chip className="bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">{financial.transaction_count || 0} linked rows</Chip>}>
              Financial position and complete linked expenditure
            </SectionTitle>
            <div className="grid gap-2 sm:grid-cols-4">
              <DossierField label="Sanctioned" value={financial.sanctioned_display} />
              <DossierField label="Linked expenditure" value={financial.expenditure_display} />
              <DossierField label="Utilization" value={financial.utilization_pct != null ? `${financial.utilization_pct}%` : null} />
              <DossierField label="Transaction sum shown" value={financial.transaction_total_display} />
            </div>
            {financial.reconciliation_note && <p className="mt-2 rounded bg-amber-50 p-2 text-[0.8rem] text-amber-700 dark:bg-amber-950/20 dark:text-amber-300">{financial.reconciliation_note}</p>}
            <p className="mt-2 text-[0.8rem] text-gray-500 dark:text-gray-400">
              Work-key linkage: {financial.work_key_rows || 1} catalogue row(s) · {financial.has_expenditure ? "payment ledger linked" : "no linked payment ledger"}.
              {" "}Payment states: {Object.entries(financial.payment_status_summary || {}).map(([key, value]) => `${key} (${value})`).join(", ") || "not recorded"}.
            </p>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-[0.8125rem]">
                <thead className="border-b border-gray-200 text-[0.7rem] uppercase tracking-wider text-gray-400 dark:border-gray-700">
                  <tr><th className="py-1.5 pr-2">Record</th><th className="py-1.5 pr-2">Date</th><th className="py-1.5 pr-2">Amount</th><th className="py-1.5 pr-2">Vendor</th><th className="py-1.5">Payment status</th></tr>
                </thead>
                <tbody>
                  {transactions.map((tx) => (
                    <tr key={tx.reference || tx.id} className="border-b border-gray-100 last:border-0 dark:border-gray-800">
                      <td className="py-1.5 pr-2 font-mono text-[0.75rem] text-blue-700 dark:text-blue-300">{tx.reference || `expenditures.id=${tx.id}`}</td>
                      <td className="py-1.5 pr-2 text-gray-600 dark:text-gray-300">{tx.date || "—"}</td>
                      <td className="py-1.5 pr-2 font-semibold text-gray-800 dark:text-gray-200">{tx.amount_display || formatMoney(tx.amount || 0)}</td>
                      <td className="py-1.5 pr-2 text-gray-600 dark:text-gray-300">{tx.vendor || "Not recorded"}</td>
                      <td className="py-1.5 text-gray-600 dark:text-gray-300">{tx.payment_status || "Not recorded"}</td>
                    </tr>
                  ))}
                  {!transactions.length && <tr><td colSpan={5} className="py-2 text-gray-400">No dated linked transaction rows are available.</td></tr>}
                </tbody>
              </table>
            </div>
            {financial.transactions_truncated && <p className="mt-1 text-[0.8rem] text-amber-600 dark:text-amber-400">This view is bounded to 1,000 rows; the project-level total remains the aggregate above.</p>}
          </Card>

          <div className="grid gap-3 sm:grid-cols-2">
            <Card>
              <SectionTitle>Risk and ML explanations</SectionTitle>
              <div className="flex flex-wrap gap-2">
                <Chip className="bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">Risk {risk.score ?? "—"}</Chip>
                <Chip className="bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">{risk.level || "Unranked"}</Chip>
                <Chip className={risk.ml_anomaly ? SEVERITY_STYLE.medium : STATUS_STYLE.not_available}>{risk.ml_anomaly ? "ML outlier" : "No ML flag"}</Chip>
              </div>
              {risk.reasons?.length > 0 ? <ul className="mt-2 space-y-1 text-[0.8125rem] text-gray-600 dark:text-gray-300">{risk.reasons.map((reason, i) => <li key={i}>• {reason}</li>)}</ul> : <p className="mt-2 text-[0.8125rem] text-gray-400">No rule indicators recorded.</p>}
              {risk.shap_explanations?.length > 0 && (
                <div className="mt-2 border-t border-gray-100 pt-2 dark:border-gray-800">
                  <p className="text-[0.7rem] font-bold uppercase tracking-wider text-gray-400">SHAP model factors</p>
                  {risk.shap_explanations.slice(0, 6).map((factor, i) => <p key={i} className="text-[0.8125rem] text-gray-500 dark:text-gray-400">• {factor.label || factor.feature || "factor"}: {factor.impact ?? factor.value ?? "—"}</p>)}
                </div>
              )}
            </Card>
            <Card>
              <SectionTitle>Vendors and similar projects</SectionTitle>
              <p className="text-[0.8125rem] text-gray-500 dark:text-gray-400">Vendors linked to this work</p>
              {dossier.vendors?.length > 0 ? <ul className="mt-1 space-y-1 text-[0.875rem] text-gray-700 dark:text-gray-200">{dossier.vendors.map((vendor, i) => <li key={i}>• {vendor.vendor || vendor.name || String(vendor)}</li>)}</ul> : <p className="text-[0.875rem] text-gray-400">No vendor name is recorded on the linked payment rows.</p>}
              <p className="mt-3 text-[0.8125rem] text-gray-500 dark:text-gray-400">Peer group · {similar.peer_count ?? 0} comparable projects ({similar.scope || "state"} scope)</p>
              {(similar.interpretations || []).slice(0, 2).map((line, i) => <p key={i} className="mt-1 text-[0.8125rem] text-amber-700 dark:text-amber-300">{line}</p>)}
              {(similar.metrics || []).slice(0, 4).map((metric) => (
                <p key={metric.metric} className="mt-0.5 text-[0.8rem] text-gray-500 dark:text-gray-400">
                  {metric.label}: project {metric.project_value} {metric.unit}, peer median {metric.peer_median} {metric.unit}
                </p>
              ))}
              {(similar.comparable_projects || []).slice(0, 3).map((peer) => (
                <p key={peer.id} className="mt-1 text-[0.8rem] text-gray-600 dark:text-gray-300">
                  #{peer.id} {peer.project_name} · {peer.constituency || "—"} · sanction {peer.sanctioned_amount != null ? formatMoney(peer.sanctioned_amount) : "—"} · risk {peer.risk_score ?? "—"}
                </p>
              ))}
              {!similar.comparable_projects?.length && <p className="mt-1 text-[0.8125rem] text-gray-400">No comparable project rows were returned for this peer group.</p>}
            </Card>
          </div>

          <Card>
            <SectionTitle>Data quality</SectionTitle>
            <div className="grid gap-1 sm:grid-cols-2">
              {(dq.checks || []).slice(0, 10).map((check, i) => <p key={i} className="text-[0.8125rem] text-gray-600 dark:text-gray-300">{check.ok ? "✓" : "⚠"} {check.text}</p>)}
            </div>
            {dq.warnings?.length > 0 && <p className="mt-2 text-[0.8125rem] text-amber-700 dark:text-amber-300">{dq.warnings.slice(0, 4).join(" · ")}</p>}
            <p className="mt-1 text-[0.8rem] text-gray-400">{dq.limitation || "Data-quality assessment is limited to the available fields."}</p>
          </Card>
        </div>
      )}

      {tab === "findings" && (
        <Card>
          <SectionTitle right={<Chip className="bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">{unresolved.length} unresolved</Chip>}>
            Evidence-linked findings
          </SectionTitle>
          <p className="mb-3 text-[0.8125rem] leading-relaxed text-gray-500 dark:text-gray-400">
            Findings are recomputed from the current database records. A risk indicator is a review trigger; only a human reviewer can verify or close a finding.
          </p>
          <div className="space-y-3">
            {findings.map((finding) => (
              <div key={finding.key} className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-[1rem] font-bold text-gray-900 dark:text-white">{finding.title}</p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      <Chip className={FINDING_CLASS_STYLE[finding.classification] || FINDING_CLASS_STYLE.missing_information}>{finding.classification_label}</Chip>
                      <Chip className={SEVERITY_STYLE[finding.severity] || SEVERITY_STYLE.unknown}>{String(finding.severity || "unknown").toUpperCase()}</Chip>
                      <Chip className={FINDING_STATUS_STYLE[finding.status] || FINDING_STATUS_STYLE.Open}>{finding.status}</Chip>
                    </div>
                  </div>
                  <button onClick={() => openReview(finding)} disabled={busy} className="rounded-lg border border-gray-300 px-2.5 py-1 text-[0.75rem] font-bold text-gray-700 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800">
                    Review finding
                  </button>
                </div>
                <p className="mt-2 text-[0.875rem] leading-relaxed text-gray-600 dark:text-gray-300">{finding.explanation}</p>
                <div className="mt-2 grid gap-1 text-[0.8rem] text-gray-500 dark:text-gray-400 sm:grid-cols-2">
                  <p><span className="font-bold">Source:</span> {finding.source}</p>
                  <p><span className="font-bold">Suggested verification:</span> {finding.suggested_action}</p>
                  {Object.keys(finding.amounts || {}).length > 0 && <p><span className="font-bold">Amounts:</span> {Object.entries(finding.amounts).map(([k, v]) => `${k}: ${typeof v === "number" ? formatMoney(v) : v}`).join(" · ")}</p>}
                  {Object.keys(finding.dates || {}).length > 0 && <p><span className="font-bold">Dates:</span> {Object.entries(finding.dates).map(([k, v]) => `${k}: ${v || "—"}`).join(" · ")}</p>}
                </div>
                {finding.limitations?.length > 0 && <p className="mt-1 text-[0.8rem] leading-relaxed text-amber-700 dark:text-amber-300">Limitations: {finding.limitations.join(" ")}</p>}
                <details className="mt-2 rounded border border-gray-200 bg-gray-50 p-2 dark:border-gray-700 dark:bg-gray-900/40">
                  <summary className="cursor-pointer text-[0.8rem] font-bold text-gray-600 dark:text-gray-300">Supporting records ({finding.evidence?.records?.length || 0})</summary>
                  <div className="mt-2"><EvidenceRecords records={finding.evidence?.records || []} /></div>
                </details>
                {finding.review?.reviewer_name && <p className="mt-1 text-[0.75rem] text-gray-400">Last reviewed by {finding.review.reviewer_name} · {dateShort(finding.review.updated_at)}</p>}
                {(finding.review?.comment || finding.review?.evidence_reference || finding.review?.document_name || finding.review?.resolution) && (
                  <div className="mt-1 rounded border border-gray-100 bg-gray-50 p-2 text-[0.8rem] text-gray-600 dark:border-gray-800 dark:bg-gray-900/40 dark:text-gray-300">
                    {finding.review?.comment && <p><span className="font-bold">Review comment:</span> {finding.review.comment}</p>}
                    {finding.review?.evidence_reference && <p><span className="font-bold">Evidence reference:</span> {finding.review.evidence_reference}</p>}
                    {finding.review?.document_name && <p><span className="font-bold">Document:</span> {finding.review.document_name}{safeUrl(finding.review.document_url) ? <> · <a href={safeUrl(finding.review.document_url)} target="_blank" rel="noreferrer" className="text-blue-700 underline dark:text-blue-300">open</a></> : null}</p>}
                    {finding.review?.resolution && <p><span className="font-bold">Resolution:</span> {finding.review.resolution}</p>}
                  </div>
                )}
                {editingKey === finding.key && (
                  <div className="mt-2 rounded-lg border border-blue-200 bg-blue-50/40 p-2 dark:border-blue-900/40 dark:bg-blue-950/10">
                    <div className="grid gap-2 sm:grid-cols-2">
                      <label className="text-[0.75rem] font-bold uppercase tracking-wider text-gray-400">Review status
                        <select value={drafts[finding.key]?.status || "Open"} onChange={(e) => setDraft(finding.key, "status", e.target.value)} className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-[0.8125rem] font-normal normal-case text-gray-800 dark:border-gray-600 dark:bg-[#000000] dark:text-gray-200">
                          {(ws.finding_status_options || []).map((status) => <option key={status} value={status}>{status}</option>)}
                        </select>
                      </label>
                      <label className="text-[0.75rem] font-bold uppercase tracking-wider text-gray-400">Evidence reference
                        <input value={drafts[finding.key]?.evidence_reference || ""} onChange={(e) => setDraft(finding.key, "evidence_reference", e.target.value)} placeholder="e.g. verification_reports.id=..." className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-[0.8125rem] font-normal normal-case text-gray-800 dark:border-gray-600 dark:bg-[#000000] dark:text-gray-200" />
                      </label>
                    </div>
                    <label className="mt-2 block text-[0.75rem] font-bold uppercase tracking-wider text-gray-400">Review comment
                      <textarea value={drafts[finding.key]?.comment || ""} onChange={(e) => setDraft(finding.key, "comment", e.target.value)} rows={2} className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-[0.8125rem] font-normal normal-case text-gray-800 dark:border-gray-600 dark:bg-[#000000] dark:text-gray-200" />
                    </label>
                    <div className="mt-2 grid gap-2 sm:grid-cols-2">
                      <label className="text-[0.75rem] font-bold uppercase tracking-wider text-gray-400">Supporting document name
                        <input value={drafts[finding.key]?.document_name || ""} onChange={(e) => setDraft(finding.key, "document_name", e.target.value)} placeholder="Measurement book / clarification letter" className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-[0.8125rem] font-normal normal-case text-gray-800 dark:border-gray-600 dark:bg-[#000000] dark:text-gray-200" />
                      </label>
                      <label className="text-[0.75rem] font-bold uppercase tracking-wider text-gray-400">Document URL
                        <input value={drafts[finding.key]?.document_url || ""} onChange={(e) => setDraft(finding.key, "document_url", e.target.value)} placeholder="https://..." className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-[0.8125rem] font-normal normal-case text-gray-800 dark:border-gray-600 dark:bg-[#000000] dark:text-gray-200" />
                      </label>
                    </div>
                    <label className="mt-2 block text-[0.75rem] font-bold uppercase tracking-wider text-gray-400">Resolution explanation (required for Verified / Not substantiated / Resolved)
                      <textarea value={drafts[finding.key]?.resolution || ""} onChange={(e) => setDraft(finding.key, "resolution", e.target.value)} rows={2} className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-[0.8125rem] font-normal normal-case text-gray-800 dark:border-gray-600 dark:bg-[#000000] dark:text-gray-200" />
                    </label>
                    <div className="mt-2 flex gap-1.5">
                      <button onClick={() => saveReview(finding.key)} disabled={busy} className="rounded bg-blue-600 px-3 py-1.5 text-[0.75rem] font-bold text-white disabled:opacity-50">{busy ? "Saving..." : "Save review"}</button>
                      <button onClick={() => setEditingKey(null)} disabled={busy} className="rounded border border-gray-300 px-3 py-1.5 text-[0.75rem] font-bold text-gray-600 dark:border-gray-600 dark:text-gray-300">Cancel</button>
                    </div>
                  </div>
                )}
                {!finding.recommendations?.length && (
                  <button onClick={() => addAction(finding.key)} disabled={busy || (ws.recommendations || []).find((r) => r.finding_key === finding.key)?.added} className="mt-2 rounded border border-gray-300 px-2.5 py-1 text-[0.75rem] font-bold text-gray-600 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                    {(ws.recommendations || []).find((r) => r.finding_key === finding.key)?.added ? "Added as action item" : "Add suggested action"}
                  </button>
                )}
              </div>
            ))}
            {!findings.length && <p className="text-[0.9375rem] text-gray-500">No findings were generated from the current records.</p>}
          </div>
        </Card>
      )}

      {tab === "evidence" && (
        <div className="space-y-3">
          <Card>
            <SectionTitle right={<Chip className="bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">{ws.total_items || 0} items</Chip>}>
              Evidence checklist and action items
            </SectionTitle>
            <div className="space-y-2.5">
              {(ws.items || []).map((item) => (
                <div key={item.item_key} className="rounded-lg border border-gray-200 p-2.5 dark:border-gray-700">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-[0.9375rem] font-semibold text-gray-800 dark:text-gray-200">{item.label}</p>
                      <p className="mt-0.5 text-[0.875rem] leading-relaxed text-gray-500 dark:text-gray-400">{item.rationale}</p>
                    </div>
                    <Chip className={`flex-shrink-0 ${SEVERITY_STYLE[item.severity] || SEVERITY_STYLE.low}`}>{(item.severity || "").toUpperCase()}</Chip>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {ITEM_STATUSES.map((status) => (
                      <button key={status} onClick={() => handleItem(item.item_key, status)} disabled={busy} className={`rounded border px-2 py-1 text-[0.75rem] font-bold transition disabled:opacity-60 ${item.status === status ? ITEM_STATUS_STYLE[status] : "border-gray-200 text-gray-400 hover:border-gray-300 dark:border-gray-700 dark:text-gray-500"}`}>
                        {ITEM_STATUS_SHORT[status]}
                      </button>
                    ))}
                  </div>
                  <p className="mt-1 text-[0.75rem] text-gray-400">{item.category} · updated {dateShort(item.updated_at)}</p>
                </div>
              ))}
            </div>
          </Card>
          <Card>
            <SectionTitle>Ground evidence and documents</SectionTitle>
            <div className="grid gap-2 sm:grid-cols-3">
              <DossierField label="Verification reports" value={evidence.verification_count ?? 0} />
              <DossierField label="Photo records" value={evidence.photo_records ?? 0} />
              <DossierField label="Documents" value={(evidence.documents || []).length} />
            </div>
            {evidence.verification_reports?.length > 0 ? (
              <div className="mt-3 space-y-2">
                {evidence.verification_reports.map((report) => (
                  <div key={report.id} className="rounded border border-gray-200 p-2 text-[0.8125rem] dark:border-gray-700">
                    <p className="font-bold text-gray-700 dark:text-gray-200">{report.reference} · {dateShort(report.date)} · {report.status || report.review_status || "recorded"}</p>
                    <p className="text-gray-600 dark:text-gray-300">{report.note || "No note recorded."}</p>
                    <p className="text-gray-400">Reporter: {report.reporter_name || "not recorded"} · photo: {report.photo_hash_id || "none"}</p>
                  </div>
                ))}
              </div>
            ) : <p className="mt-2 text-[0.875rem] text-amber-700 dark:text-amber-300">No ground-verification reports are linked to this project.</p>}
            <p className="mt-2 text-[0.8rem] leading-relaxed text-gray-400">{evidence.document_note || "No project-level document store is available in the source dataset."}</p>
          </Card>
        </div>
      )}

      {tab === "actions" && (
        <Card>
          <SectionTitle right={<Chip className="bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">{ws.recommendations?.length || 0} recommendations</Chip>}>
            Intelligent recommendations
          </SectionTitle>
          <p className="mb-3 text-[0.8125rem] leading-relaxed text-gray-500 dark:text-gray-400">
            Each recommendation explains the finding behind it. Adding one creates a pending checklist action item; it does not execute any external action.
          </p>
          <div className="space-y-2">
            {(ws.recommendations || []).map((recommendation) => (
              <div key={recommendation.key} className="rounded-lg border border-gray-200 p-2.5 dark:border-gray-700">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-[0.9375rem] font-bold text-gray-800 dark:text-gray-200">{recommendation.title}</p>
                    <p className="mt-0.5 text-[0.8rem] text-gray-400">From: {recommendation.finding_title} · {recommendation.severity} priority</p>
                  </div>
                  <button onClick={() => addAction(recommendation.finding_key)} disabled={busy || recommendation.added} className="rounded border border-gray-300 px-2.5 py-1 text-[0.75rem] font-bold text-gray-600 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                    {recommendation.added ? "Action item added" : "Add as action item"}
                  </button>
                </div>
                <p className="mt-1 text-[0.8125rem] leading-relaxed text-gray-500 dark:text-gray-400">Why: {recommendation.why}</p>
              </div>
            ))}
            {!ws.recommendations?.length && <p className="text-[0.9375rem] text-gray-500">No recommendations are currently generated.</p>}
          </div>
        </Card>
      )}

      {tab === "history" && (
        <Card>
          <SectionTitle>Investigation history</SectionTitle>
          <div className="space-y-2">
            {(ws.history || []).map((event, index) => (
              <div key={event.id ?? `legacy-${index}`} className="border-l-2 border-gray-200 pl-2.5 dark:border-gray-700">
                <p className="text-[0.875rem] font-bold text-gray-700 dark:text-gray-200">{event.label || event.event}</p>
                <p className="text-[0.8rem] text-gray-500 dark:text-gray-400">{event.detail}</p>
                <p className="text-[0.75rem] text-gray-400">{dateShort(event.created_at)} · {event.actor_name || "System"} ({event.actor_role || "system"})</p>
              </div>
            ))}
          </div>
        </Card>
      )}

      <p className="text-[0.75rem] leading-relaxed text-gray-400">
        Preliminary analysis from recorded data. Risk-model and rule indicators are review triggers, not confirmed allegations.
        The assistant cannot change investigation records; only an authorized reviewer can set finding status.
      </p>
    </div>
  )
}

/* ═══════════════ 4. Anomaly explorer ═══════════════ */

/* ═══════════════ Predictive insights ═══════════════
   Derived ONLY from the project's recorded values and the existing risk
   engine outputs. Every estimate is labelled as an analytical indication,
   never a confirmed fact. */

export function PredictiveInsightsPanel({ detail }) {
  const proj = detail?.project
  const risk = detail?.risk
  const sanctioned = Number(proj.sanctioned_amount || 0)
  const expenditure = Number(proj.expenditure || 0)
  const completion = Number(proj.completion_percentage || 0)
  const utilization = sanctioned > 0 ? (expenditure / sanctioned) * 100 : 0
  const status = (proj.status || "").toLowerCase()

  // ── Predictive estimates (analytical only) ──
  const predictions = []
  if (expenditure > 0 && completion < 50) {
    const severity = utilization >= 80 ? "High" : "Medium"
    predictions.push({
      icon: "⏱", label: "Delay risk",
      level: severity,
      text: `${severity} — ${formatMoney(expenditure)} already spent with only ${completion}% physical progress. Funds are flowing faster than recorded work.`,
    })
  }
  if (sanctioned > 0 && expenditure > sanctioned) {
    predictions.push({
      icon: "📈", label: "Cost escalation risk",
      level: "High",
      text: `Expenditure is ${(utilization).toFixed(0)}% of sanction. If the pattern continues, further cost escalation beyond the sanctioned amount is likely without a revised approval.`,
    })
  }
  if (utilization > 0 && Math.abs(utilization - completion) > 25 && completion < 90) {
    predictions.push({
      icon: "⚖", label: "Progress / expenditure mismatch risk",
      level: Math.abs(utilization - completion) > 50 ? "High" : "Medium",
      text: `Financial utilization (${utilization.toFixed(0)}%) and physical progress (${completion}%) diverge by ${Math.abs(utilization - completion).toFixed(0)} points — one of the two records may be outdated.`,
    })
  }
  if (status === "completed" && completion < 90) {
    predictions.push({
      icon: "🏁", label: "Status-accuracy risk",
      level: "Medium",
      text: `Work is marked completed at ${completion}% physical progress — the completion claim may need re-validation on the ground.`,
    })
  }
  if (predictions.length === 0) {
    predictions.push({ icon: "🟢", label: "No early-warning indicators", level: "Low", text: "Current recorded values show no predictive risk pattern on available fields." })
  }

  const levelCls = (l) => l === "High"
    ? "bg-red-100 text-red-700 dark:bg-red-950/60 dark:text-red-300"
    : l === "Medium"
      ? "bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-300"
      : "bg-green-100 text-green-700 dark:bg-green-950/60 dark:text-green-300"

  return (
    <div className="space-y-3">
      {/* Predictive / early-warning estimates */}
      <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#000000]">
        <h4 className="mb-1 text-xs font-bold uppercase tracking-wider text-gray-400">Predictive Insights</h4>
        <p className="mb-3 text-[0.625rem] text-gray-400 dark:text-gray-500">
          🤖 AI/analytical estimates derived from this project's recorded values — indications for review, not confirmed facts.
        </p>
        <div className="space-y-1.5">
          {predictions.map((p) => (
            <div key={p.label} className="rounded-lg bg-white px-3 py-2.5 dark:bg-[#0d090b]">
              <div className="flex items-center gap-2">
                <span className="text-sm">{p.icon}</span>
                <p className="text-xs font-bold">{p.label}</p>
                <span className={`ml-auto rounded px-1.5 py-0.5 text-[0.5625rem] font-bold ${levelCls(p.level)}`}>{p.level}</span>
              </div>
              <p className="mt-1 pl-6 text-[0.6875rem] leading-snug text-gray-500 dark:text-gray-400">{p.text}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Payment-level data availability — honest dataset boundary */}
      <div className="rounded-lg border border-dashed border-gray-300 bg-gray-50/60 p-3 dark:border-gray-600 dark:bg-gray-900/40">
        <p className="text-xs font-bold text-gray-500 dark:text-gray-400">💳 Payment-level data</p>
        <p className="mt-0.5 text-[0.6875rem] text-gray-400">
          Payment-level data unavailable in source dataset. Aggregated expenditure and payment-status summaries come from the monthly activity record.
        </p>
      </div>
    </div>
  )
}

export function AnomalyExplorerPanel({ projectId }) {
  const [explorer, setExplorer] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  useEffect(() => {
    let alive = true
    setLoading(true); setError("")
    getAnomalyExplorer(projectId)
      .then((d) => alive && setExplorer(d))
      .catch(() => alive && setError("Could not load the anomaly explorer."))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [projectId])

  if (loading) return <Spinner label="Breaking down the anomaly dimensions..." />
  if (error) return <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-[0.9375rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">{error}</div>
  if (!explorer) return null
  return (
    <div className="space-y-3">
      <Card>
        <SectionTitle right={<Chip className="bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">{explorer.highest_severity?.toUpperCase()} severity</Chip>}>
          Anomaly Explorer
        </SectionTitle>
        <p className="text-[1rem] leading-relaxed text-gray-600 dark:text-gray-300">{explorer.summary}</p>
      </Card>

      {explorer.dimensions.map((d) => (
        <Card key={d.dimension}>
          <SectionTitle
            right={
              <div className="flex gap-1">
                <Chip className={SEVERITY_STYLE[d.severity] || SEVERITY_STYLE.unknown}>{(d.severity || "").toUpperCase()}</Chip>
                <Chip className="bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400">{d.basis}</Chip>
              </div>
            }
          >
            {d.title}
          </SectionTitle>

          <p className={`text-[1rem] font-semibold ${
            d.status === "flagged" ? "text-gray-800 dark:text-gray-200" : "text-gray-500 dark:text-gray-400"
          }`}>{d.finding}</p>

          {/* Observed */}
          <div className="mt-2 rounded-lg bg-gray-50 p-2 dark:bg-[#120b0e]">
            <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">What we observed</p>
            <div className="mt-1 grid grid-cols-1 gap-1 sm:grid-cols-2">
              {d.observed.map((o, i) => (
                <div key={i} className="flex items-baseline justify-between gap-2">
                  <span className="text-[0.9375rem] leading-relaxed text-gray-500 dark:text-gray-400">{o.label}</span>
                  <span className="font-mono text-[1rem] font-bold text-gray-800 dark:text-gray-200">{o.value}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="mt-2">
            <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Why it matters</p>
            <p className="mt-0.5 text-[0.9375rem] leading-relaxed text-gray-600 dark:text-gray-300">{d.why_it_matters}</p>
          </div>

          <div className="mt-2">
            <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">What should be verified</p>
            <ul className="mt-0.5 space-y-1">
              {d.what_to_verify.map((v, i) => (
                <li key={i} className="flex items-start gap-1.5 text-[0.9375rem] leading-relaxed text-gray-600 dark:text-gray-300">
                  <span className="mt-0.5 text-blue-600 dark:text-blue-400">→</span><span>{v}</span>
                </li>
              ))}
            </ul>
          </div>

          {d.data_caveat && (
            <p className="mt-2 rounded-lg bg-amber-50 p-2 text-[0.9375rem] text-amber-700 dark:bg-amber-950/20 dark:text-amber-300">
              ⚠ {d.data_caveat}
            </p>
          )}
        </Card>
      ))}
    </div>
  )
}

/* ═══════════════ 6. What-if simulator ═══════════════ */

export function WhatIfSimulator({ detail }) {
  const proj = detail?.project
  const [form, setForm] = useState({ sanctioned_amount: "", expenditure: "", completion_percentage: "" })
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    if (!proj) return
    setForm({
      sanctioned_amount: String(proj.sanctioned_amount ?? 0),
      expenditure: String(proj.expenditure ?? 0),
      completion_percentage: String(proj.completion_percentage ?? 0),
    })
    setResult(null)
  }, [proj?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!proj) return null

  const run = async () => {
    setBusy(true); setError("")
    try {
      const payload = {}
      const s = parseFloat(form.sanctioned_amount)
      const e = parseFloat(form.expenditure)
      const c = parseFloat(form.completion_percentage)
      if (!Number.isNaN(s) && s !== Number(proj.sanctioned_amount || 0)) payload.sanctioned_amount = s
      if (!Number.isNaN(e) && e !== Number(proj.expenditure || 0)) payload.expenditure = e
      if (!Number.isNaN(c) && c !== Number(proj.completion_percentage || 0)) payload.completion_percentage = c
      if (Object.keys(payload).length === 0) {
        setError("Change at least one value to run a simulation.")
        setBusy(false)
        return
      }
      const data = await simulateRisk(proj.id, payload)
      setResult(data)
    } catch (err) {
      setError(err?.message || "Simulation failed.")
    } finally { setBusy(false) }
  }

  const reset = () => {
    setForm({
      sanctioned_amount: String(proj.sanctioned_amount ?? 0),
      expenditure: String(proj.expenditure ?? 0),
      completion_percentage: String(proj.completion_percentage ?? 0),
    })
    setResult(null)
    setError("")
  }

  const field = (key, label) => (
    <div>
      <label className="block text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">{label}</label>
      <input
        type="number"
        value={form[key]}
        onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 font-mono text-[1rem] text-gray-900 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#000000] dark:text-white"
      />
    </div>
  )

  return (
    <div className="space-y-3">
      <Card className="border-purple-200 bg-purple-50/50 dark:border-purple-900/40 dark:bg-purple-950/20">
        <SectionTitle>What would change the risk?</SectionTitle>
        <p className="text-[0.9375rem] leading-relaxed text-purple-700 dark:text-purple-300">
          Adjust the recorded values below to see which risk conditions would no longer be triggered. The simulated
          figures are recalculated with the same rules and model used for the stored risk score.
        </p>
      </Card>

      <Card>
        <SectionTitle>Simulation inputs</SectionTitle>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {field("sanctioned_amount", "Sanctioned amount (₹)")}
          {field("expenditure", "Expenditure (₹)")}
          {field("completion_percentage", "Physical progress (%)")}
        </div>
        <div className="mt-3 flex items-center gap-2">
          <button
            onClick={run}
            disabled={busy}
            className="rounded-lg bg-[#c8102e] px-4 py-2 text-[0.9375rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
          >
            {busy ? "Simulating..." : "Run simulation"}
          </button>
          <button
            onClick={reset}
            disabled={busy}
            className="rounded-lg border border-gray-300 px-3 py-2 text-[0.875rem] font-bold text-gray-600 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
          >
            Reset
          </button>
        </div>
        {error && <p className="mt-2 text-[0.9375rem] font-semibold text-red-600 dark:text-red-400">{error}</p>}
      </Card>

      {result && (
        <>
          <Card className="border-red-300 bg-red-50 dark:border-red-800/60 dark:bg-red-950/30">
            <p className="text-[0.9375rem] font-bold text-red-700 dark:text-red-300">{result.notice}</p>
            <p className="mt-1 text-[0.8125rem] text-red-600 dark:text-red-400">
              The project record has not been modified. {result.method}
            </p>
          </Card>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <Card>
              <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Current (actual data)</p>
              <p className="mt-1 font-mono text-2xl font-bold">{result.current.risk_score}<span className="text-sm text-gray-400">/100</span></p>
              <p className="text-[1rem] font-semibold text-gray-600 dark:text-gray-300">{result.current.risk_level}</p>
              <p className="mt-1 font-mono text-[0.875rem] text-gray-500">
                {formatMoney(result.current.sanctioned_amount)} sanctioned · {formatMoney(result.current.expenditure)} spent · {result.current.completion_percentage}%
              </p>
              <ul className="mt-2 space-y-1">
                {result.current.conditions.map((c) => (
                  <li key={c} className="text-[0.9375rem] leading-relaxed text-gray-500 dark:text-gray-400">• {c}</li>
                ))}
              </ul>
            </Card>
            <Card className="border-purple-300 dark:border-purple-800/60">
              <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-purple-500">Simulated (not project data)</p>
              <p className="mt-1 font-mono text-2xl font-bold text-purple-700 dark:text-purple-300">
                {result.simulated.risk_score}<span className="text-sm text-gray-400">/100</span>
              </p>
              <p className="text-[1rem] font-semibold text-purple-700 dark:text-purple-300">{result.simulated.risk_level}</p>
              <p className="mt-1 font-mono text-[0.875rem] text-gray-500">
                {formatMoney(result.simulated.sanctioned_amount)} sanctioned · {formatMoney(result.simulated.expenditure)} spent · {result.simulated.completion_percentage}%
              </p>
              <ul className="mt-2 space-y-1">
                {result.simulated.conditions.length > 0
                  ? result.simulated.conditions.map((c) => (
                      <li key={c} className="text-[0.875rem] leading-relaxed text-purple-700 dark:text-purple-300">• {c}</li>
                    ))
                  : <li className="text-[0.9375rem] leading-relaxed text-gray-500 dark:text-gray-400">No risk conditions triggered.</li>}
              </ul>
            </Card>
          </div>

          <Card>
            <SectionTitle
              right={
                <Chip className={result.score_delta === 0 ? "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                  : result.score_delta < 0 ? "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
                  : "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"}>
                  {result.score_delta > 0 ? `+${result.score_delta}` : result.score_delta} points
                </Chip>
              }
            >
              Simulation result
            </SectionTitle>
            <ul className="space-y-1">
              {result.explanation.map((n, i) => (
                <li key={i} className="text-[0.9375rem] leading-relaxed text-gray-600 dark:text-gray-300">• {n}</li>
              ))}
            </ul>
            {result.changed_inputs?.length > 0 && (
              <p className="mt-2 font-mono text-[0.8125rem] text-gray-400">simulated inputs: {result.changed_inputs.join(" · ")}</p>
            )}
            {!result.model_available && (
              <p className="mt-1 text-[0.8125rem] text-gray-400">ML model not loaded in this process — rule-based conditions only.</p>
            )}
          </Card>
        </>
      )}
    </div>
  )
}

/* ═══════════════ 3. Peer benchmarking ═══════════════ */

const SCOPE_OPTIONS = [
  { value: "constituency", label: "Constituency" },
  { value: "state", label: "State" },
  { value: "national", label: "National" },
]

export function PeerBenchmarkPanel({ projectId, onOpenProject }) {
  const [scope, setScope] = useState("state")
  const [typeFilter, setTypeFilter] = useState("")
  const [band, setBand] = useState("default")
  const [statusFilter, setStatusFilter] = useState("")
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  useEffect(() => {
    let alive = true
    setLoading(true); setError("")
    const params = { scope, band }
    if (typeFilter) params.project_type = typeFilter
    if (statusFilter) params.status = statusFilter
    getPeerBenchmark(projectId, params)
      .then((d) => alive && setData(d))
      .catch(() => alive && setError("Could not load the peer comparison."))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [projectId, scope, typeFilter, band, statusFilter])

  const fmtValue = (v, unit) => {
    if (v === null || v === undefined) return "N/A"
    if (unit === "₹") return formatMoney(v)
    if (unit === "points") return String(v)
    return `${v}%`
  }
  const fmtMetric = (m) => fmtValue(m.project_value, m.unit)
  const fmtPeer = (v, unit) => fmtValue(v, unit)

  return (
    <div className="space-y-3">
      <Card>
        <SectionTitle
          right={
            data && (
              <Chip className={
                data.reliability === "good" ? "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
                : data.reliability === "limited" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                : "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
              }>
                {data.peer_count} comparable · {data.reliability}
              </Chip>
            )
          }
        >
          Compare with similar projects
        </SectionTitle>

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div>
            <label className="block text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Scope</label>
            <select value={scope} onChange={(e) => setScope(e.target.value)}
              className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-[0.875rem] text-gray-900 outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white">
              {SCOPE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Category</label>
            <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}
              className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-[0.875rem] text-gray-900 outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white">
              <option value="">Same category</option>
              <option value="all">All categories</option>
            </select>
          </div>
          <div>
            <label className="block text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Value band</label>
            <select value={band} onChange={(e) => setBand(e.target.value)}
              className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-[0.875rem] text-gray-900 outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white">
              <option value="narrow">Narrow (0.7–1.4×)</option>
              <option value="default">Similar (0.3–3×)</option>
              <option value="all">Any value</option>
            </select>
          </div>
          <div>
            <label className="block text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Status</label>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
              className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-[0.875rem] text-gray-900 outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-[#0d090b] dark:text-white">
              <option value="">All statuses</option>
              <option value="Ongoing">Ongoing</option>
              <option value="Completed">Completed</option>
            </select>
          </div>
        </div>
      </Card>

      {loading && <Spinner label="Comparing with similar projects..." />}
      {error && <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-[0.9375rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">{error}</div>}

      {data && !loading && (
        <>
          <Card>
            <SectionTitle>This project vs comparable projects</SectionTitle>
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <thead>
                  <tr className="border-b border-gray-200 dark:border-gray-700">
                    <th className="py-1.5 text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Metric</th>
                    <th className="py-1.5 text-right text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">This project</th>
                    <th className="py-1.5 text-right text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Peer median</th>
                    <th className="py-1.5 text-right text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">Peer average</th>
                  </tr>
                </thead>
                <tbody>
                  {data.metrics.map((m) => (
                    <tr key={m.metric} className="border-b border-gray-100 last:border-0 dark:border-gray-700/60">
                      <td className="py-1.5 text-[0.9375rem] leading-relaxed text-gray-600 dark:text-gray-300">{m.label}</td>
                      <td className="py-2 text-right font-mono text-[0.9375rem] font-bold">{fmtMetric(m)}</td>
                      <td className="py-2 text-right font-mono text-[0.9375rem] text-gray-500">{fmtPeer(m.peer_median, m.unit)}</td>
                      <td className="py-2 text-right font-mono text-[0.9375rem] text-gray-500">{fmtPeer(m.peer_average, m.unit)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {data.interpretations?.length > 0 && (
              <div className="mt-3 space-y-1.5">
                {data.interpretations.map((t, i) => (
                  <p key={i} className="rounded-lg bg-gray-50 p-2 text-[0.9375rem] leading-relaxed text-gray-600 dark:bg-[#120b0e] dark:text-gray-300">{t}</p>
                ))}
              </div>
            )}
            <p className="mt-2 text-[0.8125rem] leading-relaxed text-gray-400">{data.methodology}</p>
            <p className="mt-1 text-[0.8125rem] font-semibold text-gray-400">{data.labels?.no_claim}</p>
          </Card>

          {data.comparable_projects?.length > 0 && (
            <Card>
              <SectionTitle>Comparable projects ({data.scope})</SectionTitle>
              <div className="space-y-1.5">
                {data.comparable_projects.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => onOpenProject && onOpenProject(p.id)}
                    className="w-full rounded-lg border border-gray-200 p-2 text-left transition hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-[#0d090b]"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="font-mono text-[0.8125rem] font-bold text-gray-400">#{p.id}</p>
                        <p className="truncate text-[1rem] font-semibold text-gray-800 dark:text-gray-200">{p.project_name || "Unnamed"}</p>
                        <p className="text-[0.8125rem] text-gray-500">{p.state} — {p.constituency || "N/A"}</p>
                      </div>
                      <span className={`flex-shrink-0 rounded px-2 py-0.5 text-[0.8125rem] font-bold ${
                        p.risk_level === "High" ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                        : p.risk_level === "Medium" ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                        : "bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-300"
                      }`}>{p.risk_level || "N/A"}</span>
                    </div>
                    <div className="mt-1 grid grid-cols-4 gap-2">
                      <span className="font-mono text-[0.8125rem] text-gray-500">S {formatMoney(p.sanctioned_amount)}</span>
                      <span className="font-mono text-[0.8125rem] text-gray-500">E {formatMoney(p.expenditure)}</span>
                      <span className="font-mono text-[0.8125rem] text-gray-500">U {p.utilization_pct ?? "N/A"}%</span>
                      <span className="font-mono text-[0.8125rem] text-gray-500">P {p.completion_percentage}%</span>
                    </div>
                  </button>
                ))}
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  )
}

/* ═══════════════ 7 + 8. Audit case + escalation draft ═══════════════ */

export function AuditCasePanel({ projectId }) {
  const { user, hasRole } = useAuth()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  const [copied, setCopied] = useState(false)
  const [caseSaved, setCaseSaved] = useState(false)
  const [caseSaveMsg, setCaseSaveMsg] = useState("")
  const [invStarted, setInvStarted] = useState(false)
  const [invMsg, setInvMsg] = useState("")

  useEffect(() => { setData(null); setError("") }, [projectId])

  const generate = async () => {
    setLoading(true); setError("")
    try {
      const caseData = await getAuditCase(projectId)
      setData(caseData)
    } catch (e) {
      setError(e?.message || "Could not generate the audit case.")
    } finally { setLoading(false) }
  }

  const copy = async () => {
    if (!data?.escalation_draft?.body) return
    try {
      await navigator.clipboard.writeText(data.escalation_draft.body)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      setError("Clipboard not available in this browser.")
    }
  }

  const download = () => {
    if (!data) return
    const blob = new Blob([data.escalation_draft.body], { type: "text/plain;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `${data.case_id}-escalation-draft.txt`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const canManageCases = hasRole("auditor")
  const canInvestigate = hasRole("analyst")

  const handleSaveCase = async () => {
    setCaseSaveMsg("")
    try {
      await saveAuditCase(projectId)
      setCaseSaved(true)
      setCaseSaveMsg("Saved to My Audit Cases")
    } catch (e) { setCaseSaveMsg(e.message || "Could not save the case") }
  }

  const handleStartInvestigation = async () => {
    setInvMsg("")
    try {
      await createMyInvestigation(projectId)
      setInvStarted(true)
      setInvMsg("Investigation added to My Investigations")
    } catch (e) { setInvMsg(e.message || "Could not start the investigation") }
  }

  return (
    <div className="space-y-3">
      <Card>
        <SectionTitle>Automatic audit case generator</SectionTitle>
        <p className="text-[1rem] leading-relaxed text-gray-600 dark:text-gray-300">
          Generates a structured case from this project's recorded values, risk/anomaly results, peer comparison and
          missing evidence, together with a review-ready escalation draft.
        </p>
        <button
          onClick={generate}
          disabled={loading}
          className="mt-3 rounded-lg bg-[#c8102e] px-4 py-2 text-[0.9375rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
        >
          {loading ? "Generating..." : "CREATE AUDIT CASE"}
        </button>
        {error && <p className="mt-2 text-[0.9375rem] font-semibold text-red-600 dark:text-red-400">{error}</p>}
      </Card>

      {data && (
        <>
          <Card>
            <SectionTitle right={<Chip className={TIER_STYLE[data.priority.tier] || TIER_STYLE.P4}>{data.priority.tier} — {data.priority.tier_label}</Chip>}>
              {data.case_id}
            </SectionTitle>
            <div className="grid grid-cols-2 gap-2">
              {[
                ["Work", data.project.project_name || "Not recorded"],
                ["Location", `${data.project.constituency || "N/A"}, ${data.project.state || "N/A"}`],
                ["Category", data.project.project_type || "Not recorded"],
                ["Status", data.project.status || "Not recorded"],
                ["Sanctioned", data.financials.sanctioned_display],
                ["Expenditure", data.financials.expenditure_display],
                ["Reported progress", `${data.financials.completion_percentage}%`],
                ["Utilization", data.financials.utilization_pct !== null ? `${data.financials.utilization_pct}%` : "N/A"],
              ].map(([k, v]) => (
                <div key={k} className="rounded-lg bg-gray-50 p-2 dark:bg-[#120b0e]">
                  <p className="text-[0.8125rem] font-bold uppercase tracking-wider text-gray-400">{k}</p>
                  <p className="text-[1rem] font-semibold text-gray-800 dark:text-gray-200">{v}</p>
                </div>
              ))}
            </div>
            <p className="mt-2 font-mono text-[0.8125rem] text-gray-400">generated {data.generated_at?.replace("T", " ").slice(0, 19)}</p>
          </Card>

          <Card>
            <SectionTitle>Triggered anomalies</SectionTitle>
            <p className="text-[1rem] font-semibold text-gray-800 dark:text-gray-200">Main anomaly: {data.main_anomaly}</p>
            <ul className="mt-1.5 space-y-1">
              {data.risk.reasons?.map((r, i) => (
                <li key={i} className="flex items-start gap-1.5 text-[0.9375rem] leading-relaxed text-gray-600 dark:text-gray-300">
                  <span className="mt-0.5 text-red-500">•</span><span>{r}</span>
                </li>
              ))}
            </ul>
            {data.risk.ml_anomaly && (
              <p className="mt-1.5 text-[0.875rem] font-semibold text-purple-600 dark:text-purple-400">
                ✨ ML model finding: flagged as a statistical outlier.
              </p>
            )}
          </Card>

          <Card>
            <SectionTitle>Evidence available / missing</SectionTitle>
            <p className="text-[0.9375rem] leading-relaxed text-gray-500 dark:text-gray-400">Available — {
              data.evidence.items.filter((i) => i.status === "available").map((i) => i.label).join("; ") || "None beyond the project record."
            }</p>
            <p className="mt-1.5 text-[0.9375rem] leading-relaxed text-gray-500 dark:text-gray-400">Missing — {
              data.evidence.missing_items.map((i) => i.label).join("; ") || "None identified."
            }</p>
          </Card>

          <Card>
            <SectionTitle>Recommended verification actions</SectionTitle>
            <ul className="space-y-1">
              {data.recommended_verification_actions.map((a, i) => (
                <li key={i} className="flex items-start gap-1.5 text-[0.9375rem] leading-relaxed text-gray-600 dark:text-gray-300">
                  <span className="mt-0.5 text-blue-600 dark:text-blue-400">→</span><span>{a}</span>
                </li>
              ))}
            </ul>
          </Card>

          <Card className="border-amber-200 bg-amber-50/40 dark:border-amber-900/40 dark:bg-amber-950/20">
            <SectionTitle>Suggested review level</SectionTitle>
            <p className="text-[1rem] font-semibold text-amber-700 dark:text-amber-300">{data.review_level.level}</p>
            <p className="mt-0.5 text-[0.875rem] text-amber-600 dark:text-amber-400">{data.review_level.basis}</p>
          </Card>

          <Card>
            <SectionTitle
              right={<Chip className="bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300">{data.escalation_draft.status_label}</Chip>}
            >
              Report / escalate
            </SectionTitle>
            <p className="text-[1rem] font-semibold text-gray-800 dark:text-gray-200">{data.escalation_draft.subject}</p>
            <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-gray-50 p-2.5 font-sans text-[0.875rem] leading-relaxed text-gray-600 dark:bg-[#120b0e] dark:text-gray-300">
{data.escalation_draft.body}
            </pre>
            <div className="mt-2 flex flex-wrap gap-2">
              <button onClick={generate} className="rounded-lg border border-gray-300 px-3 py-1.5 text-[0.875rem] font-bold text-gray-700 transition hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800">Generate Report</button>
              <button onClick={copy} className="rounded-lg border border-gray-300 px-3 py-1.5 text-[0.875rem] font-bold text-gray-700 transition hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800">{copied ? "Copied ✓" : "Copy Report"}</button>
              <button onClick={download} className="rounded-lg border border-gray-300 px-3 py-1.5 text-[0.875rem] font-bold text-gray-700 transition hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800">Download Report</button>
              {user && canManageCases && (
                <button onClick={handleSaveCase} className={`rounded-lg px-3 py-1.5 text-[0.875rem] font-bold text-white transition ${caseSaved ? "bg-green-600 hover:bg-green-500" : "bg-[#c8102e] hover:bg-[#a50f29] dark:bg-blue-600 dark:hover:bg-blue-500"}`}>
                  {caseSaved ? "✓ Case saved" : "Save case"}
                </button>
              )}
              {user && canInvestigate && (
                <button onClick={handleStartInvestigation} disabled={invStarted} className={`rounded-lg px-3 py-1.5 text-[0.875rem] font-bold transition disabled:opacity-60 ${invStarted ? "border border-green-400 bg-green-50 text-green-700 dark:border-green-700 dark:bg-green-950/40 dark:text-green-300" : "border border-gray-300 text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"}`}>
                  {invStarted ? "✓ In My Investigations" : "Track investigation"}
                </button>
              )}
            </div>
            {(caseSaveMsg || invMsg) && (
              <p className={`mt-1.5 text-[0.8125rem] font-semibold ${
                (caseSaved || invStarted) ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400"
              }`}>
                {caseSaveMsg || invMsg}
              </p>
            )}
            {!user && (
              <p className="mt-1.5 text-[0.8125rem] text-gray-400">Sign in to save this case or track an investigation in your workspace.</p>
            )}
            <p className="mt-2 text-[0.8125rem] leading-relaxed text-gray-400">{data.escalation_draft.submission_note}</p>
          </Card>

          {data.disclaimers?.length > 0 && (
            <Card className="bg-gray-50 dark:bg-[#120b0e]">
              <ul className="space-y-1">
                {data.disclaimers.map((d, i) => (
                  <li key={i} className="text-[0.8125rem] leading-relaxed text-gray-400">• {d}</li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  )
}

/* Small re-exports used by the project drawer */
export { Card as AuditCard, SectionTitle as AuditSectionTitle, Spinner as AuditSpinner }
