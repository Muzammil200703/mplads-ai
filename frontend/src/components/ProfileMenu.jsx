import { useEffect, useRef, useState } from "react"
import { useAuth, ROLE_LABELS } from "../context/AuthContext"

/* ═══════════════════════════════════════════════════════════
   Profile menu for the TopBar — shows the signed-in user's
   name/email/role and the sign-out action.
   ═══════════════════════════════════════════════════════════ */

const ROLE_BADGE = {
  public: "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300",
  citizen: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  field_verifier: "bg-teal-100 text-teal-700 dark:bg-teal-950 dark:text-teal-300",
  district_authority: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300",
  analyst: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  auditor: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  admin: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
}

export default function ProfileMenu({ onNavigate }) {
  const { user, logout, hasRole } = useAuth()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener("mousedown", onDoc)
    return () => document.removeEventListener("mousedown", onDoc)
  }, [])

  if (!user) {
    return (
      <button
        onClick={() => onNavigate && onNavigate("Sign in")}
        className="flex h-10 shrink-0 items-center gap-2 rounded-lg bg-[#c8102e] px-3 text-sm font-bold text-white transition hover:bg-[#a50f29] dark:bg-blue-600 dark:hover:bg-blue-500"
        aria-label="Sign in"
      >
        <span>👤</span>
        <span className="hidden sm:inline">Sign in</span>
      </button>
    )
  }

  const initial = (user.name || user.email || "?").trim().charAt(0).toUpperCase()

  return (
    <div className="relative shrink-0" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex h-10 items-center gap-2 rounded-lg px-2 transition hover:bg-[#f8f1f1] dark:hover:bg-[#0d090b]"
        aria-label="Profile menu"
        aria-expanded={open}
      >
        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#292326] text-sm font-bold text-white dark:bg-[#351923]">
          {initial}
        </span>
        <span className="hidden max-w-[140px] truncate text-sm font-semibold text-[#292326] dark:text-[#f5eff1] lg:inline">
          {user.name}
        </span>
        <span className="text-[0.625rem] text-[#71666a] dark:text-[#b9aeb2]">▾</span>
      </button>

      {open && (
        <div
          className="pop-in-down absolute right-0 top-12 z-50 w-72 overflow-hidden rounded-2xl border border-[#e8dadc] bg-white shadow-soft-lg dark:border-[#351923] dark:bg-[#000000]"
          style={{ transformOrigin: "top right" }}
        >
          <div className="border-b border-[#e8dadc] px-5 pb-4 pt-5 dark:border-[#351923]">
            <p className="truncate text-sm font-bold text-[#292326] dark:text-[#f5eff1]">{user.name}</p>
            <p className="mt-0.5 truncate text-xs text-[#71666a] dark:text-[#b9aeb2]">{user.email}</p>
            <span className={`mt-2 inline-block rounded px-2 py-0.5 text-[0.6875rem] font-bold ${ROLE_BADGE[user.role] || ROLE_BADGE.public}`}>
              {ROLE_LABELS[user.role] || user.role}
              {(user.assigned_district || user.assigned_state) && (
                <span className="ml-1 font-semibold opacity-80">· 📍 {user.assigned_district || user.assigned_state}</span>
              )}
            </span>
          </div>
          <div className="px-4 py-3 text-xs text-[#71666a] dark:text-[#b9aeb2]">
            <p className="font-semibold uppercase tracking-wider">Workspace</p>
            <div className="mt-1.5 space-y-1">
              {[
                { label: "Saved Projects", page: "Saved Projects", minRole: "analyst" },
                { label: "My Investigations", page: "My Investigations", minRole: "analyst" },
                { label: "My Audit Cases", page: "My Audit Cases", minRole: "auditor" },
                { label: "My Verifications", page: "My Verifications", cap: "verification:read_own" },
                { label: "My District", page: "My District", cap: "inquiry:respond" },
                { label: "Inquiries", page: "Inquiries", cap: "inquiry:review" },
              ]
                .filter((it) =>
                  it.cap
                    ? Array.isArray(user.capabilities) && user.capabilities.includes(it.cap)
                    : it.minRole === "analyst"
                      ? hasRole("analyst")
                      : hasRole("auditor")
                )
                .map((it) => (
                  <button
                    key={it.page}
                    onClick={() => { setOpen(false); onNavigate && onNavigate(it.page) }}
                    className="block w-full rounded-lg px-2.5 py-2 text-left text-[0.8125rem] text-[#292326] transition hover:bg-[#f8f1f1] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]"
                  >
                    {it.label}
                  </button>
                ))}
            </div>
          </div>
          <div className="border-t border-[#e8dadc] dark:border-[#351923]">
            <button
              onClick={() => { setOpen(false); logout() }}
              className="block w-full px-5 py-4 text-left text-sm font-semibold text-red-600 transition hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/30"
            >
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
