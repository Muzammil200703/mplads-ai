import { useEffect, useState } from "react"
import { getRootCause } from "../services/api"
import { formatMoney } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════
   FEATURE 1 — Anomaly root-cause graph
   Rendered inside the existing investigation workspace. The backend
   returns the contributing indicators, the stored rule reasons, the
   statistical peer deviation and the evidence gaps as a node/edge graph.
   Correlated deviation is labelled apart from contributing indicators,
   and the graph never claims causation.
   ═══════════════════════════════════════════════════════════════════ */

const KIND_TONE = {
  outcome: "border-red-300 bg-red-50 dark:border-red-900/50 dark:bg-red-950/20",
  gap: "border-amber-300 bg-amber-50 dark:border-amber-900/50 dark:bg-amber-950/20",
  context: "border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-900/40",
  default: "border-blue-200 bg-blue-50/60 dark:border-blue-900/40 dark:bg-blue-950/10",
}

const RELATION_TONE = {
  "contributing indicator": "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  "correlated deviation": "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  "evidence gap": "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  context: "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300",
}

function relationFor(edges, nodeId) {
  return (edges || []).filter((e) => e.from === nodeId || e.to === nodeId)
}

function EvidenceList({ evidence }) {
  if (!evidence?.length) return null
  return (
    <div className="mt-2 space-y-1">
      {evidence.map((e, i) => (
        <div key={`${e.table}-${i}`} className="rounded border border-gray-200 bg-white/70 px-2 py-1 dark:border-gray-700 dark:bg-black/40">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[0.65rem] font-bold uppercase text-gray-500 dark:bg-gray-700 dark:text-gray-300">
              {e.table}
            </span>
            <code className="text-[0.72rem] font-semibold text-blue-700 dark:text-blue-300">{e.reference}</code>
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-2.5 gap-y-0.5">
            {Object.entries(e.fields || {}).map(([k, v]) => (
              <span key={k} className="text-[0.72rem] text-gray-500 dark:text-gray-400">
                <span className="font-semibold">{k}:</span> {String(v ?? "—")}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

export default function RootCauseGraph({ projectId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await getRootCause(projectId)
        if (alive) { setData(res); setError("") }
      } catch (e) {
        if (alive) setError(e.message || "Could not load the root-cause analysis.")
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => { alive = false }
  }, [projectId])

  if (loading) {
    return (
      <p className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-[0.875rem] text-gray-500 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-400">
        Building the root-cause graph from stored project, risk and payment records…
      </p>
    )
  }
  if (error) {
    return <p className="rounded-lg border border-red-200 bg-red-50 p-3 text-[0.875rem] font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-300">{error}</p>
  }
  if (!data) return null

  const { project, outcome, nodes = [], edges = [], gaps = [], limitations = [], peer } = data
  const drivers = nodes.filter((n) => n.kind !== "outcome" && n.kind !== "gap" && n.kind !== "context")

  return (
    <div className="space-y-3">
      <section className="rounded-xl border border-gray-200 bg-white p-3.5 dark:border-gray-700 dark:bg-[#000000]">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <h4 className="text-[0.72rem] font-bold uppercase tracking-wider text-gray-400">Anomaly root-cause graph</h4>
            <p className="mt-0.5 text-[1rem] font-bold text-gray-900 dark:text-white">{project?.name || `Project #${projectId}`}</p>
            <p className="mt-0.5 text-[0.8rem] text-gray-500 dark:text-gray-400">
              {[project?.house, project?.constituency, project?.state].filter(Boolean).join(" · ") || "Location not recorded"}
            </p>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <span className={`rounded px-2 py-0.5 text-[0.72rem] font-bold ${outcome?.flagged ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" : "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"}`}>
              {outcome?.risk_level || "Unscored"}{outcome?.risk_score != null ? ` · ${Number(outcome.risk_score).toFixed(1)}` : ""}
            </span>
            <span className="rounded bg-blue-100 px-2 py-0.5 text-[0.72rem] font-bold text-blue-700 dark:bg-blue-950 dark:text-blue-300">
              confidence: {data.confidence}
            </span>
            {outcome?.ml_anomaly && (
              <span className="rounded bg-purple-100 px-2 py-0.5 text-[0.72rem] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">ML anomaly</span>
            )}
          </div>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {[
            ["Sanctioned", project?.sanctioned != null ? formatMoney(project.sanctioned) : "—"],
            ["Linked payments", project?.linked_expenditure != null ? formatMoney(project.linked_expenditure) : "—"],
            ["Recorded completion", project?.status || "—"],
            ["Comparable works", peer?.count ?? 0],
          ].map(([label, value]) => (
            <div key={label} className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/50">
              <p className="text-[0.65rem] font-bold uppercase tracking-wider text-gray-400">{label}</p>
              <p className="truncate text-[0.875rem] font-bold text-gray-900 dark:text-white" title={String(value)}>{value}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="rounded-xl border border-gray-200 bg-white p-3.5 dark:border-gray-700 dark:bg-[#000000]">
        <h4 className="text-[0.72rem] font-bold uppercase tracking-wider text-gray-400">Contributing factors → observed outcome</h4>
        {drivers.length === 0 ? (
          <p className="mt-2 rounded-lg bg-gray-50 p-2.5 text-[0.85rem] text-gray-500 dark:bg-gray-900/40 dark:text-gray-400">
            No contributing factor could be attributed from stored records. The gaps below explain why.
          </p>
        ) : (
          <div className="mt-2 space-y-2">
            {drivers.map((node) => {
              const rels = relationFor(edges, node.id)
              return (
                <div key={node.id} className={`rounded-lg border p-2.5 ${KIND_TONE.default}`}>
                  <div className="flex flex-wrap items-center justify-between gap-1.5">
                    <p className="text-[0.875rem] font-bold text-gray-900 dark:text-white">{node.label}</p>
                    <div className="flex flex-wrap gap-1">
                      {rels.map((e, i) => (
                        <span key={i} className={`rounded px-1.5 py-0.5 text-[0.65rem] font-bold ${RELATION_TONE[e.relation] || RELATION_TONE.context}`}>
                          {e.relation}{e.strength != null ? ` · ${Math.round(e.strength * 100)}%` : ""}
                        </span>
                      ))}
                    </div>
                  </div>
                  <p className="mt-1 text-[0.85rem] leading-snug text-gray-600 dark:text-gray-300">{node.detail}</p>
                  <EvidenceList evidence={node.evidence} />
                </div>
              )
            })}
          </div>
        )}
        <p className="mt-2 text-[0.75rem] italic text-gray-400">Edge strength is the engine's attribution weight, not a probability of cause.</p>
      </section>

      {gaps.length > 0 && (
        <section className="rounded-xl border border-amber-200 bg-amber-50/60 p-3.5 dark:border-amber-900/40 dark:bg-amber-950/10">
          <h4 className="text-[0.72rem] font-bold uppercase tracking-wider text-amber-600 dark:text-amber-400">Missing information</h4>
          <ul className="mt-2 space-y-2">
            {gaps.map((node) => (
              <li key={node.id} className="rounded-lg border border-amber-200 bg-white/70 p-2 dark:border-amber-900/40 dark:bg-black/30">
                <p className="text-[0.85rem] font-bold text-gray-900 dark:text-white">{node.label}</p>
                <p className="mt-0.5 text-[0.82rem] text-gray-600 dark:text-gray-300">{node.detail}</p>
                <EvidenceList evidence={node.evidence} />
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="rounded-xl border border-gray-200 bg-white p-3.5 dark:border-gray-700 dark:bg-[#000000]">
        <p className="text-[0.85rem] leading-relaxed text-gray-700 dark:text-gray-300">{data.causation_note}</p>
        {limitations.length > 0 && (
          <ul className="mt-2 list-inside list-disc space-y-1 text-[0.8rem] text-gray-500 dark:text-gray-400">
            {limitations.map((l) => <li key={l}>{l}</li>)}
          </ul>
        )}
      </section>
    </div>
  )
}
