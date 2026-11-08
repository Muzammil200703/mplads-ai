import { useEffect, useMemo, useState } from "react"
import { getVendorNetwork } from "../services/api"
import { formatMoney } from "../utils/format"
import VendorIntelPanel from "../components/VendorIntelPanel"

/* ═══════════════════════════════════════════════════════════════════════
   VENDOR COLLUSION & NETWORK MAP — SIH 26102 (/vendors)
   ─────────────────────────────────────────────────────────────────────
   Visualizes vendor entities and suspicious shared-identity clusters.
   Collusion signals derive ONLY from recorded dataset fields (shared
   implementing agency as a registered-office proxy + shared name
   family). The dataset has no PAN/bank columns — the UI says so plainly.
   ═══════════════════════════════════════════════════════════════════ */

function riskColor(score) {
  if (score >= 80) return "#e14b4b"
  if (score >= 50) return "#f59e0b"
  return "#269b71"
}

function severityBadge(score) {
  const cls = score >= 80
    ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
    : score >= 50
      ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
      : "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
  return <span className={`inline-flex min-w-[2.75rem] justify-center rounded-full px-2 py-0.5 text-xs font-bold ${cls}`}>{score}</span>
}

/* Radial cluster graph: each cluster is a hub (the shared IDA) with
   member vendors around it. Pure SVG — no dependencies. */
function ClusterGraph({ clusters, onSelect }) {
  const [selected, setSelected] = useState(null)
  if (!clusters.length) return null
  const HUB = { x: 200, y: 150 }
  const R = 110

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-bold text-gray-900 dark:text-white">Suspicious Cluster Network</h3>
        <p className="text-xs text-gray-500">Hub = shared registered office (implementing agency) · spokes = vendor name variants</p>
      </div>
      <div className="mt-3 flex flex-col gap-4 lg:flex-row">
        <div className="mx-auto overflow-hidden rounded-lg border border-gray-100 dark:border-gray-700">
          <svg viewBox="0 0 400 300" className="h-72 w-full max-w-md" role="img" aria-label="Vendor cluster network graph">
            {(() => {
              const cluster = clusters[selected ?? 0]
              if (!cluster) return null
              const n = cluster.members.length
              return (
                <g>
                  {cluster.members.map((m, i) => {
                    const angle = (i / n) * Math.PI * 2 - Math.PI / 2
                    const x = HUB.x + R * Math.cos(angle)
                    const y = HUB.y + R * Math.sin(angle) * 0.75
                    return (
                      <g key={m}>
                        <line x1={HUB.x} y1={HUB.y} x2={x} y2={y} stroke={riskColor(cluster.risk_score)} strokeWidth="1.5" strokeDasharray="4 3" opacity="0.7" />
                        <circle cx={x} cy={y} r="7" fill={riskColor(cluster.risk_score)} opacity="0.85" />
                        <text x={x} y={y - 12} textAnchor="middle" className="fill-gray-600 dark:fill-gray-300" fontSize="9" fontWeight="600">
                          {m.length > 18 ? m.slice(0, 17) + "…" : m}
                        </text>
                      </g>
                    )
                  })}
                  <circle cx={HUB.x} cy={HUB.y} r="26" fill="#a50f29" />
                  <text x={HUB.x} y={HUB.y - 2} textAnchor="middle" fill="white" fontSize="8.5" fontWeight="700">SHARED</text>
                  <text x={HUB.x} y={HUB.y + 8} textAnchor="middle" fill="white" fontSize="8.5" fontWeight="700">OFFICE</text>
                  <text x={HUB.x} y={HUB.y + 42} textAnchor="middle" className="fill-gray-500" fontSize="10">
                    {cluster.ida} · {cluster.member_count} entities
                  </text>
                </g>
              )
            })()}
          </svg>
        </div>
        <div className="min-w-0 flex-1 space-y-1.5">
          {clusters.slice(0, 8).map((c, idx) => (
            <button
              key={c.cluster_id}
              onClick={() => { setSelected(idx); onSelect?.(c) }}
              className={`flex w-full items-center justify-between gap-3 rounded-lg border p-2.5 text-left transition ${
                (selected ?? 0) === idx
                  ? "border-blue-400 bg-blue-50 dark:border-blue-700 dark:bg-blue-950/40"
                  : "border-gray-200 hover:border-blue-300 dark:border-gray-700 dark:hover:border-blue-700"
              }`}
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-gray-900 dark:text-white">
                  “{c.name_family}” family · {c.member_count} entities
                </p>
                <p className="truncate text-xs text-gray-500">via {c.ida} · {formatMoney(c.combined_value)} · {c.combined_works} works</p>
              </div>
              {severityBadge(c.risk_score)}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

export default function VendorNetwork() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  // Vendor opened from the risk table — the shared intelligence panel is
  // reused here so both pages show identical analysis.
  const [openVendor, setOpenVendor] = useState(null)

  useEffect(() => {
    let alive = true
    getVendorNetwork({ limit: 60 })
      .then((d) => { if (alive) setData(d) })
      .catch((e) => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [])

  const topFlags = useMemo(() => {
    if (!data) return null
    const clustered = data.vendors.filter((v) => v.conflict_flags.includes("Clustered identity")).length
    return { clustered }
  }, [data])

  if (error) {
    return (
      <div className="min-h-full p-4 sm:p-6">
        <div className="mx-auto max-w-[1440px]">
          <div className="rounded-xl border border-red-200 bg-red-50 p-6 text-center dark:border-red-900 dark:bg-red-950/40">
            <p className="text-sm font-semibold text-red-700 dark:text-red-300">Unable to load the vendor network: {error}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-full p-4 sm:p-6">
    <div className="mx-auto max-w-[1440px] space-y-4 sm:space-y-6">
      <header className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white sm:text-3xl">Vendor Collusion &amp; Network Map</h1>
        </div>
        <p className="max-w-3xl text-sm text-gray-500 dark:text-gray-400">
          Detects suspicious vendor clusters and concentration risk across {data ? data.total_vendors.toLocaleString("en-IN") : "…"} vendor entities.
        </p>
      </header>

      {!data && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {[...Array(3)].map((_, i) => <div key={i} className="h-24 animate-pulse rounded-xl bg-gray-100 dark:bg-gray-800" />)}
        </div>
      )}

      {data && (
        <>
          <section className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
              <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">Vendor Entities</p>
              <p className="mt-1 text-2xl font-bold text-gray-900 dark:text-white">{data.total_vendors.toLocaleString("en-IN")}</p>
            </div>
            <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
              <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">Suspicious Clusters</p>
              <p className={`mt-1 text-2xl font-bold ${data.cluster_count > 0 ? "text-red-600 dark:text-red-400" : "text-green-600 dark:text-green-400"}`}>{data.cluster_count}</p>
            </div>
            <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
              <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">Vendors in Clusters</p>
              <p className="mt-1 text-2xl font-bold text-gray-900 dark:text-white">{topFlags?.clustered ?? 0}</p>
            </div>
          </section>

          {data.clusters.length > 0 ? (
            <ClusterGraph clusters={data.clusters} />
          ) : (
            <section className="rounded-xl border border-green-200 bg-green-50 p-5 text-center dark:border-green-900 dark:bg-green-950/30">
              <p className="text-2xl" aria-hidden>✅</p>
              <p className="mt-1 text-sm font-semibold text-green-800 dark:text-green-300">No shared-identity clusters detected</p>
              <p className="mt-1 text-xs text-green-700 dark:text-green-400">No group of vendors currently shares both a registered office (implementing agency) and a name family.</p>
            </section>
          )}

          {/* Vendor risk table */}
          <section className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 px-4 py-3 dark:border-gray-700">
              <h3 className="text-sm font-bold text-gray-900 dark:text-white">Vendor Risk Table</h3>
              <p className="text-xs text-gray-500">Ranked by composite risk (clustering, concentration, name variants)</p>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-left text-sm">
                <thead className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500 dark:border-gray-700">
                  <tr>
                    <th className="px-4 py-3 font-semibold">Vendor Name</th>
                    <th className="px-4 py-3 text-right font-semibold">Active Contracts</th>
                    <th className="px-4 py-3 text-right font-semibold">Total Value</th>
                    <th className="px-4 py-3 font-semibold">Conflict Flags</th>
                    <th className="px-4 py-3 text-right font-semibold">Risk Score</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                  {data.vendors.map((v) => (
                    <tr
                      key={v.vendor_name}
                      onClick={() => setOpenVendor({ vendorKey: v.vendor_key, vendorName: v.vendor_name })}
                      className="cursor-pointer transition hover:bg-gray-50 dark:hover:bg-gray-700/40"
                    >
                      <td className="max-w-[16rem] px-4 py-3">
                        <p className="truncate font-semibold text-gray-900 dark:text-white" title={v.vendor_name}>{v.vendor_name}</p>
                        {v.raw_names.length > 1 && <p className="truncate text-xs text-gray-400" title={v.raw_names.join(", ")}>a.k.a. {v.raw_names.slice(1).join(", ")}</p>}
                        <p className="mt-0.5 text-[0.625rem] text-blue-600 dark:text-blue-400">Open performance &amp; risk analysis →</p>
                      </td>
                      <td className="px-4 py-3 text-right text-gray-700 dark:text-gray-300">{v.active_contracts}</td>
                      <td className="px-4 py-3 text-right font-medium text-gray-800 dark:text-gray-200">{formatMoney(v.total_value)}</td>
                      <td className="px-4 py-3">
                        {v.conflict_flags.length === 0 ? (
                          <span className="text-xs text-gray-400">—</span>
                        ) : (
                          <div className="flex flex-wrap gap-1">
                            {v.conflict_flags.map((f) => (
                              <span key={f} className="rounded-sm bg-red-100 px-1.5 py-0.5 text-[0.6875rem] font-semibold text-red-700 dark:bg-red-950 dark:text-red-300">{f}</span>
                            ))}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">{severityBadge(v.risk_score)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <p className="rounded-xl border border-gray-200 bg-gray-50 p-3 text-xs leading-relaxed text-gray-500 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-400">
            <strong>Method &amp; limits:</strong> {data.method} Flags are investigative leads for auditors — they are not proof of wrongdoing.
          </p>
        </>
      )}

      {openVendor && (
        <VendorIntelPanel
          vendorKey={openVendor.vendorKey}
          vendorName={openVendor.vendorName}
          onClose={() => setOpenVendor(null)}
        />
      )}
    </div>
    </div>
  )
}
