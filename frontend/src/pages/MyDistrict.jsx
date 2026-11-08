import { useCallback, useEffect, useState } from "react"
import { useAuth } from "../context/AuthContext"
import { getDistrictProjects, getDistrictSummary, getMyDistrictInquiries } from "../services/api"
import { formatMoney } from "../utils/format"
import { RequireRole } from "./Workspace"

const RISK_BADGE = {
  High: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  Medium: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  Low: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
}

const INQ_BADGE = {
  open: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  responded: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  closed: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
}

export default function MyDistrict({ onOpenProject }) {
  return (
    <RequireRole minRole="district_authority">
      <MyDistrictInner onOpenProject={onOpenProject} />
    </RequireRole>
  )
}

function MyDistrictInner({ onOpenProject }) {
  const { user } = useAuth()
  const [summary, setSummary] = useState(null)
  const [projects, setProjects] = useState(null)
  const [inquiries, setInquiries] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [showRespond, setShowRespond] = useState(null) // inquiry id
  const [responseText, setResponseText] = useState("")
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState("")

  const load = useCallback(() => {
    setLoading(true); setError("")
    Promise.all([getDistrictSummary(), getDistrictProjects(), getMyDistrictInquiries()])
      .then(([s, p, i]) => { setSummary(s); setProjects(p); setInquiries(i) })
      .catch((e) => setError(e.message || "Could not load your district data"))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  // A district authority responds to an inquiry through the ground-truth
  // pipeline: POST /ai/verify/report with inquiry_id + authoritative note.
  const respondToInquiry = async (inq) => {
    if (!responseText.trim()) return
    setBusy(true)
    try {
      const { submitVerificationReport } = await import("../services/api")
      await submitVerificationReport({
        projectId: inq.project_id,
        status: "Functional",
        note: responseText.trim(),
        inquiryId: inq.inquiry_id,
      })
      setFlash(`Response recorded for inquiry #${inq.inquiry_id} — the auditor will now review it.`)
      setShowRespond(null); setResponseText("")
      getMyDistrictInquiries().then((d) => setInquiries(d)).catch(() => {})
    } catch (e) {
      setError(e.message || "Could not record the response")
    } finally { setBusy(false) }
  }

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 text-[#292326] transition-colors duration-200 sm:p-6 dark:bg-[#000000] dark:text-gray-100">
      <div className="mx-auto max-w-[1440px] space-y-4">
        <div className="mb-1">
          <h2 className="text-2xl font-bold text-[#292326] dark:text-[#f5eff1]">My District</h2>
          <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">
            Projects and auditor inquiries for {summary?.scope || `your assigned area (${user?.assigned_district || user?.assigned_state || "not assigned yet"})`}.
            You can view project data and respond to inquiries — risk scores and audit flags are managed by auditors.
          </p>
        </div>

        {flash && <div className="rounded-xl border border-green-300 bg-green-50 p-3 text-sm font-semibold text-green-700 dark:border-green-900 dark:bg-green-950/40 dark:text-green-300">{flash}</div>}
        {error && (
          <div className="rounded-xl border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
            {error} <button onClick={() => { setError(""); load() }} className="ml-2 font-bold underline">Retry</button>
          </div>
        )}
        {loading && (
          <div className="rounded-xl border border-[#e8dadc] bg-white p-8 text-center text-sm text-[#71666a] dark:border-[#351923] dark:bg-[#000000] dark:text-[#b9aeb2]">Loading your district…</div>
        )}

        {!loading && summary && (
          <>
            {/* Summary cards */}
            <div className="grid grid-cols-1 min-[400px]:grid-cols-2 gap-3 lg:grid-cols-4">
              {[
                { label: "Projects", value: summary.projects, icon: "🏗" },
                { label: "High-risk (≥75)", value: summary.high_risk, icon: "⚠" },
                { label: "Open inquiries", value: summary.open_inquiries, icon: "✉️" },
                { label: "Total sanctioned", value: formatMoney(summary.total_sanctioned), icon: "💰" },
              ].map((c) => (
                <div key={c.label} className="min-w-0 rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
                  <p className="text-xs font-bold uppercase tracking-wide text-[#71666a]/70 dark:text-[#b9aeb2]/70">{c.icon} {c.label}</p>
                  <p className="value-nowrap mt-1 text-xl font-extrabold tabular-nums text-[#292326] dark:text-[#f5eff1]" title={String(c.value)}>{c.value}</p>
                </div>
              ))}
            </div>

            {/* Inquiries */}
            <div className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
              <h3 className="mb-3 text-lg font-bold text-[#292326] dark:text-[#f5eff1]">Auditor inquiries ({inquiries?.total ?? 0})</h3>
              {inquiries && inquiries.items.length === 0 && (
                <p className="text-sm text-[#71666a] dark:text-[#b9aeb2]">No inquiries for your district yet. When an auditor issues one, it appears here for your official response.</p>
              )}
              <div className="space-y-2.5">
                {(inquiries?.items || []).map((i) => (
                  <div key={i.inquiry_id} className="rounded-lg border border-[#e8dadc] p-3 dark:border-[#351923]">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded px-2 py-0.5 text-xs font-bold uppercase ${INQ_BADGE[i.status] || ""}`}>{i.status}</span>
                      <span className="rounded bg-[#f8f1f1] px-2 py-0.5 font-mono text-xs font-bold text-[#292326] dark:bg-[#0d090b] dark:text-[#f5eff1]">#{i.project_id}</span>
                      <span className="min-w-0 flex-1 truncate text-[0.9375rem] font-semibold text-[#292326] dark:text-[#f5eff1]">{i.project_name || "Project"}</span>
                    </div>
                    <p className="mt-2 text-sm text-[#292326] dark:text-gray-200"><span className="font-bold">Auditor asks:</span> {i.question}</p>
                    {i.status !== "open" && i.response_text && (
                      <p className="mt-1 rounded bg-gray-50 p-2 text-sm text-[#71666a] dark:bg-[#0d090b] dark:text-[#b9aeb2]">
                        <span className="font-bold">Your response ({i.responded_by_name}, {i.responded_at?.replace("T", " ").slice(0, 16)}):</span> {i.response_text}
                      </p>
                    )}
                    <div className="mt-2 flex items-center gap-2">
                      <button onClick={() => onOpenProject?.(i.project_id)} className="rounded-lg border border-[#e8dadc] px-3 py-1.5 text-xs font-bold text-[#292326] transition hover:bg-[#f8f1f1] dark:border-[#351923] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]">View project</button>
                      {i.status === "open" && (
                        <button onClick={() => { setShowRespond(i.inquiry_id); setResponseText("") }} className="rounded-lg bg-[#c8102e] px-3 py-1.5 text-xs font-bold text-white transition hover:bg-[#a50f29] dark:bg-blue-600 dark:hover:bg-blue-500">
                          Respond officially
                        </button>
                      )}
                    </div>
                    {showRespond === i.inquiry_id && (
                      <div className="mt-3 rounded-lg border border-[#e8dadc] bg-[#faf7f6] p-3 dark:border-[#351923] dark:bg-[#120b0e]">
                        <label className="text-xs font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">Official response / status update</label>
                        <textarea
                          value={responseText}
                          onChange={(e) => setResponseText(e.target.value)}
                          rows={3}
                          placeholder="Describe the verified ground status, actions taken, or clarifications…"
                          className="mt-1.5 w-full rounded-lg border border-[#e8dadc] bg-white p-2 text-sm dark:border-[#351923] dark:bg-[#000000] dark:text-gray-100"
                        />
                        <div className="mt-2 flex gap-2">
                          <button disabled={busy || !responseText.trim()} onClick={() => respondToInquiry(i)} className="rounded-lg bg-[#c8102e] px-3 py-1.5 text-xs font-bold text-white disabled:opacity-50 dark:bg-blue-600">Submit response</button>
                          <button onClick={() => setShowRespond(null)} className="rounded-lg border border-[#e8dadc] px-3 py-1.5 text-xs font-bold dark:border-[#351923]">Cancel</button>
                        </div>
                        <p className="mt-1.5 text-xs text-[#71666a]/70 dark:text-[#b9aeb2]/70">Your response is recorded with your identity, role, and timestamp, and becomes part of the audit trail.</p>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>

            {/* District projects */}
            <div className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
              <h3 className="mb-3 text-lg font-bold text-[#292326] dark:text-[#f5eff1]">Projects in {summary.scope || "your area"} ({projects?.total ?? 0})</h3>
              {projects && projects.items.length === 0 && (
                <p className="text-sm text-[#71666a] dark:text-[#b9aeb2]">{projects.message || "No projects found in your assigned area."}</p>
              )}
              <div className="space-y-2">
                {(projects?.items || []).map((p) => (
                  <div key={p.project_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[#e8dadc] px-3 py-2.5 dark:border-[#351923]">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[0.875rem] font-semibold text-[#292326] dark:text-[#f5eff1]">#{p.project_id} {p.project_name}</p>
                      <p className="text-xs text-[#71666a] dark:text-[#b9aeb2]">
                        {p.constituency || "N/A"} · sanctioned {formatMoney(p.sanctioned_amount)} · expenditure {formatMoney(p.expenditure)} · completion {p.completion_percentage ?? "—"}%
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      {p.risk_level && <span className={`rounded px-2 py-0.5 text-xs font-bold ${RISK_BADGE[p.risk_level] || "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"}`}>{p.risk_level} {p.risk_score != null ? `· ${p.risk_score}` : ""}</span>}
                      <button onClick={() => onOpenProject?.(p.project_id)} className="rounded-lg border border-[#e8dadc] px-3 py-1.5 text-xs font-bold text-[#292326] transition hover:bg-[#f8f1f1] dark:border-[#351923] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]">Open</button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
