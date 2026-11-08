import { useState } from "react"
import { useAuth } from "../context/AuthContext"

/* Forced credential rotation: a provisioned or reset password is temporary,
   so the workspace stays locked until the owner chooses their own password. */

const inputClass =
  "mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-3 py-2.5 text-[0.9375rem] text-[#292326] outline-none transition focus:border-[#c8102e] focus:ring-2 focus:ring-[#c8102e]/10 dark:border-[#351923] dark:bg-[#0d090b] dark:text-[#f5eff1]"

export default function SetPassword() {
  const { user, changePassword, logout } = useAuth()
  const [form, setForm] = useState({ current: "", next: "", confirm: "" })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const submit = async (event) => {
    event.preventDefault()
    if (form.next.length < 8) {
      setError("Choose a password with at least 8 characters")
      return
    }
    if (form.next !== form.confirm) {
      setError("The new passwords do not match")
      return
    }
    setBusy(true)
    setError("")
    try {
      await changePassword(form.current, form.next)
    } catch (err) {
      setError(err.message || "Could not update the password")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-[100dvh] items-center justify-center px-4 py-10">
      <div className="w-full max-w-md rounded-xl border border-[#e8dadc] bg-white p-6 shadow-sm dark:border-[#351923] dark:bg-[#000000]">
        <div className="mb-5 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-[#120b0e] text-xl text-white dark:bg-[#351923]">
            🔐
          </div>
          <h2 className="text-xl font-bold text-[#292326] dark:text-[#f5eff1]">Set a new password</h2>
          <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">
            This account signed in with a temporary password. Choose your own password to continue.
          </p>
        </div>

        <p className="mb-4 truncate rounded-lg bg-[#faf7f6] px-3 py-2 text-xs text-[#71666a] dark:bg-[#0d090b] dark:text-[#b9aeb2]">
          Signed in as <span className="font-semibold">{user?.email}</span>
        </p>

        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Temporary password</label>
            <input
              type="password"
              required
              autoComplete="current-password"
              value={form.current}
              onChange={(e) => setForm((f) => ({ ...f, current: e.target.value }))}
              className={inputClass}
              placeholder="Password you just signed in with"
            />
          </div>
          <div>
            <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">New password</label>
            <input
              type="password"
              required
              minLength={8}
              autoComplete="new-password"
              value={form.next}
              onChange={(e) => setForm((f) => ({ ...f, next: e.target.value }))}
              className={inputClass}
              placeholder="Minimum 8 characters"
            />
          </div>
          <div>
            <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Confirm new password</label>
            <input
              type="password"
              required
              minLength={8}
              autoComplete="new-password"
              value={form.confirm}
              onChange={(e) => setForm((f) => ({ ...f, confirm: e.target.value }))}
              className={inputClass}
              placeholder="Repeat the new password"
            />
          </div>
          {error && (
            <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm font-semibold text-red-700 dark:bg-red-950/30 dark:text-red-300">
              {error}
            </p>
          )}
          <button
            disabled={busy}
            className="w-full rounded-lg bg-[#c8102e] px-4 py-2.5 text-[0.9375rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500"
          >
            {busy ? "Updating…" : "Update password"}
          </button>
        </form>

        <div className="mt-5 border-t border-[#e8dadc] pt-4 text-center dark:border-[#351923]">
          <button
            type="button"
            onClick={logout}
            className="text-sm font-semibold text-[#71666a] hover:underline dark:text-[#b9aeb2]"
          >
            Sign out
          </button>
        </div>
      </div>
    </div>
  )
}
