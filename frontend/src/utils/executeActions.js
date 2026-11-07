/* ═════════════════════════════════════════════════════════════════════
   Assistant ACTION EXECUTOR — the application's control plane.

   The backend assistant returns structured actions; this module is the
   ONLY way they touch the application. Every action type, target page
   and parameter key is checked against an explicit allowlist mirroring
   the backend contract (assistant.py ACTION_PARAM_KEYS / NAV_TARGETS),
   so assistant output can never execute arbitrary code, navigate to
   arbitrary URLs or call arbitrary endpoints.

   ActionTypes:
     navigate       {state, constituency, status, risk_level, tier, fy, keyword}
     open_project   {project_id in target}
     clear_filters  {}
     refresh_data   {}
   ═════════════════════════════════════════════════════════════════════ */

// Project drawer tabs the open_project action can deep-link (must mirror
// ProjectDetail.jsx tab ids; anything else falls back to the default tab).
const PROJECT_TABS = new Set(["overview", "financial", "risk", "forensics", "audit", "timeline", "changes", "similar", "dna"])

// Page keys mirror App.jsx page registry exactly.
const NAV_PAGES = new Set([
  "Overview", "Projects", "Risk Center", "AI Audit Center", "Vendor Network",
  "Ground Truth Verification", "Reports", "Leaderboard", "State Intelligence",
  "Audit Priority", "Compare Projects", "FAQ", "Vendor Intelligence", "Settings",
  "Saved Projects", "MP Intelligence",
  "My Investigations", "My Audit Cases", "My Verifications", "My District",
  "Inquiries", "Evidence Queue", "Administration", "Data Management",
])

const PARAM_KEYS = {
  navigate: new Set(["state", "constituency", "status", "risk_level", "tier", "severity", "fy", "keyword", "house", "mp_id", "vendor", "a", "b", "project"]),
  open_project: new Set(["tab"]),
  clear_filters: new Set([]),
  refresh_data: new Set([]),
}

/** Sanitize one action; returns null when it fails the allowlist. */
function sanitize(action) {
  if (!action || typeof action !== "object") return null
  const type = String(action.action || action.type || "").toLowerCase()
  if (!(type in PARAM_KEYS)) return null
  let target = action.target == null ? "" : String(action.target)
  let params = {}
  const src = action.params && typeof action.params === "object" ? action.params : {}
  for (const [k, v] of Object.entries(src)) {
    if (PARAM_KEYS[type].has(k) && v !== null && v !== undefined && v !== "") {
      params[k] = v
    }
  }
  if (type === "navigate" && !NAV_PAGES.has(target)) return null
  if (type === "open_project") {
    if (!/^\d{1,10}$/.test(target)) return null
    if (params.tab && !PROJECT_TABS.has(String(params.tab))) delete params.tab
  }
  if (type === "clear_filters" || type === "refresh_data") target = ""
  return { action: type, target, params, label: String(action.label || "").slice(0, 60) }
}

/**
 * Execute an allowlisted assistant action through the app's existing
 * mechanisms. Returns a short human status:
 *   "ok" | "failed:<reason>" | "rejected"
 * (the widget turns these into honest confirmations).
 */
export function executeAssistantAction(action, handlers) {
  const a = sanitize(action)
  if (!a) return "rejected"

  const {
    navigate,             // (page) => void           — existing handleNavigate
    drillDown,            // (page, params) => void   — existing handleDrillDown
    openProject,          // (projectId:number) => void — existing openProjectFromWorkspace
    clearFilters,         // () => void               — broadcast to mounted filter pages
    refreshData,          // () => void               — reload current page data
    hasPermission,        // (pageKey) => bool        — optional gate
  } = handlers

  try {
    if (a.action === "navigate") {
      if (hasPermission && !hasPermission(a.target)) {
        return "rejected:permissions"
      }
      // Drill-down carries filter params; plain navigate otherwise. This is
      // the SAME path the sidebar/TopBar use — no second routing system.
      if (Object.keys(a.params).length > 0) {
        drillDown(a.target, a.params)
      } else {
        navigate(a.target)
      }
      return "ok"
    }

    if (a.action === "open_project") {
      // Tab deep-links ride along as an event detail; ProjectDetail listens
      // and activates the requested tab once the drawer opens.
      openProject(parseInt(a.target, 10), a.params?.tab || undefined)
      return "ok"
    }

    if (a.action === "clear_filters") {
      clearFilters()
      return "ok"
    }

    if (a.action === "refresh_data") {
      refreshData()
      return "ok"
    }
  } catch {
    return "failed:route"
  }
  return "failed:unknown"
}
