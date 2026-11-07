import { useEffect, useRef, useState } from "react"
import { getForensicBundle } from "../services/api"
import { formatMoney } from "../utils/format"

/* ── Project Forensic Mode ────────────────────────────────────────────
   Consolidates evidence that already exists for the selected project
   (financial, physical, ML, rules, vendor, ground, data quality) into
   one investigation view. Read-only — nothing here modifies data.      */

function Section({ title, children, accent }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-[#000000]">
      <p className={`text-[0.625rem] font-bold uppercase tracking-wider ${accent || "text-gray-400"}`}>{title}</p>
      <div className="mt-2.5">{children}</div>
    </div>
  )
}

function Row({ label, value, mono }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="text-[0.6875rem] text-gray-500 dark:text-gray-400">{label}</span>
      <span className={`text-right text-xs font-bold ${mono ? "font-mono" : ""}`}>{value}</span>
    </div>
  )
}

export default function ForensicModeModal({ projectId, onClose }) {
  const [data, setData] = useState(null)
  const [status, setStatus] = useState("loading") // loading | done | error
  const [closing, setClosing] = useState(false)
  const closingRef = useRef(false)

  const requestClose = () => {
    if (closingRef.current) return
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) { onClose(); return }
    closingRef.current = true
    setClosing(true)
    setTimeout(onClose, 150)
  }

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") requestClose() }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  useEffect(() => {
    getForensicBundle(projectId)
      .then((d) => { setData(d); setStatus("done") })
      .catch(() => setStatus("error"))
  }, [projectId])

  if (status === "loading") {
    return (
      <div className="anim-overlay-in fixed inset-0 z-[90] flex items-center justify-center bg-black/50 backdrop-blur-2xs" onClick={onClose}>
        <div className="anim-card-in rounded-2xl bg-white p-10 text-center shadow-soft-lg dark:bg-[#0d090b]" onClick={(e) => e.stopPropagation()}>
          <div className="inline-block h-7 w-7 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent" />
          <p className="mt-3 text-xs font-medium text-gray-500">Assembling forensic evidence…</p>
        </div>
      </div>
    )
  }
  if (status === "error") {
    return (
      <div className="anim-overlay-in fixed inset-0 z-[90] flex items-center justify-center bg-black/50 backdrop-blur-2xs" onClick={onClose}>
        <div className="anim-card-in rounded-2xl bg-white p-8 text-center shadow-soft-lg dark:bg-[#0d090b]" onClick={(e) => e.stopPropagation()}>
          <p className="text-sm text-gray-500">Could not load the forensic bundle.</p>
          <button onClick={onClose} className="mt-4 rounded-lg bg-[#c8102e] px-4 py-2 text-xs font-bold text-white dark:bg-blue-600">Close</button>
        </div>
      </div>
    )
  }

  const p = data.project || {}
  const fin = data.financial || {}
  const phy = data.physical || {}
  const ml = data.ml || {}
  const rules = data.rules || {}
  const vend = data.vendor_evidence || {}
  const ground = data.ground || {}
  const dq = data.data_quality || {}
  const sc = rules.score_consistency
  const utilization = fin.utilization_pct

  return (
    <div className={`anim-overlay-in fixed inset-0 z-[90] flex items-start justify-center overflow-y-auto bg-black/50 p-3 backdrop-blur-2xs transition-opacity sm:p-6 ${closing ? "is-closing" : ""}`} onClick={requestClose}>
      <div className="anim-card-in my-auto w-full max-w-2xl rounded-2xl border border-[#e8dadc] bg-white p-5 shadow-soft-lg dark:border-[#351923] dark:bg-[#0d090b] sm:p-6" onClick={(e) => e.stopPropagation()}>

        {/* Header */}
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Forensic Analysis</p>
            <h3 className="mt-0.5 text-sm font-bold leading-snug">{p.name || "Unnamed Project"}</h3>
            <p className="mt-0.5 font-mono text-[0.625rem] text-gray-400">Project #{p.id}</p>
          </div>
          <button onClick={requestClose} className="rounded-lg p-1.5 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700">✕</button>
        </div>

        <div className="mt-4 max-h-[70vh] space-y-3 overflow-y-auto pr-1">

          {/* Project overview */}
          <Section title="Project Overview">
            <div className="grid grid-cols-2 gap-x-4 sm:grid-cols-4">
              {[
                ["State", p.state], ["District", p.district], ["Constituency", p.constituency],
                ["Category", p.category], ["Status", p.status], ["Financial Year", p.fy],
              ].map(([label, value]) => (
                <div key={label} className="py-1">
                  <p className="text-[0.625rem] text-gray-400">{label}</p>
                  <p className="text-xs font-bold">{value || "Not available"}</p>
                </div>
              ))}
            </div>
          </Section>

          {/* Financial evidence */}
          <Section title="Financial Evidence">
            <Row label="Sanctioned amount" value={formatMoney(fin.sanctioned_amount)} mono />
            <Row label="Recorded expenditure" value={formatMoney(fin.expenditure)} mono />
            <Row label="Utilization" value={utilization != null ? `${Number(utilization).toFixed(1)}%` : "Not available"} mono />
            {fin.remaining_amount != null && <Row label="Remaining" value={formatMoney(fin.remaining_amount)} mono />}
            {fin.discrepancy_indicators?.overspend_amount > 0 && (
              <p className="mt-2 rounded-lg bg-red-50 px-2.5 py-1.5 text-[0.6875rem] font-semibold text-red-700 dark:bg-red-950/40 dark:text-red-300">
                Expenditure exceeds sanction by {formatMoney(fin.discrepancy_indicators.overspend_amount)} — indicates a discrepancy that warrants verification.
              </p>
            )}
            <p className="mt-2 text-[0.625rem] text-gray-400">
              Unverified spend (expenditure beyond recorded progress): {formatMoney(fin.discrepancy_indicators?.unverified_spend || 0)} — calculated as expenditure × (1 − reported progress ÷ 100).
            </p>
          </Section>

          {/* Physical evidence */}
          <Section title="Physical Evidence">
            <Row label="Recorded completion" value={`${Number(phy.completion_percentage || 0).toFixed(0)}%`} mono />
            {phy.status_consistency && (
              <>
                <Row label={`Status "${phy.status_consistency.status}" vs progress`} value={phy.status_consistency.consistent ? "Consistent" : "Mismatch"} />
                {(phy.status_consistency.notes || []).map((n, i) => (
                  <p key={i} className="mt-1.5 rounded-lg bg-amber-50 px-2.5 py-1.5 text-[0.6875rem] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">{n}</p>
                ))}
              </>
            )}
          </Section>

          {/* ML analysis */}
          <Section title="ML Analysis (Isolation Forest)">
            <Row label="Anomaly flag" value={ml.isolation_forest_flag ? "Flagged (statistical outlier)" : "Not flagged"} />
            {ml.ml_score != null && <Row label="Model score" value={Number(ml.ml_score).toFixed(4)} mono />}
            {(ml.top_factors || []).length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {ml.top_factors.map((f, i) => (
                  <span key={i} className="rounded bg-purple-100 px-2 py-0.5 text-[0.625rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">{f}</span>
                ))}
              </div>
            )}
            <p className="mt-2 text-[0.625rem] text-gray-400">{ml.note || "ML output is a statistical indicator — it does not establish wrongdoing."}</p>
          </Section>

          {/* Rule-based analysis */}
          <Section title="Rule-Based Risk Analysis">
            <Row label="Stored risk score" value={`${rules.risk_score ?? 0}/100 (${rules.risk_level || "None"})`} mono />
            {(rules.triggered || []).length > 0 ? (
              <div className="mt-2 space-y-1.5">
                {rules.triggered.map((r, i) => (
                  <div key={i} className="flex items-center justify-between gap-2 rounded-lg bg-white px-2.5 py-1.5 dark:bg-[#0d090b]">
                    <span className="text-[0.6875rem] font-semibold">{r.reason}</span>
                    <span className="flex shrink-0 items-center gap-1.5">
                      {r.source === "ml" && <span className="rounded bg-purple-100 px-1.5 py-0.5 text-[0.5625rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">ML</span>}
                      {r.points != null && <span className="font-mono text-[0.6875rem] font-bold text-red-600 dark:text-red-400">+{r.points}</span>}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-1.5 text-xs text-gray-500">No risk rules triggered for this project.</p>
            )}
            {sc && !sc.consistent && (
              <p className="rise-in mt-2.5 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-2 text-[0.6875rem] leading-relaxed text-amber-700 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300">
                {sc.note}
              </p>
            )}
          </Section>

          {/* Vendor / relationship evidence */}
          <Section title="Vendor / Relationship Evidence">
            {vend.available && (vend.vendors || []).length > 0 ? (
              <>
                <div className="space-y-1.5">
                  {vend.vendors.slice(0, 5).map((v, i) => (
                    <div key={i} className="flex items-center justify-between gap-2 rounded-lg bg-white px-2.5 py-1.5 dark:bg-[#0d090b]">
                      <span className="truncate text-[0.6875rem] font-semibold">{v.vendor}</span>
                      <span className="shrink-0 font-mono text-[0.625rem] text-gray-500">{v.transactions} txns · {formatMoney(v.total_amount)}</span>
                    </div>
                  ))}
                </div>
                {vend.match_scope && <p className="mt-2 text-[0.625rem] text-gray-400">{vend.match_scope}</p>}
              </>
            ) : (
              <p className="text-xs text-gray-500">{vend.note || "No vendor linkage recorded for this project in the expenditure data."}</p>
            )}
          </Section>

          {/* Ground / evidence */}
          <Section title="Ground Evidence">
            {ground.evidence_count > 0 ? (
              <div className="space-y-1.5">
                {ground.reports.map((r, i) => (
                  <div key={i} className="rounded-lg bg-white px-2.5 py-1.5 dark:bg-[#0d090b]">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[0.6875rem] font-bold">{r.status}</span>
                      <span className="text-[0.625rem] text-gray-400">{r.reporter} ({r.reporter_role})</span>
                    </div>
                    {r.note && <p className="mt-1 text-[0.625rem] text-gray-500">"{r.note}"</p>}
                    {r.gps && <p className="mt-0.5 font-mono text-[0.5625rem] text-gray-400">GPS {Number(r.gps.lat).toFixed(4)}, {Number(r.gps.lon).toFixed(4)}</p>}
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-500">{ground.note || "No ground-verification reports exist for this project."}</p>
            )}
            <p className="mt-2 text-[0.625rem] text-gray-400">{ground.note}</p>
          </Section>

          {/* Data quality */}
          <Section title="Data Quality">
            {dq.stale_flag && (
              <p className="rounded-lg bg-amber-50 px-2.5 py-1.5 text-[0.6875rem] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                {dq.stale_flag === "POSSIBLY_STALE" ? "Possibly stale: " : `${dq.stale_flag}: `}{dq.stale_reason}
              </p>
            )}
            {(dq.missing_fields || []).length > 0 && (
              <ul className="mt-1.5 space-y-0.5">
                {dq.missing_fields.map((m, i) => (
                  <li key={i} className="text-[0.6875rem] text-gray-500 dark:text-gray-400">• {m}</li>
                ))}
              </ul>
            )}
            {!(dq.missing_fields || []).length && !dq.stale_flag && (
              <p className="text-xs text-gray-500">No notable data-quality issues recorded for this project.</p>
            )}
          </Section>

          {/* Forensic summary */}
          <div className="rounded-xl border border-blue-200 bg-blue-50 p-4 dark:border-blue-900/60 dark:bg-blue-950/30">
            <p className="text-[0.625rem] font-bold uppercase tracking-wider text-blue-700 dark:text-blue-300">Forensic Summary</p>
            <p className="mt-2 text-xs leading-relaxed text-gray-700 dark:text-gray-200">{data.summary}</p>
            <p className="mt-2 text-[0.625rem] text-gray-500 dark:text-gray-400">
              Evidence-based observations only — nothing here constitutes a finding of fraud or wrongdoing; all indicators require human review.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
