import { useEffect, useMemo, useState } from "react"
import { getVendorComparison } from "../services/api"
import { formatMoney, formatNumber } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════════
   VENDOR COMPARISON
   ─────────────────────────────────────────────────────────────────────
   Side-by-side, like-for-like comparison of up to four vendors. The server
   aggregates every vendor with the same queries, returns peer percentile
   ranks so different sizes stay comparable, and lists each vendor's data
   gaps. Values are never interpolated: a missing figure renders as "—".
   ═══════════════════════════════════════════════════════════════════ */

const ROWS = [
  { key: "risk_score", label: "Vendor risk (indicative)", render: (v) => `${v.risk_score} · ${v.risk_category}` },
  { key: "total_expenditure", label: "Recorded expenditure", render: (v) => formatMoney(v.total_expenditure) },
  { key: "transaction_count", label: "Transactions", render: (v) => formatNumber(v.transaction_count) },
  { key: "average_transaction", label: "Average payment", render: (v) => formatMoney(v.average_transaction) },
  { key: "largest_transaction_share", label: "Largest payment share", render: (v) => `${v.largest_transaction_share}%` },
  { key: "works", label: "Works recorded", render: (v) => formatNumber(v.works) },
  { key: "mp_count", label: "MPs", render: (v) => formatNumber(v.mp_count) },
  { key: "constituency_count", label: "Constituencies", render: (v) => formatNumber(v.constituency_count) },
  { key: "state_count", label: "States", render: (v) => formatNumber(v.state_count) },
  { key: "top_mp_share", label: "Largest MP share", render: (v) => `${v.top_mp_share}%` },
  { key: "in_progress_share", label: "Payments in progress", render: (v) => `${v.in_progress_share}%` },
  { key: "repeated_amount_share", label: "Repeated-amount payments", render: (v) => `${v.repeated_amount_share}%` },
  { key: "burst_ratio", label: "Busiest 30-day window", render: (v) => `${v.burst_ratio}× monthly average` },
  { key: "linked_projects", label: "Linked projects", render: (v) => (v.linked_projects ? formatNumber(v.linked_projects) : "—") },
  { key: "projects_completed", label: "Completed (linked)", render: (v) => (v.linked_projects ? formatNumber(v.projects_completed) : "—") },
  { key: "projects_in_progress", label: "In progress (linked)", render: (v) => (v.linked_projects ? formatNumber(v.projects_in_progress) : "—") },
  { key: "projects_not_started", label: "Not started (linked)", render: (v) => (v.linked_projects ? formatNumber(v.projects_not_started) : "—") },
  { key: "high_risk_projects", label: "High-risk linked projects", render: (v) => (v.linked_projects ? formatNumber(v.high_risk_projects) : "—") },
  { key: "expenditure_percentile", label: "Expenditure percentile rank", render: (v) => `${v.expenditure_percentile}th` },
  { key: "transaction_percentile", label: "Transaction-count percentile", render: (v) => `${v.transaction_percentile}th` },
  { key: "average_transaction_percentile", label: "Average-payment percentile", render: (v) => `${v.average_transaction_percentile}th` },
]

const NUMERIC_MAX = new Set(["largest_transaction_share", "top_mp_share", "in_progress_share", "repeated_amount_share", "burst_ratio", "risk_score"])

export default function VendorComparison({ vendorKeys = [], house = "", onRemove, onClose }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  // Stable signature so the effect re-runs only when the selected set changes.
  const keysSignature = vendorKeys.join(",")
  const keys = useMemo(() => (keysSignature ? keysSignature.split(",") : []), [keysSignature])

  useEffect(() => {
    let active = true
    if (keys.length === 0) {
      setData(null)
      setLoading(false)
      return () => { active = false }
    }
    setLoading(true)
    setError("")
    getVendorComparison(keys, { house: house || undefined })
      .then((payload) => { if (active) setData(payload) })
      .catch((err) => { if (active) setError(err.message || "Unable to compare these vendors") })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [keys, house])

  if (vendorKeys.length === 0) return null

  const vendors = data?.vendors || []

  return (
    <section className="mt-5 rounded-xl border border-[#e8dadc] bg-white p-4 shadow-sm dark:border-[#351923] dark:bg-[#0d090b]">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-bold text-[#292326] dark:text-white">Compare vendors</h2>
          <p className="mt-1 text-xs text-[#71666a] dark:text-[#b9aeb2]">
            {vendors.length} vendor(s) · scope {data?.house_scope || "loading"} · same aggregation for every vendor
          </p>
        </div>
        {onClose && (
          <button type="button" onClick={onClose} className="text-xs text-[#c8102e] underline">
            Clear comparison
          </button>
        )}
      </div>

      {loading && <p className="mt-3 text-xs text-[#71666a] dark:text-[#b9aeb2]">Aggregating selected vendors…</p>}
      {error && (
        <p className="mt-3 rounded-lg border border-red-200 bg-red-50 p-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}

      {!loading && !error && vendors.length > 0 && (
        <>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[680px] border-collapse text-left text-xs">
              <thead>
                <tr className="border-b border-[#e8dadc] dark:border-[#351923]">
                  <th className="sticky left-0 z-10 bg-white p-2 text-[0.625rem] font-semibold uppercase tracking-wider text-[#71666a] dark:bg-[#0d090b] dark:text-[#b9aeb2]">
                    Metric
                  </th>
                  {vendors.map((vendor) => (
                    <th key={vendor.vendor_key} className="p-2 align-top">
                      <span className="block max-w-[14rem] truncate text-xs font-bold text-[#292326] dark:text-white" title={vendor.vendor_name}>
                        {vendor.vendor_name}
                      </span>
                      <span className="mt-0.5 block text-[0.625rem] font-normal text-[#71666a] dark:text-[#b9aeb2]">{vendor.vendor_type}</span>
                      {onRemove && (
                        <button type="button" onClick={() => onRemove(vendor.vendor_key)} className="mt-1 text-[0.625rem] text-[#c8102e] underline">
                          remove
                        </button>
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ROWS.map((row) => {
                  const values = vendors.map((vendor) => Number(vendor[row.key]) || 0)
                  const max = Math.max(...values)
                  return (
                    <tr key={row.key} className="border-b border-[#faf7f6] dark:border-[#29131b]">
                      <th scope="row" className="sticky left-0 z-10 bg-white p-2 text-[0.6875rem] font-medium text-[#71666a] dark:bg-[#0d090b] dark:text-gray-300">
                        {row.label}
                      </th>
                      {vendors.map((vendor, index) => {
                        const emphasise = NUMERIC_MAX.has(row.key) && values[index] === max && values.filter((value) => value === max).length < values.length
                        return (
                          <td
                            key={`${vendor.vendor_key}-${row.key}`}
                            className={`p-2 align-top font-mono text-[0.6875rem] ${emphasise ? "font-bold text-amber-700 dark:text-amber-300" : "text-[#292326] dark:text-gray-200"}`}
                          >
                            {row.render(vendor)}
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-[0.625rem] text-[#b9aeb2]">
            Emphasised cells mark the highest value in the row for anomaly-style metrics — a comparison aid, not a judgement.
            “—” means the dataset has no reliable figure for that vendor.
          </p>

          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
            {vendors.filter((vendor) => vendor.data_gaps.length > 0).map((vendor) => (
              <div key={`gap-${vendor.vendor_key}`} className="rounded-lg border border-dashed border-[#e8dadc] p-2.5 text-[0.6875rem] dark:border-[#351923]">
                <p className="font-semibold text-[#292326] dark:text-gray-200">{vendor.vendor_name} — data gaps</p>
                <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[#71666a] dark:text-[#b9aeb2]">
                  {vendor.data_gaps.map((gap) => <li key={gap}>{gap}</li>)}
                </ul>
              </div>
            ))}
          </div>

          {data.notes.length > 0 && (
            <ul className="mt-3 space-y-1 text-[0.625rem] text-[#71666a] dark:text-[#b9aeb2]">
              {data.notes.map((note) => <li key={note}>• {note}</li>)}
            </ul>
          )}
          <p className="mt-2 text-[0.625rem] leading-relaxed text-[#b9aeb2]">{data.disclaimer}</p>
        </>
      )}
    </section>
  )
}
