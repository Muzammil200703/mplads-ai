import { useEffect, useRef, useState } from "react"
import { getProjectStressTest } from "../services/api"
import { formatMoney } from "../utils/format"

/* ── Risk Stress Test ───────────────────────────────────────────────
   Interactive what-if over the EXISTING rule engine via
   /forensic-tools/stress-test/{id}. Pure simulation: nothing is
   written to the database and stored scores stay untouched.

   The baseline (current score + recorded inputs) comes from the
   project data already loaded by ProjectDetail — no extra fetch.
   Each input change is debounced 350ms before hitting the backend.
   The first simulation response also confirms the server-side
   current score, so any stored-vs-computed divergence is surfaced.  */

const DEBOUNCE_MS = 350

function levelColor(level) {
  return level === "High" || level === "Critical" ? "text-red-600 dark:text-red-400"
    : level === "Medium" ? "text-amber-600 dark:text-amber-400"
    : "text-green-600 dark:text-green-400"
}

function ScoreCard({ label, score, level, accent }) {
  return (
    <div className={`flex-1 rounded-xl border p-3.5 ${accent || "border-gray-200 bg-gray-50/50 dark:border-gray-700 dark:bg-[#000000]"}`}>
      <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">{label}</p>
      <p className={`mt-1 font-mono text-2xl font-bold ${levelColor(level)}`}>
        {score ?? "—"}{score != null && <span className="text-sm text-gray-400">/100</span>}
      </p>
      {level && <p className="text-[0.625rem] font-semibold text-gray-500 dark:text-gray-400">{level}</p>}
    </div>
  )
}

export default function RiskStressTest({ projectId, baseline }) {
  // baseline: { riskScore, riskLevel, sanctioned, expenditure, completion }
  const [inputs, setInputs] = useState(() => ({
    expenditure: Number(baseline?.expenditure || 0),
    completion_percentage: Number(baseline?.completion || 0),
    sanctioned_amount: Number(baseline?.sanctioned || 0),
  }))
  const [simulated, setSimulated] = useState(null)
  const [simStatus, setSimStatus] = useState("idle") // idle | simulating | error
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState(null)
  const debounceRef = useRef(null)
  const seqRef = useRef(0)

  // Debounced simulation on input change
  useEffect(() => {
    if (!dirty) return
    setSimStatus("simulating")
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      const seq = ++seqRef.current
      getProjectStressTest(projectId, {
        expenditure: inputs.expenditure,
        completion_percentage: inputs.completion_percentage,
        sanctioned_amount: inputs.sanctioned_amount,
      })
        .then((d) => { if (seq === seqRef.current) { setSimulated(d); setSimStatus("done"); setError(null) } })
        .catch(() => { if (seq === seqRef.current) { setSimStatus("error"); setError("Simulation failed — try again.") } })
    }, DEBOUNCE_MS)
    return () => clearTimeout(debounceRef.current)
  }, [inputs, dirty, projectId])

  const setField = (field, raw) => {
    const n = Number(raw)
    setInputs((prev) => ({ ...prev, [field]: Number.isFinite(n) ? n : 0 }))
    setDirty(true)
  }
  const reset = () => {
    setInputs({
      expenditure: Number(baseline?.expenditure || 0),
      completion_percentage: Number(baseline?.completion || 0),
      sanctioned_amount: Number(baseline?.sanctioned || 0),
    })
    setDirty(false)
    setSimulated(null)
    setError(null)
  }

  const serverCurrent = simulated?.current
  const scoreMismatch = serverCurrent && serverCurrent.risk_score !== baseline?.riskScore
  const simScore = simulated?.simulated?.risk_score
  const simLevel = simulated?.simulated?.risk_level
  const delta = simulated?.score_delta
  const drivers = simulated?.risk_drivers || []
  const method = simulated?.method
  const statusNote = simulated?.status_note

  return (
    <div className="space-y-3">
      {/* Simulation banner */}
      <div className="flex items-center gap-2 rounded-xl border border-purple-300 bg-purple-50 px-3 py-2 dark:border-purple-800 dark:bg-purple-950/40">
        <span className="text-sm">🧪</span>
        <p className="text-[0.6875rem] font-bold text-purple-700 dark:text-purple-300">
          SIMULATION — Does not modify actual project data. Stored scores, records and the ML model are untouched.
        </p>
      </div>

      {/* Current vs simulated */}
      <div className="flex gap-3">
        <ScoreCard label="Current Risk" score={baseline?.riskScore ?? 0} level={baseline?.riskLevel} />
        <ScoreCard
          label={simStatus === "simulating" ? "Simulating…" : "Simulated Risk"}
          score={simScore}
          level={simLevel}
          accent={delta != null && delta !== 0 ? "border-blue-300 bg-blue-50 dark:border-blue-800 dark:bg-blue-950/40" : undefined}
        />
      </div>
      {delta != null && delta !== 0 && (
        <p className="rise-in text-center font-mono text-xs font-bold text-gray-500 dark:text-gray-400">
          Δ {delta > 0 ? "+" : ""}{delta} vs stored score
        </p>
      )}
      {scoreMismatch && (
        <p className="rounded-lg bg-amber-50 px-2.5 py-1.5 text-[0.625rem] leading-relaxed text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
          Note: the stored score is {baseline?.riskScore}, but the rule engine scores the project's current recorded values at {serverCurrent.risk_score}. The stored score may predate a data correction.
        </p>
      )}
      {error && simStatus === "error" && (
        <p className="rise-in rounded-lg bg-red-50 px-2.5 py-1.5 text-[0.625rem] font-semibold text-red-700 dark:bg-red-950/40 dark:text-red-300">{error}</p>
      )}

      {/* Inputs */}
      <div className="space-y-2.5 rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-[#000000]">
        <div>
          <div className="flex items-baseline justify-between">
            <label className="text-[0.6875rem] font-bold">Expenditure (₹)</label>
            <span className="font-mono text-[0.625rem] text-gray-400">recorded: {formatMoney(baseline?.expenditure || 0)}</span>
          </div>
          <input type="number" min="0" value={inputs.expenditure}
            onChange={(e) => setField("expenditure", e.target.value)}
            className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 font-mono text-xs focus:border-blue-500 focus:outline-none dark:border-gray-600 dark:bg-[#0d090b]" />
        </div>
        <div>
          <div className="flex items-baseline justify-between">
            <label className="text-[0.6875rem] font-bold">Reported progress (%)</label>
            <span className="font-mono text-[0.625rem] text-gray-400">recorded: {Number(baseline?.completion || 0).toFixed(0)}%</span>
          </div>
          <input type="number" min="0" max="100" value={inputs.completion_percentage}
            onChange={(e) => setField("completion_percentage", e.target.value)}
            className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 font-mono text-xs focus:border-blue-500 focus:outline-none dark:border-gray-600 dark:bg-[#0d090b]" />
        </div>
        <div>
          <div className="flex items-baseline justify-between">
            <label className="text-[0.6875rem] font-bold">Sanctioned amount (₹)</label>
            <span className="font-mono text-[0.625rem] text-gray-400">recorded: {formatMoney(baseline?.sanctioned || 0)}</span>
          </div>
          <input type="number" min="0" value={inputs.sanctioned_amount}
            onChange={(e) => setField("sanctioned_amount", e.target.value)}
            className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 font-mono text-xs focus:border-blue-500 focus:outline-none dark:border-gray-600 dark:bg-[#0d090b]" />
        </div>
        <div className="flex items-center justify-between gap-2">
          <p className="text-[0.625rem] text-gray-400">Status and the stored ML anomaly flag are held as recorded (ML is not re-predicted on hypothetical values).</p>
          <button onClick={reset} className="shrink-0 rounded-lg border border-gray-300 px-2.5 py-1 text-[0.625rem] font-bold text-gray-600 transition hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
            Reset
          </button>
        </div>
      </div>

      {/* Rule contributions */}
      {drivers.length > 0 && (
        <div className="rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-[#000000]">
          <p className="text-[0.625rem] font-bold uppercase tracking-wider text-gray-400">Risk Drivers — rule contributions</p>
          <div className="mt-2.5 space-y-1.5">
            {drivers.map((d, i) => {
              const simPts = d.simulated_points || 0
              const curPts = d.current_points || 0
              const changedRow = d.status !== "unchanged"
              return (
                <div key={i} className={`flex items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 transition-colors ${changedRow ? "bg-blue-50 dark:bg-blue-950/30" : "bg-white dark:bg-[#0d090b]"}`}>
                  <span className="min-w-0 truncate text-[0.6875rem] font-semibold">{d.reason}</span>
                  <span className="shrink-0 font-mono text-[0.6875rem]">
                    {curPts === simPts
                      ? <span className="text-gray-400">{simPts > 0 ? `+${simPts}` : "0"}</span>
                      : <span className="text-gray-400">{curPts > 0 ? `+${curPts}` : "0"} → </span>}
                    {curPts !== simPts && <span className={`font-bold ${simPts > curPts ? "text-red-600 dark:text-red-400" : "text-green-600 dark:text-green-400"}`}>{simPts > 0 ? `+${simPts}` : "0"}</span>}
                  </span>
                </div>
              )
            })}
          </div>
          {method && <p className="mt-2 text-[0.625rem] text-gray-400">{method}</p>}
          {statusNote && <p className="mt-1 text-[0.625rem] text-gray-400">{statusNote}</p>}
        </div>
      )}
    </div>
  )
}
