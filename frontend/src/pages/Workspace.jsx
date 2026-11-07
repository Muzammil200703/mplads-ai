import { useCallback, useEffect, useState } from "react"
import { useAuth } from "../context/AuthContext"
import {
  getSavedProjects, unsaveProject,
  getMyInvestigations, updateMyInvestigation,
  getMyAuditCases, updateCaseStatus,
  listUsers, updateUser,
  getAllInvestigations,
} from "../services/api"
import { formatMoney } from "../utils/format"

/* ═══════════════════════════════════════════════════════════
   Investigator workspace pages. Every row is real user data or
   real project data from the backend — nothing is mocked.
   ═══════════════════════════════════════════════════════════ */

const RISK_BADGE = {
  High: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  Medium: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  Low: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
}

const TIER_BADGE = {
  P1: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  P2: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300",
  P3: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  P4: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
}

const INV_STATUSES = ["Open", "Under Review", "Resolved"]

function PageHeader({ title, subtitle, right }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 className="text-2xl font-bold text-[#292326] dark:text-[#f5eff1]">{title}</h2>
        <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">{subtitle}</p>
      </div>
      {right}
    </div>
  )
}

function StatusSelect({ value, options, onChange, busy }) {
  return (
    <select
      value={value}
      disabled={busy}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-lg border border-[#e8dadc] bg-white px-2 py-1.5 text-[0.8125rem] font-semibold text-[#292326] outline-none focus:border-[#c8102e] disabled:opacity-50 dark:border-[#351923] dark:bg-[#0d090b] dark:text-[#f5eff1]"
    >
      {options.map((s) => <option key={s} value={s}>{s}</option>)}
    </select>
  )
}

function OpenProjectButton({ projectId, onOpenProject }) {
  return (
    <button
      onClick={() => onOpenProject && onOpenProject(projectId)}
      className="rounded-lg bg-[#c8102e] px-3 py-1.5 text-[0.8125rem] font-bold text-white transition hover:bg-[#a50f29] dark:bg-blue-600 dark:hover:bg-blue-500"
    >
      Open project
    </button>
  )
}

function EmptyState({ icon, title, hint }) {
  return (
    <div className="rounded-xl border border-dashed border-[#e8dadc] bg-white/50 px-4 py-12 text-center sm:px-6 dark:border-[#351923] dark:bg-[#000000]/50">
      <p className="text-3xl">{icon}</p>
      <p className="mt-2 font-semibold text-[#292326] dark:text-[#f5eff1]">{title}</p>
      <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">{hint}</p>
    </div>
  )
}

function LoadingState({ label }) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-[#e8dadc] bg-white px-4 py-6 dark:border-[#351923] dark:bg-[#000000]">
      <div className="h-5 w-5 animate-spin rounded-full border-2 border-blue-600 border-r-transparent" />
      <span className="text-sm text-[#71666a] dark:text-[#b9aeb2]">{label}</span>
    </div>
  )
}

function ErrorState({ message, onRetry }) {
  return (
    <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-4 dark:border-red-900/40 dark:bg-red-950/20">
      <p className="text-sm font-semibold text-red-700 dark:text-red-300">{message}</p>
      {onRetry && (
        <button onClick={onRetry} className="mt-2 rounded-lg border border-red-300 px-3 py-1.5 text-[0.8125rem] font-bold text-red-700 hover:bg-red-100 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950/40">
          Retry
        </button>
      )}
    </div>
  )
}

export function RequireRole({ minRole, children }) {
  const { user, loading, hasRole } = useAuth()
  if (loading) return <LoadingState label="Checking your session…" />
  if (!user) {
    return (
      <EmptyState icon="🔒" title="Sign in required" hint="Use the Sign in button in the header to access your workspace." />
    )
  }
  if (!hasRole(minRole)) {
    return (
      <EmptyState icon="⛔" title={`This page requires the ${minRole} role`} hint={`You are signed in as ${user.name} (${user.role}). Ask an administrator if you need broader access.`} />
    )
  }
  return children
}

/* ═══════════════ Saved Projects ═══════════════ */

export function SavedProjectsPage({ onOpenProject }) {
  return (
    <RequireRole minRole="analyst">
      <SavedProjectsInner onOpenProject={onOpenProject} />
    </RequireRole>
  )
}

function SavedProjectsInner({ onOpenProject }) {
  const [items, setItems] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [busyId, setBusyId] = useState(null)

  const load = useCallback(() => {
    setLoading(true); setError("")
    getSavedProjects()
      .then((d) => setItems(d.items || []))
      .catch((e) => setError(e.message || "Could not load saved projects"))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const remove = async (projectId) => {
    setBusyId(projectId)
    try {
      await unsaveProject(projectId)
      setItems((list) => (list || []).filter((i) => i.project_id !== projectId))
    } catch (e) { setError(e.message || "Could not remove the project") }
    finally { setBusyId(null) }
  }

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 text-[#292326] transition-colors duration-200 sm:p-6 dark:bg-[#000000] dark:text-gray-100">
      <div className="mx-auto max-w-[1440px] space-y-4">
      <PageHeader
        title="Saved Projects"
        subtitle="Projects you bookmarked for your investigation workflow."
        right={<span className="rounded-lg bg-white px-3 py-1.5 text-sm font-bold text-[#292326] shadow-sm dark:bg-[#000000] dark:text-[#f5eff1]">{items ? `${items.length} saved` : "…"}</span>}
      />
      {loading && <LoadingState label="Loading saved projects…" />}
      {error && <ErrorState message={error} onRetry={load} />}
      {!loading && !error && items && items.length === 0 && (
        <EmptyState icon="☆" title="No saved projects yet" hint="Open any project and use “Save project” to bookmark it here." />
      )}
      {!loading && !error && items && items.length > 0 && (
        <div className="space-y-2.5">
          {items.map((p) => (
            <div key={p.project_id} className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs font-bold text-[#71666a] dark:text-[#b9aeb2]">#{p.project_id}</span>
                    {p.risk_level && <span className={`rounded px-2 py-0.5 text-xs font-bold ${RISK_BADGE[p.risk_level] || "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"}`}>{p.risk_level} risk</span>}
                    {p.ml_anomaly && <span className="rounded bg-purple-100 px-2 py-0.5 text-xs font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">ML outlier</span>}
                    <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300">{p.status || "Unknown status"}</span>
                  </div>
                  <p className="mt-1.5 truncate text-[0.9375rem] font-semibold text-[#292326] dark:text-[#f5eff1]">{p.project_name || "Unnamed project"}</p>
                  <p className="mt-0.5 text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">
                    {p.state} — {p.constituency || "N/A"} · Sanctioned {formatMoney(p.sanctioned_amount ?? 0)} · Spent {formatMoney(p.expenditure ?? 0)}
                  </p>
                  <p className="mt-1 text-xs text-[#71666a]/70 dark:text-[#b9aeb2]/70">
                    Risk score {p.risk_score ?? "N/A"}/100 · Saved {p.saved_at?.replace("T", " ").slice(0, 16)}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <OpenProjectButton projectId={p.project_id} onOpenProject={onOpenProject} />
                  <button
                    onClick={() => remove(p.project_id)}
                    disabled={busyId === p.project_id}
                    className="rounded-lg border border-[#e8dadc] px-3 py-1.5 text-[0.8125rem] font-bold text-red-600 transition hover:bg-red-50 disabled:opacity-50 dark:border-[#351923] dark:text-red-400 dark:hover:bg-red-950/30"
                  >
                    {busyId === p.project_id ? "Removing…" : "Remove"}
                  </button>
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

/* ═══════════════ My Investigations ═══════════════ */

export function MyInvestigationsPage({ onOpenProject }) {
  return (
    <RequireRole minRole="analyst">
      <MyInvestigationsInner onOpenProject={onOpenProject} />
    </RequireRole>
  )
}

function MyInvestigationsInner({ onOpenProject }) {
  const [items, setItems] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [busyId, setBusyId] = useState(null)

  const load = useCallback(() => {
    setLoading(true); setError("")
    getMyInvestigations()
      .then((d) => setItems(d.items || []))
      .catch((e) => setError(e.message || "Could not load investigations"))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const setStatus = async (projectId, status) => {
    setBusyId(projectId)
    try {
      await updateMyInvestigation(projectId, status)
      setItems((list) => (list || []).map((i) => (i.project_id === projectId ? { ...i, investigation_status: status } : i)))
    } catch (e) { setError(e.message || "Could not update the investigation") }
    finally { setBusyId(null) }
  }

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 text-[#292326] transition-colors duration-200 sm:p-6 dark:bg-[#000000] dark:text-gray-100">
      <div className="mx-auto max-w-[1440px] space-y-4">
      <PageHeader
        title="My Investigations"
        subtitle="Your investigation history. Evidence checklists live inside each project's drawer."
        right={<span className="rounded-lg bg-white px-3 py-1.5 text-sm font-bold text-[#292326] shadow-sm dark:bg-[#000000] dark:text-[#f5eff1]">{items ? `${items.length} total` : "…"}</span>}
      />
      {loading && <LoadingState label="Loading investigations…" />}
      {error && <ErrorState message={error} onRetry={load} />}
      {!loading && !error && items && items.length === 0 && (
        <EmptyState icon="🔍" title="No investigations yet" hint="Open a flagged project, start an investigation and it will appear here." />
      )}
      {!loading && !error && items && items.length > 0 && (
        <div className="space-y-2.5">
          {items.map((inv) => (
            <div key={inv.investigation_id} className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs font-bold text-[#71666a] dark:text-[#b9aeb2]">#{inv.project_id}</span>
                    <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300">{inv.investigation_status}</span>
                    {inv.priority_tier && (
                      <span className={`rounded px-2 py-0.5 text-xs font-bold ${TIER_BADGE[inv.priority_tier] || TIER_BADGE.P4}`}>
                        {inv.priority_tier} — {inv.priority_label}
                      </span>
                    )}
                    <span className={`rounded px-2 py-0.5 text-xs font-bold ${RISK_BADGE[inv.risk_score_at_start >= 67 ? "High" : inv.risk_score_at_start >= 34 ? "Medium" : "Low"] || ""}`}>
                      Risk {inv.risk_score_at_start ?? "—"}/100
                    </span>
                  </div>
                  <p className="mt-1.5 truncate text-[0.9375rem] font-semibold text-[#292326] dark:text-[#f5eff1]">{inv.project_name || "Unnamed project"}</p>
                  <p className="mt-0.5 text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">{inv.state} · {inv.status || "Unknown status"}</p>
                  {inv.recommendations?.length > 0 && (
                    <ul className="mt-2 space-y-0.5">
                      {inv.recommendations.slice(0, 3).map((r, i) => (
                        <li key={i} className="flex items-start gap-1.5 text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">
                          <span className="mt-0.5 text-blue-600 dark:text-blue-400">→</span><span>{r}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <p className="mt-1.5 text-xs text-[#71666a]/70 dark:text-[#b9aeb2]/70">
                    Started {inv.created_at?.replace("T", " ").slice(0, 16)} · Updated {inv.updated_at?.replace("T", " ").slice(0, 16)}
                  </p>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-2">
                  <OpenProjectButton projectId={inv.project_id} onOpenProject={onOpenProject} />
                  <StatusSelect value={inv.investigation_status} options={INV_STATUSES} busy={busyId === inv.project_id} onChange={(s) => setStatus(inv.project_id, s)} />
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

/* ═══════════════ My Audit Cases ═══════════════ */

export function MyAuditCasesPage({ onOpenProject }) {
  return (
    <RequireRole minRole="auditor">
      <MyAuditCasesInner onOpenProject={onOpenProject} />
    </RequireRole>
  )
}

function MyAuditCasesInner({ onOpenProject }) {
  const [items, setItems] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [busyId, setBusyId] = useState(null)

  const load = useCallback(() => {
    setLoading(true); setError("")
    getMyAuditCases()
      .then((d) => setItems(d.items || []))
      .catch((e) => setError(e.message || "Could not load audit cases"))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const setStatus = async (projectId, status) => {
    setBusyId(projectId)
    try {
      await updateCaseStatus(projectId, status)
      setItems((list) => (list || []).map((c) => (c.project_id === projectId ? { ...c, case_status: status, investigation_status: status } : c)))
    } catch (e) { setError(e.message || "Could not update the case") }
    finally { setBusyId(null) }
  }

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 text-[#292326] transition-colors duration-200 sm:p-6 dark:bg-[#000000] dark:text-gray-100">
      <div className="mx-auto max-w-[1440px] space-y-4">
      <PageHeader
        title="My Audit Cases"
        subtitle="Cases you saved from the case generator. Case content is always regenerated from live project data."
        right={<span className="rounded-lg bg-white px-3 py-1.5 text-sm font-bold text-[#292326] shadow-sm dark:bg-[#000000] dark:text-[#f5eff1]">{items ? `${items.length} cases` : "…"}</span>}
      />
      {loading && <LoadingState label="Loading audit cases…" />}
      {error && <ErrorState message={error} onRetry={load} />}
      {!loading && !error && items && items.length === 0 && (
        <EmptyState icon="📁" title="No audit cases yet" hint="Open a project → Investigate → Audit Case → “Save case” to add one." />
      )}
      {!loading && !error && items && items.length > 0 && (
        <div className="space-y-2.5">
          {items.map((c) => (
            <div key={c.case_row_id} className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded bg-[#f8f1f1] px-2 py-0.5 font-mono text-xs font-bold text-[#292326] dark:bg-[#0d090b] dark:text-[#f5eff1]">{c.case_id}</span>
                    {c.priority_tier && (
                      <span className={`rounded px-2 py-0.5 text-xs font-bold ${TIER_BADGE[c.priority_tier] || TIER_BADGE.P4}`}>
                        {c.priority_tier} — {c.priority_label}
                      </span>
                    )}
                    {c.risk_level && <span className={`rounded px-2 py-0.5 text-xs font-bold ${RISK_BADGE[c.risk_level] || "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"}`}>{c.risk_level} risk · {c.risk_score}/100</span>}
                    <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300">{c.case_status}</span>
                    {c.investigation_status && <span className="text-xs text-[#71666a]/70 dark:text-[#b9aeb2]/70">investigation: {c.investigation_status}</span>}
                  </div>
                  <p className="mt-1.5 truncate text-[0.9375rem] font-semibold text-[#292326] dark:text-[#f5eff1]">{c.project_name || "Unnamed project"}</p>
                  <p className="mt-0.5 text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">
                    #{c.project_id} · {c.state} — {c.constituency || "N/A"} · Created {c.created_at?.replace("T", " ").slice(0, 16)}
                  </p>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-2">
                  <OpenProjectButton projectId={c.project_id} onOpenProject={onOpenProject} />
                  <StatusSelect value={c.case_status} options={INV_STATUSES} busy={busyId === c.project_id} onChange={(s) => setStatus(c.project_id, s)} />
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

/* ═══════════════ Admin: users + system-wide investigations ═══════════════ */

export function AdminPage() {
  return (
    <RequireRole minRole="admin">
      <AdminInner />
    </RequireRole>
  )
}

const ALL_ROLES = ["public", "citizen", "field_verifier", "district_authority", "analyst", "auditor", "admin"]
const ROLE_LABEL = {
  public: "Guest",
  citizen: "Citizen",
  field_verifier: "Field Verifier",
  district_authority: "District Authority",
  analyst: "Analyst",
  auditor: "Auditor",
  admin: "Administrator",
}

function AdminInner() {
  const [users, setUsers] = useState(null)
  const [invs, setInvs] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [busyId, setBusyId] = useState(null)
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ name: "", email: "", password: "", role: "field_verifier", assigned_district: "", assigned_state: "" })
  const [createMsg, setCreateMsg] = useState(null)

  const load = useCallback(() => {
    setLoading(true); setError("")
    Promise.all([listUsers(), getAllInvestigations()])
      .then(([u, i]) => { setUsers(u.users || []); setInvs(i.items || []) })
      .catch((e) => setError(e.message || "Could not load admin data"))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const changeRole = async (userId, role) => {
    setBusyId(userId)
    try { await updateUser(userId, { role }); setUsers((list) => (list || []).map((u) => (u.id === userId ? { ...u, role } : u))) }
    catch (e) { setError(e.message || "Could not update the user") }
    finally { setBusyId(null) }
  }

  const saveAssignment = async (userId, payload) => {
    setBusyId(userId)
    try { await updateUser(userId, payload); setUsers((list) => (list || []).map((u) => (u.id === userId ? { ...u, ...payload } : u))) }
    catch (e) { setError(e.message || "Could not update the assignment") }
    finally { setBusyId(null) }
  }

  const toggleActive = async (u) => {
    setBusyId(u.id)
    try { await updateUser(u.id, { is_active: !u.is_active }); setUsers((list) => (list || []).map((x) => (x.id === u.id ? { ...x, is_active: !u.is_active } : x))) }
    catch (e) { setError(e.message || "Could not update the user") }
    finally { setBusyId(null) }
  }

  const createUser = async (e) => {
    e.preventDefault()
    setBusyId(-1); setCreateMsg(null)
    try {
      const { createUser: apiCreate } = await import("../services/api")
      await apiCreate({
        name: form.name, email: form.email, password: form.password, role: form.role,
        assigned_district: form.assigned_district || null,
        assigned_state: form.assigned_state || null,
      })
      setCreateMsg({ ok: true, text: `Account created for ${form.name} (${ROLE_LABEL[form.role]}).` })
      setForm({ name: "", email: "", password: "", role: form.role, assigned_district: "", assigned_state: "" })
      load()
    } catch (e2) {
      setCreateMsg({ ok: false, text: e2.message || "Could not create the account" })
    } finally { setBusyId(null) }
  }

  return (
    <div>
      <PageHeader
        title="Administration"
        subtitle="Manage users and roles, assign Field Verifiers and District Authorities, and view system-wide activity."
        right={
          <button onClick={() => setShowCreate((v) => !v)} className="rounded-lg bg-[#c8102e] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#a50f29] dark:bg-blue-600 dark:hover:bg-blue-500">
            {showCreate ? "Close form" : "+ Create user"}
          </button>
        }
      />
      {loading && <LoadingState label="Loading admin data…" />}
      {error && <ErrorState message={error} onRetry={load} />}

      {showCreate && (
        <form onSubmit={createUser} className="mb-6 rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
          <h3 className="mb-3 text-lg font-bold text-[#292326] dark:text-[#f5eff1]">Provision a new account</h3>
          {createMsg && (
            <p className={`mb-3 rounded-lg p-2 text-sm ${createMsg.ok ? "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300" : "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"}`}>{createMsg.text}</p>
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <label className="text-sm font-semibold text-[#292326] dark:text-[#f5eff1]">Name
              <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-3 py-2 text-sm dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100" />
            </label>
            <label className="text-sm font-semibold text-[#292326] dark:text-[#f5eff1]">Email
              <input required type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} className="mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-3 py-2 text-sm dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100" />
            </label>
            <label className="text-sm font-semibold text-[#292326] dark:text-[#f5eff1]">Temporary password
              <input required minLength={8} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} className="mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-3 py-2 text-sm dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100" />
            </label>
            <label className="text-sm font-semibold text-[#292326] dark:text-[#f5eff1]">Role
              <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className="mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-3 py-2 text-sm dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100">
                {ALL_ROLES.filter((r) => r !== "public").map((r) => <option key={r} value={r}>{ROLE_LABEL[r]} ({r})</option>)}
              </select>
            </label>
            <label className="text-sm font-semibold text-[#292326] dark:text-[#f5eff1]">Assigned district (optional)
              <input value={form.assigned_district} onChange={(e) => setForm({ ...form, assigned_district: e.target.value })} placeholder="e.g. Port Blair" className="mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-3 py-2 text-sm dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100" />
            </label>
            <label className="text-sm font-semibold text-[#292326] dark:text-[#f5eff1]">Assigned state (optional)
              <input value={form.assigned_state} onChange={(e) => setForm({ ...form, assigned_state: e.target.value })} placeholder="e.g. Andaman and Nicobar Islands" className="mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-3 py-2 text-sm dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100" />
            </label>
          </div>
          <div className="mt-3 flex items-center gap-3">
            <button type="submit" disabled={busyId === -1} className="rounded-lg bg-[#c8102e] px-4 py-2 text-sm font-bold text-white disabled:opacity-50 dark:bg-blue-600">Create account</button>
            <p className="text-xs text-[#71666a] dark:text-[#b9aeb2]">District/state assignment is required for District Authorities to see scoped data; Field Verifiers don't need one.</p>
          </div>
        </form>
      )}

      {!loading && users && (
        <div className="mb-6 rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
          <h3 className="mb-3 text-lg font-bold text-[#292326] dark:text-[#f5eff1]">Users ({users.length})</h3>
          <div className="space-y-2">
            {users.map((u) => (
              <div key={u.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[#e8dadc] px-3 py-2.5 dark:border-[#351923]">
                <div className="min-w-0">
                  <p className="truncate text-[0.9375rem] font-semibold text-[#292326] dark:text-[#f5eff1]">{u.name} {!u.is_active && <span className="ml-1 rounded bg-red-100 px-1.5 py-0.5 text-xs font-bold text-red-700 dark:bg-red-950 dark:text-red-300">disabled</span>}</p>
                  <p className="truncate text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">
                    {u.email} · {ROLE_LABEL[u.role] || u.role}
                    {(u.assigned_district || u.assigned_state) ? ` · 📍 ${u.assigned_district || u.assigned_state}` : ""}
                    {" · joined "}{u.created_at?.slice(0, 10)}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    defaultValue={u.assigned_district || ""}
                    placeholder="district"
                    onBlur={(e) => { const v = e.target.value.trim(); if (v !== (u.assigned_district || "")) saveAssignment(u.id, { assigned_district: v }) }}
                    className="w-28 rounded-lg border border-[#e8dadc] bg-white px-2 py-1.5 text-[0.8125rem] dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100"
                  />
                  <input
                    defaultValue={u.assigned_state || ""}
                    placeholder="state"
                    onBlur={(e) => { const v = e.target.value.trim(); if (v !== (u.assigned_state || "")) saveAssignment(u.id, { assigned_state: v }) }}
                    className="w-32 rounded-lg border border-[#e8dadc] bg-white px-2 py-1.5 text-[0.8125rem] dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100"
                  />
                  <select
                    value={u.role}
                    disabled={busyId === u.id}
                    onChange={(e) => changeRole(u.id, e.target.value)}
                    className="rounded-lg border border-[#e8dadc] bg-white px-2 py-1.5 text-[0.8125rem] font-semibold dark:border-[#351923] dark:bg-[#0d090b] dark:text-[#f5eff1]"
                  >
                    {ALL_ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]} ({r})</option>)}
                  </select>
                  <button
                    onClick={() => toggleActive(u)}
                    disabled={busyId === u.id}
                    className="rounded-lg border border-[#e8dadc] px-3 py-1.5 text-[0.8125rem] font-bold text-[#292326] transition hover:bg-gray-50 disabled:opacity-50 dark:border-[#351923] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]"
                  >
                    {u.is_active ? "Deactivate" : "Activate"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {!loading && invs && (
        <div className="rounded-xl border border-[#e8dadc] bg-white p-4 dark:border-[#351923] dark:bg-[#000000]">
          <h3 className="mb-3 text-lg font-bold text-[#292326] dark:text-[#f5eff1]">System-wide investigations ({invs.length})</h3>
          {invs.length === 0 ? (
            <p className="text-sm text-[#71666a] dark:text-[#b9aeb2]">No user investigations recorded yet.</p>
          ) : (
            <div className="space-y-2">
              {invs.map((i) => (
                <div key={i.investigation_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[#e8dadc] px-3 py-2.5 dark:border-[#351923]">
                  <div className="min-w-0">
                    <p className="truncate text-[0.875rem] font-semibold text-[#292326] dark:text-[#f5eff1]">#{i.project_id} {i.project_name}</p>
                    <p className="text-[0.8125rem] text-[#71666a] dark:text-[#b9aeb2]">{i.user_name} ({i.user_email}) · {i.investigation_status} · {i.priority_tier || "—"} · updated {i.updated_at?.replace("T", " ").slice(0, 16)}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
