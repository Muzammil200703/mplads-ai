import { useEffect, useRef, useState } from "react"
import { getProjectDNA } from "../services/api"
import { formatMoney } from "../utils/format"

/* ── Project DNA + Twins ────────────────────────────────────────────
   Analytical fingerprint (attribute-derived bars) plus genuinely
   similar projects found by weighted attribute matching on the
   backend — never name search. Read-only.                            */

function DNABar({ label, value, display }) {
  const v = Math.max(0, Math.min(100, Number(value) || 0))
  const color =
    v >= 75 ? "bg-red-500" :
    v >= 45 ? "bg-amber-500" :
    "bg-blue-600"
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[0.6875rem] font-semibold">{label}</span>
        <span className="text-[0.625rem] text-gray-500 dark:text-gray-400">{display}</span>
      </div>
      <div className="mt-1 h-2 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${v}%` }} />
      </div>
    </div>
  )
}

export default function ProjectDNASection({ projectId, onOpenProject }) {
  const [data, setData] = useState(null)
  const [status, setStatus] = useState("loading") // loading | done | error

  useEffect(() => {
    setStatus("loading")
    getProjectDNA(projectId)
      .then((d) => { setData(d); setStatus("done") })
      .catch(() => setStatus("error"))
  }, [projectId])

  if (status === "loading") {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-r-transparent" />
        <span className="text-xs text-gray-400">Computing project fingerprint…</span>
      </div>
    )
  }
  if (status === "error") {
    return (
      <div className="rounded-xl border border-gray-200 bg-gray-50 p-4 text-center dark:border-gray-700 dark:bg-[#171b20]">
        <p className="text-xs text-gray-500">Could not compute Project DNA.</p>
        <button onClick={() => setStatus("loading")} className="mt-2 rounded bg-[#46566a] px-3 py-1 text-[0.625rem] font-bold text-white dark:bg-blue-600">Retry</button>
      </div>
    )
  }

  const dims = data.dna?.dimensions || []
  const twins = data.twins || {}
  const peers = twins.peers || []

  return (
    <div className="space-y-4">
      {/* Fingerprint */}
      <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
        <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Project DNA — Analytical Fingerprint</p>
        <p className="mt-1 text-[0.625rem] text-gray-400">Each dimension is derived from the project's recorded attributes; unavailable data is omitted rather than shown as zero.</p>
        {dims.length > 0 ? (
          <div className="mt-3 space-y-3">
            {dims.map((d) => <DNABar key={d.key} label={d.label} value={d.value} display={d.display} />)}
          </div>
        ) : (
          <p className="mt-2 text-xs text-gray-500">Not enough recorded data to build a fingerprint for this project.</p>
        )}
      </div>

      {/* Twins */}
      <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-[#171b20]">
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Project Twins — Analytical Peers</p>
          <span className="text-[0.625rem] text-gray-400">{twins.cohort_size ?? 0} candidates · {peers.length} shown</span>
        </div>
        <p className="mt-1 text-[0.625rem] text-gray-400">{twins.criteria?.weights}</p>

        {peers.length > 0 ? (
          <>
            <div className="mt-3 space-y-2">
              {peers.map((t) => (
                <button
                  key={t.id}
                  onClick={() => onOpenProject?.(t.id)}
                  className="w-full rounded-lg border border-gray-200 bg-white p-3 text-left transition hover:border-blue-300 hover:shadow-soft dark:border-gray-700 dark:bg-[#222831] dark:hover:border-blue-700"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <span className="font-mono text-[0.625rem] font-bold text-gray-400">#{t.id}</span>
                      <p className="truncate text-xs font-semibold">{t.project_name}</p>
                      <p className="text-[0.625rem] text-gray-500">{t.constituency || "N/A"} · {t.status}</p>
                    </div>
                    <span className="shrink-0 rounded bg-blue-100 px-2 py-0.5 font-mono text-[0.625rem] font-bold text-blue-700 dark:bg-blue-950 dark:text-blue-300">{t.similarity}%</span>
                  </div>
                  <div className="mt-2 grid grid-cols-4 gap-2 text-[0.625rem]">
                    <div><p className="text-gray-400">Sanctioned</p><p className="font-bold">{formatMoney(t.sanctioned_amount)}</p></div>
                    <div><p className="text-gray-400">Spent</p><p className="font-bold">{formatMoney(t.expenditure)}</p></div>
                    <div><p className="text-gray-400">Progress</p><p className="font-bold">{Number(t.completion_percentage || 0).toFixed(0)}%</p></div>
                    <div>
                      <p className="text-gray-400">Risk</p>
                      <p className={`font-bold ${t.risk_level === "High" ? "text-red-600 dark:text-red-400" : t.risk_level === "Medium" ? "text-amber-600 dark:text-amber-400" : ""}`}>
                        {t.risk_level}
                      </p>
                    </div>
                  </div>
                </button>
              ))}
            </div>

            {/* Contextual comparisons */}
            {twins.comparisons && Object.keys(twins.comparisons).length > 0 && (
              <div className="mt-3 space-y-1.5">
                {Object.values(twins.comparisons).map((cmp, i) => (
                  <p key={i} className="rounded-lg bg-white px-2.5 py-1.5 text-[0.6875rem] text-gray-600 dark:bg-[#222831] dark:text-gray-300">{cmp.text}</p>
                ))}
              </div>
            )}
            <p className="mt-2 text-[0.625rem] text-gray-400">{twins.note}</p>
          </>
        ) : (
          <p className="mt-2 text-xs text-gray-500">
            {twins.cohort_size > 0
              ? "No comparable peers match enough attributes to be listed."
              : "No comparable cohort found for this project in the dataset."}
          </p>
        )}
      </div>
    </div>
  )
}
