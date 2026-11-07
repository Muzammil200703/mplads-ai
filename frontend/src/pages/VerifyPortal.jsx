import { useEffect, useRef, useState } from "react"
import { useAuth } from "../context/AuthContext"
import { submitVerificationReport, verifyProjectLookup } from "../services/api"
import { formatMoney } from "../utils/format"

/* ═══════════════════════════════════════════════════════════════════════
   GROUND TRUTH VERIFICATION PORTAL — SIH 26102 (/verify)
   ─────────────────────────────────────────────────────────────────────
   Mobile-first page for citizens and field inspectors:
     1. Look up a project by ID (or ?project=123 deep link / QR payload).
     2. Capture a live photo (optional) — its EXIF GPS is read server-side.
     3. Capture browser geolocation (with graceful permission handling).
     4. One-click status: Functional / Non-Functional / Work Not Started.
   ═══════════════════════════════════════════════════════════════════ */

const STATUS_OPTIONS = [
  { value: "Functional", icon: "✅", tone: "border-green-500 bg-green-50 text-green-700 dark:bg-green-950/40 dark:text-green-300" },
  { value: "Non-Functional", icon: "⚠️", tone: "border-red-500 bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300" },
  { value: "Work Not Started", icon: "🚧", tone: "border-amber-500 bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300" },
]

/* Recorded Data vs Ground Evidence — pure presentation of the submission
   records already fetched by verifyProjectLookup(). Never rewrites the
   official record; deviations only recommend human review. */
function GroundEvidenceComparison({ progress, recordedStatus, reports }) {
  const reportsList = Array.isArray(reports) ? reports : []
  if (!reportsList.length && progress == null) return null

  const withDistance = reportsList.filter((r) => typeof r.distance_m === "number")
  const maxDistance = withDistance.length ? Math.max(...withDistance.map((r) => r.distance_m)) : null
  const statusCounts = reportsList.reduce((acc, r) => {
    if (r?.status) acc[r.status] = (acc[r.status] || 0) + 1
    return acc
  }, {})
  const disagree =
    recordedStatus &&
    Object.keys(statusCounts).length > 0 &&
    statusCounts[recordedStatus] !== reportsList.length
  const farCapture = maxDistance != null && maxDistance > 500

  return (
    <div className="mt-3 rounded-lg border border-gray-200 bg-gray-50/60 p-3 dark:border-gray-700 dark:bg-gray-900/40">
      <p className="text-[0.6875rem] font-bold uppercase tracking-wide text-gray-500 dark:text-gray-400">Recorded Data vs Ground Evidence</p>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <div className="rounded-lg bg-white p-2.5 dark:bg-gray-800">
          <p className="text-[0.6875rem] uppercase text-gray-400">Recorded progress</p>
          <p className="text-sm font-bold text-gray-800 dark:text-gray-200">{progress != null ? `${progress}%` : "Data unavailable in source dataset"}</p>
        </div>
        <div className="rounded-lg bg-white p-2.5 dark:bg-gray-800">
          <p className="text-[0.6875rem] uppercase text-gray-400">Ground evidence</p>
          {reportsList.length ? (
            <p className="text-sm font-bold text-gray-800 dark:text-gray-200">
              {reportsList.length} submission{reportsList.length > 1 ? "s" : ""}
              {Object.entries(statusCounts).map(([s, n]) => ` · ${s}: ${n}`).join("")}
            </p>
          ) : (
            <p className="text-sm font-semibold text-gray-500 dark:text-gray-400">No evidence submitted yet</p>
          )}
        </div>
      </div>
      {withDistance.length > 0 && (
        <p className="mt-2 text-xs text-gray-600 dark:text-gray-300">
          Field photo GPS captured up to <strong>{Math.round(maxDistance)}m</strong> from the recorded site position.
          {farCapture && " This is above the 500m reference threshold."}
        </p>
      )}
      {(disagree || farCapture) && (
        <p className="mt-1.5 rounded-md bg-amber-50 px-2.5 py-1.5 text-xs font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
          Significant deviation detected — human verification recommended. An AI/field flag is never treated as confirmed fraud on its own.
        </p>
      )}
    </div>
  )
}

function useGeolocation() {
  const [coords, setCoords] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const capture = () => {
    if (!("geolocation" in navigator)) {
      setError("Geolocation is not supported by this browser.")
      return
    }
    setBusy(true); setError(null)
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setCoords({ lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: pos.coords.accuracy })
        setBusy(false)
      },
      (err) => {
        setError(
          err.code === err.PERMISSION_DENIED
            ? "Location permission denied — you can still submit without GPS."
            : "Could not read your location — you can still submit without GPS."
        )
        setBusy(false)
      },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 }
    )
  }

  return { coords, error, busy, capture, reset: () => { setCoords(null); setError(null) } }
}

export default function VerifyPortal() {
  const { user, can } = useAuth()
  // Citizen mode: signed-in citizens (and guests) submit CITIZEN EVIDENCE —
  // kept pending until a field verifier dispositions it. Field verifiers,
  // auditors and admins submit trusted field reports as before.
  const isCitizenMode = !user || user.role === "citizen"
  const [query, setQuery] = useState(() => {
    const m = window.location.hash.match(/project=(\d+)/)
    return m ? m[1] : ""
  })
  const [project, setProject] = useState(null)
  const [recent, setRecent] = useState([])
  const [loadErr, setLoadErr] = useState(null)
  const [loading, setLoading] = useState(false)

  const [status, setStatus] = useState(null)
  const [note, setNote] = useState("")
  const [reporterName, setReporterName] = useState("")
  const [file, setFile] = useState(null)
  const [previewUrl, setPreviewUrl] = useState(null)
  const [gps, setGps] = useState({ coords: null, error: null, busy: false, capture: () => {} })
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState(null)
  const [submitErr, setSubmitErr] = useState(null)
  const fileRef = useRef(null)

  const lookup = (idRaw) => {
    const id = parseInt(String(idRaw || query).replace(/\D/g, ""), 10)
    if (!id) { setLoadErr("Enter a numeric project ID (e.g. 80649)."); return }
    setLoading(true); setLoadErr(null); setProject(null); setResult(null)
    verifyProjectLookup(id)
      .then((d) => { setProject(d.project); setRecent(d.recent_reports || []) })
      .catch((e) => setLoadErr(e.message))
      .finally(() => setLoading(false))
  }

  // auto-lookup on deep link
  useEffect(() => {
    const m = window.location.hash.match(/project=(\d+)/)
    if (m) lookup(m[1])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onPickFile = (f) => {
    if (!f) return
    if (f.size > 12 * 1024 * 1024) { setSubmitErr("Photo too large (max 12MB)."); return }
    setFile(f)
    setPreviewUrl(URL.createObjectURL(f))
  }

  const submit = async () => {
    if (!project || !status) return
    setSubmitting(true); setSubmitErr(null); setResult(null)
    try {
      const res = await submitVerificationReport({
        projectId: project.id,
        status,
        lat: gps.coords?.lat,
        lon: gps.coords?.lon,
        reporterName: reporterName.trim() || undefined,
        note: note.trim() || undefined,
        file: file || undefined,
      })
      // Citizen mode hint: where to track the submission afterwards
      if (isCitizenMode && user) {
        setResult({ _mineHint: true, ...res })
      } else {
        setResult(res)
      }
      // refresh recent list
      verifyProjectLookup(project.id).then((d) => setRecent(d.recent_reports || [])).catch(() => {})
      setStatus(null); setNote(""); setFile(null)
      if (previewUrl) URL.revokeObjectURL(previewUrl); setPreviewUrl(null)
    } catch (e) {
      setSubmitErr(e.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="min-h-full p-4 sm:p-6">
    <div className="mx-auto max-w-[1440px] space-y-4 sm:space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex flex-wrap items-center gap-2 text-2xl font-bold text-gray-900 dark:text-white">
            Ground Truth Verification
            {isCitizenMode && (
              <span className="rounded-full bg-emerald-100 px-2.5 py-0.5 text-[0.6875rem] font-bold uppercase tracking-wide text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                Submit Evidence as Citizen
              </span>
            )}
          </h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {isCitizenMode
              ? "Report what a project actually looks like on the ground. Your photo, GPS and status go in as Citizen Evidence — a field verifier reviews it before it counts as verified. It never changes the official record."
              : "Authorized field verification: your live photo and GPS report are recorded as trusted field evidence auditors can compare against recorded data."}
          </p>
        </div>
        {user && can("verification:read_own") && (
          <span className="rounded-lg bg-white px-3 py-1.5 text-xs font-bold text-[#292326] shadow-sm dark:bg-[#000000] dark:text-[#f5eff1]">
            Track yours under {user.role === "citizen" ? "My Evidence" : "My Verifications"}
          </span>
        )}
      </header>

      {/* Step 1 — find the project */}
      <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
        <h2 className="flex items-center gap-2 text-sm font-bold text-gray-900 dark:text-white"><span className="flex h-6 w-6 items-center justify-center rounded-full bg-blue-600 text-xs font-bold text-white">1</span> Find the project</h2>
        <div className="mt-3 flex gap-2">
          <input
            inputMode="numeric"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && lookup()}
            placeholder="Project ID — also works by scanning its QR code"
            className="min-w-0 flex-1 rounded-lg border border-gray-300 bg-white px-3 py-2.5 text-sm outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200"
          />
          <button onClick={() => lookup()} disabled={loading} className="shrink-0 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-bold text-white transition hover:bg-blue-700 disabled:opacity-50">
            {loading ? "…" : "Look up"}
          </button>
        </div>
        {loadErr && <p className="mt-2 text-sm text-red-600 dark:text-red-400">{loadErr}</p>}
      </section>

      {/* Project snapshot */}
      {project && (
        <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
          <p className="text-base font-bold leading-snug text-gray-900 dark:text-white">{project.project_name}</p>
          <p className="mt-1 text-xs text-gray-500">
            #{project.id} · {project.district || "—"}, {project.state} · {project.project_type || "Works"}
          </p>
          <div className="mt-3 grid grid-cols-3 gap-2 text-center">
            <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/60">
              <p className="text-[0.6875rem] uppercase text-gray-400">Sanctioned</p>
              <p className="text-sm font-bold text-gray-800 dark:text-gray-200">{formatMoney(project.sanctioned_amount)}</p>
            </div>
            <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/60">
              <p className="text-[0.6875rem] uppercase text-gray-400">Recorded progress</p>
              <p className="text-sm font-bold text-gray-800 dark:text-gray-200">{project.completion_percentage}%</p>
            </div>
            <div className="rounded-lg bg-gray-50 p-2 dark:bg-gray-900/60">
              <p className="text-[0.6875rem] uppercase text-gray-400">Recorded status</p>
              <p className="text-sm font-bold text-gray-800 dark:text-gray-200">{project.status || "—"}</p>
            </div>
          </div>
          <p className="mt-2 text-[0.6875rem] text-gray-400">These are the officially recorded values — your report is how the ground truth gets checked against them.</p>
          <GroundEvidenceComparison progress={project.completion_percentage} recordedStatus={project.status} reports={recent} />
        </section>
      )}

      {/* Step 2 — evidence */}
      {project && (
        <section className="rise-in rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
          <h2 className="flex items-center gap-2 text-sm font-bold text-gray-900 dark:text-white"><span className="flex h-6 w-6 items-center justify-center rounded-full bg-blue-600 text-xs font-bold text-white">2</span> Add live evidence (optional but powerful)</h2>

          <div className="mt-3 space-y-3">
            <div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => fileRef.current?.click()}
                  className="rounded-lg border border-gray-300 px-3.5 py-2 text-sm font-semibold text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
                >
                  📷 Take / choose photo
                </button>
                {file && <span className="truncate text-xs text-gray-500">{file.name}</span>}
                <input ref={fileRef} type="file" accept="image/*" capture="environment" className="hidden"
                  onChange={(e) => onPickFile(e.target.files?.[0])} />
              </div>
              {previewUrl && (
                <img src={previewUrl} alt="Preview of the photo you are submitting" className="mt-2 max-h-44 rounded-lg border border-gray-200 object-cover dark:border-gray-700" />
              )}
              <p className="mt-1 text-[0.6875rem] text-gray-400">
                The photo's EXIF GPS (if present) is extracted server-side and perceptually hashed — duplicate submissions across projects are detectable.
              </p>
            </div>

            <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-sm font-semibold text-gray-800 dark:text-gray-200">📍 Location capture</p>
                <button onClick={gps.capture} disabled={gps.busy} className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-bold text-white dark:bg-gray-100 dark:text-gray-900 disabled:opacity-50">
                  {gps.busy ? "Locating…" : gps.coords ? "Re-capture GPS" : "Capture my GPS"}
                </button>
              </div>
              {gps.coords && (
                <p className="rise-in mt-1.5 text-xs text-green-700 dark:text-green-400">
                  Captured: {gps.coords.lat.toFixed(5)}, {gps.coords.lon.toFixed(5)} (±{Math.round(gps.coords.accuracy)}m)
                </p>
              )}
              {gps.error && <p className="rise-in mt-1.5 text-xs text-amber-600 dark:text-amber-400">{gps.error}</p>}
            </div>
          </div>
        </section>
      )}

      {/* Step 3 — status */}
      {project && (
        <section className="rise-in rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
          <h2 className="flex items-center gap-2 text-sm font-bold text-gray-900 dark:text-white"><span className="flex h-6 w-6 items-center justify-center rounded-full bg-blue-600 text-xs font-bold text-white">3</span> Report the status</h2>
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            {STATUS_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => setStatus(opt.value)}
                aria-pressed={status === opt.value}
                className={`rounded-lg border-2 p-3 text-sm font-bold transition ${status === opt.value ? opt.tone : "border-gray-200 text-gray-600 hover:border-gray-300 dark:border-gray-700 dark:text-gray-300"}`}
              >
                <span className="mr-1" aria-hidden>{opt.icon}</span>{opt.value}
              </button>
            ))}
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <input value={reporterName} onChange={(e) => setReporterName(e.target.value)} placeholder="Your name (optional)"
              className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200" />
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Short note (optional)"
              className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200" />
          </div>
          {submitErr && <p className="rise-in mt-2 text-sm text-red-600 dark:text-red-400">{submitErr}</p>}
          <button
            onClick={submit}
            disabled={!status || submitting}
            className="mt-3 w-full rounded-lg bg-blue-600 py-3 text-sm font-bold text-white transition hover:bg-blue-700 disabled:opacity-50"
          >
            {submitting ? "Submitting…" : (isCitizenMode ? "Submit Citizen Evidence" : "Submit verification report")}
          </button>
          {result && (
            <div className="rise-in mt-3 rounded-lg bg-green-50 p-3 text-sm text-green-800 dark:bg-green-950/40 dark:text-green-300">
              <p className="font-bold">✓ {result.message}</p>
              <p className="mt-0.5 text-xs">
                {result.submission_kind === "citizen" && <>Review status: {result.review_status} · </>}
                GPS captured: {result.gps_captured ? `yes (${result.gps_source})` : "no"} · photo stored: {result.photo_stored ? "yes" : "no"}
                {result.distance_from_previous_field_fix_m != null && <> · {result.distance_from_previous_field_fix_m}m from the previous field position</>}
              </p>
              {result._mineHint && (
                <p className="mt-1 text-xs font-semibold">Track this submission under “My Evidence” in the sidebar.</p>
              )}
            </div>
          )}
        </section>
      )}

      {/* Recent community reports */}
      {project && recent.length > 0 && (
        <section className="rise-in rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800">
          <h3 className="text-sm font-bold text-gray-900 dark:text-white">Recent reports for this project</h3>
          <ul className="mt-2 divide-y divide-gray-100 dark:divide-gray-700">
            {recent.map((r, i) => (
              <li key={i} className="flex items-center justify-between gap-3 py-2 text-sm">
                <span className={`font-semibold ${r.status === "Functional" ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400"}`}>{r.status}</span>
                <span className="min-w-0 flex-1 truncate text-xs text-gray-500">{r.reporter}{r.distance_m != null && ` · ${r.distance_m}m`}</span>
                <span className="text-[0.6875rem] text-gray-400">{r.created_at?.slice(0, 10)}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[0.6875rem] text-gray-400">Reports shown here are individual submissions — verification status is managed by field verifiers and auditors.</p>
        </section>
      )}
    </div>
    </div>
  )
}
