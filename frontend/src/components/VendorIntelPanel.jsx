import { useCallback, useEffect, useMemo, useState } from "react"
import {
  askVendorAssistant,
  getVendorActivity,
  getVendorPerformance,
  getVendorRelationships,
  getVendorRiskAnalysis,
} from "../services/api"
import { formatDateHuman, formatMoney, formatNumber } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════════
   VENDOR INTELLIGENCE PANEL
   ─────────────────────────────────────────────────────────────────────
   Shared slide-over used by the Vendor Intelligence directory and the
   Vendor Network page. Every figure is rendered from the analysis
   endpoints (performance / risk-analysis / relationships / activity /
   assistant); nothing is computed or guessed in the browser.

   Presentation rules encoded here:
   • Linkage coverage is always shown next to completion or category data,
     so a missing project link never reads as "not completed".
   • Risk never renders as a verdict: the category is labelled as an audit
     lead and the method/disclaimer text is part of the panel.
   • Anomalies and "possible name variants" are visually separated from
     confirmed record facts.
   ═══════════════════════════════════════════════════════════════════ */

const TABS = [
  { id: "profile", label: "Performance" },
  { id: "risk", label: "Risk & behaviour" },
  { id: "network", label: "Network" },
  { id: "timeline", label: "Timeline" },
  { id: "ask", label: "AI analyst" },
]

const HOUSES = [
  { value: "", label: "All houses" },
  { value: "Lok Sabha", label: "Lok Sabha" },
  { value: "Rajya Sabha", label: "Rajya Sabha" },
]

const SUGGESTED_QUESTIONS = [
  "Why has this vendor been flagged?",
  "Which works contribute most to this vendor's expenditure?",
  "Which MPs are associated with this vendor?",
  "What unusual transaction patterns exist?",
  "Compare this vendor with similar vendors",
  "Which records should an auditor examine?",
]

const SEVERITY_STYLES = {
  High: "bg-red-50 text-red-700 border-red-200 dark:bg-red-950/50 dark:text-red-300 dark:border-red-900",
  Moderate: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/50 dark:text-amber-300 dark:border-amber-900",
  Low: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/50 dark:text-emerald-300 dark:border-emerald-900",
}

function StatTile({ label, value, detail }) {
  return (
    <div className="rounded-lg border border-[#e8dadc] bg-white p-3 dark:border-[#351923] dark:bg-[#0d090b]">
      <p className="text-[0.625rem] font-bold uppercase tracking-wider text-[#71666a] dark:text-[#b9aeb2]">{label}</p>
      <p className="mt-1 font-mono text-lg font-bold leading-tight text-[#292326] dark:text-white">{value}</p>
      {detail && <p className="mt-0.5 text-[0.6875rem] leading-snug text-[#71666a] dark:text-[#b9aeb2]">{detail}</p>}
    </div>
  )
}

function BarRow({ label, value, share, meta, tone = "bg-[#c8102e]" }) {
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="min-w-0 truncate font-medium text-[#292326] dark:text-gray-200" title={label}>{label}</span>
        <span className="shrink-0 font-mono text-[#71666a] dark:text-[#b9aeb2]">{meta ?? formatMoney(value)}</span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-[#e8dadc] dark:bg-gray-700">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.max(2, Math.min(100, share || 0))}%` }} />
      </div>
    </div>
  )
}

function Section({ title, description, children, right }) {
  return (
    <section className="rounded-xl border border-[#e8dadc] bg-white p-4 shadow-sm dark:border-[#351923] dark:bg-[#0d090b]">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-bold text-[#292326] dark:text-white">{title}</h3>
          {description && <p className="mt-1 text-xs leading-relaxed text-[#71666a] dark:text-[#b9aeb2]">{description}</p>}
        </div>
        {right}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  )
}

function ErrorNote({ message, onRetry }) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs dark:border-red-900 dark:bg-red-950/40">
      <p className="font-semibold text-red-700 dark:text-red-300">{message}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-2 rounded-sm border border-red-300 bg-white px-2 py-1 font-semibold text-red-700 hover:bg-red-100 dark:border-red-800 dark:bg-transparent dark:text-red-300"
        >
          Retry
        </button>
      )}
    </div>
  )
}

function Loading({ label = "Loading analysis…" }) {
  return (
    <div className="space-y-2" aria-busy="true">
      <p className="text-xs text-[#71666a] dark:text-[#b9aeb2]">{label}</p>
      <div className="h-16 animate-pulse rounded-lg bg-[#e8dadc] dark:bg-gray-800" />
      <div className="h-16 animate-pulse rounded-lg bg-[#e8dadc] dark:bg-gray-800" />
    </div>
  )
}

/* ── Performance tab ─────────────────────────────────────────────── */

function PerformanceTab({ data }) {
  const totals = data.totals
  const paymentTotal = data.payment_status.reduce((sum, row) => sum + row.count, 0) || 1
  const mpMax = data.mp_distribution[0]?.amount || 1

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <StatTile label="Recorded expenditure" value={formatMoney(totals.total_expenditure)} detail={`${totals.expenditure_share}% of all vendor-linked spend`} />
        <StatTile label="Works recorded" value={formatNumber(totals.work_count)} detail={`${formatNumber(totals.transaction_count)} payment rows`} />
        <StatTile label="MPs associated" value={formatNumber(totals.mp_count)} detail={`${formatNumber(totals.constituency_count)} constituencies · ${formatNumber(totals.state_count)} states`} />
        <StatTile label="Average per work" value={formatMoney(totals.work_count ? totals.total_expenditure / totals.work_count : 0)} detail={`Median payment ${formatMoney(totals.median_expenditure)}`} />
        <StatTile label="Largest payment" value={formatMoney(totals.largest_transaction)} detail={`${formatNumber(totals.active_months)} active months`} />
        <StatTile
          label="Expenditure percentile"
          value={`${totals.expenditure_percentile}th`}
          detail={`vs ${formatNumber(data.peer_context.vendor_count)} vendors · median ${formatMoney(data.peer_context.median_vendor_expenditure)}`}
        />
      </div>

      <Section title="Payment status" description="Recorded status of every payment row for this vendor.">
        <div className="space-y-2">
          {data.payment_status.map((row) => (
            <BarRow
              key={row.status}
              label={row.status}
              value={row.amount}
              share={(row.count / paymentTotal) * 100}
              meta={`${formatNumber(row.count)} (${row.share}%)`}
              tone={row.status === "Payment Success" ? "bg-emerald-500" : "bg-amber-500"}
            />
          ))}
          {data.payment_status.length === 0 && <p className="text-xs text-gray-500">No payment rows recorded.</p>}
        </div>
      </Section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Section title="MP distribution" description="Recorded expenditure per MP payer.">
          <div className="space-y-2">
            {data.mp_distribution.map((row) => (
              <BarRow
                key={row.label}
                label={row.label}
                value={row.amount}
                share={(row.amount / mpMax) * 100}
                meta={`${row.share}%`}
              />
            ))}
            {data.mp_distribution.length === 0 && <p className="text-xs text-gray-500">No MP recorded.</p>}
          </div>
        </Section>

        <Section title="Geography" description="Constituencies and states in which the vendor is recorded.">
          <div className="space-y-2">
            {data.constituency_distribution.slice(0, 6).map((row) => (
              <BarRow
                key={`${row.label}`}
                label={row.label}
                value={row.amount}
                share={row.share}
                meta={`${row.share}%`}
                tone="bg-sky-500"
              />
            ))}
            {data.constituency_distribution.length === 0 && <p className="text-xs text-gray-500">No constituency recorded.</p>}
          </div>
          <p className="mt-3 text-[0.6875rem] leading-relaxed text-[#71666a] dark:text-[#b9aeb2]">
            States: {data.state_distribution.map((row) => row.label).join(", ") || "—"}
          </p>
        </Section>
      </div>

      <Section
        title="Work categories"
        description={data.category_distribution.coverage_note}
      >
        {data.category_distribution.available ? (
          <div className="space-y-2">
            {data.category_distribution.items.map((row) => (
              <BarRow
                key={row.category}
                label={row.category}
                share={(row.works / (data.category_distribution.linked_works || 1)) * 100}
                value={0}
                meta={`${row.works} linked works`}
                tone="bg-violet-500"
              />
            ))}
          </div>
        ) : (
          <p className="text-xs text-gray-500">
            No work of this vendor matches a recommendation/completion row exactly, so no category can be attributed.
            Unmatched works are never classified by guessing.
          </p>
        )}
        <p className="mt-2 text-[0.6875rem] text-[#71666a] dark:text-[#b9aeb2]">
          Coverage: {data.coverage.works_with_category} of {data.coverage.works_recorded} works (
          {data.coverage.category_link_pct}%).
        </p>
      </Section>

      <Section title="Completion evidence" description={data.completion.coverage_note}>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <StatTile label="Linked projects" value={formatNumber(data.completion.linked_projects)} />
          <StatTile label="Completed" value={formatNumber(data.completion.completed)} />
          <StatTile label="In progress" value={formatNumber(data.completion.in_progress)} />
          <StatTile label="Not started" value={formatNumber(data.completion.not_started)} />
        </div>
        <p className="mt-2 text-[0.6875rem] leading-relaxed text-[#71666a] dark:text-[#b9aeb2]">
          {data.completion.linked_projects === 0
            ? "No exact project link exists for this vendor's work keys — completion status is unavailable, not zero."
            : `${data.completion.high_risk_linked} linked project(s) carry a High project risk score. Coverage: ${data.coverage.project_link_pct}% of this vendor's works.`}
        </p>
      </Section>

      <Section title="Monthly recorded activity" description="Payment volume per month (most recent 36 months with activity).">
        {data.monthly_activity.length === 0 ? (
          <p className="text-xs text-gray-500">No dated payments recorded.</p>
        ) : (
          <div className="flex h-28 items-end gap-1 overflow-x-auto">
            {(() => {
              const maxAmount = Math.max(...data.monthly_activity.map((m) => m.amount), 1)
              return data.monthly_activity.map((month) => (
                <div key={month.month} className="group flex min-w-[10px] flex-1 flex-col items-center justify-end gap-1">
                  <div
                    className="w-full rounded-t bg-[#c8102e]/70 transition group-hover:bg-[#c8102e]"
                    style={{ height: `${Math.max(3, (month.amount / maxAmount) * 88)}px` }}
                    title={`${month.month}: ${formatMoney(month.amount)} · ${month.transactions} payments`}
                  />
                  <span className="text-[0.5625rem] text-[#71666a] dark:text-[#b9aeb2]">{month.month?.slice(2)}</span>
                </div>
              ))
            })()}
          </div>
        )}
      </Section>

      <Section title="Largest works" description="Distinct works recorded in this vendor's payment rows, by expenditure.">
        <div className="divide-y divide-[#e8dadc] text-xs dark:divide-[#351923]">
          {data.works.map((work) => (
            <div key={`${work.work_description}-${work.constituency}`} className="flex items-start justify-between gap-3 py-2">
              <div className="min-w-0">
                <p className="font-medium text-[#292326] dark:text-gray-100">{work.work_description}</p>
                <p className="mt-0.5 text-[0.6875rem] text-[#71666a] dark:text-[#b9aeb2]">
                  {work.constituency}, {work.state} · {work.transactions} payments · {work.share}%
                </p>
              </div>
              <span className="shrink-0 font-mono font-semibold text-[#292326] dark:text-white">{formatMoney(work.amount)}</span>
            </div>
          ))}
          {data.works.length === 0 && <p className="py-2 text-gray-500">No work description recorded.</p>}
        </div>
      </Section>

      <p className="text-[0.6875rem] leading-relaxed text-[#71666a] dark:text-[#b9aeb2]">{data.method}</p>
    </div>
  )
}

/* ── Risk tab ────────────────────────────────────────────────────── */

function RiskGauge({ score, category }) {
  const tone = score >= 65 ? "#e14b4b" : score >= 45 ? "#f59e0b" : score >= 25 ? "#d98a08" : "#269b71"
  return (
    <div className="flex items-center gap-4">
      <div className="relative h-24 w-24 shrink-0">
        <svg viewBox="0 0 100 100" className="h-full w-full" role="img" aria-label={`Vendor risk score ${score} of 100`}>
          <circle cx="50" cy="50" r="42" fill="none" stroke="#e8dadc" strokeWidth="10" />
          <circle
            cx="50" cy="50" r="42" fill="none" stroke={tone} strokeWidth="10" strokeLinecap="round"
            strokeDasharray={`${(Math.min(100, score) / 100) * 264} 264`}
            transform="rotate(-90 50 50)"
          />
          <text x="50" y="52" textAnchor="middle" fontSize="24" fontWeight="700" fill={tone}>{Math.round(score)}</text>
          <text x="50" y="68" textAnchor="middle" fontSize="9" fill="#71666a">/ 100</text>
        </svg>
      </div>
      <div className="min-w-0">
        <p className="text-xs font-bold uppercase tracking-wider text-[#71666a] dark:text-[#b9aeb2]">Indicative category</p>
        <p className="text-xl font-bold text-[#292326] dark:text-white">{category}</p>
        <p className="mt-1 text-[0.6875rem] leading-relaxed text-[#71666a] dark:text-[#b9aeb2]">
          A composite of the indicators below. This is an audit lead, not a finding about the vendor.
        </p>
      </div>
    </div>
  )
}

function IndicatorCard({ indicator }) {
  const style = SEVERITY_STYLES[indicator.severity_label] || SEVERITY_STYLES.Low
  return (
    <div className="rounded-lg border border-[#e8dadc] p-3 dark:border-[#351923]">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="text-xs font-semibold text-[#292326] dark:text-gray-100">{indicator.label}</p>
        <span className={`rounded-full border px-2 py-0.5 text-[0.625rem] font-bold ${indicator.assessable ? style : "border-[#e8dadc] bg-[#faf7f6] text-[#71666a] dark:border-[#351923] dark:bg-[#000000] dark:text-[#b9aeb2]"}`}>
          {indicator.assessable ? `${indicator.severity_label} · weight ${indicator.weight}` : "Not assessable"}
        </span>
      </div>
      <p className="mt-1.5 text-[0.6875rem] font-medium text-[#292326] dark:text-gray-200">{indicator.value}</p>
      <p className="mt-1 text-[0.6875rem] text-[#71666a] dark:text-[#b9aeb2]">{indicator.peer_context}</p>
      <p className="mt-1.5 text-[0.6875rem] leading-relaxed text-[#71666a] dark:text-gray-300">{indicator.explanation}</p>
      {indicator.note && <p className="mt-1 text-[0.625rem] italic text-[#b9aeb2]">{indicator.note}</p>}
      {indicator.severity > 0 && indicator.assessable && (
        <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-[#e8dadc] dark:bg-gray-700">
          <div
            className={`h-full rounded-full ${indicator.severity >= 0.66 ? "bg-red-500" : indicator.severity >= 0.33 ? "bg-amber-500" : "bg-emerald-500"}`}
            style={{ width: `${Math.max(3, indicator.severity * 100)}%` }}
          />
        </div>
      )}
    </div>
  )
}

function RiskTab({ data }) {
  return (
    <div className="space-y-4">
      <Section title="Vendor risk assessment" description={data.category_detail}>
        <RiskGauge score={data.risk_score} category={data.risk_category} />
        <p className="mt-3 text-[0.6875rem] text-[#71666a] dark:text-[#b9aeb2]">
          Confidence: <strong className="text-[#000000] dark:text-gray-200">{data.confidence}</strong> ·
          Based on {formatNumber(data.transaction_count)} payments ({formatMoney(data.total_expenditure)}) in the{" "}
          {data.house_scope} scope.
        </p>
      </Section>

      {data.top_signals.length > 0 && (
        <Section title="Why this vendor is flagged" description="Indicators currently above their peer threshold, highest weighted first.">
          <ul className="space-y-2 text-xs">
            {data.top_signals.map((signal) => (
              <li key={signal.key} className="rounded-lg border border-amber-200 bg-amber-50 p-2.5 dark:border-amber-900 dark:bg-amber-950/30">
                <p className="font-semibold text-amber-900 dark:text-amber-200">{signal.label} — {signal.severity_label}</p>
                <p className="mt-0.5 text-amber-800 dark:text-amber-200/90">{signal.value}</p>
                <p className="mt-1 text-[0.6875rem] text-amber-800/80 dark:text-amber-200/70">{signal.explanation}</p>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="All indicators" description="Every dimension is measured against the vendor population in the same house scope.">
        <div className="space-y-2">
          {data.indicators.map((indicator) => (
            <IndicatorCard key={indicator.key} indicator={indicator} />
          ))}
        </div>
      </Section>

      <Section title="Supporting transactions" description="Records behind the largest-payment indicator.">
        {data.evidence.largest_transactions.length === 0 ? (
          <p className="text-xs text-gray-500">No transactions recorded.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-left text-xs">
              <thead className="text-[0.625rem] uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">
                <tr>
                  <th className="py-1.5 pr-2 font-semibold">Date</th>
                  <th className="py-1.5 pr-2 font-semibold">Record</th>
                  <th className="py-1.5 pr-2 font-semibold">MP / location</th>
                  <th className="py-1.5 pr-2 text-right font-semibold">Amount</th>
                  <th className="py-1.5 font-semibold">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#e8dadc] dark:divide-[#351923]">
                {data.evidence.largest_transactions.map((row) => (
                  <tr key={`${row.record_id}`}>
                    <td className="py-1.5 pr-2 whitespace-nowrap text-[#71666a] dark:text-[#b9aeb2]">{formatDateHuman(row.date) || "—"}</td>
                    <td className="py-1.5 pr-2">
                      <span className="text-[#292326] dark:text-gray-200">{row.title}</span>
                      <span className="ml-1 text-[0.625rem] text-[#b9aeb2]">#{row.record_id}</span>
                    </td>
                    <td className="py-1.5 pr-2 text-[#71666a] dark:text-[#b9aeb2]">{row.mp_name || "MP not recorded"}<br />{row.constituency || "—"}</td>
                    <td className="py-1.5 pr-2 text-right font-mono font-semibold">{formatMoney(row.amount)}</td>
                    <td className="py-1.5 text-[#71666a] dark:text-[#b9aeb2]">{row.status || "not recorded"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {data.evidence.repeated_amounts.length > 0 && (
        <Section title="Repeated identical amounts" description="Payment amounts recorded three or more times for this vendor.">
          <div className="flex flex-wrap gap-2">
            {data.evidence.repeated_amounts.map((row) => (
              <span key={row.amount} className="rounded-lg border border-[#e8dadc] bg-[#faf7f6] px-2 py-1 text-[0.6875rem] dark:border-[#351923] dark:bg-[#000000]">
                <strong className="font-mono">{formatMoney(row.amount)}</strong> × {row.occurrences}
              </span>
            ))}
          </div>
        </Section>
      )}

      <p className="rounded-lg border border-[#e8dadc] bg-[#faf7f6] p-3 text-[0.6875rem] leading-relaxed text-[#71666a] dark:border-[#351923] dark:bg-[#000000] dark:text-[#b9aeb2]">
        <strong className="text-[#292326] dark:text-white">Method:</strong> {data.method}
      </p>
    </div>
  )
}

/* ── Network tab ─────────────────────────────────────────────────── */

const RING_RADIUS = {
  mp: 78,
  constituency: 126,
  work: 176,
  project: 218,
  state: 246,
}

const NODE_COLORS = {
  vendor: "#a50f29",
  mp: "#c8102e",
  constituency: "#b84a5d",
  state: "#b84a5d",
  work: "#f59e0b",
  project: "#e14b4b",
}

function truncateLabel(label, max = 16) {
  const text = String(label || "")
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function RelationshipGraph({ nodes, edges }) {
  const [selectedId, setSelectedId] = useState(null)
  const vendor = nodes.find((node) => node.type === "vendor")

  const positioned = useMemo(() => {
    const centre = { x: 320, y: 300 }
    const groups = {
      mp: nodes.filter((n) => n.type === "mp").slice(0, 8),
      constituency: nodes.filter((n) => n.type === "constituency").slice(0, 7),
      work: nodes.filter((n) => n.type === "work").slice(0, 8),
      project: nodes.filter((n) => n.type === "project").slice(0, 6),
      state: nodes.filter((n) => n.type === "state").slice(0, 5),
    }
    const positions = new Map()
    if (vendor) positions.set(vendor.id, { ...centre, type: "vendor", node: vendor })
    Object.entries(groups).forEach(([type, list]) => {
      const radius = RING_RADIUS[type]
      list.forEach((node, index) => {
        const angle = (index / Math.max(1, list.length)) * Math.PI * 2 - Math.PI / 2
        positions.set(node.id, {
          x: centre.x + radius * Math.cos(angle),
          y: centre.y + radius * Math.sin(angle) * 0.82,
          type,
          node,
        })
      })
    })
    return positions
  }, [nodes, vendor])

  const visibleEdges = edges.filter((edge) => positioned.has(edge.source) && positioned.has(edge.target))
  const selected = selectedId ? positioned.get(selectedId) : null

  return (
    <div className="space-y-3">
      <div className="overflow-hidden rounded-lg border border-[#e8dadc] bg-[#ffffff] dark:border-[#351923] dark:bg-[#120b0e]">
        <svg viewBox="0 0 640 600" className="h-auto w-full" role="img" aria-label="Vendor relationship network">
          {visibleEdges.map((edge, index) => {
            const from = positioned.get(edge.source)
            const to = positioned.get(edge.target)
            const highlighted = selectedId && (edge.source === selectedId || edge.target === selectedId)
            return (
              <line
                key={`${edge.source}-${edge.target}-${index}`}
                x1={from.x} y1={from.y} x2={to.x} y2={to.y}
                stroke={highlighted ? "#c8102e" : "#dfcbcd"}
                strokeWidth={highlighted ? 2 : Math.min(3, 0.6 + Math.log10(Math.max(1, edge.amount)) / 3)}
                opacity={selectedId && !highlighted ? 0.25 : 0.75}
              >
                <title>{`${edge.type}: ${edge.label || ""}`}</title>
              </line>
            )
          })}
          {[...positioned.values()].map((entry) => {
            const radius = entry.type === "vendor" ? 26 : entry.type === "mp" ? 9 : 7
            const active = !selectedId || selectedId === entry.node.id
            return (
              <g
                key={entry.node.id}
                onClick={() => setSelectedId(selectedId === entry.node.id ? null : entry.node.id)}
                className="cursor-pointer"
                opacity={active ? 1 : 0.45}
              >
                <circle cx={entry.x} cy={entry.y} r={radius} fill={NODE_COLORS[entry.type]} />
                {entry.type === "vendor" && (
                  <text x={entry.x} y={entry.y + 4} textAnchor="middle" fill="#fff" fontSize="10" fontWeight="700">VENDOR</text>
                )}
                <text
                  x={entry.x}
                  y={entry.y - radius - 4}
                  textAnchor="middle"
                  fontSize="9.5"
                  className="fill-[#71666a] dark:fill-gray-300"
                >
                  {truncateLabel(entry.node.label, entry.type === "vendor" ? 20 : 16)}
                </text>
                <title>{`${entry.type}: ${entry.node.label}\n₹${formatNumber(entry.node.value)}`}</title>
              </g>
            )
          })}
        </svg>
      </div>

      <div className="flex flex-wrap gap-2 text-[0.625rem]">
        {Object.entries(NODE_COLORS).map(([type, color]) => (
          <span key={type} className="inline-flex items-center gap-1 rounded-full border border-[#e8dadc] px-2 py-0.5 dark:border-[#351923]">
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: color }} />
            <span className="capitalize text-[#71666a] dark:text-gray-300">{type}</span>
            <span className="text-[#b9aeb2]">{nodes.filter((n) => n.type === type).length}</span>
          </span>
        ))}
      </div>

      {selected && (
        <div className="rounded-lg border border-blue-200 bg-blue-50/60 p-3 text-xs dark:border-blue-900 dark:bg-blue-950/30">
          <p className="font-semibold text-[#292326] dark:text-white">{selected.node.label}</p>
          <p className="mt-0.5 capitalize text-[#71666a] dark:text-[#b9aeb2]">{selected.node.type.replaceAll("_", " ")} · {formatMoney(selected.node.value)}</p>
          {selected.node.meta && (
            <dl className="mt-2 grid grid-cols-2 gap-1">
              {Object.entries(selected.node.meta).filter(([, value]) => value !== null && value !== undefined && value !== "").map(([key, value]) => (
                <div key={key} className="flex gap-1">
                  <dt className="text-[#71666a] dark:text-[#b9aeb2]">{key.replaceAll("_", " ")}:</dt>
                  <dd className="font-medium text-[#292326] dark:text-gray-200">{String(value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      )}
    </div>
  )
}

function NetworkTab({ data }) {
  return (
    <div className="space-y-4">
      <Section
        title="Relationship network"
        description="Only recorded connections are drawn. Shared transactions never imply ownership, family ties or collusion."
        right={<span className="text-[0.625rem] text-[#b9aeb2]">{data.nodes.length} nodes · {data.edges.length} links</span>}
      >
        <RelationshipGraph nodes={data.nodes} edges={data.edges} />
      </Section>

      {data.highlights.length > 0 && (
        <Section title="Concentration highlights" description="Recorded concentration, shown as context for review.">
          <ul className="space-y-2 text-xs">
            {data.highlights.map((highlight) => (
              <li
                key={highlight.type}
                className={`rounded-lg border p-2.5 ${
                  highlight.severity === "elevated"
                    ? "border-amber-200 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/30"
                    : "border-[#e8dadc] bg-[#faf7f6] dark:border-[#351923] dark:bg-[#120b0e]"
                }`}
              >
                <p className="font-semibold text-[#292326] dark:text-gray-100">{highlight.label}</p>
                <p className="mt-0.5 text-[#71666a] dark:text-gray-300">{highlight.detail}</p>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section
        title="Possible name variants"
        description="Vendors whose names share tokens with this one but have a different normalized key. Listed separately — never merged into the figures above."
      >
        {data.uncertain_matches.length === 0 ? (
          <p className="text-xs text-gray-500">No comparable name variant found in the current dataset.</p>
        ) : (
          <ul className="space-y-2 text-xs">
            {data.uncertain_matches.map((match) => (
              <li key={match.vendor_key} className="rounded-lg border border-dashed border-[#e8dadc] p-2.5 dark:border-[#351923]">
                <p className="font-semibold text-[#292326] dark:text-gray-100">{match.vendor_name}</p>
                <p className="mt-0.5 text-[#71666a] dark:text-[#b9aeb2]">
                  {formatMoney(match.total_expenditure)} · {formatNumber(match.transactions)} payments ·{" "}
                  {match.states} state(s) · name-token overlap {Math.round(match.token_overlap * 100)}%
                </p>
                <p className="mt-1 text-[0.625rem] text-[#b9aeb2]">{match.reason}</p>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <p className="text-[0.6875rem] leading-relaxed text-[#71666a] dark:text-[#b9aeb2]">{data.method}</p>
    </div>
  )
}

/* ── Timeline tab ────────────────────────────────────────────────── */

function TimelineTab({ vendorKey, house }) {
  const [state, setState] = useState({ events: [], milestones: [], total: 0, yearly: [], skip: 0, limit: 40, loading: true, error: "", done: false })

  const load = useCallback(
    async (skip, replace) => {
      setState((current) => ({ ...current, loading: true, error: "" }))
      try {
        const payload = await getVendorActivity(vendorKey, { house: house || undefined, skip, limit: 40 })
        setState((current) => {
          const events = replace ? payload.events : [...current.events, ...payload.events]
          return {
            events,
            milestones: replace ? payload.milestones : current.milestones,
            total: payload.total_events,
            yearly: payload.yearly,
            skip: skip + payload.events.length,
            limit: 40,
            loading: false,
            error: "",
            done: events.length >= payload.total_events,
          }
        })
      } catch (error) {
        setState((current) => ({ ...current, loading: false, error: error.message || "Unable to load activity" }))
      }
    },
    [vendorKey, house]
  )

  useEffect(() => {
    setState({ events: [], milestones: [], total: 0, yearly: [], skip: 0, limit: 40, loading: true, error: "", done: false })
    load(0, true)
  }, [load])

  return (
    <div className="space-y-4">
      {state.yearly.length > 0 && (
        <Section title="Activity by year" description="Recorded payments per calendar year.">
          <div className="flex flex-wrap gap-2">
            {state.yearly.map((year) => (
              <div key={year.year} className="rounded-lg border border-[#e8dadc] px-2.5 py-1.5 text-xs dark:border-[#351923]">
                <p className="font-mono font-bold text-[#292326] dark:text-white">{year.year}</p>
                <p className="text-[0.625rem] text-[#71666a] dark:text-[#b9aeb2]">{formatMoney(year.amount)} · {formatNumber(year.transactions)}</p>
              </div>
            ))}
          </div>
        </Section>
      )}

      {state.milestones.length > 0 && (
        <Section title="Completion milestones" description="Only works whose key matches an official completed-work row exactly.">
          <ul className="space-y-2">
            {state.milestones.map((milestone) => (
              <li key={`m-${milestone.record_id}`} className="rounded-lg border border-emerald-200 bg-emerald-50 p-2.5 text-xs dark:border-emerald-900 dark:bg-emerald-950/30">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="font-semibold text-emerald-900 dark:text-emerald-200">{milestone.title}</p>
                  <span className="font-mono text-[0.6875rem] text-emerald-800 dark:text-emerald-300">{formatMoney(milestone.amount)}</span>
                </div>
                <p className="mt-0.5 text-[0.6875rem] text-emerald-800/80 dark:text-emerald-200/70">
                  Completed {formatDateHuman(milestone.date) || "date not recorded"} · {milestone.constituency}, {milestone.state}
                  {milestone.category ? ` · ${milestone.category}` : ""}
                </p>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Payment timeline" description={`${formatNumber(state.total)} recorded payments, newest first.`}>
        {state.error && <ErrorNote message={state.error} onRetry={() => load(0, true)} />}
        {!state.error && state.events.length === 0 && !state.loading && (
          <p className="text-xs text-gray-500">No payment records for this scope.</p>
        )}
        <ol className="relative space-y-2 border-l border-[#e8dadc] pl-3 dark:border-[#351923]">
          {state.events.map((event) => (
            <li key={`e-${event.record_id}`} className="relative">
              <span className="absolute -left-[1.05rem] top-1.5 h-2 w-2 rounded-full bg-[#c8102e]" aria-hidden />
              <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
                <p className="min-w-0 font-medium text-[#292326] dark:text-gray-100">{event.title}</p>
                <span className="shrink-0 font-mono font-semibold text-[#292326] dark:text-white">{formatMoney(event.amount)}</span>
              </div>
              <p className="text-[0.6875rem] text-[#71666a] dark:text-[#b9aeb2]">
                {formatDateHuman(event.date) || "Date not recorded"} · {event.mp_name || "MP not recorded"} ·{" "}
                {event.constituency || "—"}, {event.state || "—"}
                {event.house ? ` · ${event.house}` : ""}
                {event.status ? ` · ${event.status}` : ""}
              </p>
            </li>
          ))}
        </ol>
        {state.loading && <p className="mt-2 text-xs text-[#71666a] dark:text-[#b9aeb2]">Loading payments…</p>}
        {!state.loading && !state.done && state.events.length > 0 && (
          <button
            type="button"
            onClick={() => load(state.skip, false)}
            className="mt-3 rounded-lg border border-[#c8102e] px-3 py-1.5 text-xs font-semibold text-[#c8102e] hover:bg-blue-50 dark:hover:bg-blue-950/30"
          >
            Load more payments
          </button>
        )}
      </Section>
    </div>
  )
}

/* ── Assistant tab ───────────────────────────────────────────────── */

function AssistantTab({ vendorKey, house, vendorName }) {
  const [question, setQuestion] = useState("")
  const [asked, setAsked] = useState("")
  const [answer, setAnswer] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")

  const ask = useCallback(
    async (text) => {
      const trimmed = (text || "").trim()
      if (trimmed.length < 2) return
      setLoading(true)
      setError("")
      setAsked(trimmed)
      try {
        const payload = await askVendorAssistant(vendorKey, trimmed, house)
        setAnswer(payload)
      } catch (requestError) {
        setError(requestError.message || "The analyst could not answer that question.")
        setAnswer(null)
      } finally {
        setLoading(false)
      }
    },
    [vendorKey, house]
  )

  return (
    <div className="space-y-4">
      <Section
        title="Ask about this vendor"
        description={`Answers are built from this vendor's stored records (${vendorName}). No model generates or estimates figures — every number is copied from a query result.`}
      >
        <form
          onSubmit={(event) => {
            event.preventDefault()
            ask(question)
          }}
          className="flex flex-col gap-2 sm:flex-row"
        >
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="e.g. Which records should an auditor examine?"
            aria-label="Ask about this vendor"
            className="h-10 flex-1 rounded-lg border border-[#e8dadc] bg-white px-3 text-sm outline-none focus:border-[#c8102e] dark:border-[#82767b] dark:bg-[#000000]"
          />
          <button
            type="submit"
            disabled={loading || question.trim().length < 2}
            className="h-10 rounded-lg bg-[#c8102e] px-4 text-sm font-semibold text-white hover:bg-[#a50f29] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "Analysing…" : "Ask"}
          </button>
        </form>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {SUGGESTED_QUESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => {
                setQuestion(suggestion)
                ask(suggestion)
              }}
              className="rounded-full border border-[#e8dadc] px-2.5 py-1 text-[0.6875rem] text-[#292326] hover:border-[#c8102e] hover:text-[#c8102e] dark:border-[#351923] dark:text-gray-300"
            >
              {suggestion}
            </button>
          ))}
        </div>
      </Section>

      {error && <ErrorNote message={error} onRetry={() => ask(asked)} />}
      {loading && <Loading label="Retrieving records and analysing…" />}

      {answer && !loading && (
        <Section title="Analysis" description={`Question: “${answer.question}”`}>
          <p className="text-sm leading-relaxed text-[#292326] dark:text-gray-200">{answer.answer}</p>
          {answer.bullets.length > 0 && (
            <ul className="mt-3 space-y-1.5 text-xs">
              {answer.bullets.map((bullet, index) => (
                <li key={index} className="flex gap-2 text-[#71666a] dark:text-gray-300">
                  <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-[#c8102e]" aria-hidden />
                  <span>{bullet}</span>
                </li>
              ))}
            </ul>
          )}
          <dl className="mt-4 grid grid-cols-2 gap-2 text-[0.6875rem] sm:grid-cols-3">
            <div><dt className="text-[#71666a] dark:text-[#b9aeb2]">Transactions</dt><dd className="font-mono font-semibold">{formatNumber(answer.snapshot.transaction_count)}</dd></div>
            <div><dt className="text-[#71666a] dark:text-[#b9aeb2]">Expenditure</dt><dd className="font-mono font-semibold">{formatMoney(answer.snapshot.total_expenditure)}</dd></div>
            <div><dt className="text-[#71666a] dark:text-[#b9aeb2]">Risk (indicative)</dt><dd className="font-mono font-semibold">{answer.snapshot.risk_score} · {answer.snapshot.risk_category}</dd></div>
          </dl>
          <p className="mt-3 text-[0.625rem] text-[#b9aeb2]">
            Sources: {answer.sources.join(", ")} · scope: {answer.data_scope}
          </p>
          <p className="mt-1 text-[0.625rem] leading-relaxed text-[#b9aeb2]">{answer.method}</p>
        </Section>
      )}
    </div>
  )
}

/* ── Panel shell ─────────────────────────────────────────────────── */

export default function VendorIntelPanel({
  vendorKey,
  vendorName,
  house: initialHouse = "",
  onClose,
  onOpenProject,
  onCompare,
  inCompare,
  onContact,
}) {
  const [tab, setTab] = useState("profile")
  const [house, setHouse] = useState(initialHouse)
  const [cache, setCache] = useState({})
  const [loading, setLoading] = useState({})
  const [errors, setErrors] = useState({})

  const requestKey = useCallback((kind) => `${kind}|${vendorKey}|${house}`, [vendorKey, house])

  const load = useCallback(
    async (kind) => {
      const key = requestKey(kind)
      if (cache[key] || loading[key]) return
      setLoading((current) => ({ ...current, [key]: true }))
      setErrors((current) => ({ ...current, [key]: "" }))
      try {
        let payload
        if (kind === "performance") payload = await getVendorPerformance(vendorKey, { house: house || undefined })
        else if (kind === "risk") payload = await getVendorRiskAnalysis(vendorKey, { house: house || undefined })
        else payload = await getVendorRelationships(vendorKey, { house: house || undefined })
        setCache((current) => ({ ...current, [key]: payload }))
      } catch (error) {
        setErrors((current) => ({ ...current, [key]: error.message || "Unable to load this analysis" }))
      } finally {
        setLoading((current) => ({ ...current, [key]: false }))
      }
    },
    [cache, loading, house, requestKey, vendorKey]
  )

  // Performance is always needed (header totals); other tabs load on demand.
  useEffect(() => {
    load("performance")
  }, [load])

  useEffect(() => {
    if (tab === "risk") load("risk")
    if (tab === "network") load("network")
  }, [tab, load])

  useEffect(() => {
    const handler = (event) => {
      if (event.key === "Escape") onClose?.()
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [onClose])

  const performanceKey = requestKey("performance")
  const riskKey = requestKey("risk")
  const networkKey = requestKey("network")
  const performance = cache[performanceKey]

  return (
    <div
      className="anim-overlay-in fixed inset-0 z-[85] flex justify-end bg-black/40"
      onClick={() => onClose?.()}
      role="dialog"
      aria-modal="true"
      aria-label={`Vendor intelligence for ${vendorName}`}
    >
      <aside
        className="anim-panel-in flex h-full w-full flex-col overflow-hidden bg-[#faf7f6] shadow-soft-lg dark:bg-[#000000] sm:max-w-[760px]"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="border-b border-[#e8dadc] bg-white px-4 py-3 dark:border-[#351923] dark:bg-[#0d090b] sm:px-5">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="truncate text-lg font-bold text-[#292326] dark:text-white" title={vendorName}>
                {vendorName}
              </h2>
              <p className="mt-0.5 text-xs text-[#71666a] dark:text-[#b9aeb2]">
                {performance
                  ? `${performance.vendor_type} · ${formatNumber(performance.totals.transaction_count)} payments · ${formatMoney(performance.totals.total_expenditure)}`
                  : "Loading performance summary…"}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <select
                value={house}
                onChange={(event) => setHouse(event.target.value)}
                aria-label="House filter"
                className="h-8 rounded-lg border border-[#e8dadc] bg-white px-2 text-xs dark:border-[#82767b] dark:bg-[#000000]"
              >
                {HOUSES.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
              {onContact && (
                <button
                  type="button"
                  onClick={onContact}
                  className="hidden rounded-md border border-[#c8102e] px-2.5 py-1.5 text-xs font-semibold text-[#c8102e] hover:bg-blue-50 dark:hover:bg-blue-950/30 sm:block"
                >
                  Contact
                </button>
              )}
              <button
                type="button"
                onClick={() => onClose?.()}
                aria-label="Close vendor intelligence panel"
                className="rounded-lg px-2 text-2xl leading-none text-[#71666a] hover:text-[#292326] dark:hover:text-white"
              >
                ×
              </button>
            </div>
          </div>

          <nav className="mt-3 flex gap-1 overflow-x-auto" aria-label="Vendor analysis sections">
            {TABS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                aria-current={tab === item.id ? "page" : undefined}
                className={`shrink-0 rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                  tab === item.id
                    ? "bg-[#c8102e] text-white"
                    : "text-[#71666a] hover:bg-[#e8dadc] dark:text-gray-300 dark:hover:bg-[#120b0e]"
                }`}
              >
                {item.label}
              </button>
            ))}
          </nav>
        </header>

        <div className="flex-1 overflow-y-auto px-4 py-4 sm:px-5">
          {tab === "profile" && (
            <>
              {errors[performanceKey] && <ErrorNote message={errors[performanceKey]} onRetry={() => { setCache((c) => ({ ...c, [performanceKey]: undefined })); load("performance") }} />}
              {!performance && !errors[performanceKey] && <Loading label="Aggregating this vendor's records…" />}
              {performance && <PerformanceTab data={performance} />}
            </>
          )}

          {tab === "risk" && (
            <>
              {errors[riskKey] && <ErrorNote message={errors[riskKey]} onRetry={() => { setCache((c) => ({ ...c, [riskKey]: undefined })); load("risk") }} />}
              {!cache[riskKey] && !errors[riskKey] && <Loading label="Measuring indicators against peer vendors…" />}
              {cache[riskKey] && <RiskTab data={cache[riskKey]} />}
            </>
          )}

          {tab === "network" && (
            <>
              {errors[networkKey] && <ErrorNote message={errors[networkKey]} onRetry={() => { setCache((c) => ({ ...c, [networkKey]: undefined })); load("network") }} />}
              {!cache[networkKey] && !errors[networkKey] && <Loading label="Building the relationship graph…" />}
              {cache[networkKey] && <NetworkTab data={cache[networkKey]} />}
            </>
          )}

          {tab === "timeline" && <TimelineTab vendorKey={vendorKey} house={house} />}

          {tab === "ask" && <AssistantTab vendorKey={vendorKey} house={house} vendorName={vendorName} />}
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-[#e8dadc] bg-white px-4 py-2.5 text-[0.6875rem] text-[#71666a] dark:border-[#351923] dark:bg-[#0d090b] dark:text-[#b9aeb2] sm:px-5">
          <span>Anomalies are audit leads, not proof of misconduct.</span>
          <div className="flex items-center gap-2">
            {onCompare && (
              <button
                type="button"
                onClick={onCompare}
                className="rounded-sm border border-[#c8102e] px-2 py-1 font-semibold text-[#c8102e] hover:bg-blue-50 dark:hover:bg-blue-950/30"
              >
                {inCompare ? "Remove from comparison" : "Add to comparison"}
              </button>
            )}
            {onOpenProject && (
              <button
                type="button"
                onClick={() => onOpenProject(vendorKey)}
                className="rounded-sm border border-[#e8dadc] px-2 py-1 font-semibold text-[#292326] hover:border-[#c8102e] dark:border-[#351923] dark:text-gray-200"
              >
                Open vendor directory
              </button>
            )}
          </div>
        </footer>
      </aside>
    </div>
  )
}
