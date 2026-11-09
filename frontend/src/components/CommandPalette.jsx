import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useAuth } from "../context/AuthContext"
import { globalSearchRecords, askAssistant } from "../services/api"

/* ═══════════════════════════════════════════════════════════════════
   GLOBAL COMMAND PALETTE 3.0
   One keyboard-first surface over the application that already exists:
   navigation comes from the shell's page registry, records from the
   backend /global-search endpoint, analytical questions from the existing
   assistant NLU (/assistant/ask), and every action is executed through the
   shell's own handlers (onNavigate / onDrillDown / onOpenProject).

   Nothing here invents routes, data or permissions: commands that need a
   role are filtered client-side and re-checked by the backend. The palette
   is deliberately text-only — no icons, no glyphs, no decorative marks.

   Simple view is a palette-scoped preference (persisted per user): it keeps
   pages, recents and quick actions, and hides record search + assistant
   answers. It never changes page data, filters or the rest of the app.
   ═══════════════════════════════════════════════════════════════════ */

const STORE_PREFIX = "mplads_palette_v2"
const MAX_RECENTS = 8
const MAX_PINS = 12
const MAX_TERMS = 6
const MAX_USAGE = 40
const SEARCH_TTL = 30000
const SEARCH_CACHE_MAX = 60
const AI_TTL = 120000

const TYPE_META = {
  project: { group: "Projects" },
  completed_work: { group: "Completed works" },
  mp: { group: "People" },
  vendor: { group: "Vendors" },
  constituency: { group: "Geography" },
  district: { group: "Geography" },
  state: { group: "Geography" },
  investigation: { group: "Investigations" },
  expenditure: { group: "Payments" },
  finding: { group: "Audit findings" },
  anomaly: { group: "Anomalies" },
  batch: { group: "Imports" },
  change: { group: "Recorded changes" },
  report: { group: "Reports" },
}

const RECORD_GROUP_ORDER = [
  "Projects", "Completed works", "People", "Vendors", "Geography",
  "Anomalies", "Investigations", "Payments", "Audit findings", "Reports",
  "Imports", "Recorded changes",
]

// Page keys mirror App.jsx exactly. Nothing is added unless the shell can
// actually render it; Historical Replay and Support intentionally point at
// the surfaces that already exist (State Intelligence panel, AI Assistant).
const PAGE_COMMANDS = [
  { id: "page-overview", title: "Open Executive Overview", page: "Overview", hint: "Portfolio dashboard and headline metrics", keywords: "home dashboard overview portfolio summary executive", access: "guest" },
  { id: "page-projects", title: "Open Projects", page: "Projects", hint: "Browse sanctioned works and search records", keywords: "projects works list search records catalog", access: "guest" },
  { id: "page-risk", title: "Open Risk Center", page: "Risk Center", hint: "Risk signals and anomaly records", keywords: "risk red flags anomalies risk center severity", access: "guest" },
  { id: "page-audit-priority", title: "Open Audit Priority", page: "Audit Priority", hint: "Flagged works ranked for audit", keywords: "risk audit priority flagged tier queue", access: "analyst" },
  { id: "page-state", title: "Open State Intelligence", page: "State Intelligence", hint: "State and House utilisation", keywords: "state map utilisation geography intelligence", access: "guest" },
  { id: "page-mp", title: "Open MP Intelligence", page: "MP Intelligence", hint: "MP portfolios by House", keywords: "mp member parliament constituency intelligence", access: "guest" },
  { id: "page-reports", title: "Open Reports", page: "Reports", hint: "Audit reports and data exports", keywords: "reports export download csv json audit summary", access: "guest" },
  { id: "page-audit-center", title: "Open AI Audit Center", page: "AI Audit Center", hint: "Automated audit review workspace", keywords: "ai audit center cases forensics review", access: "analyst" },
  { id: "page-vendor-network", title: "Open Vendor Network", page: "Vendor Network", hint: "Vendor relationships and links", keywords: "vendor network suppliers contractors relationships", access: "analyst" },
  { id: "page-vendor-intel", title: "Open Vendor Intelligence", page: "Vendor Intelligence", hint: "Vendor performance and risk", keywords: "vendor intelligence contractor payments performance", access: "analyst" },
  { id: "page-compare", title: "Compare Projects", page: "Compare Projects", hint: "Side-by-side project comparison", keywords: "compare projects side by side versus", access: "guest" },
  { id: "page-leaderboard", title: "Open Leaderboard", page: "Leaderboard", hint: "Ranked states and MPs", keywords: "leaderboard ranking top performers states mps", access: "guest" },
  { id: "page-ground", title: "Open Ground Truth Verification", page: "Ground Truth Verification", hint: "Field verification portal", keywords: "field inspection verification ground truth portal", access: "guest" },
  { id: "page-history", title: "Open Historical Data Replay", page: "State Intelligence", hint: "Historical replay panel · committed imports only", keywords: "historical data replay snapshots imports timeline changes", access: "guest" },
  { id: "page-faq", title: "Open FAQ", page: "FAQ", hint: "Frequently asked questions", keywords: "faq support help questions answers", access: "guest" },
  { id: "page-settings", title: "Open Settings", page: "Settings", hint: "Appearance, account and preferences", keywords: "settings theme dark light profile preferences", access: "guest" },
  { id: "page-saved", title: "Open Saved Projects", page: "Saved Projects", hint: "Your bookmarked works", keywords: "saved bookmarks workspace", access: "analyst" },
  { id: "page-investigation", title: "Open Project Investigation", page: "My Investigations", hint: "Your investigations", keywords: "investigation case inquiry workspace", access: "analyst" },
  { id: "page-audit-cases", title: "Open My Audit Cases", page: "My Audit Cases", hint: "Your audit cases", keywords: "audit cases workspace", access: "auditor" },
  { id: "page-inquiries", title: "Open Inquiries", page: "Inquiries", hint: "District inquiries", keywords: "inquiries letters respond", capability: "inquiry:review" },
  { id: "page-verifications", title: "Open My Verifications", page: "My Verifications", hint: "Your verification submissions", keywords: "verification evidence field submissions", capability: "verification:read_own" },
  { id: "page-evidence", title: "Open Evidence Queue", page: "Evidence Queue", hint: "Verification review queue", keywords: "evidence queue review verify", capability: "verification:review" },
  { id: "page-district", title: "Open My District", page: "My District", hint: "District authority workspace", keywords: "district authority workspace", capability: "inquiry:respond" },
  { id: "page-admin", title: "Open Administration", page: "Administration", hint: "Users and roles", keywords: "admin users roles access", access: "admin" },
  { id: "page-data", title: "Open Data Management", page: "Data Management", hint: "Imports and dataset health", keywords: "import upload data dataset health", access: "admin" },
]

// Commands that only exist because the current page can actually perform them.
const CONTEXT_BY_PAGE = {
  Overview: [
    { id: "ctx-ov-risk", title: "View high-risk projects", hint: "Risk Center lists every flagged work", page: "Risk Center", keywords: "high risk critical projects overview" },
    { id: "ctx-ov-state", title: "Open State Intelligence", hint: "State and House utilisation", page: "State Intelligence", keywords: "state intelligence utilisation" },
    { id: "ctx-ov-history", title: "Open historical import replay", hint: "Recorded imports only · State Intelligence", page: "State Intelligence", keywords: "recent dataset changes historical replay imports" },
  ],
  Projects: [
    { id: "ctx-pr-risk", title: "Filter high-risk projects", hint: "Continue in Risk Center", page: "Risk Center", keywords: "filter high risk projects" },
    { id: "ctx-pr-history", title: "Open historical data replay", hint: "Recorded imports only", page: "State Intelligence", keywords: "historical data replay changes" },
    { id: "ctx-pr-compare", title: "Compare project progress", hint: "Side-by-side comparison", page: "Compare Projects", keywords: "compare project progress" },
    { id: "ctx-pr-audit", title: "Review in the AI Audit Center", hint: "Automated audit workspace", page: "AI Audit Center", keywords: "audit review risk", access: "analyst" },
  ],
  "Risk Center": [
    { id: "ctx-rk-critical", title: "View critical projects", hint: "Flagged works in this list", page: "Risk Center", keywords: "critical projects high risk" },
    { id: "ctx-rk-emerging", title: "View emerging risk indicators", hint: "Risk intelligence layer on this page", page: "Risk Center", keywords: "emerging risk indicators trends", access: "analyst" },
    { id: "ctx-rk-patterns", title: "Compare risk patterns", hint: "Side-by-side projects", page: "Compare Projects", keywords: "compare risk patterns anomalies" },
    { id: "ctx-rk-priority", title: "Open Audit Priority", hint: "Ranked flagged works", page: "Audit Priority", keywords: "audit priority queue", access: "analyst" },
  ],
  "State Intelligence": [
    { id: "ctx-si-compare", title: "Compare states", hint: "Leaderboard ranking", page: "Leaderboard", keywords: "compare states ranking" },
    { id: "ctx-si-geo", title: "Open geospatial intelligence", hint: "Coverage, patterns and outliers", page: "State Intelligence", keywords: "geospatial outliers coverage", access: "analyst" },
    { id: "ctx-si-projects", title: "Browse works in this state", hint: "Projects filtered view", page: "Projects", keywords: "projects state filter" },
  ],
  "MP Intelligence": [
    { id: "ctx-mp-leader", title: "Compare MP performance", hint: "Leaderboard ranking", page: "Leaderboard", keywords: "compare mp performance" },
    { id: "ctx-mp-projects", title: "Browse MP works", hint: "Projects list", page: "Projects", keywords: "mp works projects" },
  ],
  "Vendor Intelligence": [
    { id: "ctx-vi-network", title: "Open the Vendor Network", hint: "Relationship graph", page: "Vendor Network", keywords: "vendor network relationships" },
    { id: "ctx-vi-projects", title: "Open linked projects", hint: "Browse the works catalog", page: "Projects", keywords: "vendor projects works" },
  ],
  "Vendor Network": [
    { id: "ctx-vn-intel", title: "Open Vendor Intelligence", hint: "Performance and risk detail", page: "Vendor Intelligence", keywords: "vendor intelligence performance" },
  ],
}

const ANALYTICAL = /^(show|find|list|which|what|how many|compare|give me|are there|rank|top|tell me|projects (with|where|in|that)|vendors (with|linked|appearing)|districts (with|where)|states (with|where)|mps? (with|where))/i

// Flat lookup for commands that live on a specific page, so a pin/frequent
// shortcut still resolves after the user leaves that page.
const CONTEXT_COMMAND_INDEX = new Map()
for (const commands of Object.values(CONTEXT_BY_PAGE)) {
  for (const command of commands) CONTEXT_COMMAND_INDEX.set(command.id, command)
}

function looksAnalytical(value) {
  const text = value.trim()
  return text.length >= 10 && text.split(/\s+/).length >= 3 && ANALYTICAL.test(text)
}

function storeKey(kind, user) {
  return `${STORE_PREFIX}:${kind}:${user?.id ?? "guest"}`
}

function loadList(kind, user, max) {
  try {
    const raw = JSON.parse(localStorage.getItem(storeKey(kind, user)) || "[]")
    return Array.isArray(raw) ? raw.slice(0, max) : []
  } catch {
    return []
  }
}

function saveList(kind, user, value) {
  try {
    localStorage.setItem(storeKey(kind, user), JSON.stringify(value))
  } catch {
    /* storage unavailable — the palette still works for this session */
  }
}

function loadObject(kind, user) {
  try {
    const raw = JSON.parse(localStorage.getItem(storeKey(kind, user)) || "{}")
    return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {}
  } catch {
    return {}
  }
}

function saveObject(kind, user, value) {
  try {
    localStorage.setItem(storeKey(kind, user), JSON.stringify(value))
  } catch {
    /* storage unavailable — the palette still works for this session */
  }
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

/** 120 exact · 100 prefix · 80 word-prefix · 60 substring · 30 subsequence. */
function matchScore(query, text) {
  const q = query.trim().toLowerCase()
  const t = (text || "").toLowerCase()
  if (!q || !t) return 0
  if (t === q) return 120
  if (t.startsWith(q)) return 100
  if (new RegExp(`(^|[^a-z0-9])${escapeRegExp(q)}`).test(t)) return 80
  if (t.includes(q)) return 60
  let cursor = 0
  for (const ch of t) {
    if (ch === q[cursor]) cursor += 1
    if (cursor === q.length) return 30
  }
  return 0
}

/* ── Session caches ──────────────────────────────────────────────────
   The palette remounts on every open (the shell keys it by open state), so
   these live at module scope. They are small, time-bounded and read-only:
   typing a character then deleting it replays the previous response without
   a second network request. */
const SEARCH_CACHE = new Map()
const AI_CACHE = new Map()

function searchCacheGet(key) {
  const hit = SEARCH_CACHE.get(key)
  if (!hit) return null
  if (Date.now() - hit.at > SEARCH_TTL) {
    SEARCH_CACHE.delete(key)
    return null
  }
  return hit.data
}

function searchCacheSet(key, data) {
  if (SEARCH_CACHE.size >= SEARCH_CACHE_MAX) SEARCH_CACHE.clear()
  SEARCH_CACHE.set(key, { at: Date.now(), data })
}

function aiCacheGet(key) {
  const hit = AI_CACHE.get(key)
  if (!hit) return null
  if (Date.now() - hit.at > AI_TTL) {
    AI_CACHE.delete(key)
    return null
  }
  return hit.data
}

function aiCacheSet(key, data) {
  if (AI_CACHE.size >= 30) AI_CACHE.clear()
  AI_CACHE.set(key, { at: Date.now(), data })
}

function Highlight({ text, query }) {
  const value = String(text ?? "")
  const term = query.trim()
  if (term.length < 2) return value
  const lower = value.toLowerCase()
  const needle = term.toLowerCase()
  const parts = []
  let from = 0
  while (from < value.length) {
    const index = lower.indexOf(needle, from)
    if (index < 0) break
    if (index > from) parts.push(value.slice(from, index))
    parts.push(
      <mark key={`mark-${index}`} className="rounded-[3px] bg-[#fadadd] px-0.5 text-[#8b0d23] dark:bg-[#5c1622] dark:text-[#f8dde0]">
        {value.slice(index, index + term.length)}
      </mark>
    )
    from = index + term.length
    if (parts.length > 24) break
  }
  if (!parts.length) return value
  if (from < value.length) parts.push(value.slice(from))
  return parts
}

function Kbd({ children }) {
  return (
    <kbd className="rounded-sm border border-[#e8dadc] bg-[#faf7f6] px-1.5 py-0.5 font-sans text-[0.625rem] font-semibold text-[#7a6f74] dark:border-[#351923] dark:bg-[#140c10] dark:text-[#b9aeb2]">
      {children}
    </kbd>
  )
}

export default function CommandPalette({
  open,
  onOpenChange,
  currentPage,
  house,
  fy,
  darkMode,
  onToggleTheme,
  onHouseChange,
  onFYChange,
  onNavigate,
  onDrillDown,
  onOpenProject,
  contextProjectId,
}) {
  const { user, hasRole, can } = useAuth()
  const inputRef = useRef(null)
  const listRef = useRef(null)
  const closeTimer = useRef(null)
  const aiRequestRef = useRef(0)
  const aiInFlightRef = useRef("")
  const [query, setQuery] = useState("")
  const [records, setRecords] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  const [active, setActive] = useState(0)
  const [helpOpen, setHelpOpen] = useState(false)
  const [confirmId, setConfirmId] = useState(null)
  const [closing, setClosing] = useState(false)
  const [recents, setRecents] = useState(() => loadList("recent", user, MAX_RECENTS))
  const [pins, setPins] = useState(() => loadList("pins", user, MAX_PINS))
  const [terms, setTerms] = useState(() => loadList("terms", user, MAX_TERMS))
  const [usage, setUsage] = useState(() => loadObject("usage", user))
  const [simpleMode, setSimpleMode] = useState(() => loadObject("prefs", user).simpleMode === true)
  const [ai, setAi] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState("")

  const requestClose = useCallback(() => {
    if (closeTimer.current) return
    setClosing(true)
    closeTimer.current = setTimeout(() => {
      closeTimer.current = null
      onOpenChange(false)
    }, 130)
  }, [onOpenChange])

  useEffect(() => () => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    aiRequestRef.current += 1
  }, [])

  const canSee = useCallback(
    (command) => {
      if (command.access === "analyst") return !!user && hasRole("analyst")
      if (command.access === "auditor") return !!user && hasRole("auditor")
      if (command.access === "admin") return !!user && hasRole("admin")
      if (command.capability) {
        try {
          return !!user && can(command.capability)
        } catch {
          return false
        }
      }
      return true
    },
    [user, hasRole, can]
  )

  // A record destination is a page plus optional params. Only Projects treats
  // projectId as "open the drawer"; other pages (audit cases, investigations)
  // are destinations in their own right, so the id must not hijack navigation.
  const openDestination = useCallback(
    (destination) => {
      if (!destination?.page) return
      const params = { ...(destination.params || {}) }
      const projectId = params.projectId
      delete params.projectId
      if (projectId && destination.page === "Projects") {
        onOpenProject?.(projectId)
        return
      }
      if (Object.keys(params).length) {
        onDrillDown?.(destination.page, params)
        return
      }
      onNavigate?.(destination.page)
    },
    [onNavigate, onDrillDown, onOpenProject]
  )

  // ── Global shortcut ────────────────────────────────────────────────────
  useEffect(() => {
    const onKeyDown = (event) => {
      const key = event.key.toLowerCase()
      if ((event.ctrlKey || event.metaKey) && !event.altKey && key === "k") {
        event.preventDefault()
        if (open) requestClose()
        else onOpenChange(true)
        return
      }
      if (event.key === "Escape" && open) {
        event.preventDefault()
        if (helpOpen) setHelpOpen(false)
        else requestClose()
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [open, onOpenChange, helpOpen, requestClose])

  // ── Open/close lifecycle ───────────────────────────────────────────────
  useEffect(() => {
    if (!open) return
    const previous = document.activeElement
    document.body.style.overflow = "hidden"
    const timer = setTimeout(() => inputRef.current?.focus(), 30)
    return () => {
      clearTimeout(timer)
      document.body.style.overflow = ""
      if (previous instanceof HTMLElement) previous.focus?.()
    }
  }, [open])

  const remember = useCallback((entry) => {
    if (!entry?.title) return
    // Persist synchronously: executing an item closes (and remounts) the
    // palette, so a state-updater side effect would never run.
    const key = entry.key || `${entry.type}:${entry.title}`
    const stored = loadList("recent", user, MAX_RECENTS)
    const next = [entry, ...stored.filter((r) => (r.key || `${r.type}:${r.title}`) !== key)].slice(0, MAX_RECENTS)
    saveList("recent", user, next)
    setRecents(next)
  }, [user])

  const rememberTerm = useCallback((term) => {
    const text = term.trim()
    if (text.length < 2) return
    setTerms((prev) => {
      const next = [text, ...prev.filter((t) => t.toLowerCase() !== text.toLowerCase())].slice(0, MAX_TERMS)
      saveList("terms", user, next)
      return next
    })
  }, [user])

  const rememberUsage = useCallback((id) => {
    if (!id) return
    setUsage((prev) => {
      const ranked = { ...prev, [id]: (prev[id] || 0) + 1 }
      const next = Object.fromEntries(
        Object.entries(ranked)
          .sort((a, b) => b[1] - a[1])
          .slice(0, MAX_USAGE)
      )
      saveObject("usage", user, next)
      return next
    })
  }, [user])

  // ── Debounced, abortable, cached record search ────────────────────────
  useEffect(() => {
    if (!open) return
    const term = query.trim()
    if (simpleMode || term.length < 2) return
    const key = `${user?.id ?? "guest"}|${term.toLowerCase()}|${house || "All"}|5`
    const cached = searchCacheGet(key)
    if (cached) {
      // Cache hits still commit on a tick so the effect never causes a
      // synchronous cascading render; typing back to a term is instant.
      const timer = setTimeout(() => {
        setRecords(cached)
        setLoading(false)
        setError("")
      }, 0)
      return () => clearTimeout(timer)
    }
    const controller = new AbortController()
    let cancelled = false
    const timer = setTimeout(async () => {
      setLoading(true)
      try {
        const data = await globalSearchRecords({
          q: term,
          limit: 5,
          house: house && house !== "All" ? house : undefined,
          signal: controller.signal,
        })
        if (cancelled) return
        const rows = Array.isArray(data?.results) ? data.results : []
        setRecords(rows)
        searchCacheSet(key, rows)
        setError("")
      } catch {
        if (cancelled || controller.signal.aborted) return
        setRecords([])
        setError("Record search is unavailable right now. Navigation and quick actions still work.")
      } finally {
        if (!cancelled) setLoading(false)
      }
    }, 200)
    return () => {
      cancelled = true
      clearTimeout(timer)
      controller.abort()
    }
  }, [query, open, house, simpleMode, user?.id])

  // ── Natural-language question → existing assistant engine ─────────────
  const runAI = useCallback(async (question) => {
    const text = question.trim()
    if (text.length < 6) return
    const key = `${user?.id ?? "guest"}|${text.toLowerCase()}|${currentPage || ""}|${house || "All"}`
    const cached = aiCacheGet(key)
    if (cached) {
      setAi(cached)
      setAiError("")
      return
    }
    if (aiInFlightRef.current === key) return
    aiInFlightRef.current = key
    const requestId = aiRequestRef.current + 1
    aiRequestRef.current = requestId
    setAiLoading(true)
    setAiError("")
    try {
      const data = await askAssistant(text, currentPage, null, house && house !== "All" ? house : null)
      if (requestId !== aiRequestRef.current) return
      const payload = {
        question: text,
        answer: data?.answer || "",
        actions: Array.isArray(data?.actions) ? data.actions : [],
        table: data?.table || null,
        source: data?.source || "",
      }
      setAi(payload)
      aiCacheSet(key, payload)
      rememberTerm(text)
    } catch {
      if (requestId !== aiRequestRef.current) return
      setAiError("The assistant could not answer that just now. Record search is unaffected.")
    } finally {
      if (aiInFlightRef.current === key) aiInFlightRef.current = ""
      if (requestId === aiRequestRef.current) setAiLoading(false)
    }
  }, [currentPage, house, rememberTerm, user?.id])

  useEffect(() => {
    if (!open || simpleMode) return
    const term = query.trim()
    if (!looksAnalytical(term)) return
    const key = `${user?.id ?? "guest"}|${term.toLowerCase()}|${currentPage || ""}|${house || "All"}`
    const cached = aiCacheGet(key)
    if (cached) {
      const timer = setTimeout(() => {
        setAi(cached)
        setAiError("")
      }, 0)
      return () => clearTimeout(timer)
    }
    const timer = setTimeout(() => { runAI(term) }, 600)
    return () => clearTimeout(timer)
  }, [query, open, runAI, simpleMode, currentPage, house, user?.id])

  // ── Commands ───────────────────────────────────────────────────────────
  const pageItems = useMemo(
    () =>
      PAGE_COMMANDS.filter(canSee).map((command) => ({
        id: command.id,
        kind: "command",
        group: "Navigation",
        title: command.title,
        subtitle: command.hint,
        keywords: command.keywords,
        page: command.page,
        pinnable: true,
        run: () => onNavigate?.(command.page),
        recent: { type: "page", title: command.title, subtitle: command.hint, destination: { page: command.page }, key: command.id },
      })),
    [canSee, onNavigate]
  )

  const quickActions = useMemo(() => {
    const items = [
      {
        id: "action-high-risk",
        kind: "action",
        group: "Quick actions",
        title: "View high-risk projects",
        subtitle: "Apply the High risk filter on Risk Center",
        keywords: "high risk critical projects flagged",
        pinnable: true,
        run: () => onDrillDown?.("Risk Center", { risk_level: "High" }),
      },
      {
        id: "action-anomalies",
        kind: "action",
        group: "Quick actions",
        title: "View recent anomalies",
        subtitle: "Risk Center anomaly records and ML outliers",
        keywords: "recent anomalies ml outliers risk flagged",
        pinnable: true,
        run: () => onNavigate?.("Risk Center"),
      },
      {
        id: "action-audit-review",
        kind: "action",
        group: "Quick actions",
        title: "Open pending audit reviews",
        subtitle: "AI Audit Center ranks flagged works for review",
        keywords: "pending audit reviews queue cases",
        access: "analyst",
        pinnable: true,
        run: () => onNavigate?.("AI Audit Center"),
      },
      {
        id: "action-vendor-intel",
        kind: "action",
        group: "Quick actions",
        title: "Open Vendor Intelligence",
        subtitle: "Contractor payments, concentration and risk",
        keywords: "vendor intelligence contractor payments performance",
        access: "analyst",
        pinnable: true,
        run: () => onNavigate?.("Vendor Intelligence"),
      },
      {
        id: "action-compare",
        kind: "action",
        group: "Quick actions",
        title: "Compare projects",
        subtitle: "Side-by-side works on the existing comparison page",
        keywords: "compare projects side by side selected",
        pinnable: true,
        run: () => onNavigate?.("Compare Projects"),
      },
      {
        id: "action-history",
        kind: "action",
        group: "Quick actions",
        title: "Open historical replay",
        subtitle: "Committed import checkpoints only · State Intelligence",
        keywords: "historical replay imports snapshots timeline",
        pinnable: true,
        run: () => onNavigate?.("State Intelligence"),
      },
      {
        id: "action-support",
        kind: "action",
        group: "Quick actions",
        title: "Open Support",
        subtitle: "The existing AI Assistant answers product questions",
        keywords: "support help assistant contact questions",
        pinnable: true,
        run: () => window.dispatchEvent(new CustomEvent("assistant:open")),
      },
      {
        id: "action-theme",
        kind: "action",
        group: "Quick actions",
        title: darkMode ? "Switch to light theme" : "Switch to dark theme",
        subtitle: "Appearance only — data and filters stay as they are",
        keywords: "theme dark light appearance toggle",
        pinnable: true,
        run: () => onToggleTheme?.(),
      },
      {
        id: "action-simple",
        kind: "action",
        group: "Quick actions",
        title: simpleMode ? "Switch to standard view" : "Switch to simple view",
        subtitle: "Palette only · hides record search and AI answers",
        keywords: "simple standard mode view density",
        pinnable: true,
        checked: simpleMode,
        run: () => {
          const next = !simpleMode
          setSimpleMode(next)
          if (next) {
            setRecords([])
            setLoading(false)
            setError("")
          }
          saveObject("prefs", user, { ...loadObject("prefs", user), simpleMode: next })
        },
      },
      {
        id: "action-settings",
        kind: "action",
        group: "Quick actions",
        title: "Open Settings",
        subtitle: "Appearance, account and preferences",
        keywords: "settings preferences account theme",
        pinnable: true,
        run: () => onNavigate?.("Settings"),
      },
      {
        id: "action-clear",
        kind: "action",
        group: "Quick actions",
        title: "Clear active page filters",
        subtitle: "Resets the current page's own filter state",
        keywords: "clear filters reset page search",
        danger: true,
        run: () => window.dispatchEvent(new CustomEvent("assistant:clear-filters")),
      },
      {
        id: "action-help",
        kind: "action",
        group: "Quick actions",
        title: "Keyboard shortcuts",
        subtitle: "Every key the palette understands",
        keywords: "help shortcuts keys keyboard command",
        run: () => setHelpOpen(true),
      },
    ]

    const houses = [
      ["All", "All Houses"],
      ["Lok Sabha", "Lok Sabha"],
      ["Rajya Sabha", "Rajya Sabha"],
    ]
    for (const [value, label] of houses) {
      items.push({
        id: `action-house-${value}`,
        kind: "action",
        group: "Quick actions",
        title: `House scope: ${label}`,
        subtitle: house === value ? "Currently applied" : "Apply across House-aware pages",
        keywords: `house ${label} lok sabha rajya sabha scope`,
        checked: house === value,
        run: () => onHouseChange?.(value),
      })
    }

    if (fy) {
      items.push({
        id: "action-fy-clear",
        kind: "action",
        group: "Quick actions",
        title: "Clear financial-year filter",
        subtitle: `Currently ${fy} · return to every recorded year`,
        keywords: "financial year fy clear period",
        run: () => onFYChange?.(""),
      })
    }

    return items.filter(canSee)
  }, [darkMode, house, fy, simpleMode, user, onToggleTheme, onNavigate, onDrillDown, onHouseChange, onFYChange, canSee])

  const contextItems = useMemo(() => {
    const items = []
    for (const command of (CONTEXT_BY_PAGE[currentPage] || []).filter(canSee)) {
      items.push({
        id: command.id,
        kind: "command",
        group: "On this page",
        title: command.title,
        subtitle: command.hint,
        keywords: command.keywords,
        page: command.page,
        pinnable: true,
        run: () => onNavigate?.(command.page),
      })
    }

    if (contextProjectId) {
      items.push(
        {
          id: "ctx-record-open",
          kind: "action",
          group: "On this page",
          title: `Investigate project #${contextProjectId}`,
          subtitle: "Open the project drawer with its existing detail views",
          keywords: "investigate selected project open",
          run: () => onOpenProject?.(contextProjectId),
        },
        {
          id: "ctx-record-changes",
          kind: "action",
          group: "On this page",
          title: "View this project's recorded changes",
          subtitle: `Project #${contextProjectId} · recorded imports only`,
          keywords: "history changes previous current progress compare",
          run: () => window.dispatchEvent(new CustomEvent("open-project", { detail: { projectId: contextProjectId, tab: "changes" } })),
        },
        {
          id: "ctx-record-vendor",
          kind: "action",
          group: "On this page",
          title: "View associated vendors",
          subtitle: "Vendor Intelligence · payments recorded against works",
          keywords: "related vendor expenditure payments",
          run: () => onNavigate?.("Vendor Intelligence"),
        },
        {
          id: "ctx-record-ask",
          kind: "action",
          group: "On this page",
          title: "Ask AI about this project",
          subtitle: "Uses the existing project context in the assistant",
          keywords: "ask ai project question",
          run: () => window.dispatchEvent(new CustomEvent("assistant:open")),
        }
      )
    }
    return items
  }, [currentPage, canSee, contextProjectId, onNavigate, onOpenProject])

  const aiItems = useMemo(() => {
    if (!ai) return []
    return ai.actions.slice(0, 4).map((action, index) => {
      const params = action.params || {}
      const projectId = params.projectId
      const target = action.page
      const rest = { ...params }
      delete rest.projectId
      return {
        id: `ai-action-${index}`,
        kind: "action",
        group: "Assistant actions",
        title: action.label || `Open ${target || "result"}`,
        subtitle: target ? `Assistant · ${target}` : "Assistant action",
        keywords: "",
        run: () => {
          if (projectId) onOpenProject?.(projectId)
          else if (target && Object.keys(rest).length) onDrillDown?.(target, rest)
          else if (target) onNavigate?.(target)
        },
      }
    })
  }, [ai, onOpenProject, onDrillDown, onNavigate])

  const recordItems = useMemo(
    () =>
      records.map((record) => ({
        id: `record-${record.id}`,
        kind: "record",
        group: (TYPE_META[record.type]?.group) || "Records",
        title: record.title,
        subtitle: record.subtitle,
        meta: record.meta,
        keywords: "",
        recordType: record.type,
        run: () => openDestination(record.destination),
        recent: {
          type: record.type,
          title: record.title,
          subtitle: record.subtitle,
          meta: record.meta,
          destination: record.destination,
          key: `record:${record.type}:${record.id}`,
        },
      })),
    [records, openDestination]
  )

  const resolveItem = useCallback(
    (id) => {
      const direct = [...contextItems, ...quickActions, ...pageItems].find((item) => item.id === id)
      if (direct) return direct
      const command = CONTEXT_COMMAND_INDEX.get(id)
      if (!command || !canSee(command)) return null
      return {
        id: command.id,
        kind: "command",
        group: "Pinned",
        title: command.title,
        subtitle: command.hint,
        keywords: command.keywords,
        page: command.page,
        pinnable: true,
        run: () => onNavigate?.(command.page),
      }
    },
    [contextItems, quickActions, pageItems, canSee, onNavigate]
  )

  const pinnedItems = useMemo(
    () => pins.map(resolveItem).filter(Boolean).map((item) => ({ ...item, group: "Pinned" })),
    [pins, resolveItem]
  )

  const frequentItems = useMemo(
    () =>
      Object.entries(usage)
        .sort((a, b) => b[1] - a[1])
        .map(([id]) => resolveItem(id))
        .filter((item) => item && !pins.includes(item.id))
        .slice(0, 4)
        .map((item) => ({ ...item, group: "Frequently used", kind: "frequent" })),
    [usage, resolveItem, pins]
  )

  const recentItems = useMemo(
    () =>
      recents.map((entry) => {
        const recentKey = entry.key || `${entry.type}:${entry.title}`
        return {
          id: `recent-${recentKey}`,
          kind: "recent",
          group: "Recent activity",
          title: entry.title,
          subtitle: entry.subtitle,
          meta: entry.meta,
          keywords: "",
          recentId: recentKey,
          run: () => openDestination(entry.destination),
        }
      }),
    [recents, openDestination]
  )

  const termItems = useMemo(
    () =>
      terms.map((term) => ({
        id: `term-${term}`,
        kind: "term",
        group: "Recent searches",
        title: term,
        subtitle: "Search again",
        keywords: "",
        recentTerm: term,
        run: () => setQuery(term),
      })),
    [terms]
  )

  const rank = useCallback((items, term) => {
    if (!term) return items
    return items
      .map((item) => ({
        item,
        score: Math.max(
          matchScore(term, item.title),
          matchScore(term, item.keywords || "") - 10,
          matchScore(term, item.subtitle || "") - 20
        ),
      }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score)
      .map((entry) => entry.item)
  }, [])

  const sections = useMemo(() => {
    const term = query.trim()
    if (helpOpen) {
      return [{ title: "Keyboard shortcuts", items: [], help: true }]
    }
    if (!term) {
      const out = []
      if (pinnedItems.length) out.push({ title: "Pinned", items: pinnedItems, kind: "pinned" })
      const recentSection = [...recentItems.slice(0, 4), ...termItems.slice(0, 3)]
      if (recentSection.length) out.push({ title: "Recent activity", items: recentSection, kind: "recent" })
      if (frequentItems.length) out.push({ title: "Frequently used", items: frequentItems })
      if (contextItems.length) out.push({ title: "On this page", items: contextItems })
      out.push({ title: "Quick actions", items: quickActions })
      out.push({ title: "Navigation", items: pageItems })
      return out
    }

    const out = []
    const ctx = rank(contextItems, term)
    if (ctx.length) out.push({ title: "On this page", items: ctx })
    const acts = rank(quickActions, term)
    if (acts.length) out.push({ title: "Quick actions", items: acts })
    const nav = rank(pageItems, term)
    if (nav.length) out.push({ title: "Navigation", items: nav })

    if (!simpleMode) {
      const grouped = new Map()
      for (const item of recordItems) {
        const bucket = grouped.get(item.group) || []
        bucket.push(item)
        grouped.set(item.group, bucket)
      }
      for (const group of RECORD_GROUP_ORDER) {
        const items = grouped.get(group)
        if (items?.length) out.push({ title: group, items })
      }
      if (ai && ai.question === term) {
        out.unshift({ title: "Assistant", items: aiItems, answer: ai })
      }
    }
    return out
  }, [query, helpOpen, simpleMode, pinnedItems, recentItems, termItems, frequentItems, contextItems, quickActions, pageItems, recordItems, ai, aiItems, rank])

  const flat = useMemo(() => sections.flatMap((section) => section.items), [sections])
  const clampedActive = Math.min(active, Math.max(0, flat.length - 1))
  const activeItem = flat[clampedActive]

  const sectionOffsets = useMemo(() => {
    let offset = 0
    return sections.map((section) => {
      const entry = { start: offset, count: section.items.length }
      offset += section.items.length
      return entry
    })
  }, [sections])

  const currentSectionIndex = useMemo(() => {
    let found = 0
    sectionOffsets.forEach((section, index) => {
      if (section.count > 0 && clampedActive >= section.start && clampedActive < section.start + section.count) {
        found = index
      }
    })
    return found
  }, [sectionOffsets, clampedActive])

  useEffect(() => {
    if (!open || !listRef.current) return
    const node = listRef.current.querySelector(`[data-index="${clampedActive}"]`)
    node?.scrollIntoView({ block: "nearest" })
  }, [clampedActive, open])

  const togglePin = useCallback((item) => {
    if (!item?.pinnable) return
    setPins((prev) => {
      const next = prev.includes(item.id)
        ? prev.filter((id) => id !== item.id)
        : [item.id, ...prev].slice(0, MAX_PINS)
      saveList("pins", user, next)
      return next
    })
  }, [user])

  const removeRecent = useCallback((key) => {
    setRecents((prev) => {
      const next = prev.filter((entry) => `${entry.type}:${entry.title}` !== key)
      saveList("recent", user, next)
      return next
    })
  }, [user])

  const removeTerm = useCallback((term) => {
    setTerms((prev) => {
      const next = prev.filter((t) => t !== term)
      saveList("terms", user, next)
      return next
    })
  }, [user])

  const execute = useCallback(
    (item) => {
      if (!item || closing) return
      if (item.danger && confirmId !== item.id) {
        setConfirmId(item.id)
        return
      }
      setConfirmId(null)
      if (item.recent) remember(item.recent)
      if (item.kind === "command" || item.kind === "action" || item.kind === "frequent") {
        rememberUsage(item.id)
      }
      requestClose()
      item.run?.()
    },
    [confirmId, remember, rememberUsage, requestClose, closing]
  )

  const jumpGroup = (direction) => {
    if (!flat.length) return
    let index = currentSectionIndex
    for (let step = 0; step < sections.length; step += 1) {
      index += direction
      if (index < 0 || index >= sections.length) return
      if (sectionOffsets[index].count > 0) {
        setActive(sectionOffsets[index].start)
        return
      }
    }
  }

  const onKeyDown = (event) => {
    if (closing) return
    const term = query.trim()
    if (event.key === "ArrowDown" && event.altKey) {
      event.preventDefault()
      jumpGroup(1)
    } else if (event.key === "ArrowUp" && event.altKey) {
      event.preventDefault()
      jumpGroup(-1)
    } else if (event.key === "ArrowDown") {
      event.preventDefault()
      setActive((prev) => (flat.length ? (Math.min(prev, flat.length - 1) + 1) % flat.length : 0))
    } else if (event.key === "ArrowUp") {
      event.preventDefault()
      setActive((prev) => (flat.length ? (Math.min(prev, flat.length - 1) - 1 + flat.length) % flat.length : 0))
    } else if (event.key === "PageDown") {
      event.preventDefault()
      setActive((prev) => (flat.length ? Math.min(prev + 5, flat.length - 1) : 0))
    } else if (event.key === "PageUp") {
      event.preventDefault()
      setActive((prev) => Math.max(0, prev - 5))
    } else if (event.key === "Enter") {
      event.preventDefault()
      if (event.shiftKey && term.length >= 6 && !simpleMode) {
        runAI(term)
        return
      }
      execute(flat[clampedActive])
    } else if (event.key === "Home") {
      event.preventDefault()
      setActive(0)
    } else if (event.key === "End") {
      event.preventDefault()
      setActive(Math.max(0, flat.length - 1))
    } else if (event.key === "Tab") {
      event.preventDefault()
      jumpGroup(event.shiftKey ? -1 : 1)
    }
  }

  if (!open) return null

  let runningIndex = -1
  const activeId = flat.length ? `command-option-${clampedActive}` : undefined

  return (
    <div
      className={`fixed inset-0 z-[95] flex items-end justify-center sm:items-start sm:px-3 sm:pt-[10vh] ${darkMode ? "dark" : ""}`}
    >
      <div
        className={`absolute inset-0 bg-black/45 transition-opacity duration-150 dark:bg-black/70 ${closing ? "opacity-0" : "opacity-100"}`}
        onClick={requestClose}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onKeyDown}
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
        className={`${closing ? "pop-exit" : "pop-in-down"} relative flex max-h-[90dvh] w-full flex-col overflow-hidden rounded-t-2xl border border-[#e8dadc] bg-white shadow-soft-lg sm:max-h-[78vh] sm:max-w-3xl sm:rounded-2xl dark:border-[#351923] dark:bg-[#0d090b]`}
      >
        <div className="mx-auto mt-2 h-1 w-10 rounded-full bg-[#e8dadc] sm:hidden dark:bg-[#351923]" />

        <div className="flex items-center gap-3 border-b border-[#f0e6e7] px-4 dark:border-[#2a141a]">
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls="command-listbox"
            aria-autocomplete="list"
            aria-activedescendant={activeId}
            aria-label="Search records, pages and actions"
            autoComplete="off"
            spellCheck="false"
            placeholder={simpleMode ? "Search pages and actions…" : "Search projects, MPs, vendors, districts, anomalies, reports… or ask a question"}
            value={query}
            onChange={(event) => {
              const value = event.target.value
              setQuery(value)
              setActive(0)
              setConfirmId(null)
              setAiError("")
              if (simpleMode || value.trim().length < 2) {
                setRecords([])
                setLoading(false)
                setError("")
              }
            }}
            className="h-12 min-w-0 flex-1 bg-transparent text-base text-[#292326] outline-none placeholder:text-[#b9aeb2] sm:text-sm dark:text-[#f5eff1] dark:placeholder:text-[#82767b]"
          />
          {query && (
            <button
              type="button"
              onClick={() => {
                setQuery("")
                setActive(0)
                setRecords([])
                setLoading(false)
                setError("")
                inputRef.current?.focus()
              }}
              className="flex-none rounded-sm px-1.5 py-1 text-[0.6875rem] font-medium text-[#7a6f74] hover:bg-black/5 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[#c8102e] dark:text-[#b9aeb2] dark:hover:bg-white/10"
            >
              Clear
            </button>
          )}
          <span className="hidden flex-none items-center gap-1 sm:flex">
            <Kbd>Esc</Kbd>
          </span>
        </div>

        <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,1fr)_16rem]">
          <div ref={listRef} id="command-listbox" role="listbox" aria-label="Commands and records" className="max-h-[58vh] overflow-y-auto overscroll-contain py-1 lg:max-h-[60vh]">
            {error && (
              <div role="status" className="mx-3 mt-2 rounded-lg border border-[#f0d6d9] bg-[#fdf2f3] px-3 py-2 text-xs text-[#8a2b36] dark:border-[#3a1b22] dark:bg-[#1a0f13] dark:text-[#e2a1ab]">
                {error}
              </div>
            )}

            {aiError && (
              <div role="status" className="mx-3 mt-2 rounded-lg border border-[#f0d6d9] bg-[#fdf2f3] px-3 py-2 text-xs text-[#8a2b36] dark:border-[#3a1b22] dark:bg-[#1a0f13] dark:text-[#e2a1ab]">
                {aiError}
              </div>
            )}

            {(loading || aiLoading) && (
              <div role="status" className="animate-pulse px-4 py-2.5 text-xs text-[#7a6f74] dark:text-[#b9aeb2]">
                {aiLoading ? "Asking the assistant…" : "Searching records…"}
              </div>
            )}

            {helpOpen && (
              <div className="space-y-2 px-4 py-3 text-xs text-[#5e5257] dark:text-[#b9aeb2]">
                {[
                  ["Ctrl / Cmd + K", "Open or close the palette"],
                  ["Esc", "Close the palette (or leave this help)"],
                  ["Up / Down", "Move through results"],
                  ["Tab / Shift + Tab", "Move to the next or previous category"],
                  ["Alt + Up / Down", "Jump to the previous or next category"],
                  ["Page Up / Page Down", "Move five results at a time"],
                  ["Enter", "Run the highlighted command"],
                  ["Shift + Enter", "Ask the typed question (standard view)"],
                ].map(([keys, meaning]) => (
                  <div key={keys} className="flex items-center justify-between gap-3">
                    <span className="font-semibold text-[#292326] dark:text-[#f5eff1]">{keys}</span>
                    <span className="text-right">{meaning}</span>
                  </div>
                ))}
                <button type="button" onClick={() => setHelpOpen(false)} className="mt-2 rounded-md border border-[#e8dadc] px-3 py-1.5 font-semibold text-[#292326] hover:bg-[#faf7f6] dark:border-[#351923] dark:text-[#f5eff1] dark:hover:bg-[#140c10]">
                  Back to search
                </button>
              </div>
            )}

            {!helpOpen && sections.map((section) => (
              <div key={section.title} role="group" aria-label={section.title}>
                <div className="flex items-baseline justify-between gap-3 px-4 pb-1 pt-3">
                  <span className="text-[0.625rem] font-bold uppercase tracking-wider text-[#b9aeb2] dark:text-[#82767b]">
                    {section.title}
                  </span>
                  {section.items.length > 0 && (
                    <span className="text-[0.625rem] tabular-nums text-[#b9aeb2] dark:text-[#82767b]">{section.items.length}</span>
                  )}
                </div>

                {section.answer && (
                  <div className="mx-3 mb-2 rounded-lg border border-[#f0e6e7] bg-[#faf7f6] p-2.5 text-xs leading-snug text-[#3a3135] dark:border-[#2a141a] dark:bg-[#140c10] dark:text-[#ece4e6]">
                    {section.answer.answer ? (
                      <p className="whitespace-pre-line">{section.answer.answer}</p>
                    ) : (
                      <p className="text-[#7a6f74] dark:text-[#b9aeb2]">
                        No source-backed answer was returned. Try a narrower question or open the records directly.
                      </p>
                    )}
                    {section.answer.table?.columns?.length > 0 && (
                      <div className="mt-2 overflow-x-auto">
                        <table className="w-full text-left text-[0.6875rem]">
                          <thead>
                            <tr className="text-[#7a6f74] dark:text-[#b9aeb2]">
                              {section.answer.table.columns.map((column) => (
                                <th key={column} className="whitespace-nowrap py-1 pr-2 font-semibold">{column}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {(section.answer.table.rows || []).slice(0, 4).map((row, index) => (
                              <tr key={index} className="border-t border-[#f0e6e7] dark:border-[#2a141a]">
                                {(Array.isArray(row) ? row : section.answer.table.columns.map((c) => row?.[c])).map((cell, cellIndex) => (
                                  <td key={cellIndex} className="whitespace-nowrap py-1 pr-2">{String(cell ?? "—")}</td>
                                ))}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    <p className="mt-1 text-[0.625rem] text-[#9a8b90]">
                      Answered by the existing assistant engine · {section.answer.source || "assistant"}
                    </p>
                  </div>
                )}

                {section.items.map((item) => {
                  runningIndex += 1
                  const index = runningIndex
                  const isActive = index === clampedActive
                  const pinned = pins.includes(item.id)
                  return (
                    <div
                      key={item.id}
                      id={`command-option-${index}`}
                      data-index={index}
                      role="option"
                      aria-selected={isActive}
                      tabIndex={-1}
                      onMouseEnter={() => setActive(index)}
                      onClick={() => {
                        // execute is a stable useCallback, not a ref.
                        // eslint-disable-next-line react-hooks/refs
                        execute(item)
                      }}
                      className={`group flex w-full cursor-pointer items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                        isActive
                          ? "bg-[#fdf2f3] shadow-[inset_2px_0_0_#c8102e] dark:bg-[#1a0f13] dark:shadow-[inset_2px_0_0_#8b2233]"
                          : "hover:bg-[#faf7f6] dark:hover:bg-[#140c10]"
                      }`}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[0.8125rem] font-medium text-[#292326] sm:text-sm dark:text-[#f5eff1]">
                          <Highlight text={item.title} query={query} />
                        </span>
                        {item.subtitle && (
                          <span className="block truncate text-[0.6875rem] text-[#7a6f74] sm:text-xs dark:text-[#b9aeb2]">
                            {confirmId === item.id ? "Press Enter again to confirm" : <Highlight text={item.subtitle} query={query} />}
                          </span>
                        )}
                      </span>

                      <span className="flex flex-none items-center gap-2">
                        {item.checked && (
                          <span className="rounded-sm bg-[#f5efef] px-1.5 py-0.5 text-[0.5625rem] font-semibold uppercase tracking-wide text-[#c8102e] dark:bg-[#170d11] dark:text-[#e2a1ab]">
                            Current
                          </span>
                        )}
                        {item.meta && (
                          <span className="hidden text-[0.6875rem] font-medium tabular-nums text-[#7a6f74] sm:block dark:text-[#b9aeb2]">{item.meta}</span>
                        )}

                        {item.kind === "recent" || item.kind === "term" ? (
                          <button
                            type="button"
                            aria-label={item.kind === "term" ? "Remove this recent search" : "Remove from recent activity"}
                            onClick={(event) => {
                              event.stopPropagation()
                              if (item.kind === "term") removeTerm(item.recentTerm)
                              else removeRecent(item.recentId)
                            }}
                            className="rounded-sm px-1.5 py-1 text-[0.625rem] font-medium text-[#9a8b90] opacity-100 transition hover:bg-black/5 focus:opacity-100 group-hover:opacity-100 sm:opacity-0 sm:focus:opacity-100 sm:group-hover:opacity-100 dark:hover:bg-white/10"
                          >
                            Remove
                          </button>
                        ) : item.pinnable ? (
                          <button
                            type="button"
                            aria-label={pinned ? "Unpin this command" : "Pin this command"}
                            aria-pressed={pinned}
                            onClick={(event) => {
                              event.stopPropagation()
                              togglePin(item)
                            }}
                            className={`rounded-sm px-1.5 py-1 text-[0.625rem] font-medium transition hover:bg-black/5 dark:hover:bg-white/10 ${pinned ? "text-[#c8102e] opacity-100 dark:text-[#e2a1ab]" : "text-[#9a8b90] opacity-100 focus:opacity-100 group-hover:opacity-100 sm:opacity-0 sm:focus:opacity-100 sm:group-hover:opacity-100"}`}
                          >
                            {pinned ? "Unpin" : "Pin"}
                          </button>
                        ) : null}
                      </span>
                    </div>
                  )
                })}
              </div>
            ))}

            {!helpOpen && !loading && !aiLoading && flat.length === 0 && (
              <div className="px-4 py-10 text-center" role="status">
                <div className="text-sm font-medium text-[#292326] dark:text-[#f5eff1]">
                  {query.trim() ? `No matches for “${query.trim()}”` : "Start typing to search"}
                </div>
                <div className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-[#7a6f74] dark:text-[#b9aeb2]">
                  {query.trim()
                    ? simpleMode
                      ? "Simple view searches pages and actions only. Try a module name, or switch to standard view for records and AI answers."
                      : "Try a project ID, a vendor, a constituency, a district, an anomaly, or press Shift + Enter to ask the assistant."
                    : "Projects and IDs, MPs, vendors, districts, anomalies, audit findings, recorded changes and report types."}
                </div>
              </div>
            )}
          </div>

          {/* Desktop preview: what the highlighted row will open. Text only. */}
          <aside className="hidden border-l border-[#f0e6e7] px-4 py-4 lg:block dark:border-[#2a141a]" aria-hidden="true">
            {activeItem ? (
              <div className="space-y-2">
                <p className="text-sm font-bold leading-snug text-[#292326] dark:text-[#f5eff1]">{activeItem.title}</p>
                {activeItem.subtitle && <p className="text-xs leading-relaxed text-[#7a6f74] dark:text-[#b9aeb2]">{activeItem.subtitle}</p>}
                {activeItem.meta && <p className="font-mono text-xs font-semibold text-[#292326] dark:text-[#f5eff1]">{activeItem.meta}</p>}
                <p className="pt-2 text-[0.625rem] uppercase tracking-wider text-[#b9aeb2] dark:text-[#82767b]">{activeItem.group}</p>
                <p className="text-[0.6875rem] leading-relaxed text-[#9a8b90]">
                  {activeItem.kind === "term"
                    ? "Press Enter to search again."
                    : activeItem.danger
                      ? "Press Enter twice to confirm."
                      : "Press Enter to open. Records open their existing page or drawer."}
                </p>
              </div>
            ) : (
              <p className="text-xs text-[#9a8b90]">Highlight a row to preview it here.</p>
            )}
          </aside>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[#f0e6e7] px-4 py-2 text-[0.6875rem] text-[#7a6f74] dark:border-[#2a141a] dark:text-[#b9aeb2]">
          <span className="flex flex-wrap items-center gap-3">
            <span className="flex items-center gap-1"><Kbd>Up</Kbd><Kbd>Down</Kbd> move</span>
            <span className="flex items-center gap-1"><Kbd>Enter</Kbd> open</span>
            <span className="hidden items-center gap-1 sm:flex"><Kbd>Shift</Kbd><Kbd>Enter</Kbd> ask</span>
            <span className="hidden items-center gap-1 md:flex"><Kbd>Tab</Kbd> category</span>
          </span>
          <span className="flex items-center gap-1"><Kbd>Ctrl</Kbd><Kbd>K</Kbd><span className="ml-1">anywhere</span></span>
        </div>
      </div>
    </div>
  )
}
