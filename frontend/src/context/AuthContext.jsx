import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react"
import { fetchMe, getToken, login as apiLogin, signup as apiSignup, logout as apiLogout, changePassword as apiChangePassword } from "../services/api"

const AuthContext = createContext(null)

// Role hierarchy for UI gating — mirrors backend auth.ROLE_RANK.
// field_verifier / district_authority are LATERAL roles below analyst:
// they never inherit the analyst workspace and instead rely on the
// explicit capabilities the backend returns with /auth/me.
const ROLE_RANK = {
  public: 0,
  citizen: 1,
  field_verifier: 1,
  district_authority: 1,
  analyst: 2,
  auditor: 3,
  admin: 4,
}

// Display labels + accent colors for the profile area.
export const ROLE_LABELS = {
  public: "Guest",
  citizen: "Citizen",
  field_verifier: "Field Verifier",
  district_authority: "District Authority",
  analyst: "Analyst",
  auditor: "Auditor",
  admin: "Administrator",
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true) // session restore on first load

  // Restore a persistent session from the stored token
  useEffect(() => {
    let alive = true
    const restore = async () => {
      if (!getToken()) { setLoading(false); return }
      try {
        const me = await fetchMe()
        if (alive) setUser(me)
      } catch {
        // token invalid/expired — api layer already cleared it
        if (alive) setUser(null)
      } finally {
        if (alive) setLoading(false)
      }
    }
    restore()
    // React to session expiry triggered by any API call
    const onExpired = () => setUser(null)
    window.addEventListener("auth-expired", onExpired)
    return () => { alive = false; window.removeEventListener("auth-expired", onExpired) }
  }, [])

  const login = useCallback(async (email, password) => {
    const data = await apiLogin(email, password)
    setUser(data.user)
    return data.user
  }, [])

  const signup = useCallback(async (payload) => {
    const data = await apiSignup(payload)
    setUser(data.user)
    return data.user
  }, [])

  const changePassword = useCallback(async (currentPassword, newPassword) => {
    const data = await apiChangePassword(currentPassword, newPassword)
    if (data?.user) setUser(data.user)
    return data
  }, [])

  const logout = useCallback(() => {
    apiLogout()
    setUser(null)
  }, [])

  const hasRole = useCallback(
    (minimum) => {
      if (!user) return false
      return (ROLE_RANK[user.role] ?? 0) >= (ROLE_RANK[minimum] ?? 99)
    },
    [user]
  )

  // Explicit capability check — mirrors backend ROLE_CAPABILITIES. Used for
  // lateral roles whose powers cannot be expressed by rank alone.
  const can = useCallback(
    (capability) => {
      if (!user) return false
      return Array.isArray(user.capabilities) && user.capabilities.includes(capability)
    },
    [user]
  )

  const value = useMemo(
    () => ({ user, loading, login, signup, changePassword, logout, hasRole, can }),
    [user, loading, login, signup, changePassword, logout, hasRole, can]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>")
  return ctx
}
