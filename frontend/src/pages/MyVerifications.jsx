import { useCallback, useEffect, useState } from "react"
import { useAuth } from "../context/AuthContext"
import { getMyVerifications } from "../services/api"
import { RequireRole } from "./Workspace"

const STATUS_BADGE = {
  Functional: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  "Non-Functional": "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  "Work Not Started": "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
}

const REVIEW_BADGE = {
  pending: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  verified: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  rejected: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  escalated: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300",
}

export default function MyVerifications({ onOpenProject }) {
  return (
    <RequireRole minRole="citizen">
      <MyVerificationsInner onOpenProject={onOpenProject} />
    </RequireRole>
  )
}

function MyVerificationsInner({ onOpenProject }) {
  const { user } = useAuth()
  const [items, setItems] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  const load = useCallback(() => {
    setLoading(true); setError("")
    getMyVerifications()
      .then((d) => setItems(d.items || []))
      .catch((e) => setError(e.message || "Could not load your verification reports"))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 text-[#292326] transition-colors duration-200 sm:p-6 dark:bg-[#000000] dark:text-gray-100">
      <div className="mx-auto max-w-[1440px] space-y-4">          <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-2xl font-bold text-[#292326] dark:text-[#f5eff1]">My Evidence Submissions</h2>
            <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">
              Citizen evidence you submitted, attributed to {user?.name || "your account"}. A field verifier reviews each one —
              your submission never changes the official project record.
            </p>
          </div>
          <span className="rounded-lg bg-white px-3 py-1.5 text-sm font-bold text-[#292326] shadow-sm dark:bg-[#000000] dark:text-[#f5eff1]">
            {items ? `${items.length} reports` : "…"}
          </span>
        </div>

        {loading && (
          <div className="rounded-xl border border-[#e8dadc] bg-white p-8 text-center text-sm text-[#71666a] dark:border-[#351923] dark:bg-[#000000] dark:text-[#b9aeb2]">
            Loading your submissions…
          </div>
        )}
        {error && (
          <div className="rounded-xl border border-red-300 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
            {error}{" "}
            <button onClick={load} className="ml-2 font-bold underline">Retry</button>
          </div>
        )}
        {!loading && !error && items && items.length === 0 && (
          <div className="rounded-xl border border-[#e8dadc] bg-white p-10 text-center dark:border-[#351923] dark:bg-[#000000]">
            <div className="text-4xl">✅</div>
            <p className="mt-3 font-semibold text-[#292326] dark:text-[#f5eff1]">No verifications submitted yet</p>
            <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">
              Open <span className="font-semibold">Ground Verification</span>, look up your assigned project, capture a photo + GPS, and submit a status.
            </p>
          </div>
        )}
        {!loading && !error && items && items.length > 0 && (
          <div className="space-y-2.5">
            {items.map((r) => (
              <div key={r.report_id} className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[0.6875rem] font-bold uppercase tracking-wide text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">Citizen Evidence</span>
                      <span className={`rounded-sm px-2 py-0.5 text-xs font-bold ${STATUS_BADGE[r.status] || "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"}`}>{r.status}</span>
                      <span className={`rounded-sm px-2 py-0.5 text-xs font-bold uppercase ${REVIEW_BADGE[r.review_status] || ""}`}>
                        {r.review_status === "pending" ? "awaiting review" : r.review_status}
                      </span>
                      <span className="rounded-sm bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                        GPS: {r.lat != null ? `${r.lat.toFixed(4)}, ${r.lon?.toFixed(4)} (${r.gps_source})` : "not captured"}
                      </span>
                      {r.photo_hash_id && <span className="rounded-sm bg-[#f8f1f1] px-2 py-0.5 text-xs font-semibold text-[#292326] dark:bg-[#0d090b] dark:text-[#f5eff1]">📷 photo stored</span>}
                      {r.inquiry_id && <span className="rounded-sm bg-violet-100 px-2 py-0.5 text-xs font-semibold text-violet-700 dark:bg-violet-950 dark:text-violet-300">responds to inquiry #{r.inquiry_id}</span>}
                    </div>
                    <p className="mt-1.5 truncate text-[0.9375rem] font-semibold text-[#292326] dark:text-[#f5eff1]">
                      #{r.project_id} {r.project_name || "Project"}
                    </p>
                    {r.note && <p className="mt-0.5 line-clamp-2 text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">“{r.note}”</p>}
                    {r.review_status !== "pending" && r.reviewed_by_name && (
                      <p className="mt-1 text-xs font-semibold text-[#292326] dark:text-[#f5eff1]">
                        {r.review_status} by {r.reviewed_by_name}{r.review_note ? ` — “${r.review_note}”` : ""}
                      </p>
                    )}
                    <p className="mt-1 text-xs text-[#71666a]/70 dark:text-[#b9aeb2]/70">Submitted {r.created_at?.replace("T", " ").slice(0, 16)}</p>
                  </div>
                  {onOpenProject && (
                    <button
                      onClick={() => onOpenProject(r.project_id)}
                      className="shrink-0 rounded-lg border border-[#e8dadc] px-3 py-1.5 text-[0.8125rem] font-bold text-[#292326] transition hover:bg-[#f8f1f1] dark:border-[#351923] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]"
                    >
                      Open project
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
