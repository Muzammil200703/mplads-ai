import { useCallback, useEffect, useState } from "react"
import { getVendorBehaviourScan } from "../services/api"
import { formatMoney, formatNumber } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════════
   VENDOR BEHAVIOUR SCAN
   ─────────────────────────────────────────────────────────────────────
   Dataset-level screening over vendors large enough to characterise. The
   scan is opt-in because it aggregates every vendor; results are cached on
   the server and every threshold is shown so a reviewer can see exactly why
   a vendor surfaced. Flags are leads, never conclusions.
   ═══════════════════════════════════════════════════════════════════ */

const THRESHOLD_LABELS = {
  min_transactions: "Minimum payments to be screened",
  amount_variety_below: "Amount variety below (more repeats)",
  largest_payment_share_at_or_above: "Largest payment share at or above",
  top_mp_share_at_or_above: "Top-MP share at or above",
  single_mp_min_transactions: "Single-MP vendors: minimum payments",
  single_mp_spend_at_or_above: "Single-MP vendors: expenditure at or above",
  payments_per_month_at_or_above: "Payments per month at or above",
  pending_share_at_or_above: "In-progress payments share at or above",
  multi_constituency_min: "Constituencies for multi-constituency flag",
  multi_state_min: "States for multi-state flag",
  top_1pct_expenditure: "Top-1% expenditure reference",
}

function formatThreshold(key, value) {
  if (typeof value !== "number") return String(value)
  if (key.includes("share") || key.includes("variety")) {
    return `${(value * 100).toFixed(1)}%`
  }
  if (key.includes("expenditure") || key.includes("spend")) return formatMoney(value)
  return formatNumber(value)
}

export default function VendorBehaviourScan({ house = "", onSelectVendor }) {
  const [state, setState] = useState({ items: [], stats: null, total: 0, capped: false, loading: false, error: "", started: false })
  const [flag, setFlag] = useState("")

  const run = useCallback(
    async (nextFlag = "", skip = 0, append = false) => {
      setState((current) => ({ ...current, loading: true, error: "", started: true }))
      try {
        const payload = await getVendorBehaviourScan({
          house: house || undefined,
          flag: nextFlag || undefined,
          skip,
          limit: 20,
        })
        setState((current) => ({
          items: append ? [...current.items, ...payload.items] : payload.items,
          stats: payload.stats,
          total: payload.total,
          capped: payload.capped,
          loading: false,
          error: "",
          started: true,
        }))
      } catch (error) {
        setState((current) => ({ ...current, loading: false, error: error.message || "Behaviour scan failed" }))
      }
    },
    [house]
  )

  // A house change invalidates previous results (different population).
  useEffect(() => {
    setState((current) => ({ ...current, items: [], stats: null, total: 0, started: false }))
    setFlag("")
  }, [house])

  return (
    <section className="rounded-xl border border-[#e8dadc] bg-white p-4 shadow-sm dark:border-[#351923] dark:bg-[#0d090b]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="font-bold text-[#292326] dark:text-white">Vendor behaviour scan</h2>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-[#71666a] dark:text-[#b9aeb2]">
            Screens every vendor with enough recorded payments and flags unusual concentration, repeated amounts,
            burst activity and pending payments against the screened population. Each flag is an audit lead.
          </p>
        </div>
        <button
          type="button"
          onClick={() => run(flag, 0, false)}
          disabled={state.loading}
          className="rounded-lg bg-[#c8102e] px-3 py-1.5 text-xs font-semibold text-white hover:bg-[#a50f29] disabled:opacity-60"
        >
          {state.loading ? "Scanning…" : state.started ? "Re-run scan" : "Run behaviour scan"}
        </button>
      </div>

      {state.error && (
        <p className="mt-3 rounded-lg border border-red-200 bg-red-50 p-2.5 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {state.error}
        </p>
      )}

      {state.loading && state.items.length === 0 && (
        <p className="mt-3 text-xs text-[#71666a] dark:text-[#b9aeb2]">
          Characterising every vendor in the dataset — this takes a few seconds on the first run, then serves from cache.
        </p>
      )}

      {state.stats && (
        <>
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div className="rounded-lg border border-[#e8dadc] p-2.5 text-xs dark:border-[#351923]">
              <p className="text-[0.625rem] uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">Vendors screened</p>
              <p className="mt-0.5 font-mono text-base font-bold text-[#292326] dark:text-white">{formatNumber(state.stats.evaluated_vendors)}</p>
            </div>
            <div className="rounded-lg border border-[#e8dadc] p-2.5 text-xs dark:border-[#351923]">
              <p className="text-[0.625rem] uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">With at least one flag</p>
              <p className="mt-0.5 font-mono text-base font-bold text-[#292326] dark:text-white">{formatNumber(state.stats.flagged_vendors)}</p>
            </div>
            <div className="rounded-lg border border-[#e8dadc] p-2.5 text-xs dark:border-[#351923]">
              <p className="text-[0.625rem] uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">Minimum payments</p>
              <p className="mt-0.5 font-mono text-base font-bold text-[#292326] dark:text-white">{state.stats.thresholds.min_transactions}</p>
            </div>
            <div className="rounded-lg border border-[#e8dadc] p-2.5 text-xs dark:border-[#351923]">
              <p className="text-[0.625rem] uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">Results shown</p>
              <p className="mt-0.5 font-mono text-base font-bold text-[#292326] dark:text-white">{formatNumber(state.items.length)} / {formatNumber(state.total)}</p>
            </div>
          </div>

          <div className="mt-3 flex flex-wrap gap-1.5">
            <button
              type="button"
              onClick={() => { setFlag(""); run("", 0, false) }}
              className={`rounded-full border px-2.5 py-1 text-[0.6875rem] font-semibold ${
                flag === "" ? "border-[#c8102e] bg-blue-50 text-[#c8102e] dark:bg-blue-950/30" : "border-[#e8dadc] text-[#71666a] dark:border-[#351923] dark:text-gray-300"
              }`}
            >
              All flags
            </button>
            {Object.entries(state.stats.flag_counts).map(([key, count]) => (
              <button
                key={key}
                type="button"
                onClick={() => { setFlag(key); run(key, 0, false) }}
                className={`rounded-full border px-2.5 py-1 text-[0.6875rem] font-semibold ${
                  flag === key ? "border-[#c8102e] bg-blue-50 text-[#c8102e] dark:bg-blue-950/30" : "border-[#e8dadc] text-[#71666a] dark:border-[#351923] dark:text-gray-300"
                }`}
              >
                {key.replaceAll("_", " ")} · {count}
              </button>
            ))}
          </div>

          <div className="mt-3 divide-y divide-[#e8dadc] dark:divide-[#351923]">
            {state.items.map((vendor) => (
              <div key={vendor.vendor_key} className="flex flex-wrap items-start justify-between gap-2 py-2.5">
                <div className="min-w-0">
                  <button
                    type="button"
                    onClick={() => onSelectVendor?.(vendor.vendor_key, vendor.vendor_name)}
                    className="truncate text-left text-sm font-semibold text-[#292326] hover:text-[#c8102e] hover:underline dark:text-white"
                  >
                    {vendor.vendor_name}
                  </button>
                  <p className="mt-0.5 text-[0.6875rem] text-[#71666a] dark:text-[#b9aeb2]">
                    {formatMoney(vendor.total_expenditure)} · {formatNumber(vendor.transactions)} payments ·{" "}
                    {vendor.mp_count} MP(s) · {vendor.constituency_count} constituencies ·{" "}
                    {vendor.payments_per_month}/month
                  </p>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {vendor.flag_labels.map((label) => (
                      <span key={label} className="rounded-sm bg-amber-50 px-1.5 py-0.5 text-[0.625rem] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                        {label}
                      </span>
                    ))}
                  </div>
                </div>
                <span className="shrink-0 rounded-full bg-[#faf7f6] px-2 py-0.5 text-[0.6875rem] font-bold text-[#71666a] dark:bg-[#120b0e] dark:text-gray-300">
                  lead score {(vendor.severity * 100).toFixed(0)}
                </span>
              </div>
            ))}
            {state.items.length === 0 && !state.loading && (
              <p className="py-3 text-xs text-gray-500">No vendor carries this flag in the current scope.</p>
            )}
          </div>

          {state.items.length < state.total && (
            <button
              type="button"
              onClick={() => run(flag, state.items.length, true)}
              disabled={state.loading}
              className="mt-3 rounded-lg border border-[#c8102e] px-3 py-1.5 text-xs font-semibold text-[#c8102e] hover:bg-blue-50 disabled:opacity-60 dark:hover:bg-blue-950/30"
            >
              {state.loading ? "Loading…" : "Load more leads"}
            </button>
          )}

          <details className="mt-3 rounded-lg border border-[#e8dadc] p-2.5 text-[0.6875rem] dark:border-[#351923]">
            <summary className="cursor-pointer font-semibold text-[#292326] dark:text-gray-200">
              Thresholds used for this scan
            </summary>
            <dl className="mt-2 grid grid-cols-1 gap-1 sm:grid-cols-2">
              {Object.entries(state.stats.thresholds)
                .filter(([key]) => THRESHOLD_LABELS[key])
                .map(([key, value]) => (
                  <div key={key} className="flex justify-between gap-2">
                    <dt className="text-[#71666a] dark:text-[#b9aeb2]">{THRESHOLD_LABELS[key]}</dt>
                    <dd className="font-mono font-semibold text-[#292326] dark:text-gray-200">{formatThreshold(key, value)}</dd>
                  </div>
                ))}
            </dl>
            <p className="mt-2 text-[0.625rem] text-[#b9aeb2]">
              {state.stats.thresholds.basis}. Flags describe statistical patterns in recorded data, not misconduct.
            </p>
          </details>
        </>
      )}
    </section>
  )
}
