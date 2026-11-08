import { useCallback, useEffect, useState } from "react"
import { getAllInquiries, closeInquiry } from "../services/api"
import { RequireRole } from "./Workspace"

const INQ_BADGE = {
  open: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  responded: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  closed: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
}

export default function Inquiries({ onOpenProject }) {
  return (
    <RequireRole minRole="auditor">
      <InquiriesInner onOpenProject={onOpenProject} />
    </RequireRole>
  )
}

function InquiriesInner({ onOpenProject }) {
  const [filter, setFilter] = useState(null)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [busyId, setBusyId] = useState(null)

  const load = useCallback(() => {
    setLoading(true); setError("")
    getAllInquiries(filter)
      .then((d) => setData(d))
      .catch((e) => setError(e.message || "Could not load inquiries"))
      .finally(() => setLoading(false))
  }, [filter])

  useEffect(() => { load() }, [load])

  const close = async (id) => {
    setBusyId(id)
    try {
      await closeInquiry(id)
      setData((d) => d && ({
        ...d,
        items: d.items.map((i) => (i.inquiry_id === id ? { ...i, status: "closed" } : i)),
        counts: { ...d.counts, closed: (d.counts?.closed || 0) + 1, responded: Math.max(0, (d.counts?.responded || 0) - 1) },
      }))
    } catch (e) { setError(e.message || "Could not close the inquiry") }
    finally { setBusyId(null) }
  }

  const pills = [
    { key: null, label: "All" },
    { key: "open", label: "Open" },
    { key: "responded", label: "Awaiting review" },
    { key: "closed", label: "Closed" },
  ]

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 text-[#292326] transition-colors duration-200 sm:p-6 dark:bg-[#000000] dark:text-gray-100">
      <div className="mx-auto max-w-[1440px] space-y-4">
        <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-2xl font-bold text-[#292326] dark:text-[#f5eff1]">Inquiries</h2>
            <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">
              Inquiries you and other auditors issued to district authorities. Issue new ones from any inspection modal via <span className="font-semibold">Audit Action → Inquiry</span>.
            </p>
          </div>
          {data?.counts && (
            <div className="flex gap-2 text-xs font-bold">
              <span className="rounded-lg bg-red-100 px-3 py-1.5 text-red-700 dark:bg-red-950 dark:text-red-300">{data.counts.open || 0} open</span>
              <span className="rounded-lg bg-amber-100 px-3 py-1.5 text-amber-700 dark:bg-amber-950 dark:text-amber-300">{data.counts.responded || 0} awaiting review</span>
              <span className="rounded-lg bg-green-100 px-3 py-1.5 text-green-700 dark:bg-green-950 dark:text-green-300">{data.counts.closed || 0} closed</span>
            </div>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          {pills.map((p) => (
            <button
              key={p.label}
              onClick={() => setFilter(p.key)}
              className={`rounded-full px-3.5 py-1.5 text-[0.8125rem] font-bold transition ${
                filter === p.key
                  ? "bg-[#c8102e] text-white dark:bg-blue-600"
                  : "border border-[#e8dadc] bg-white text-[#71666a] hover:bg-gray-50 dark:border-[#351923] dark:bg-[#000000] dark:text-[#b9aeb2] dark:hover:bg-[#0d090b]"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>

        {loading && <div className="rounded-xl border border-[#e8dadc] bg-white p-8 text-center text-sm text-[#71666a] dark:border-[#351923] dark:bg-[#000000] dark:text-[#b9aeb2]">Loading inquiries…</div>}
        {error && (
          <div className="rounded-xl border border-red-300 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
            {error} <button onClick={load} className="ml-2 font-bold underline">Retry</button>
          </div>
        )}
        {!loading && !error && data && data.items.length === 0 && (
          <div className="rounded-xl border border-[#e8dadc] bg-white p-10 text-center dark:border-[#351923] dark:bg-[#000000]">
            <div className="text-4xl">✉️</div>
            <p className="mt-3 font-semibold text-[#292326] dark:text-[#f5eff1]">No inquiries {filter ? `with status “${filter}”` : "yet"}</p>
            <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">Open a flagged project's inspection modal and choose <span className="font-semibold">Inquiry</span> to ask the district authority for an official response.</p>
          </div>
        )}
        {!loading && !error && data && data.items.length > 0 && (
          <div className="space-y-2.5">
            {data.items.map((i) => (
              <div key={i.inquiry_id} className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded-sm px-2 py-0.5 text-xs font-bold uppercase ${INQ_BADGE[i.status] || ""}`}>{i.status}</span>
                      <span className="rounded-sm bg-[#f8f1f1] px-2 py-0.5 font-mono text-xs font-bold text-[#292326] dark:bg-[#0d090b] dark:text-[#f5eff1]">#{i.project_id}</span>
                      <span className="rounded-sm bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300">{i.district || "no district"}</span>
                      <span className="text-xs text-[#71666a]/70 dark:text-[#b9aeb2]/70">issued by {i.issued_by} · {i.created_at?.replace("T", " ").slice(0, 16)}</span>
                    </div>
                    <p className="mt-1.5 truncate text-[0.9375rem] font-semibold text-[#292326] dark:text-[#f5eff1]">{i.project_name || "Project"}</p>
                    <p className="mt-1 text-sm text-[#292326] dark:text-gray-200"><span className="font-bold">Question:</span> {i.question}</p>
                    {i.response_text && (
                      <p className="mt-2 rounded-lg bg-gray-50 p-2.5 text-sm dark:bg-[#0d090b]">
                        <span className="font-bold text-[#292326] dark:text-[#f5eff1]">Official response — {i.responded_by} ({i.responded_at?.replace("T", " ").slice(0, 16)}):</span>{" "}
                        <span className="text-[#71666a] dark:text-[#b9aeb2]">{i.response_text}</span>
                      </p>
                    )}
                    {i.status === "open" && (
                      <p className="mt-2 text-xs italic text-[#71666a]/70 dark:text-[#b9aeb2]/70">Waiting for the district authority's response.</p>
                    )}
                  </div>
                  <div className="flex shrink-0 flex-col gap-2">
                    <button onClick={() => onOpenProject?.(i.project_id)} className="rounded-lg border border-[#e8dadc] px-3 py-1.5 text-[0.8125rem] font-bold text-[#292326] transition hover:bg-[#f8f1f1] dark:border-[#351923] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]">
                      Open project
                    </button>
                    {i.status === "responded" && (
                      <button disabled={busyId === i.inquiry_id} onClick={() => close(i.inquiry_id)} className="rounded-lg bg-[#c8102e] px-3 py-1.5 text-[0.8125rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500">
                        Review ✓ Close
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
