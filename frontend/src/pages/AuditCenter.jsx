import { useCallback, useEffect, useMemo, useState } from "react"
import { getAuditQueue } from "../services/api"
import { TableRowsSkeleton } from "../components/Skeletons"
import InspectionModal from "../components/InspectionModal"
import { formatMoney } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════════
   AI AUDIT COMMAND CENTER — SIH 26102
   ─────────────────────────────────────────────────────────────────────
   Top metric cards + audit table ranked by AI Risk Index (0-100), with
   dynamic filter pills. Clicking a row opens the AI Inspection & Forensic
   Summary modal (InspectionModal, below).

   Every number shown is derived from recorded data or the documented
   heuristic pipeline — no synthetic claims.
   ═══════════════════════════════════════════════════════════════════ */

const FILTER_PILLS = [
  { key: "", label: "All" },
  { key: "photo_fraud", label: "Photo Fraud" },
  { key: "cost_outlier", label: "Cost Outlier" },
  { key: "gps_mismatch", label: "GPS Mismatch" },
  { key: "delayed", label: "Delayed SLA" },
]

const SEVERITY_STYLES = {
  Critical: "bg-red-100 text-red-700 border-red-200 dark:bg-red-950 dark:text-red-300 dark:border-red-900",
  Moderate: "bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-900",
  Low: "bg-green-100 text-green-700 border-green-200 dark:bg-green-950 dark:text-green-300 dark:border-green-900",
}

function RiskBadge({ score, severity }) {
  return (
    <span className={`inline-flex min-w-[3.25rem] items-center justify-center rounded-full border px-2.5 py-1 text-xs font-bold ${SEVERITY_STYLES[severity] || SEVERITY_STYLES.Low}`}>
      {score}
    </span>
  )
}

function FlagPill({ label }) {
  const tone = label.includes("Photo") ? "bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300"
    : label.includes("Cost") ? "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300"
    : label.includes("GPS") ? "bg-cyan-100 text-cyan-700 dark:bg-cyan-950 dark:text-cyan-300"
    : "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
  return <span className={`inline-block whitespace-nowrap rounded px-1.5 py-0.5 text-[0.6875rem] font-semibold ${tone}`}>{label}</span>
}

/* local import-free FlagPill used by the table; ForensicsPanel has its own shared copy */

/* ──────────── Inspection modal — shared component (components/InspectionModal.jsx) ──────────── */


/* ──────────── Main page ──────────── */

export default function AuditCenter({ onOpenProject, fy }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  const [pill, setPill] = useState("")
  const [inspectionId, setInspectionId] = useState(null)

  const load = useCallback((flag) => {
    setItems(null); setError(null)
    getAuditQueue({ flag: flag || undefined, limit: 50, fy })
      .then(setItems)
      .catch((e) => setError(e.message))
  }, [fy])

  useEffect(() => { load(pill) }, [load, pill])

  const metrics = useMemo(() => {
    const src = items?.items || []
    const critical = src.filter((i) => i.severity === "Critical").length
    const photoFraud = src.filter((i) => i.flags.some((f) => f.type === "duplicate_photo")).length
    const outliers = src.filter((i) => i.flags.some((f) => f.type === "cost_outlier"))
    const flaggedValue = outliers.reduce((sum, i) => sum + Math.max(0, i.sanctioned_amount - (i.benchmark_amount || 0)), 0)
    return { scanned: items?.total_candidates ?? null, critical, photoFraud, outliers: outliers.length, flaggedValue }
  }, [items])

  return (
    <div className="min-h-full p-4 sm:p-6">
    <div className="mx-auto max-w-[1440px] space-y-4 sm:space-y-6">
      <header className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white sm:text-3xl">AI Audit Command Center</h1>
        </div>
        <p className="max-w-3xl text-sm text-gray-500 dark:text-gray-400">
          Detect anomalies, fraud indicators, and financial irregularities across the MPLADS portfolio — ranked by a 0-100 audit risk index combining documented heuristic flags (photo fraud, cost outliers, GPS mismatches, delayed SLAs) with the existing ML risk pipeline.
        </p>
      </header>

      {/* Metric cards */}
      <section className="grid grid-cols-1 min-[400px]:grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricCard icon="🔍" label="Projects Scanned" value={metrics.scanned == null ? "…" : metrics.scanned.toLocaleString("en-IN")} tone="blue" />
        <MetricCard icon="🚨" label="High-Risk Flagged" value={items ? metrics.critical : "…"} tone="red" hint="Critical band (80-100) in current view" />
        <MetricCard icon="🖼️" label="Duplicate Photos Caught" value={items ? metrics.photoFraud : "…"} tone="purple" hint="Projects with near-duplicate submissions" />
        <MetricCard icon="💸" label="Cost Outliers Flagged" value={items ? metrics.outliers : "…"} tone="orange" hint="Sanctioned ≥ 2× category benchmark" />
      </section>

      {/* Filter pills */}
      <div className="flex flex-wrap gap-2" role="tablist" aria-label="Filter by AI flag">
        {FILTER_PILLS.map((p) => (
          <button
            key={p.key}
            role="tab"
            aria-selected={pill === p.key}
            onClick={() => setPill(p.key)}
            className={`rounded-full border px-3.5 py-1.5 text-sm font-semibold transition ${
              pill === p.key
                ? "border-blue-600 bg-blue-600 text-white"
                : "border-gray-300 bg-white text-gray-600 hover:border-blue-400 hover:text-blue-600 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300"
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Audit table */}
      <section className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800">
        {error && (
          <div className="p-6 text-center">
            <p className="text-sm text-red-600 dark:text-red-400">Unable to load the audit queue: {error}</p>
            <button onClick={() => load(pill)} className="mt-3 rounded-lg border border-gray-300 px-4 py-2 text-sm font-semibold hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-700">Retry</button>
          </div>
        )}
        {!error && !items && <div className="p-4"><TableRowsSkeleton rows={10} /></div>}
        {!error && items && (
          items.items.length === 0 ? (
            <div className="p-10 text-center">
              <p className="text-3xl" aria-hidden>🛡️</p>
              <p className="mt-2 text-sm font-semibold text-gray-700 dark:text-gray-300">No projects match this filter</p>
              <p className="mt-1 text-xs text-gray-500">Try the “All” pill — flagged projects appear here as the forensic pipeline records findings.</p>
            </div>
          ) : (
            <>
              {/* Desktop table */}
              <div className="hidden overflow-x-auto md:block">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500 dark:border-gray-700">
                    <tr>
                      <th className="px-4 py-3 font-semibold">Project</th>
                      <th className="px-4 py-3 font-semibold">District</th>
                      <th className="px-4 py-3 text-right font-semibold">Sanctioned</th>
                      <th className="px-4 py-3 font-semibold">Risk Index</th>
                      <th className="px-4 py-3 font-semibold">Primary AI Flag</th>
                      <th className="px-4 py-3 text-right font-semibold">Quick Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                    {items.items.map((row) => (
                      <tr key={row.project_id} className="transition hover:bg-gray-50 dark:hover:bg-gray-700/40">
                        <td className="max-w-[22rem] px-4 py-3">
                          <p className="truncate font-semibold text-gray-900 dark:text-white" title={row.title}>{row.title || `Project #${row.project_id}`}</p>
                          <p className="text-xs text-gray-400">#{row.project_id} · {row.state}</p>
                        </td>
                        <td className="px-4 py-3 text-gray-600 dark:text-gray-300">{row.district || "—"}</td>
                        <td className="px-4 py-3 text-right font-medium text-gray-800 dark:text-gray-200">{formatMoney(row.sanctioned_amount)}</td>
                        <td className="px-4 py-3"><RiskBadge score={row.risk_index} severity={row.severity} /></td>
                        <td className="px-4 py-3"><FlagPill label={row.primary_flag} /></td>
                        <td className="px-4 py-3 text-right">
                          <button onClick={() => setInspectionId(row.project_id)} className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-xs font-bold text-blue-700 transition hover:bg-blue-100 dark:border-blue-800 dark:bg-blue-950 dark:text-blue-300">
                            Inspect
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {/* Mobile cards */}
              <ul className="divide-y divide-gray-100 md:hidden dark:divide-gray-700">
                {items.items.map((row) => (
                  <li key={row.project_id} className="space-y-2 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <p className="min-w-0 text-sm font-semibold text-gray-900 dark:text-white">{row.title || `Project #${row.project_id}`}</p>
                      <RiskBadge score={row.risk_index} severity={row.severity} />
                    </div>
                    <p className="text-xs text-gray-500">{row.district || "—"}, {row.state} · {formatMoney(row.sanctioned_amount)}</p>
                    <div className="flex items-center justify-between">
                      <FlagPill label={row.primary_flag} />
                      <button onClick={() => setInspectionId(row.project_id)} className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-xs font-bold text-blue-700 dark:border-blue-800 dark:bg-blue-950 dark:text-blue-300">Inspect</button>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )
        )}
      </section>

      {inspectionId != null && (
        <InspectionModal projectId={inspectionId} onClose={() => setInspectionId(null)} onOpenProject={onOpenProject} />
      )}
    </div>
    </div>
  )
}

const TONES = {
  blue: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  red: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  purple: "bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300",
  orange: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300",
}

function MetricCard({ icon, label, value, tone, hint }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
      <div className="flex items-center gap-2">
        <span className={`flex h-8 w-8 items-center justify-center rounded-lg text-base ${TONES[tone]}`} aria-hidden>{icon}</span>
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">{label}</span>
      </div>
      <p className="value-nowrap mt-2 text-2xl font-bold text-gray-900 dark:text-white">{value}</p>
      {hint && <p className="mt-0.5 text-[0.6875rem] text-gray-400">{hint}</p>}
    </div>
  )
}
