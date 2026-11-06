import { memo, useEffect, useState } from "react"
import { getVendorIntelligence, getVendorProfile } from "../services/api"
import VendorIntelPanel from "../components/VendorIntelPanel"
import VendorComparison from "../components/VendorComparison"
import VendorBehaviourScan from "../components/VendorBehaviourScan"
import { formatMoney, formatNumber } from "../utils/format"

const TYPE_OPTIONS = ["All types", "Company/Business", "Supplier/Contractor", "Individual", "Government Department", "Panchayat/Local Body", "Other/Unknown", "Unknown"]

function StatCard({ label, value, detail }) {
  return (
    <div className="rounded-xl border border-[#dde1e5] bg-white p-4 shadow-sm dark:border-[#39434E] dark:bg-[#222831]">
      <p className="text-[0.6875rem] font-bold uppercase tracking-wider text-[#69717c] dark:text-[#aab3bd]">{label}</p>
      <p className="mt-2 font-mono text-2xl font-bold text-[#30343A] dark:text-white">{value}</p>
      {detail && <p className="mt-1 text-xs text-[#69717c] dark:text-[#aab3bd]">{detail}</p>}
    </div>
  )
}

function RiskPill({ count, level }) {
  const styles = {
    High: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
    Medium: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
    Low: "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300",
  }
  return <span className={`rounded-full px-2 py-1 text-[0.6875rem] font-bold ${styles[level]}`}>{count} {level}</span>
}

/* Contact lookup — exactly "<vendor name> <constituency>".
   The single constituency is the vendor's primary work location: the
   engagement with the highest recorded expenditure when the vendor's
   profile is loaded, otherwise the vendor's first recorded constituency.
   No state, district, IDA, project name, category, MP name or unrelated
   record data is ever included in the query. */
function investigateVendor(vendor, engagements) {
  const name = (vendor.vendor_name || "").trim()
  const primary = Array.isArray(engagements) && engagements.length > 0
    ? [...engagements].sort((a, b) => (Number(b.total_amount) || 0) - (Number(a.total_amount) || 0))[0]
    : null
  const constituency = (primary?.constituency || (vendor.constituencies || [])[0] || "").trim()
  const query = [name, constituency].filter(Boolean).join(" ")
  window.open(`https://www.google.com/search?q=${encodeURIComponent(query)}`, "_blank", "noopener,noreferrer")
}

const VendorIntelligence = memo(function VendorIntelligence({ drillDownParams, onClearDrillDown }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [query, setQuery] = useState("")
  const [searchText, setSearchText] = useState("")
  const [type, setType] = useState("All types")
  const [sortBy, setSortBy] = useState("total_expenditure")
  const [sortDir, setSortDir] = useState("desc")
  const [selected, setSelected] = useState(null)
  const [profile, setProfile] = useState(null)
  const [compare, setCompare] = useState([])
  const [page, setPage] = useState(0)
  // Bumped by Retry so a failed load genuinely re-fetches (errors are never
  // cached by the API layer, so this always results in a fresh request).
  const [reloadToken, setReloadToken] = useState(0)
  // Match the Projects tab: small server-paginated pages keep the table light.
  const pageSize = 15
  // Scope for the analysis tools (panel, behaviour scan, comparison).
  // The directory table itself lists every house, as before.
  const [analysisHouse, setAnalysisHouse] = useState("")

  useEffect(() => {
    const timer = setTimeout(() => { setQuery(searchText); setPage(0) }, 250)
    return () => clearTimeout(timer)
  }, [searchText])

  // Assistant deep-link: "vendor intelligence for <vendor>" prefills the
  // directory's existing search instead of creating a second view. The
  // q filter already matches vendor or state names on the server.
  useEffect(() => {
    if (!drillDownParams) return
    const value = drillDownParams.vendor || drillDownParams.state || ""
    if (value) {
      setSearchText(String(value))
      setPage(0)
    }
    onClearDrillDown?.()
  }, [drillDownParams, onClearDrillDown])

  useEffect(() => {
    let mounted = true
    setLoading(true)
    setError("")
    const started = import.meta.env.DEV ? performance.now() : 0
    const params = {
      skip: page * pageSize,
      limit: pageSize,
      sort_by: sortBy,
      sort_dir: sortDir,
    }
    if (query.trim()) params.q = query.trim()
    if (type !== "All types") params.vendor_type = type
    getVendorIntelligence(params)
      .then((payload) => { if (mounted) setData(payload) })
      .catch((err) => {
        // Never leave the page on a spinner: surface the failure and always
        // clear the loading flag in the finally path below.
        console.error("[VendorIntelligence] load failed:", err)
        if (mounted) setError("Unable to load Vendor Intelligence. The service may be starting up or unavailable.")
      })
      .finally(() => {
        if (import.meta.env.DEV && mounted) {
          console.info(`[VendorIntelligence] page data ready in ${(performance.now() - started).toFixed(0)} ms (page ${page}, q="${query}", type=${type})`)
        }
        if (mounted) setLoading(false)
      })
    return () => { mounted = false }
  }, [query, type, sortBy, sortDir, page, reloadToken])

  useEffect(() => {
    if (!selected?.normalized_name) { setProfile(null); return }
    let mounted = true
    setProfile(null)
    getVendorProfile(selected.normalized_name)
      .then((payload) => { if (mounted) setProfile(payload) })
      .catch((err) => {
        console.error("[VendorIntelligence] profile load failed:", selected.normalized_name, err)
        if (mounted) setProfile(null)
      })
    return () => { mounted = false }
  }, [selected])

  const vendors = data?.vendors || []

  const changeSort = (field) => {
    if (sortBy === field) setSortDir((direction) => direction === "asc" ? "desc" : "asc")
    else { setSortBy(field); setSortDir(field === "vendor_name" || field === "type" ? "asc" : "desc") }
    setPage(0)
  }

  const toggleCompare = (vendor) => {
    setCompare((current) => current.some((item) => item.vendor_name === vendor.vendor_name)
      ? current.filter((item) => item.vendor_name !== vendor.vendor_name)
      : current.length < 3 ? [...current, vendor] : current)
  }

  if (loading) return <div className="p-6 text-sm text-gray-500">Loading vendor analytics…</div>
  if (error) return (
    <div className="p-6">
      <div className="rounded-xl border border-red-200 bg-red-50 p-5 dark:border-red-900 dark:bg-red-950/40">
        <p className="text-sm font-semibold text-red-700 dark:text-red-300">{error}</p>
        <p className="mt-1 text-xs text-red-600/80 dark:text-red-300/70">See the browser console for the underlying error. No data is fabricated while the service is unavailable.</p>
        <button onClick={() => setReloadToken((token) => token + 1)} className="mt-3 rounded-lg border border-red-300 bg-white px-3 py-1.5 text-xs font-semibold text-red-700 hover:bg-red-100 dark:border-red-800 dark:bg-transparent dark:text-red-300 dark:hover:bg-red-950">Retry</button>
      </div>
    </div>
  )

  const stats = data?.stats || {}
  const topVendors = (data?.vendors || []).slice(0, 10)
  const totalVendors = data?.total_vendors || 0
  const totalPages = Math.max(1, Math.ceil(totalVendors / pageSize))
  const selectedIsInCompare = selected && compare.some((item) => item.vendor_name === selected.vendor_name)

  return (
    <div className="vendor-page min-h-full min-w-0 bg-[#f4f5f6] p-4 sm:p-6 dark:bg-[#171b20]">
      <div className="mx-auto max-w-[1500px]">
        <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold text-[#30343A] dark:text-white">Vendor Intelligence</h1>
            <p className="mt-1 text-sm text-[#69717c] dark:text-[#aab3bd]">From Project-Level Risk to Vendor-Level Intelligence</p>
          </div>
          <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-800 dark:border-blue-900 dark:bg-blue-950/40 dark:text-blue-200">Derived from current MPLADS expenditure records</div>
        </div>

        <div className="mb-5 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-6">
          <StatCard label="Unique vendors" value={formatNumber(stats.unique_vendors || 0)} />
          <StatCard label="Vendor-linked records" value={formatNumber(stats.vendor_linked_records || 0)} />
          <StatCard label="Vendor expenditure" value={formatMoney(stats.total_vendor_expenditure || 0)} />
          <StatCard label="Multi-project vendors" value={formatNumber(stats.multi_project_vendors || 0)} />
          <StatCard label="Vendors with high-risk projects" value={formatNumber(stats.vendors_with_high_risk_projects || 0)} />
          <StatCard label="Concentration indicator" value={stats.unusual_concentration ? "Review" : "Not flagged"} detail={`Top 5: ${stats.top5_share || 0}% · Top 10: ${stats.top10_share || 0}%`} />
        </div>

        <div className="mb-5 rounded-xl border border-[#dde1e5] bg-white p-4 shadow-sm dark:border-[#39434E] dark:bg-[#222831]">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div><h2 className="font-bold text-[#30343A] dark:text-white">Vendor directory</h2><p className="mt-1 text-xs text-gray-500">Vendor-linked records only. Projects are counted as distinct works recorded in the vendor's expenditure records (multiple payments on one work count once); select a row for details.</p></div>
            <div className="flex flex-wrap gap-2">
              <input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder="Search vendor or state" className="h-9 w-56 rounded-lg border border-[#dde1e5] bg-white px-3 text-sm outline-none focus:border-[#46566a] dark:border-[#69717c] dark:bg-[#171b20]" />
              <select value={type} onChange={(event) => { setType(event.target.value); setPage(0) }} className="h-9 rounded-lg border border-[#dde1e5] bg-white px-2 text-sm dark:border-[#69717c] dark:bg-[#171b20]">{TYPE_OPTIONS.map((option) => <option key={option}>{option}</option>)}</select>
            </div>
          </div>
          <div className="vendor-table-wrap mt-4 overflow-x-auto">
            <table className="w-full min-w-[1500px] table-fixed text-left text-sm">
              <thead><tr className="border-b border-[#dde1e5] text-[0.6875rem] uppercase tracking-wider text-gray-500 dark:border-[#39434E]">
                <th className="w-[360px] p-3"><button onClick={() => changeSort("vendor_name")}>Vendor {sortBy === "vendor_name" ? (sortDir === "asc" ? "↑" : "↓") : ""}</button></th><th className="w-[230px] p-3"><button onClick={() => changeSort("type")}>Type</button></th><th className="w-[110px] p-3 text-right" title="Distinct works recorded in this vendor's expenditure records">Projects</th><th className="w-[140px] p-3 text-right">Transactions</th><th className="w-[190px] p-3 text-right"><button onClick={() => changeSort("total_expenditure")}>Total expenditure</button></th><th className="w-[280px] p-3">States</th><th className="w-[220px] p-3">Risk exposure</th><th className="w-[90px] p-3 text-center">Compare</th>
              </tr></thead>
              <tbody>{vendors.map((vendor) => <tr key={vendor.vendor_name} onClick={() => setSelected(vendor)} className="cursor-pointer border-b border-[#dde1e5] hover:bg-[#f4f5f6] dark:border-[#39434E] dark:hover:bg-[#171b20]">
                <td className="p-3 font-semibold text-[#30343A] dark:text-white"><div className="flex items-center gap-2"><span className="min-w-0 truncate">{vendor.vendor_name}</span><button type="button" onClick={(event) => { event.stopPropagation(); investigateVendor(vendor) }} className="shrink-0 rounded-md border border-[#46566a] bg-transparent px-2.5 py-1.5 text-xs font-semibold text-[#46566a] hover:bg-blue-50 dark:hover:bg-blue-950/30" title="Search this vendor on Google with their name and primary constituency">Contact</button></div></td><td className="p-3 text-xs text-gray-600 dark:text-gray-300">{vendor.type_confidence === "low" ? "Unknown — insufficient information" : vendor.type}<span className="ml-1 text-[0.625rem] text-gray-400">({vendor.type_confidence})</span></td><td className="p-3 text-right font-mono">{vendor.project_count}</td><td className="p-3 text-right font-mono">{vendor.transaction_count}</td><td className="p-3 text-right font-mono font-semibold">{formatMoney(vendor.total_expenditure)}</td><td className="max-w-[180px] p-3 text-xs text-gray-600 dark:text-gray-300">{vendor.states.join(", ") || "—"}</td><td className="p-3"><div className="flex flex-wrap gap-1">{Object.entries(vendor.risk_counts).map(([level, count]) => <RiskPill key={level} count={count} level={level} />)}</div></td><td className="p-3 text-center"><input type="checkbox" checked={compare.some((item) => item.vendor_name === vendor.vendor_name)} onChange={() => toggleCompare(vendor)} onClick={(event) => event.stopPropagation()} /></td>
              </tr>)}</tbody>
            </table>
            {vendors.length === 0 && <p className="p-8 text-center text-sm text-gray-500">{totalVendors === 0 ? "No vendor records available in the current dataset." : "No vendor-linked records match these filters."}</p>}
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-[#dde1e5] pt-3 text-xs text-gray-500 dark:border-[#39434E]">
            <span>Showing {totalVendors === 0 ? 0 : page * pageSize + 1}–{Math.min((page + 1) * pageSize, totalVendors)} of {formatNumber(totalVendors)} vendors</span>
            <div className="flex items-center gap-2"><button disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))} className="rounded border border-gray-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-600">Previous</button><span>Page {page + 1} of {totalPages}</span><button disabled={page + 1 >= totalPages} onClick={() => setPage((value) => Math.min(totalPages - 1, value + 1))} className="rounded border border-gray-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-600">Next</button></div>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
          <section className="rounded-xl border border-[#dde1e5] bg-white p-4 shadow-sm dark:border-[#39434E] dark:bg-[#222831]"><h2 className="font-bold text-[#30343A] dark:text-white">Concentration overview</h2><p className="mt-1 text-xs text-gray-500">Top vendor expenditure shares, calculated from vendor-linked records.</p><div className="mt-4 space-y-3">{topVendors.map((vendor) => <div key={vendor.vendor_name}><div className="mb-1 flex justify-between gap-3 text-xs"><span className="truncate font-medium">{vendor.vendor_name}</span><span className="font-mono">{vendor.expenditure_share}%</span></div><div className="h-2 rounded-full bg-gray-100 dark:bg-gray-700"><div className="bar-fill h-2 rounded-full bg-[#46566a]" style={{ width: `${Math.min(100, vendor.expenditure_share)}%` }} /></div></div>)}</div></section>
          <section className="rounded-xl border border-[#dde1e5] bg-white p-4 shadow-sm dark:border-[#39434E] dark:bg-[#222831]"><h2 className="font-bold text-[#30343A] dark:text-white">Cross-project network</h2><p className="mt-1 text-xs text-gray-500">A lightweight view of vendors appearing across multiple recorded works in the current dataset.</p><div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2">{topVendors.filter((vendor) => vendor.project_count > 1).slice(0, 6).map((vendor) => <button key={vendor.vendor_name} onClick={() => setSelected(vendor)} className="rounded-lg border border-blue-100 bg-blue-50/50 p-3 text-left hover:border-blue-300 dark:border-blue-900 dark:bg-blue-950/20"><p className="truncate text-sm font-semibold">{vendor.vendor_name}</p><p className="mt-1 text-xs text-gray-500">{vendor.project_count} works · {vendor.states.join(", ") || "state unavailable"}</p></button>)}</div>{topVendors.filter((vendor) => vendor.project_count > 1).length === 0 && <p className="mt-4 text-sm text-gray-500">No multi-project vendor connections are present in the matched records.</p>}</section>
        </div>
        <section className="mt-5 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[#dde1e5] bg-white px-4 py-3 shadow-sm dark:border-[#39434E] dark:bg-[#222831]">
          <div className="min-w-0">
            <p className="text-xs font-bold uppercase tracking-wider text-[#69717c] dark:text-[#aab3bd]">Analysis scope</p>
            <p className="mt-0.5 text-xs text-[#69717c] dark:text-[#aab3bd]">
              Applies to the behaviour scan, the comparison and the vendor intelligence panel. The directory
              above still lists every house.
            </p>
          </div>
          <label className="flex items-center gap-2 text-xs font-semibold text-[#30343A] dark:text-gray-200">
            House
            <select
              value={analysisHouse}
              onChange={(event) => setAnalysisHouse(event.target.value)}
              className="h-9 rounded-lg border border-[#dde1e5] bg-white px-2 text-sm dark:border-[#69717c] dark:bg-[#171b20]"
            >
              <option value="">All houses</option>
              <option value="Lok Sabha">Lok Sabha</option>
              <option value="Rajya Sabha">Rajya Sabha</option>
            </select>
          </label>
        </section>

        <div className="mt-5 space-y-5">
          <VendorBehaviourScan
            house={analysisHouse}
            onSelectVendor={(key, name) => setSelected({ normalized_name: key, vendor_name: name })}
          />
        </div>

        {compare.length > 0 && (
          <VendorComparison
            vendorKeys={compare.map((vendor) => vendor.normalized_name)}
            house={analysisHouse}
            onRemove={(key) => setCompare((current) => current.filter((vendor) => vendor.normalized_name !== key))}
            onClose={() => setCompare([])}
          />
        )}

        <div className="mt-5 rounded-xl border border-[#dde1e5] bg-white p-4 text-xs text-gray-600 shadow-sm dark:border-[#39434E] dark:bg-[#222831] dark:text-gray-300"><h2 className="font-bold text-[#30343A] dark:text-white">External verification status</h2><p className="mt-1">These integrations are placeholders only; no external verification was performed.</p><div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-5">{["MCA", "GST", "Government Procurement", "Debarment Records", "GeM"].map((source) => <div key={source} className="rounded-lg border border-dashed border-gray-300 p-3 dark:border-gray-600"><p className="font-semibold">{source}</p><p className="mt-1 text-[0.6875rem] text-gray-500">Not connected</p></div>)}</div><p className="mt-3">Data source: {data?.data_source}. Fields used: vendor, expenditure amount, work description, state, constituency, and project/risk fields. The current view is generated from project and expenditure records; vendor risk exposure is not a finding about any vendor.</p></div>
      </div>

      {selected && (
        <VendorIntelPanel
          vendorKey={selected.normalized_name}
          vendorName={selected.vendor_name}
          house={analysisHouse}
          inCompare={selectedIsInCompare}
          onClose={() => setSelected(null)}
          onCompare={() => toggleCompare(selected)}
          onContact={() => investigateVendor(selected, profile?.engagements)}
        />
      )}
    </div>
  )
})

export default VendorIntelligence
