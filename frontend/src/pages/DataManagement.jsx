import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  getDataOverview,
  getImportHistory,
  getImportDetail,
  uploadDataFiles,
  revalidateImport,
  commitImport,
  rollbackImport,
  refreshImport,
  discardImport,
} from "../services/api"
import { formatNumber } from "../utils/format"
import { RequireRole } from "./Workspace"

/* ═══════════════════════════════════════════════════════════════
   Admin → Data Management.
   A CSV ingestion pipeline: upload → auto-detect → validate →
   preview (new / update / unchanged / rejected) → commit → derived
   analytics refresh. Every number shown here comes from the backend.
   ═══════════════════════════════════════════════════════════════ */

const TABS = [
  { key: "upload", label: "Upload Data" },
  { key: "validate", label: "Validation" },
  { key: "history", label: "Import History" },
  { key: "overview", label: "Dataset Overview" },
]

const DATASET_OPTIONS = [
  { value: "recommended_works", label: "Recommended Works" },
  { value: "completed_works", label: "Completed Works" },
  { value: "expenditures", label: "Expenditure Ledger" },
  { value: "mp_summaries", label: "MP Summaries" },
  { value: "projects", label: "Project Records" },
]

const STATUS_STYLE = {
  preview: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  needs_type: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  invalid: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  importing: "bg-indigo-100 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300",
  refreshing: "bg-indigo-100 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300",
  success: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  partial: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  failed: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  rolled_back: "bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200",
}

const STATUS_LABEL = {
  preview: "Ready to import",
  needs_type: "Needs dataset type",
  invalid: "Rejected",
  importing: "Importing…",
  refreshing: "Refreshing analytics…",
  success: "Imported",
  partial: "Imported (refresh incomplete)",
  failed: "Failed",
  rolled_back: "Rolled back",
}

function bytes(n) {
  const v = Number(n || 0)
  if (v >= 1024 * 1024) return `${(v / (1024 * 1024)).toFixed(1)} MB`
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KB`
  return `${v} B`
}

function when(iso) {
  if (!iso) return "—"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" })
}

function Card({ children, className = "" }) {
  return (
    <div className={`rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000] ${className}`}>
      {children}
    </div>
  )
}

function Badge({ status }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-bold ${STATUS_STYLE[status] || "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"}`}>
      {STATUS_LABEL[status] || status}
    </span>
  )
}

function Stat({ label, value, tone = "" }) {
  return (
    <div className="rounded-lg border border-[#e8dadc] px-3 py-2 dark:border-[#351923]">
      <p className="text-[0.6875rem] font-semibold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">{label}</p>
      <p className={`mt-0.5 text-lg font-bold ${tone || "text-[#292326] dark:text-[#f5eff1]"}`}>{value}</p>
    </div>
  )
}

function Spinner() {
  return <div className="h-4 w-4 animate-spin rounded-full border-2 border-blue-600 border-r-transparent" />
}

/* ── one staged file / batch card ───────────────────────────────── */

function BatchCard({ batch, onChange, onBusy }) {
  const [open, setOpen] = useState(false)
  const [working, setWorking] = useState("")
  const [error, setError] = useState("")
  const [localType, setLocalType] = useState(batch.dataset_type || "")
  const [localHouse, setLocalHouse] = useState(batch.house_override || "")
  const preview = batch.preview || {}
  const issues = preview.issues || []
  const editable = ["preview", "needs_type", "invalid", "failed"].includes(batch.status)

  useEffect(() => {
    setLocalType(batch.dataset_type || "")
    setLocalHouse(batch.house_override || "")
  }, [batch.id, batch.dataset_type, batch.house_override])

  const run = async (name, fn) => {
    setWorking(name)
    setError("")
    try {
      const updated = await fn()
      if (updated && updated.id) onChange(updated)
      else if (name === "discard") onChange({ ...batch, status: "discarded" })
    } catch (e) {
      setError(e.message || "The action failed")
    } finally {
      setWorking("")
      if (onBusy) onBusy(false)
    }
  }

  const applyTypeOrHouse = async (nextType, nextHouse) => {
    if (!editable) return
    await run("revalidate", () => revalidateImport(batch.id, { datasetType: nextType, house: nextHouse }))
  }

  const confirmImport = () => {
    const changed = (batch.new_rows || 0) + (batch.update_rows || 0)
    if (!window.confirm(
      `Import ${batch.filename}?\n\nNew records: ${batch.new_rows || 0}\nRecords to update: ${batch.update_rows || 0}\n` +
      `Unchanged: ${batch.unchanged_rows || 0}\nRejected: ${batch.rejected_rows || 0}\n\n` +
      `This writes ${changed} row(s) in one transaction and then recomputes risk scores.`
    )) return
    run("commit", () => commitImport(batch.id, { datasetType: localType || undefined, house: localHouse || undefined }))
  }

  const rollback = () => {
    if (!window.confirm(`Roll back import #${batch.id} (${batch.filename})? Inserted rows are deleted and updated rows restored.`)) return
    run("rollback", () => rollbackImport(batch.id))
  }

  const reRefresh = () => run("refresh", () => refreshImport(batch.id))
  const discard = () => {
    if (!window.confirm(`Discard ${batch.filename}? The staged file is deleted; nothing in the database changes.`)) return
    run("discard", () => discardImport(batch.id))
  }

  const refreshSteps = (batch.details && batch.details.refresh && batch.details.refresh.steps) || {}

  return (
    <div className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[0.9375rem] font-bold text-[#292326] dark:text-[#f5eff1]">{batch.filename}</p>
          <p className="mt-0.5 text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">
            #{batch.id} · {bytes(batch.file_size)} · {batch.dataset_label}
            {batch.house_coverage ? ` · ${batch.house_coverage}` : ""}
            {batch.detection_confidence ? ` · auto-detect ${Math.round(batch.detection_confidence * 100)}%` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge status={batch.status} />
          {["importing", "refreshing"].includes(batch.status) && <Spinner />}
        </div>
      </div>

      {batch.error_summary && (
        <p className={`mt-2 rounded-lg px-3 py-2 text-[0.8125rem] ${batch.status === "invalid" || batch.status === "failed" ? "bg-red-50 text-red-700 dark:bg-red-950/30 dark:text-red-300" : "bg-amber-50 text-amber-800 dark:bg-amber-950/30 dark:text-amber-300"}`}>
          {batch.error_summary}
        </p>
      )}

      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
        <Stat label="Rows" value={formatNumber(batch.total_rows)} />
        <Stat label="Valid" value={formatNumber(batch.valid_rows)} />
        <Stat label="New" value={formatNumber(batch.new_rows)} tone="text-green-700 dark:text-green-400" />
        <Stat label="Updates" value={formatNumber(batch.update_rows)} tone="text-blue-700 dark:text-blue-400" />
        <Stat label="Unchanged" value={formatNumber(batch.unchanged_rows)} />
        <Stat label="Rejected" value={formatNumber(batch.rejected_rows)} tone={(batch.rejected_rows || 0) > 0 ? "text-red-700 dark:text-red-400" : ""} />
      </div>

      {editable && (
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <label className="text-[0.8125rem] font-semibold text-[#292326] dark:text-[#f5eff1]">
            Dataset type
            <select
              value={localType}
              disabled={!!working}
              onChange={(e) => { setLocalType(e.target.value); applyTypeOrHouse(e.target.value, localHouse) }}
              className="mt-1 block rounded-lg border border-[#e8dadc] bg-white px-2 py-1.5 text-[0.8125rem] dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100"
            >
              <option value="">Select…</option>
              {DATASET_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
          <label className="text-[0.8125rem] font-semibold text-[#292326] dark:text-[#f5eff1]">
            House {batch.house_coverage === "Unknown" ? "(required)" : "(when file lacks it)"}
            <select
              value={localHouse}
              disabled={!!working}
              onChange={(e) => { setLocalHouse(e.target.value); applyTypeOrHouse(localType, e.target.value) }}
              className="mt-1 block rounded-lg border border-[#e8dadc] bg-white px-2 py-1.5 text-[0.8125rem] dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100"
            >
              <option value="">Use file value</option>
              <option value="Lok Sabha">Lok Sabha</option>
              <option value="Rajya Sabha">Rajya Sabha</option>
            </select>
          </label>
        </div>
      )}

      {error && <p className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-[0.8125rem] font-semibold text-red-700 dark:bg-red-950/30 dark:text-red-300">{error}</p>}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {editable && batch.status !== "invalid" && (
          <button
            onClick={confirmImport}
            disabled={!!working || (batch.new_rows || 0) + (batch.update_rows || 0) === 0}
            className="rounded-lg bg-[#c8102e] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-40 dark:bg-blue-600 dark:hover:bg-blue-500"
          >
            {working === "commit" ? "Importing…" : "Confirm import"}
          </button>
        )}
        {["success", "partial"].includes(batch.status) && (
          <>
            <button onClick={rollback} disabled={!!working} className="rounded-lg border border-red-300 px-3 py-2 text-sm font-bold text-red-700 hover:bg-red-50 disabled:opacity-40 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950/30">
              {working === "rollback" ? "Rolling back…" : "Roll back"}
            </button>
            <button onClick={reRefresh} disabled={!!working} className="rounded-lg border border-[#e8dadc] px-3 py-2 text-sm font-bold text-[#292326] hover:bg-[#f8f1f1] disabled:opacity-40 dark:border-[#351923] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]">
              {working === "refresh" ? "Refreshing…" : "Refresh analytics"}
            </button>
          </>
        )}
        {editable && (
          <button onClick={discard} disabled={!!working} className="rounded-lg border border-[#e8dadc] px-3 py-2 text-sm font-semibold text-[#71666a] hover:bg-[#f8f1f1] disabled:opacity-40 dark:border-[#351923] dark:text-[#b9aeb2] dark:hover:bg-[#0d090b]">
            Discard
          </button>
        )}
        {(issues.length > 0 || preview.missing_linkage > 0 || Object.keys(refreshSteps).length > 0) && (
          <button onClick={() => setOpen((v) => !v)} className="rounded-lg px-3 py-2 text-sm font-semibold text-blue-700 hover:bg-blue-50 dark:text-blue-300 dark:hover:bg-blue-950/30">
            {open ? "Hide details" : `Details${preview.issue_count ? ` (${preview.issue_count})` : ""}`}
          </button>
        )}
      </div>

      {open && (
        <div className="mt-3 space-y-3 text-[0.8125rem]">
          {preview.missing_linkage > 0 && (
            <p className="rounded-lg bg-amber-50 px-3 py-2 text-amber-800 dark:bg-amber-950/30 dark:text-amber-300">
              {formatNumber(preview.missing_linkage)} work key(s) in this file have no matching recommended work — linkage may need review.
            </p>
          )}
          {batch.details && batch.details.rollback && (
            <p className="rounded-lg bg-gray-100 px-3 py-2 text-gray-700 dark:bg-gray-800 dark:text-gray-200">
              Rolled back by {batch.details.rollback.by || "admin"} · deleted {batch.details.rollback.rows_deleted} inserted row(s), restored {batch.details.rollback.rows_restored} updated row(s).
            </p>
          )}
          {Object.keys(refreshSteps).length > 0 && (
            <div className="rounded-lg border border-[#e8dadc] px-3 py-2 dark:border-[#351923]">
              <p className="font-bold text-[#292326] dark:text-[#f5eff1]">Derived analytics refresh</p>
              <ul className="mt-1 space-y-0.5 text-[#71666a] dark:text-[#b9aeb2]">
                {Object.entries(refreshSteps).map(([k, v]) => (
                  <li key={k}>
                    {k.replace(/_/g, " ")}: {typeof v === "object" ? (v.error ? `failed — ${v.error}` : JSON.stringify(v)) : String(v)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {issues.length > 0 && (
            <div className="overflow-hidden rounded-lg border border-[#e8dadc] dark:border-[#351923]">
              <table className="w-full text-left">
                <thead className="bg-[#faf7f6] text-[0.6875rem] uppercase tracking-wide text-[#71666a] dark:bg-[#0d090b] dark:text-[#b9aeb2]">
                  <tr>
                    <th className="px-3 py-1.5">Row</th>
                    <th className="px-3 py-1.5">Field</th>
                    <th className="px-3 py-1.5">Message</th>
                  </tr>
                </thead>
                <tbody>
                  {issues.slice(0, 50).map((i, idx) => (
                    <tr key={idx} className="border-t border-[#e8dadc] dark:border-[#351923]">
                      <td className="px-3 py-1.5 tabular-nums">{i.row ?? "—"}</td>
                      <td className="px-3 py-1.5 text-[#71666a] dark:text-[#b9aeb2]">{i.field}</td>
                      <td className={`px-3 py-1.5 ${i.severity === "error" ? "text-red-700 dark:text-red-300" : "text-amber-800 dark:text-amber-300"}`}>{i.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ── upload zone ────────────────────────────────────────────────── */

function UploadPanel({ onBatches }) {
  const [files, setFiles] = useState([])
  const [dragging, setDragging] = useState(false)
  const [datasetType, setDatasetType] = useState("")
  const [house, setHouse] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const inputRef = useRef(null)

  const addFiles = (list) => {
    const next = Array.from(list || []).filter((f) => /\.(csv|tsv|txt)$/i.test(f.name))
    setFiles((prev) => {
      const keys = new Set(prev.map((f) => `${f.name}:${f.size}`))
      return [...prev, ...next.filter((f) => !keys.has(`${f.name}:${f.size}`))]
    })
    if (next.length !== (list ? list.length : 0)) {
      setError("Only .csv, .tsv and .txt files are accepted.")
    } else {
      setError("")
    }
  }

  const upload = async () => {
    if (!files.length) return
    setBusy(true)
    setError("")
    try {
      const res = await uploadDataFiles(files, { datasetType: datasetType || undefined, house: house || undefined })
      const results = res.results || []
      const ok = results.filter((r) => r.ok).map((r) => r.batch)
      const failed = results.filter((r) => !r.ok)
      if (failed.length) {
        setError(failed.map((f) => `${f.filename}: ${f.error}`).join(" · "))
      }
      if (ok.length) {
        onBatches(ok)
        setFiles([])
        if (inputRef.current) inputRef.current.value = ""
      }
    } catch (e) {
      setError(e.message || "Upload failed")
    } finally {
      setBusy(false)
    }
  }

  const totalSize = useMemo(() => files.reduce((n, f) => n + (f.size || 0), 0), [files])

  return (
    <Card>
      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files) }}
        onClick={() => inputRef.current && inputRef.current.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") inputRef.current && inputRef.current.click() }}
        className={`cursor-pointer rounded-xl border-2 border-dashed px-4 py-10 text-center transition ${
          dragging
            ? "border-blue-500 bg-blue-50/60 dark:bg-blue-950/20"
            : "border-[#e8dadc] hover:border-[#c8102e] dark:border-[#351923] dark:hover:border-blue-500"
        }`}
      >
        <p className="text-3xl">⬆️</p>
        <p className="mt-2 font-bold text-[#292326] dark:text-[#f5eff1]">Drag &amp; drop official MPLADS CSV files</p>
        <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">
          or click to browse · multiple files supported · .csv / .tsv / .txt · up to 60 MB each
        </p>
        <input
          ref={inputRef}
          type="file"
          accept=".csv,.tsv,.txt"
          multiple
          className="hidden"
          onChange={(e) => addFiles(e.target.files)}
        />
      </div>

      {files.length > 0 && (
        <div className="mt-3">
          <div className="flex items-center justify-between">
            <p className="text-sm font-bold text-[#292326] dark:text-[#f5eff1]">{files.length} file(s) · {bytes(totalSize)}</p>
            <button onClick={() => setFiles([])} className="text-xs font-semibold text-[#71666a] hover:underline dark:text-[#b9aeb2]">Clear</button>
          </div>
          <ul className="mt-2 divide-y divide-[#e8dadc] rounded-lg border border-[#e8dadc] dark:divide-[#351923] dark:border-[#351923]">
            {files.map((f, i) => (
              <li key={`${f.name}-${i}`} className="flex items-center justify-between gap-2 px-3 py-2 text-[0.8125rem]">
                <span className="truncate font-semibold text-[#292326] dark:text-[#f5eff1]">{f.name}</span>
                <span className="shrink-0 text-[#71666a] dark:text-[#b9aeb2]">{bytes(f.size)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <label className="text-[0.8125rem] font-semibold text-[#292326] dark:text-[#f5eff1]">
          Dataset type
          <select value={datasetType} onChange={(e) => setDatasetType(e.target.value)} className="mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-2 py-2 text-sm dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100">
            <option value="">Auto-detect from headers</option>
            {DATASET_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </label>
        <label className="text-[0.8125rem] font-semibold text-[#292326] dark:text-[#f5eff1]">
          House (only if the file has no House column)
          <select value={house} onChange={(e) => setHouse(e.target.value)} className="mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-2 py-2 text-sm dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100">
            <option value="">Use file value</option>
            <option value="Lok Sabha">Lok Sabha</option>
            <option value="Rajya Sabha">Rajya Sabha</option>
          </select>
        </label>
        <div className="flex items-end">
          <button
            onClick={upload}
            disabled={busy || !files.length}
            className="w-full rounded-lg bg-[#c8102e] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-40 dark:bg-blue-600 dark:hover:bg-blue-500"
          >
            {busy ? "Validating…" : "Upload & validate"}
          </button>
        </div>
      </div>
      <p className="mt-2 text-xs text-[#71666a] dark:text-[#b9aeb2]">
        Files are staged, parsed as text only, and validated. Nothing is written to the database until you confirm the import.
      </p>
      {error && <p className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-[0.8125rem] font-semibold text-red-700 dark:bg-red-950/30 dark:text-red-300">{error}</p>}
    </Card>
  )
}

/* ── history ────────────────────────────────────────────────────── */

function HistoryPanel({ onOpen }) {
  const [items, setItems] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  const load = useCallback(() => {
    setLoading(true); setError("")
    getImportHistory(100)
      .then((d) => setItems(d.items || []))
      .catch((e) => setError(e.message || "Could not load import history"))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load])

  if (loading && !items) return <Card><div className="flex items-center gap-3"><Spinner /><span className="text-sm text-[#71666a] dark:text-[#b9aeb2]">Loading import history…</span></div></Card>
  if (error) return <Card><p className="text-sm font-semibold text-red-700 dark:text-red-300">{error}</p></Card>

  return (
    <Card>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm font-bold text-[#292326] dark:text-[#f5eff1]">{items?.length || 0} import batch(es)</p>
        <button onClick={load} className="text-xs font-semibold text-blue-700 hover:underline dark:text-blue-300">Refresh</button>
      </div>
      {!items?.length ? (
        <p className="py-8 text-center text-sm text-[#71666a] dark:text-[#b9aeb2]">No imports yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px] text-left text-[0.8125rem]">
            <thead className="bg-[#faf7f6] text-[0.6875rem] uppercase tracking-wide text-[#71666a] dark:bg-[#0d090b] dark:text-[#b9aeb2]">
              <tr>
                <th className="px-2 py-2">#</th>
                <th className="px-2 py-2">File</th>
                <th className="px-2 py-2">Dataset</th>
                <th className="px-2 py-2">House</th>
                <th className="px-2 py-2">Uploaded by</th>
                <th className="px-2 py-2">When</th>
                <th className="px-2 py-2">Status</th>
                <th className="px-2 py-2 text-right">New</th>
                <th className="px-2 py-2 text-right">Upd</th>
                <th className="px-2 py-2 text-right">Same</th>
                <th className="px-2 py-2 text-right">Rej</th>
                <th className="px-2 py-2" />
              </tr>
            </thead>
            <tbody>
              {(items || []).map((b) => (
                <tr key={b.id} className="border-t border-[#e8dadc] dark:border-[#351923]">
                  <td className="px-2 py-2 tabular-nums">{b.id}</td>
                  <td className="max-w-[220px] truncate px-2 py-2 font-semibold text-[#292326] dark:text-[#f5eff1]">{b.filename}</td>
                  <td className="px-2 py-2">{b.dataset_label}</td>
                  <td className="px-2 py-2">{b.house_coverage || "—"}</td>
                  <td className="px-2 py-2">{b.uploaded_by || "—"}</td>
                  <td className="px-2 py-2 whitespace-nowrap">{when(b.uploaded_at)}</td>
                  <td className="px-2 py-2"><Badge status={b.status} /></td>
                  <td className="px-2 py-2 text-right tabular-nums text-green-700 dark:text-green-400">{formatNumber(b.new_rows)}</td>
                  <td className="px-2 py-2 text-right tabular-nums text-blue-700 dark:text-blue-400">{formatNumber(b.update_rows)}</td>
                  <td className="px-2 py-2 text-right tabular-nums">{formatNumber(b.unchanged_rows)}</td>
                  <td className="px-2 py-2 text-right tabular-nums text-red-700 dark:text-red-400">{formatNumber(b.rejected_rows)}</td>
                  <td className="px-2 py-2 text-right">
                    <button onClick={() => onOpen(b.id)} className="rounded border border-[#e8dadc] px-2 py-1 text-xs font-bold hover:bg-[#f8f1f1] dark:border-[#351923] dark:hover:bg-[#0d090b]">Details</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

/* ── overview ───────────────────────────────────────────────────── */

function OverviewPanel({ data }) {
  if (!data) return null
  const houses = (row) => Object.entries(row.houses || {}).map(([h, n]) => `${h}: ${formatNumber(n)}`).join(" · ")
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Dataset version" value={data.dataset_version ?? 0} />
        <Stat label="Snapshot date" value={data.snapshot_date || "—"} />
        <Stat label="Pending validation" value={formatNumber(data.pending_validation_batches)} />
        <Stat label="Stored risk scores" value={formatNumber(data.risk_scores)} />
      </div>
      <Card>
        <p className="mb-3 text-sm font-bold text-[#292326] dark:text-[#f5eff1]">Current record counts</p>
        <div className="space-y-2">
          {(data.datasets || []).map((d) => (
            <div key={d.dataset_type} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[#e8dadc] px-3 py-2 dark:border-[#351923]">
              <span className="text-[0.8125rem] font-semibold text-[#292326] dark:text-[#f5eff1]">{d.label}</span>
              <span className="text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">{formatNumber(d.records)} records</span>
              <span className="text-xs text-[#71666a] dark:text-[#b9aeb2]">{houses(d)}</span>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-[#71666a] dark:text-[#b9aeb2]">
          Last successful import: {data.last_success_at ? when(data.last_success_at) : "none recorded"}
          {data.last_import ? ` · ${data.last_import.filename} (${data.last_import.dataset_label})` : ""}
        </p>
      </Card>
      <Card>
        <p className="mb-2 text-sm font-bold text-[#292326] dark:text-[#f5eff1]">Supported dataset formats</p>
        <div className="space-y-2">
          {(data.supported_types || []).map((t) => (
            <div key={t.dataset_type} className="rounded-lg border border-[#e8dadc] px-3 py-2 dark:border-[#351923]">
              <p className="text-[0.8125rem] font-semibold text-[#292326] dark:text-[#f5eff1]">
                {t.label}
                <span className="ml-2 rounded bg-[#f8f1f1] px-1.5 py-0.5 text-[0.6875rem] font-bold text-[#292326] dark:bg-[#0d090b] dark:text-gray-200">
                  {t.mode === "fingerprint" ? "content-keyed ledger" : t.mode === "projects" ? "id / composite key" : "source-keyed"}
                </span>
              </p>
              <p className="mt-0.5 text-xs text-[#71666a] dark:text-[#b9aeb2]">Required columns: {(t.required || []).join(", ")}</p>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}

/* ── page ───────────────────────────────────────────────────────── */

function DataManagementInner() {
  const [tab, setTab] = useState("upload")
  const [batches, setBatches] = useState([])
  const [overview, setOverview] = useState(null)
  const [overviewError, setOverviewError] = useState("")
  const [detail, setDetail] = useState(null)

  const loadOverview = useCallback(() => {
    getDataOverview()
      .then((d) => { setOverview(d); setOverviewError("") })
      .catch((e) => setOverviewError(e.message || "Could not load dataset overview"))
  }, [])

  useEffect(() => { loadOverview() }, [loadOverview])

  // Rehydrate staged batches (preview / needs_type / invalid) so validation
  // work survives a page reload instead of living only in component state.
  useEffect(() => {
    getImportHistory(50)
      .then((d) => {
        const pending = (d.items || []).filter((b) => ["preview", "needs_type", "invalid", "failed"].includes(b.status))
        if (pending.length) setBatches((prev) => [...pending.filter((p) => !prev.some((x) => x.id === p.id)), ...prev])
      })
      .catch(() => { /* the upload flow still works without history */ })
  }, [])

  // Poll batches that are actively importing/refreshing.
  useEffect(() => {
    const active = batches.filter((b) => ["importing", "refreshing"].includes(b.status))
    if (!active.length) return
    const t = setInterval(async () => {
      for (const b of active) {
        try {
          const updated = await getImportDetail(b.id)
          setBatches((prev) => prev.map((x) => (x.id === updated.id ? updated : x)))
          if (["success", "partial", "failed"].includes(updated.status)) loadOverview()
        } catch { /* transient — next tick retries */ }
      }
    }, 3000)
    return () => clearInterval(t)
  }, [batches, loadOverview])

  const onBatches = (list) => {
    setBatches((prev) => [...list, ...prev.filter((p) => !list.some((l) => l.id === p.id))])
    setTab("validate")
  }

  const updateBatch = (updated) => {
    setBatches((prev) => prev.map((b) => (b.id === updated.id ? updated : b)))
    setDetail((d) => (d && d.id === updated.id ? updated : d))
    if (["success", "partial", "rolled_back"].includes(updated.status)) loadOverview()
  }

  const openDetail = async (id) => {
    try {
      const d = await getImportDetail(id)
      setDetail(d)
    } catch (e) {
      setDetail({ id, error: e.message || "Could not load batch" })
    }
  }

  return (
    <div className="min-h-full px-4 pb-16 pt-6 sm:px-6 lg:px-8">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-2xl font-bold text-[#292326] dark:text-[#f5eff1]">Data Management</h2>
          <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">
            Upload official MPLADS CSV exports. Files are validated, previewed and imported in one transaction;
            risk scores and derived analytics refresh afterwards.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`rounded-lg px-3 py-2 text-sm font-bold transition ${
                tab === t.key
                  ? "bg-[#c8102e] text-white dark:bg-blue-600"
                  : "border border-[#e8dadc] text-[#292326] hover:bg-[#f8f1f1] dark:border-[#351923] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === "upload" && (
        <div className="space-y-4">
          <UploadPanel onBatches={onBatches} />
          {overviewError && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300">{overviewError}</p>}
          {batches.length === 0 && (
            <p className="text-sm text-[#71666a] dark:text-[#b9aeb2]">
              Tip: imports are idempotent — re-uploading a file that is already in the database produces zero new rows.
            </p>
          )}
        </div>
      )}

      {tab === "validate" && (
        <div className="space-y-4">
          {batches.length === 0 ? (
            <Card><p className="py-8 text-center text-sm text-[#71666a] dark:text-[#b9aeb2]">No files staged. Use “Upload Data” to add a CSV.</p></Card>
          ) : (
            batches.map((b) => <BatchCard key={b.id} batch={b} onChange={updateBatch} />)
          )}
        </div>
      )}

      {tab === "history" && (
        <div className="space-y-4">
          <HistoryPanel onOpen={openDetail} />
          {detail && (
            <div className="space-y-2">
              <p className="text-sm font-bold text-[#292326] dark:text-[#f5eff1]">Batch #{detail.id} details</p>
              {detail.error
                ? <Card><p className="text-sm text-red-700 dark:text-red-300">{detail.error}</p></Card>
                : <BatchCard batch={detail} onChange={updateBatch} />}
            </div>
          )}
        </div>
      )}

      {tab === "overview" && <OverviewPanel data={overview} />}
    </div>
  )
}

export default function DataManagement() {
  return (
    <RequireRole minRole="admin">
      <DataManagementInner />
    </RequireRole>
  )
}
