import { useState } from "react"
import { useAuth } from "../context/AuthContext"
import { forgotPassword, resetPassword } from "../services/api"

/* ═══════════════════════════════════════════════════════════
   Authentication views — Login, Signup, Password Reset.
   Matches the existing government-portal design language.
   No data is fabricated: every state is a real API result.
   ═══════════════════════════════════════════════════════════ */

function AuthShell({ title, subtitle, children, footer }) {
  return (
    <div className="flex min-h-full items-center justify-center px-4 py-10">
      <div className="w-full max-w-md rounded-xl border border-[#e8dadc] bg-white p-6 shadow-sm dark:border-[#351923] dark:bg-[#000000]">
        <div className="mb-5 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-[#120b0e] text-xl text-white dark:bg-[#351923]">
            🏛
          </div>
          <h2 className="text-xl font-bold text-[#292326] dark:text-[#f5eff1]">{title}</h2>
          <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">{subtitle}</p>
        </div>
        {children}
        {footer && <div className="mt-5 border-t border-[#e8dadc] pt-4 text-center text-sm dark:border-[#351923]">{footer}</div>}
      </div>
    </div>
  )
}

const inputClass =
  "mt-1 w-full rounded-lg border border-[#e8dadc] bg-white px-3 py-2.5 text-[0.9375rem] text-[#292326] outline-none transition focus:border-[#c8102e] focus:ring-2 focus:ring-[#c8102e]/10 dark:border-[#351923] dark:bg-[#0d090b] dark:text-[#f5eff1]"

export function LoginView({ onSwitch }) {
  const { login } = useAuth()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [mode, setMode] = useState("login") // login | forgot | reset

  const handleLogin = async (e) => {
    e.preventDefault()
    setBusy(true); setError("")
    try { await login(email, password) }
    catch (err) { setError(err.message || "Sign-in failed") }
    finally { setBusy(false) }
  }

  const handleForgot = async (e) => {
    e.preventDefault()
    setBusy(true); setError("")
    try {
      const res = await forgotPassword(email)
      setMode("reset")
      setError("")
      window.__lastResetInfo = res // token shown in reset form note when present
    } catch (err) { setError(err.message || "Request failed") }
    finally { setBusy(false) }
  }

  const handleReset = async (e) => {
    e.preventDefault()
    setBusy(true); setError("")
    try {
      await resetPassword(resetForm.token, resetForm.new_password)
      setMode("login")
      setResetForm({ token: "", new_password: "" })
      setError("")
      setNotice("Password updated. Sign in with your new password.")
    } catch (err) { setError(err.message || "Reset failed") }
    finally { setBusy(false) }
  }

  const [resetForm, setResetForm] = useState({ token: "", new_password: "" })
  const [notice, setNotice] = useState("")

  if (mode === "forgot") {
    return (
      <AuthShell title="Reset password" subtitle="Enter your account email to receive a single-use reset token." footer={<button onClick={() => setMode("login")} className="font-semibold text-blue-600 hover:underline dark:text-blue-400">← Back to sign in</button>}>
        <form onSubmit={handleForgot} className="space-y-4">
          <div>
            <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Email</label>
            <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={inputClass} placeholder="you@example.gov.in" />
          </div>
          {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm font-semibold text-red-700 dark:bg-red-950/30 dark:text-red-300">{error}</p>}
          <button disabled={busy} className="w-full rounded-lg bg-[#c8102e] px-4 py-2.5 text-[0.9375rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500">
            {busy ? "Requesting…" : "Get reset token"}
          </button>
        </form>
      </AuthShell>
    )
  }

  if (mode === "reset") {
    const hint = window.__lastResetInfo?.reset_token
    return (
      <AuthShell title="Set a new password" subtitle="Paste the reset token and choose a new password (min 8 characters)." footer={<button onClick={() => setMode("login")} className="font-semibold text-blue-600 hover:underline dark:text-blue-400">← Back to sign in</button>}>
        <form onSubmit={handleReset} className="space-y-4">
          {hint && (
            <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-700 dark:bg-amber-950/30 dark:text-amber-300">
              No mail server is configured in this deployment, so your single-use token is shown here (valid 30 minutes):
              <span className="mt-1 block break-all font-mono text-[0.6875rem] font-bold">{hint}</span>
            </p>
          )}
          <div>
            <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Reset token</label>
            <input required value={resetForm.token} onChange={(e) => setResetForm((f) => ({ ...f, token: e.target.value }))} className={`${inputClass} font-mono text-sm`} placeholder="Paste token" />
          </div>
          <div>
            <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">New password</label>
            <input type="password" required minLength={8} value={resetForm.new_password} onChange={(e) => setResetForm((f) => ({ ...f, new_password: e.target.value }))} className={inputClass} placeholder="Minimum 8 characters" />
          </div>
          {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm font-semibold text-red-700 dark:bg-red-950/30 dark:text-red-300">{error}</p>}
          <button disabled={busy} className="w-full rounded-lg bg-[#c8102e] px-4 py-2.5 text-[0.9375rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500">
            {busy ? "Updating…" : "Update password"}
          </button>
        </form>
      </AuthShell>
    )
  }

  return (
    <AuthShell
      title="Auditor sign in"
      subtitle="Access your saved projects, investigations and audit cases."
      footer={
        <div className="space-y-1">
          <p>
            New here?{" "}
            <button onClick={() => onSwitch("signup")} className="font-semibold text-blue-600 hover:underline dark:text-blue-400">Create an account</button>
          </p>
          <button onClick={() => setMode("forgot")} className="text-sm text-[#71666a] hover:underline dark:text-[#b9aeb2]">Forgot password?</button>
        </div>
      }
    >
      <form onSubmit={handleLogin} className="space-y-4">
        <div>
          <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Email</label>
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={inputClass} placeholder="you@example.gov.in" />
        </div>
        <div>
          <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Password</label>
          <input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} className={inputClass} placeholder="••••••••" />
        </div>
        {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm font-semibold text-red-700 dark:bg-red-950/30 dark:text-red-300">{error}</p>}
        {notice && <p className="rounded-lg bg-green-50 px-3 py-2 text-sm font-semibold text-green-700 dark:bg-green-950/30 dark:text-green-300">{notice}</p>}
        <button disabled={busy} className="w-full rounded-lg bg-[#c8102e] px-4 py-2.5 text-[0.9375rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </AuthShell>
  )
}

export function SignupView({ onSwitch }) {
  const { signup } = useAuth()
  const [form, setForm] = useState({ name: "", email: "", password: "", confirm: "", role: "analyst" })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const submit = async (e) => {
    e.preventDefault()
    if (form.password !== form.confirm) { setError("Passwords do not match"); return }
    setBusy(true); setError("")
    try {
      await signup({ name: form.name, email: form.email, password: form.password, role: form.role })
    } catch (err) { setError(err.message || "Sign-up failed") }
    finally { setBusy(false) }
  }

  return (
    <AuthShell
      title="Create your account"
      subtitle="Submit citizen evidence, save projects, run investigations and manage audit cases."
      footer={<p>New users receive the selected role; higher roles are granted by an administrator.</p>}
    >
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Full name</label>
          <input required minLength={2} value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} className={inputClass} placeholder="e.g. A. Sharma" />
        </div>
        <div>
          <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Email</label>
          <input type="email" required value={form.email} onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))} className={inputClass} placeholder="you@example.gov.in" />
        </div>
        <div>
          <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Role</label>
          <select value={form.role} onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))} className={inputClass}>
            <option value="citizen">Citizen — submit ground evidence for projects</option>
            <option value="analyst">Analyst — save projects, create investigations</option>
            <option value="auditor">Auditor — also manage audit cases</option>
          </select>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Password</label>
            <input type="password" required minLength={8} value={form.password} onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))} className={inputClass} placeholder="Min 8 characters" />
          </div>
          <div>
            <label className="text-sm font-semibold text-[#71666a] dark:text-[#b9aeb2]">Confirm</label>
            <input type="password" required minLength={8} value={form.confirm} onChange={(e) => setForm((f) => ({ ...f, confirm: e.target.value }))} className={inputClass} placeholder="Repeat password" />
          </div>
        </div>
        {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm font-semibold text-red-700 dark:bg-red-950/30 dark:text-red-300">{error}</p>}
        <button disabled={busy} className="w-full rounded-lg bg-[#c8102e] px-4 py-2.5 text-[0.9375rem] font-bold text-white transition hover:bg-[#a50f29] disabled:opacity-50 dark:bg-blue-600 dark:hover:bg-blue-500">
          {busy ? "Creating account…" : "Create account"}
        </button>
      </form>
      <p className="mt-4 text-center text-sm">
        Already registered?{" "}
        <button onClick={() => onSwitch("login")} className="font-semibold text-blue-600 hover:underline dark:text-blue-400">Sign in</button>
      </p>
    </AuthShell>
  )
}
