import { useCallback, useEffect, useState } from "react"
import { useAuth } from "../context/AuthContext"
import { getInspectionBundle, recordAuditAction } from "../services/api"
import { formatMoney } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════════
   FORENSICS PANEL — shared AI Inspection & Forensic Summary content
   (SIH 26102). Rendered inside:
     • the AI Audit Command Center's inspection modal
     • the ProjectDetail drawer's "AI Forensics" tab
   Shows: overall risk dial, AI flags, computer-vision duplicate-photo
   analysis, cost estimation audit, ground verification reports, and the
   audit action controls. All figures come from the /ai/inspection bundle.
   ═══════════════════════════════════════════════════════════════════ */

const SEVERITY_STYLES = {
  Critical: "bg-red-100 text-red-700 border-red-200 dark:bg-red-950 dark:text-red-300 dark:border-red-900",
  Moderate: "bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-900",
  Low: "bg-green-100 text-green-700 border-green-200 dark:bg-green-950 dark:text-green-300 dark:border-green-900",
}

export function FlagPill({ label }) {
  const tone = label.includes("Photo") ? "bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300"
    : label.includes("Cost") ? "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300"
    : label.includes("GPS") ? "bg-cyan-100 text-cyan-700 dark:bg-cyan-950 dark:text-cyan-300"
    : label.includes("Dispute") || label.includes("Stalled") || label.includes("Delayed") ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
    : "bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300"
  return <span className={`inline-block whitespace-nowrap rounded px-1.5 py-0.5 text-[0.6875rem] font-semibold ${tone}`}>{label}</span>
}

function ScoreDial({ score, severity }) {
  const color = severity === "Critical" ? "#e14b4b" : severity === "Moderate" ? "#f59e0b" : "#269b71"
  const angle = Math.round((Math.min(100, Math.max(0, score)) / 100) * 360)
  return (
    <div className="flex flex-col items-center">
      <div
        className="flex h-20 w-20 items-center justify-center rounded-full"
        style={{ background: `conic-gradient(${color} ${angle}deg, #e8dadc ${angle}deg)` }}
        role="img"
        aria-label={`Risk index ${score} of 100`}
      >
        <div className="flex h-14 w-14 flex-col items-center justify-center rounded-full bg-white dark:bg-gray-900">
          <span className="text-lg font-bold" style={{ color }}>{score}</span>
        </div>
      </div>
      <span className="mt-1 text-[0.6875rem] font-semibold uppercase tracking-wide text-gray-500">Risk Index</span>
    </div>
  )
}

export default function ForensicsPanel({ projectId, onOpenProject }) {
  const { can } = useAuth()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [actionNote, setActionNote] = useState("")
  const [actionBusy, setActionBusy] = useState(false)
  const [actionDone, setActionDone] = useState(null)
  // Server-enforced: /ai/audit-action rejects callers without audit:action.
  // The UI hides the controls for other roles rather than disabling them.
  const canAct = can("audit:action")

  const load = useCallback(() => {
    getInspectionBundle(projectId)
      .then(setData)
      .catch((e) => setError(e.message))
  }, [projectId])

  useEffect(() => {
    setData(null); setError(null); setActionDone(null)
    load()
  }, [load])

  const runAction = async (action) => {
    setActionBusy(true)
    try {
      const res = await recordAuditAction({ projectId, action, note: actionNote })
      setActionDone(res.message)
      setActionNote("")
      load()
    } catch (e) {
      setActionDone(e.message)
    } finally {
      setActionBusy(false)
    }
  }

  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-center dark:border-red-900 dark:bg-red-950/40">
        <p className="text-sm text-red-600 dark:text-red-400">Unable to load AI forensics: {error}</p>
        <button onClick={load} className="mt-3 rounded-lg border border-red-300 px-4 py-2 text-sm font-semibold text-red-700 dark:border-red-800 dark:text-red-300">Retry</button>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="space-y-3">
        <div className="h-6 w-2/3 animate-pulse rounded bg-gray-200 dark:bg-gray-700" />
        <div className="h-24 animate-pulse rounded-xl bg-gray-100 dark:bg-gray-800" />
        <div className="h-24 animate-pulse rounded-xl bg-gray-100 dark:bg-gray-800" />
      </div>
    )
  }

  const { project, risk_index, severity, flags, cost_anomaly, duplicate_photos, verifications, actions } = data

  return (
    <div className="space-y-5">
      {/* Header: identity + prominent overall risk badge */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-bold leading-snug text-gray-900 dark:text-white">{project.title || `Project #${project.id}`}</h3>
          <p className="mt-1 text-xs text-gray-500">
            {project.district || "District unavailable"}, {project.state} · Sanctioned {formatMoney(project.sanctioned_amount)}
            {project.completion_percentage != null && <> · {project.completion_percentage}% complete</>}
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {flags.length === 0
              ? <span className="rounded bg-green-100 px-2 py-0.5 text-xs font-semibold text-green-700 dark:bg-green-950 dark:text-green-300">No AI flags</span>
              : flags.map((f) => <FlagPill key={f.type} label={f.label} />)}
          </div>
        </div>
        <ScoreDial score={risk_index} severity={severity} />
      </div>

      {/* Verified facts — straight from the project record (SOURCE DATA) */}
      <section className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
        <div className="flex items-center justify-between gap-2">
          <h4 className="flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-gray-500">
            <span aria-hidden>📋</span> Project Details
          </h4>
          <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[0.5625rem] font-bold uppercase tracking-wide text-gray-500 dark:bg-gray-800">Source data</span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
          <div>
            <p className="text-[0.5625rem] font-bold uppercase tracking-wide text-gray-400">Project ID</p>
            <p className="font-mono text-sm font-bold text-gray-800 dark:text-gray-100">#{project.id}</p>
          </div>
          <div>
            <p className="text-[0.5625rem] font-bold uppercase tracking-wide text-gray-400">Category</p>
            <p className="truncate text-sm text-gray-800 dark:text-gray-100" title={project.project_type || ""}>{project.project_type || "—"}</p>
          </div>
          <div>
            <p className="text-[0.5625rem] font-bold uppercase tracking-wide text-gray-400">Status</p>
            <p className={`text-sm font-semibold ${project.status === "Completed" ? "text-green-600 dark:text-green-400" : project.status === "Ongoing" ? "text-blue-600 dark:text-blue-400" : "text-amber-600 dark:text-amber-400"}`}>{project.status || "—"}</p>
          </div>
          <div>
            <p className="text-[0.5625rem] font-bold uppercase tracking-wide text-gray-400">Expenditure</p>
            <p className="text-sm text-gray-800 dark:text-gray-100">{formatMoney(project.expenditure || 0)}</p>
          </div>
          <div>
            <p className="text-[0.5625rem] font-bold uppercase tracking-wide text-gray-400">Physical completion</p>
            <p className="text-sm text-gray-800 dark:text-gray-100">{project.completion_percentage ?? 0}%</p>
          </div>
          <div>
            <p className="text-[0.5625rem] font-bold uppercase tracking-wide text-gray-400">Constituency</p>
            <p className="truncate text-sm text-gray-800 dark:text-gray-100" title={project.constituency || ""}>{project.constituency || "—"}</p>
          </div>
        </div>
        <p className="mt-3 border-t border-gray-100 pt-2 text-[0.625rem] text-gray-400 dark:border-gray-800">
          Values above are recorded MPLADS source data. Flags below are AI-generated indicators that require human verification — they are not findings of fraud.
        </p>
      </section>

      {/* Detailed flags — AI/SYSTEM INFERENCE */}
      {flags.length > 0 && (
        <div>
          <div className="flex items-center justify-between gap-2">
            <h4 className="flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-gray-500">
              <span aria-hidden>🤖</span> AI-Detected Indicators
            </h4>
            <span className="rounded bg-purple-100 px-1.5 py-0.5 text-[0.5625rem] font-bold uppercase tracking-wide text-purple-600 dark:bg-purple-950 dark:text-purple-300">AI / system inference</span>
          </div>
          <ul className="mt-2 space-y-2">
            {flags.map((f) => (
              <li key={f.type} className="rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-800/60">
                <div className="flex items-center justify-between gap-2">
                  <FlagPill label={f.label} />
                  <span className="font-mono text-[0.6875rem] text-gray-400">{f.type}</span>
                </div>
                <p className="mt-1.5 text-sm text-gray-700 dark:text-gray-300">{f.detail}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Computer Vision Analysis card */}
      <section className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
        <h4 className="flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-gray-500">
          <span aria-hidden>🖼️</span> Computer Vision Analysis
        </h4>
        {duplicate_photos.length === 0 ? (
          <p className="mt-2 text-sm text-gray-500">
            No duplicate photo submissions detected for this project. Photos submitted via the{" "}
            {onOpenProject ? (
              <button onClick={() => onOpenProject("Ground Truth Verification")} className="font-semibold text-blue-600 underline dark:text-blue-400">Verification Portal</button>
            ) : (
              <span className="font-semibold">Verification Portal</span>
            )}{" "}
            are perceptually hashed and cross-checked here automatically.
          </p>
        ) : (
          <ul className="mt-3 space-y-2">
            {duplicate_photos.map((d) => (
              <li key={d.matched_project_id} className="flex items-center justify-between gap-3 rounded-lg bg-purple-50 p-3 dark:bg-purple-950/40">
                <div className="min-w-0">
                  <p className="text-sm font-bold text-purple-800 dark:text-purple-200">{d.similarity_percent}% Match with Project #{d.matched_project_id}</p>
                  <p className="truncate text-xs text-purple-600 dark:text-purple-300">{d.matched_project_name || "Unknown project"} · uploaded {d.uploaded_at?.slice(0, 10)}</p>
                </div>
                {onOpenProject && (
                  <button onClick={() => onOpenProject(d.matched_project_id)} className="shrink-0 rounded-lg border border-purple-300 px-2.5 py-1 text-xs font-semibold text-purple-700 hover:bg-purple-100 dark:border-purple-700 dark:text-purple-200">
                    Open
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        <p className="mt-2 text-[0.6875rem] text-gray-400">
          Duplicate detection uses a 64-bit perceptual hash (dHash); ≥95% similarity is reported as a near-duplicate. EXIF GPS on field submissions is compared against the project's last field-verified position — deviation &gt;500m is flagged in red. Metadata findings are evidence, not proof.
        </p>
      </section>

      {/* Cost Estimation Audit card */}
      <section className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
        <h4 className="flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-gray-500">
          <span aria-hidden>📊</span> Cost Estimation Audit
        </h4>
        {cost_anomaly?.available === false ? (
          <p className="mt-2 text-sm text-gray-500">{cost_anomaly.reason}</p>
        ) : cost_anomaly ? (
          <div className="mt-3">
            <div className="flex items-end justify-between gap-4">
              <div>
                <p className="text-2xl font-bold text-gray-900 dark:text-white">{formatMoney(cost_anomaly.proposed_amount)}</p>
                <p className="text-xs text-gray-500">Sanctioned for this project</p>
              </div>
              <div className="text-right">
                <p className="text-lg font-semibold text-gray-700 dark:text-gray-300">{formatMoney(cost_anomaly.regional_average)}</p>
                <p className="text-xs text-gray-500">{cost_anomaly.benchmark_basis} avg</p>
              </div>
            </div>
            <div className="mt-3 h-3 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700" role="img" aria-label={`Budget is ${cost_anomaly.ratio} times the regional average`}>
              <div
                className={`h-full rounded-full ${cost_anomaly.severity === "red" ? "bg-red-500" : cost_anomaly.severity === "yellow" ? "bg-amber-500" : "bg-green-500"}`}
                style={{ width: `${Math.min(100, (cost_anomaly.ratio / 3) * 100)}%` }}
              />
            </div>
            <div className="mt-1 flex justify-between text-[0.6875rem] text-gray-400"><span>0.5×</span><span>1.5×</span><span>3×+</span></div>
            <p className="mt-2 text-sm font-semibold text-gray-800 dark:text-gray-200">{cost_anomaly.classification}</p>
            <p className="text-sm text-gray-600 dark:text-gray-400">{cost_anomaly.explanation}</p>
            <p className="mt-1 text-xs text-gray-500">
              Percentile within category (est.): {cost_anomaly.percentile} · z-score ≈ {cost_anomaly.z_score} · benchmark sample {cost_anomaly.sample_size}
            </p>
          </div>
        ) : null}
      </section>

      {/* Ground verification reports */}
      <section>
        <h4 className="text-xs font-bold uppercase tracking-wide text-gray-500">Ground Verification Reports</h4>
        {verifications.length === 0 ? (
          <p className="mt-2 text-sm text-gray-500">No citizen/inspector verification reports for this project yet.</p>
        ) : (
          <ul className="mt-2 space-y-1.5">
            {verifications.map((v, i) => (
              <li key={i} className="flex items-center justify-between gap-3 rounded-lg border border-gray-200 px-3 py-2 text-sm dark:border-gray-700">
                <span className={`font-semibold ${v.status === "Functional" ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400"}`}>{v.status}</span>
                <span className="min-w-0 flex-1 truncate text-xs text-gray-500">{v.reporter}{v.distance_m != null && ` · ${v.distance_m}m from last fix`}{v.note && ` · “${v.note}”`}</span>
                <span className="shrink-0 text-[0.6875rem] text-gray-400">{v.created_at?.slice(0, 10)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Audit action controls — auditor/admin only (server-enforced too) */}
      <section className="rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-800/60">
        <h4 className="text-xs font-bold uppercase tracking-wide text-gray-500">Audit Action Controls</h4>
        {canAct ? (
          <>
            <input
              type="text"
              value={actionNote}
              onChange={(e) => setActionNote(e.target.value)}
              placeholder={"Optional note — or the inquiry question when issuing an inquiry…"}
              className="mt-2 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200"
            />
            <div className="mt-3 flex flex-wrap gap-2">
              <button disabled={actionBusy} onClick={() => runAction("approve")} className="rounded-lg bg-green-600 px-3.5 py-2 text-sm font-bold text-white transition hover:bg-green-700 disabled:opacity-50">✓ Approve &amp; Clear Flag</button>
              <button disabled={actionBusy} onClick={() => runAction("inquiry")} className="rounded-lg bg-amber-500 px-3.5 py-2 text-sm font-bold text-white transition hover:bg-amber-600 disabled:opacity-50">✉ Issue Inquiry to District Authority</button>
              <button disabled={actionBusy} onClick={() => runAction("escalate")} className="rounded-lg bg-red-600 px-3.5 py-2 text-sm font-bold text-white transition hover:bg-red-700 disabled:opacity-50">⬆ Escalate to Auditor General</button>
            </div>
            {actionDone && <p className="mt-2 text-sm font-semibold text-blue-600 dark:text-blue-400">{actionDone}</p>}
          </>
        ) : (
          <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">Audit dispositions are available to auditors and administrators only.</p>
        )}
        {actions.length > 0 && (
          <ul className="mt-3 space-y-1 border-t border-gray-200 pt-2 text-xs text-gray-500 dark:border-gray-700">
            {actions.slice(0, 4).map((a) => (
              <li key={a.id}><span className="font-semibold capitalize text-gray-700 dark:text-gray-300">{a.action}</span> by {a.actor} · {a.created_at?.slice(0, 16).replace("T", " ")}</li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
