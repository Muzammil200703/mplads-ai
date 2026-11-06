import { useEffect, useState } from "react"
import { getStates, getConstituencies } from "../services/api"

const API_URL = import.meta.env.VITE_API_URL !== undefined
  ? import.meta.env.VITE_API_URL
  : (import.meta.env.PROD ? "https://mplads-ai-optimized.onrender.com" : "http://127.0.0.1:8000")

function Reports() {
  const [reportType, setReportType] = useState("Project Audit Report")
  const [format, setFormat] = useState("CSV Dataset (.csv)")
  const [states, setStates] = useState([])
  const [constituencies, setConstituencies] = useState([])
  const [selectedState, setSelectedState] = useState("")
  const [selectedConstituency, setSelectedConstituency] = useState("")
  const [selectedRiskLevel, setSelectedRiskLevel] = useState("")
  const [selectedHouse, setSelectedHouse] = useState("")
  const [sortBy, setSortBy] = useState("")
  const [sortDir, setSortDir] = useState("desc")
  const [exportCount, setExportCount] = useState(null)
  const [aiReportText, setAiReportText] = useState("")
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState("")
  const [downloading, setDownloading] = useState(false)
  const [downloadNotice, setDownloadNotice] = useState("")
  const [error, setError] = useState("")

  useEffect(() => {
    getStates().then((s) => { if (Array.isArray(s)) setStates(s) }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!selectedState) { setConstituencies([]); setSelectedConstituency(""); return }
    getConstituencies(selectedState).then((d) => { if (Array.isArray(d)) setConstituencies(d) }).catch(() => {})
  }, [selectedState])

  // Fetch count of records that would be exported
  useEffect(() => {
    async function fetchCount() {
      try {
        const params = new URLSearchParams()
        params.set("report_type", reportType)
        if (selectedState) params.set("state", selectedState)
        if (selectedConstituency) params.set("constituency", selectedConstituency)
        if (selectedRiskLevel) params.set("risk_level", selectedRiskLevel)
        if (selectedHouse) params.set("house", selectedHouse)

        const r = await fetch(`${API_URL}/export/report-count?${params.toString()}`)
        if (r.ok) {
          const data = await r.json()
          setExportCount(data.total_records)
        }
      } catch {
        setExportCount(null)
      }
    }
    fetchCount()
  }, [reportType, selectedState, selectedConstituency, selectedRiskLevel, selectedHouse])

  const handleExport = async () => {
    try {
      setDownloading(true)
      setError("")
      setDownloadNotice("")

      const params = new URLSearchParams()
      params.set("report_type", reportType)
      if (selectedState) params.set("state", selectedState)
      if (selectedConstituency) params.set("constituency", selectedConstituency)
      if (selectedRiskLevel && reportType === "Anomaly Summary Report") params.set("risk_level", selectedRiskLevel)
      if (selectedHouse) params.set("house", selectedHouse)
      if (sortBy) {
        params.set("sort_by", sortBy)
        params.set("sort_dir", sortDir)
      }

      if (format.includes("CSV")) {
        const r = await fetch(`${API_URL}/export/report?${params.toString()}`)
        if (!r.ok) throw new Error("Export failed")
        const blob = await r.blob()
        const url = URL.createObjectURL(blob)
        const link = document.createElement("a")
        link.href = url
        const disposition = r.headers.get("content-disposition")
        const filenameMatch = disposition && disposition.match(/filename="?(.+?)"?$/)
        link.download = filenameMatch ? filenameMatch[1] : `MPLADS_Report_${new Date().toISOString().slice(0, 10)}.csv`
        document.body.appendChild(link)
        link.click()
        document.body.removeChild(link)
        URL.revokeObjectURL(url)
        setDownloadNotice(`CSV exported with ${exportCount?.toLocaleString("en-IN") || "?"} records`)
      } else if (format.includes("JSON")) {
        const r = await fetch(`${API_URL}/export/report?${params.toString()}`)
        if (!r.ok) throw new Error("Export failed")
        const text = await r.text()
        // Convert CSV to JSON
        const lines = text.split("\n").filter(Boolean)
        const headers = lines[0].split(",")
        const rows = lines.slice(1).map((line) => {
          const values = line.split(",")
          const obj = {}
          headers.forEach((h, i) => { obj[h] = values[i] || "" })
          return obj
        })
        const blob = new Blob([JSON.stringify(rows, null, 2)], { type: "application/json" })
        const url = URL.createObjectURL(blob)
        const link = document.createElement("a")
        link.href = url
        link.download = `MPLADS_Report_${new Date().toISOString().slice(0, 10)}.json`
        document.body.appendChild(link)
        link.click()
        document.body.removeChild(link)
        URL.revokeObjectURL(url)
        setDownloadNotice(`JSON exported with ${rows.length} records`)
      } else {
        setDownloadNotice("Export format not available.")
      }
      setTimeout(() => setDownloadNotice(""), 5000)
    } catch (err) {
      console.error("Export error:", err)
      setError("Export failed. Make sure the backend is running.")
    } finally {
      setDownloading(false)
    }
  }

  const generateAiReport = async () => {
    setAiLoading(true); setAiError("")
    try {
      const qs = selectedState ? `?state=${encodeURIComponent(selectedState)}` : ""
      const r = await fetch(`${API_URL}/export/ai-audit-summary${qs}`)
      if (!r.ok) throw new Error("Report generation failed")
      setAiReportText(await r.text())
    } catch (e) {
      setAiError(e.message || "Could not generate the AI Audit Report.")
    } finally {
      setAiLoading(false)
    }
  }

  const downloadAiReport = () => {
    const blob = new Blob([aiReportText], { type: "text/plain;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = `MPLADS_AI_Audit_Report_${new Date().toISOString().slice(0, 10)}.txt`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="min-h-full bg-[#f4f5f6] p-4 sm:p-6 text-[#30343A] transition-colors duration-200 dark:bg-[#171b20] dark:text-[#f1f3f5]">
      <div className="mx-auto max-w-[1440px] space-y-4 sm:space-y-6">
        <div>
          <h2 className="text-2xl font-bold text-[#30343A] dark:text-white">
            Audit Reports & Data Export
          </h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Generate and export official MPLADS audit reports from the full server-side dataset.
          </p>
        </div>

        {error && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-xs font-semibold text-red-700 dark:bg-red-950/40 dark:text-red-300">
            ⚠ {error}
          </div>
        )}

        <div className="grid grid-cols-1 gap-4 sm:gap-5 lg:grid-cols-12">
          {/* Settings Panel */}
          <div className="space-y-4 lg:col-span-4">
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#222831]">
              <h3 className="mb-3 text-xs font-bold uppercase tracking-wider text-gray-400">
                Report Settings
              </h3>
              <div className="space-y-3">
                <div>
                  <label className="block text-[0.6875rem] font-bold uppercase tracking-wider text-gray-600 dark:text-gray-300">Report Type</label>
                  <select value={reportType} onChange={(e) => setReportType(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white">
                    <option>Project Audit Report</option>
                    <option>Anomaly Summary Report</option>
                    <option>Regional Risk Report</option>
                    <option>Financial Utilization Report</option>
                  </select>
                </div>

                <div>
                  <label className="block text-[0.6875rem] font-bold uppercase tracking-wider text-gray-600 dark:text-gray-300">House Scope</label>
                  <select value={selectedHouse} onChange={(e) => setSelectedHouse(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white">
                    <option value="">All Houses</option>
                    <option value="Lok Sabha">Lok Sabha</option>
                    <option value="Rajya Sabha">Rajya Sabha</option>
                  </select>
                </div>

                <div>
                  <label className="block text-[0.6875rem] font-bold uppercase tracking-wider text-gray-600 dark:text-gray-300">State Scope</label>
                  <select value={selectedState} onChange={(e) => setSelectedState(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white">
                    <option value="">All States (Nationwide)</option>
                    {states.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </div>

                <div>
                  <label className="block text-[0.6875rem] font-bold uppercase tracking-wider text-gray-600 dark:text-gray-300">Constituency Scope</label>
                  <select value={selectedConstituency} disabled={!selectedState} onChange={(e) => setSelectedConstituency(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 disabled:opacity-50 dark:border-gray-600 dark:bg-[#171b20] dark:text-white">
                    <option value="">{selectedState ? "All Constituencies" : "Select State First"}</option>
                    {constituencies.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>

                {reportType === "Anomaly Summary Report" && (
                  <div>
                    <label className="block text-[0.6875rem] font-bold uppercase tracking-wider text-gray-600 dark:text-gray-300">Risk Level</label>
                    <select value={selectedRiskLevel} onChange={(e) => setSelectedRiskLevel(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white">
                      <option value="">All Risk Levels</option>
                      <option value="High">High Risk</option>
                      <option value="Medium">Medium Risk</option>
                      <option value="Low">Low Risk</option>
                    </select>
                  </div>
                )}

                <div>
                  <label className="block text-[0.6875rem] font-bold uppercase tracking-wider text-gray-600 dark:text-gray-300">Export Format</label>
                  <select value={format} onChange={(e) => setFormat(e.target.value)} className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white">
                    <option>CSV Dataset (.csv)</option>
                    <option>JSON Report (.json)</option>

                  </select>
                </div>

                <div>
                  <label className="block text-[0.6875rem] font-bold uppercase tracking-wider text-gray-600 dark:text-gray-300">Sort By</label>
                  <div className="mt-1 flex gap-2">
                    <select value={sortBy} onChange={(e) => setSortBy(e.target.value)} className="flex-1 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-2xs outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-600 dark:bg-[#171b20] dark:text-white">
                      <option value="">Default Order</option>
                      <option value="id">Project ID</option>
                      <option value="project_name">Project Name</option>
                      <option value="sanctioned_amount">Sanctioned Amount</option>
                      <option value="expenditure">Expenditure</option>
                      <option value="completion_percentage">Progress</option>
                      <option value="state">State</option>
                    </select>
                    <button
                      onClick={() => setSortDir((d) => d === "asc" ? "desc" : "asc")}
                      disabled={!sortBy}
                      className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-bold text-gray-700 shadow-2xs outline-none transition hover:bg-gray-50 disabled:opacity-40 dark:border-gray-600 dark:bg-[#171b20] dark:text-gray-200"
                    >
                      {sortDir === "asc" ? "↑ Asc" : "↓ Desc"}
                    </button>
                  </div>
                </div>

                {/* Export count preview */}
                {exportCount !== null && (
                  <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-center dark:border-blue-900/60 dark:bg-blue-950/30">
                    <p className="text-[0.625rem] font-bold uppercase tracking-wider text-blue-600 dark:text-blue-300">Full Dataset Export</p>
                    <p className="mt-1 font-mono text-lg font-bold text-blue-800 dark:text-blue-200">
                      {exportCount.toLocaleString("en-IN")} records
                    </p>
                    <p className="text-[0.625rem] text-blue-500 dark:text-blue-400">
                      Server-side query — all matching records will be exported
                    </p>
                  </div>
                )}

                <button
                  onClick={handleExport}
                  disabled={downloading || exportCount === 0}
                  className="mt-4 w-full rounded-lg bg-[#46566a] py-2.5 text-xs font-bold uppercase tracking-wider text-white shadow-sm transition hover:bg-[#2c3540] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-700"
                >
                  {downloading ? "Generating Report..." : `Download Report${exportCount !== null ? ` (${exportCount.toLocaleString("en-IN")} records)` : ""}`}
                </button>

                {downloadNotice && (
                  <p className="mt-2 text-center text-xs font-semibold text-green-600 dark:text-green-400">
                    ✓ {downloadNotice}
                  </p>
                )}
              </div>
            </div>
          </div>

          {/* Preview Panel */}
          <div className="space-y-4 sm:space-y-5 lg:col-span-8">
            {/* AI Audit Report — narrative summary generated from live aggregates */}
            <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-700 dark:bg-[#222831]">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-bold text-[#30343A] dark:text-white">AI Audit Report</h3>
                  <p className="mt-1 max-w-xl text-xs text-gray-500 dark:text-gray-400">
                    A narrative summary built from live database aggregates — risk distribution, dominant risk
                    indicators, ground-verification and inquiry activity. Every figure is a recorded value;
                    indicators are review signals, never confirmed findings.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={generateAiReport}
                    disabled={aiLoading}
                    className="rounded-lg bg-[#46566a] px-4 py-2 text-xs font-bold uppercase tracking-wider text-white shadow-sm transition hover:bg-[#2c3540] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-700"
                  >
                    {aiLoading ? "Generating…" : "Generate AI Audit Report"}
                  </button>
                  {aiReportText && (
                    <button
                      onClick={downloadAiReport}
                      className="rounded-lg border border-gray-300 px-3 py-2 text-xs font-bold text-gray-700 transition hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
                    >
                      Download .txt
                    </button>
                  )}
                </div>
              </div>
              {aiError && (
                <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-xs font-semibold text-red-700 dark:bg-red-950/40 dark:text-red-300">⚠ {aiError}</p>
              )}
              {aiReportText && (
                <pre className="mt-4 max-h-96 overflow-auto whitespace-pre-wrap rounded-xl border border-gray-200 bg-gray-50 p-4 font-mono text-[0.6875rem] leading-relaxed text-gray-700 dark:border-gray-700 dark:bg-[#171b20] dark:text-gray-300">
                  {aiReportText}
                </pre>
              )}
            </div>

            <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-700 dark:bg-[#222831]">
              <div className="mb-4 border-b border-gray-100 pb-3 text-center dark:border-gray-700">
                <h2 className="text-xl font-bold">{reportType}</h2>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Scope: {selectedState || "All States"}
                  {selectedConstituency ? ` > ${selectedConstituency}` : ""}
                  {reportType === "Anomaly Summary Report" && selectedRiskLevel ? ` | Risk: ${selectedRiskLevel}` : ""}
                  {" | "}
                  <span className="font-bold text-blue-600 dark:text-blue-400">
                    {exportCount !== null ? `${exportCount.toLocaleString("en-IN")} total records` : "Loading..."}
                  </span>
                </p>
              </div>

              <div className="space-y-4">
                <div className="rounded-lg border border-gray-200 bg-gray-50 p-5 text-center dark:border-gray-700 dark:bg-[#171b20]">
                  <span className="text-3xl">📊</span>
                  <h3 className="mt-3 text-sm font-bold">Server-Side Report Generation</h3>
                  <p className="mt-2 max-w-md text-xs text-gray-500 dark:text-gray-400">
                    When you click <strong>Download Report</strong>, the backend queries the full MPLADS database
                    using your selected filters and generates the CSV/JSON file server-side.
                    This ensures you receive <strong>all matching records</strong>, not just the preview subset.
                  </p>
                  <div className="mt-4 grid grid-cols-3 gap-4 text-center">
                    <div>
                      <p className="text-[0.625rem] font-bold uppercase text-gray-400">Report Type</p>
                      <p className="mt-1 text-xs font-semibold">{reportType}</p>
                    </div>
                    <div>
                      <p className="text-[0.625rem] font-bold uppercase text-gray-400">Scope</p>
                      <p className="mt-1 text-xs font-semibold">{selectedState || "Nationwide"}</p>
                    </div>
                    <div>
                      <p className="text-[0.625rem] font-bold uppercase text-gray-400">Format</p>
                      <p className="mt-1 text-xs font-semibold">{format}</p>
                    </div>
                  </div>
                </div>

                <div className="rounded-lg border border-green-200 bg-green-50 p-4 dark:border-green-900/40 dark:bg-green-950/20">
                  <div className="flex items-start gap-2">
                    <span className="mt-0.5 text-green-500">✓</span>
                    <div>
                      <p className="text-xs font-bold text-green-700 dark:text-green-400">Export includes all matching records</p>
                      <p className="mt-0.5 text-[0.625rem] text-green-600 dark:text-green-500">
                        The server queries the database directly. Filters for state, constituency, risk level, and status
                        are applied at the SQL level for maximum efficiency.
                      </p>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default Reports
