import { formatMoney } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════
   PROJECT OUTCOME PREDICTION CARD
   Rendered inside the existing assistant message stream when the answer
   chain returns the prediction payload (backend/prediction_assistant.py).
   Every number, label and limitation comes from /predict — nothing is
   computed or inferred in the browser. When the backend withholds an
   estimate (thin history, weak validation) the card says so explicitly.
   ═══════════════════════════════════════════════════════════════════ */

const pct = (v) => (v == null || Number.isNaN(Number(v)) ? null : `${Math.round(Number(v) * 100)}%`)

const CONFIDENCE_TONE = {
  high: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  moderate: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  "cohort-only": "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300",
  recorded: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
}

function Stat({ label, value, hint, tone }) {
  return (
    <div className={`rounded-lg p-2 ${tone || "bg-[#f8f1f1] dark:bg-[#120b0e]"}`}>
      <p className="text-[0.5938rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">{label}</p>
      <p className="mt-0.5 font-mono text-[0.9375rem] font-bold text-[#292326] dark:text-gray-100">{value}</p>
      {hint && <p className="text-[0.5938rem] leading-snug text-[#71666a] dark:text-[#b9aeb2]">{hint}</p>}
    </div>
  )
}

export default function OutcomePredictionCard({ data }) {
  if (!data) return null
  const current = data.current || {}
  const outcome = data.outcome || {}
  const cohort = data.cohort || {}
  const validation = data.validation || {}
  const metrics = validation.metrics || {}
  const drivers = Array.isArray(data.drivers) ? data.drivers : []
  const forecast = Array.isArray(data.forecast) ? data.forecast : []
  const limitations = Array.isArray(data.limitations) ? data.limitations : []
  const disclaimers = Array.isArray(data.disclaimers) ? data.disclaimers : []

  const prob = outcome.probability_complete
  const probPct = pct(prob)
  const confidence = outcome.confidence || "cohort-only"

  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-[#e8dadc] dark:border-[#351923]">
      <div className="flex items-center justify-between gap-2 bg-[#f8f1f1] px-2 py-1.5 dark:bg-[#120b0e]">
        <p className="text-[0.6563rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">
          Outcome prediction · #{data.project_id} · {data.model_version || "model"}
        </p>
        <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[0.5625rem] font-bold uppercase ${CONFIDENCE_TONE[confidence] || CONFIDENCE_TONE["cohort-only"]}`}>
          {confidence}
        </span>
      </div>

      <div className="space-y-2.5 p-2.5">
        <p className="text-[0.75rem] leading-snug text-[#292326] dark:text-gray-100">
          <span className="font-bold">{outcome.prediction}</span>
          {current.status ? <> · recorded {String(current.status).toLowerCase()}</> : null}
          {current.completion_percentage != null ? <> at {current.completion_percentage}% progress</> : null}
          {current.sanctioned ? <> · sanctioned {formatMoney(current.sanctioned)}</> : null}
          {current.expenditure ? <> · linked payments {formatMoney(current.expenditure)}</> : <> · no linked payments</>}
        </p>

        <div className="grid grid-cols-2 gap-1.5">
          <Stat
            label="Completion probability"
            value={probPct || "Withheld"}
            hint={probPct ? outcome.confidence_basis : "insufficient validated history"}
            tone={probPct ? "bg-green-50 dark:bg-green-950/30" : "bg-amber-50 dark:bg-amber-950/30"}
          />
          <Stat
            label="Delay likelihood at current age"
            value={pct(outcome.delay_probability) || "—"}
            hint={cohort.sample_completed ? `cohort n=${cohort.sample_completed}` : "cohort too small"}
            tone="bg-amber-50 dark:bg-amber-950/30"
          />
          <Stat
            label="Estimated remaining"
            value={outcome.expected_remaining_months != null ? `${outcome.expected_remaining_months} mo` : "—"}
            hint={outcome.expected_duration_months_total != null ? `typical completion by month ${outcome.expected_duration_months_total}` : "no cohort duration"}
          />
          <Stat
            label="Expected final expenditure"
            value={outcome.expected_final_expenditure != null ? formatMoney(outcome.expected_final_expenditure) : "—"}
            hint={cohort.cost_basis?.cohort_median_ratio ? `cohort ratio ${cohort.cost_basis.cohort_median_ratio}×` : "no cohort cost pattern"}
          />
        </div>

        {outcome.stalled_probability != null && (
          <p className="text-[0.6875rem] leading-snug text-[#71666a] dark:text-[#b9aeb2]">
            Stalled-execution likelihood: <span className="font-bold">{pct(outcome.stalled_probability)}</span> (no linked payments at this age).
          </p>
        )}

        {forecast.length > 0 && (
          <div>
            <p className="mb-1 text-[0.625rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">
              Cohort completion outlook {cohort.key ? `· ${cohort.key}` : ""}
            </p>
            <div className="space-y-1">
              {forecast.map((f) => (
                <div key={f.month} className="flex items-center gap-1.5">
                  <span className="w-8 shrink-0 font-mono text-[0.5938rem] text-[#71666a] dark:text-[#b9aeb2]">{f.label}</span>
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-[#f1e6e8] dark:bg-[#241319]">
                    <div className="h-full rounded-full bg-[#c8102e] dark:bg-blue-500" style={{ width: `${Math.min(100, Math.round((f.probability_complete || 0) * 100))}%` }} />
                  </div>
                  <span className="w-8 shrink-0 text-right font-mono text-[0.5938rem] text-[#292326] dark:text-gray-200">{pct(f.probability_complete)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {drivers.length > 0 && (
          <div>
            <p className="mb-1 text-[0.625rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">Main factors</p>
            <ul className="space-y-0.5">
              {drivers.slice(0, 4).map((d) => (
                <li key={d.factor} className="flex items-center justify-between gap-2 text-[0.6875rem] leading-snug">
                  <span className="min-w-0 truncate text-[#292326] dark:text-gray-100">{d.label}</span>
                  <span className={`shrink-0 font-mono text-[0.625rem] ${Number(d.contribution) >= 0 ? "text-green-700 dark:text-green-400" : "text-[#c8102e] dark:text-red-400"}`}>
                    {Number(d.contribution) >= 0 ? "+" : ""}{d.contribution}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {(metrics.holdout_auc != null || validation.note) && (
          <p className="rounded bg-[#faf7f6] px-2 py-1 text-[0.625rem] leading-snug text-[#71666a] dark:bg-[#120b0e] dark:text-[#b9aeb2]">
            {metrics.holdout_auc != null
              ? <>Time-aware hold-out: AUC {metrics.holdout_auc}, Brier {metrics.holdout_brier ?? "—"}, validated on {metrics.holdout_projects ?? "—"} later-vintage projects it never saw.</>
              : validation.note}
          </p>
        )}

        {limitations.length > 0 && (
          <div className="rounded bg-amber-50 p-2 dark:bg-amber-950/20">
            <p className="text-[0.5938rem] font-bold uppercase tracking-wide text-amber-700 dark:text-amber-300">Limitations</p>
            <ul className="mt-0.5 list-disc space-y-0.5 pl-3.5 text-[0.625rem] leading-snug text-amber-800 dark:text-amber-200">
              {limitations.map((l, i) => <li key={i}>{l}</li>)}
            </ul>
          </div>
        )}

        {disclaimers.length > 0 && (
          <p className="text-[0.5938rem] leading-snug text-[#71666a] dark:text-[#b9aeb2]">{disclaimers.join(" ")}</p>
        )}
      </div>
    </div>
  )
}
