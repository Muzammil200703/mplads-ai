import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useAuth } from "../context/AuthContext"
import { globalSearchRecords, askAssistant } from "../services/api"

/* ═══════════════════════════════════════════════════════════════════
   GLOBAL COMMAND PALETTE 2.0
   One keyboard-first surface over the application that already exists:
   navigation comes from the shell's page registry, records from the
   backend /global-search endpoint, analytical questions from the existing
   assistant NLU (/assistant/ask), and every action is executed through the
   shell's own handlers (onNavigate / onDrillDown / onOpenProject).

   Nothing here invents routes, data or permissions: commands that need a
   role are filtered client-side and re-checked by the backend.
   ═══════════════════════════════════════════════════════════════════ */

const STORE_PREFIX = "mplads_palette_v2"
const MAX_RECENTS = 8
const MAX_PINS = 12
const MAX_TERMS = 6

const TYPE_META = {
  project: { icon: "▤", group: "Projects" },
  completed_work: { icon: "✅", group: "Completed works" },
  mp: { icon: "👤", group: "People" },
  vendor: { icon: "🏢", group: "Vendors" },
  constituency: { icon: "📍", group: "Geography" },
  state: { icon: "🗺", group: "Geography" },
  investigation: { icon: "🔍", group: "Investigations" },
  expenditure: { icon: "₹", group: "Payments" },
  finding: { icon: "🛡", group: "Audit findings" },
  batch: { icon: "🗄", group: "Imports" },
  change: { icon: "🕘", group: "Recorded changes" },
}

const RECORD_GROUP_ORDER = [
  "Projects", "Completed works", "People", "Vendors", "Geography",
  "Investigations", "Payments", "Audit findings", "Imports", "Recorded changes",
]

// Page keys mirror App.jsx exactly.
const PAGE_COMMANDS = [
  { id: "page-overview", title: "Open Overview", page: "Overview", icon: "▦", hint: "Portfolio dashboard", keywords: "home dashboard overview portfolio summary", access: "guest" },
  { id: "page-projects", title: "Open Projects", page: "Projects", icon: "▤", hint: "Browse sanctioned works", keywords: "projects works list search records", access: "guest" },
  { id: "page-risk", title: "Open Risk Center", page: "Risk Center", icon: "⚠", hint: "Risk signals across projects", keywords: "risk red flags anomalies risk center", access: "guest" },
  { id: "page-audit-priority", title: "Open Audit Priority", page: "Audit Priority", icon: "🎯", hint: "Flagged works ranked for audit", keywords: "risk audit priority flagged tier", access: "analyst" },
  { id: "page-state", title: "Open State Intelligence", page: "State Intelligence", icon: "🗺", hint: "State and House utilisation", keywords: "state map utilisation geography", access: "guest" },
  { id: "page-mp", title: "Open MP Intelligence", page: "MP Intelligence", icon: "👤", hint: "MP portfolios by House", keywords: "mp member parliament constituency", access: "guest" },
  { id: "page-vendor-network", title: "Open Vendor Network", page: "Vendor Network", icon: "🕸", hint: "Vendor relationships", keywords: "vendor network suppliers contractors", access: "analyst" },
  { id: "page-vendor-intel", title: "Open Vendor Intelligence", page: "Vendor Intelligence", icon: "🏢", hint: "Vendor performance and risk", keywords: "vendor intelligence contractor payments", access: "analyst" },
  { id: "page-audit-center", title: "Open AI Audit Center", page: "AI Audit Center", icon: "🛡", hint: "Automated audit workspace", keywords: "ai audit center cases forensics", access: "analyst" },
  { id: "page-reports", title: "Open Reports", page: "Reports", icon: "▣", hint: "Summaries and exports", keywords: "reports export download", access: "guest" },
  { id: "page-leaderboard", title: "Open Leaderboard", page: "Leaderboard", icon: "🏆", hint: "Ranked states and MPs", keywords: "leaderboard ranking top", access: "guest" },
  { id: "page-compare", title: "Compare Projects", page: "Compare Projects", icon: "⚖", hint: "Side-by-side project comparison", keywords: "compare projects side by side", access: "guest" },
  { id: "page-saved", title: "Open Saved Projects", page: "Saved Projects", icon: "🔖", hint: "Your bookmarked works", keywords: "saved bookmarks workspace", access: "analyst" },
  { id: "page-investigation", title: "Open Project Investigation", page: "My Investigations", icon: "🔍", hint: "Your investigations", keywords: "investigation case inquiry workspace", access: "analyst" },
  { id: "page-audit-cases", title: "Open My Audit Cases", page: "My Audit Cases", icon: "🗂", hint: "Your audit cases", keywords: "audit cases workspace", access: "auditor" },
  { id: "page-inquiries", title: "Open Inquiries", page: "Inquiries", icon: "✉", hint: "District inquiries", keywords: "inquiries letters respond", capability: "inquiry:review" },
  { id: "page-verifications", title: "Open My Verifications", page: "My Verifications", icon: "✅", hint: "Your verification submissions", keywords: "verification evidence field", capability: "verification:read_own" },
  { id: "page-evidence", title: "Open Evidence Queue", page: "Evidence Queue", icon: "📥", hint: "Verification review queue", keywords: "evidence queue review verify", capability: "verification:review" },
  { id: "page-district", title: "Open My District", page: "My District", icon: "📍", hint: "District authority workspace", keywords: "district authority", capability: "inquiry:respond" },
  { id: "page-ground", title: "Open Ground Verification", page: "Ground Truth Verification", icon: "🧭", hint: "Field verification portal", keywords: "field inspection verification ground", access: "guest" },
  { id: "page-admin", title: "Open Administration", page: "Administration", icon: "🛡", hint: "Users and roles", keywords: "admin users roles access", access: "admin" },
  { id: "page-data", title: "Open Data Management", page: "Data Management", icon: "🗄", hint: "Imports and dataset health", keywords: "import upload data dataset", access: "admin" },
  { id: "page-settings", title: "Open Settings", page: "Settings", icon: "⚙", hint: "Appearance and account", keywords: "settings theme dark light profile", access: "guest" },
]

// Commands that only exist because the current page can actually perform them.
const CONTEXT_BY_PAGE = {
  Overview: [
    { id: "ctx-ov-risk", title: "View high-risk projects", icon: "⚠", hint: "Risk Center lists every flagged work", page: "Risk Center", keywords: "high risk critical projects overview" },
    { id: "ctx-ov-state", title: "Open State Intelligence", icon: "🗺", hint: "State and House utilisation", page: "State Intelligence", keywords: "state intelligence utilisation" },
    { id: "ctx-ov-history", title: "Open historical import replay", icon: "🕘", hint: "Recorded imports only · State Intelligence", page: "State Intelligence", keywords: "recent dataset changes historical replay imports" },
  ],
  Projects: [
    { id: "ctx-pr-risk", title: "Filter high-risk projects", icon: "⚠", hint: "Continue in Risk Center", page: "Risk Center", keywords: "filter high risk projects" },
    { id: "ctx-pr-history", title: "Open historical data replay", icon: "🕘", hint: "Recorded imports only", page: "State Intelligence", keywords: "historical data replay changes" },
    { id: "ctx-pr-compare", title: "Compare project progress", icon: "⚖", hint: "Side-by-side comparison", page: "Compare Projects", keywords: "compare project progress" },
    { id: "ctx-pr-audit", title: "Review in the AI Audit Center", icon: "🛡", hint: "Automated audit workspace", page: "AI Audit Center", keywords: "audit review risk", access: "analyst" },
  ],
  "Risk Center": [
    { id: "ctx-rk-critical", title: "View critical projects", icon: "🔴", hint: "Flagged works in this list", page: "Risk Center", keywords: "critical projects high risk" },
    { id: "ctx-rk-emerging", title: "View emerging risk indicators", icon: "📈", hint: "Risk intelligence layer on this page", page: "Risk Center", keywords: "emerging risk indicators trends" },
    { id: "ctx-rk-patterns", title: "Compare risk patterns", icon: "⚖", hint: "Side-by-side projects", page: "Compare Projects", keywords: "compare risk patterns anomalies" },
    { id: "ctx-rk-priority", title: "Open Audit Priority", icon: "🎯", hint: "Ranked flagged works", page: "Audit Priority", keywords: "audit priority queue", access: "analyst" },
  ],
  "State Intelligence": [
    { id: "ctx-si-compare", title: "Compare states", icon: "🏆", hint: "Leaderboard ranking", page: "Leaderboard", keywords: "compare states ranking" },
    { id: "ctx-si-geo", title: "Open geospatial intelligence", icon: "🗺", hint: "Coverage, patterns and outliers", page: "State Intelligence", keywords: "geospatial outliers coverage" },
    { id: "ctx-si-projects", title: "Browse works in this state", icon: "▤", hint: "Projects filtered view", page: "Projects", keywords: "projects state filter" },
  ],
  "MP Intelligence": [
    { id: "ctx-mp-leader", title: "Compare MP performance", icon: "🏆", hint: "Leaderboard ranking", page: "Leaderboard", keywords: "compare mp performance" },
    { id: "ctx-mp-projects", title: "Browse MP works", icon: "▤", hint: "Projects list", page: "Projects", keywords: "mp works projects" },
  ],
  "Vendor Intelligence": [
    { id: "ctx-vi-network", title: "Open the Vendor Network", icon: "🕸", hint: "Relationship graph", page: "Vendor Network", keywords: "vendor network relationships" },
    { id: "ctx-vi-projects", title: "Open linked projects", icon: "▤", hint: "Browse the works catalog", page: "Projects", keywords: "vendor projects works" },
  ],
  "Vendor Network": [
    { id: "ctx-vn-intel", title: "Open Vendor Intelligence", icon: "🏢", hint: "Performance and risk detail", page: "Vendor Intelligence", keywords: "vendor intelligence performance" },
  ],
}

const ANALYTICAL = /^(show|find|list|which|what|how many|compare|give me|are there|projects (with|where|in)|vendors (with|linked)|districts (with|where))/i

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

function Highlight({ text, query }) {
  const value = String(text ?? "")
  const term = query.trim()
  if (term.length < 2) return value
  const index = value.toLowerCase().indexOf(term.toLowerCase())
  if (index < 0) return value
  return (
    <>
      {value.slice(0, index)}
      <mark className="rounded-[3px] bg-[#fadadd] px-0.5 text-[#8b0d23] dark:bg-[#5c1622] dark:text-[#f8dde0]">
        {value.slice(index, index + term.length)}
      </mark>
      {value.slice(index + term.length)}
    </>
  )
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
  const [query, setQuery] = useState("")
  const [records, setRecords] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  const [active, setActive] = useState(0)
  const [helpOpen, setHelpOpen] = useState(false)
  const [confirmId, setConfirmId] = useState(null)
  const [recents, setRecents] = useState(() => loadList("recent", user, MAX_RECENTS))
  const [pins, setPins] = useState(() => loadList("pins", user, MAX_PINS))
  const [terms, setTerms] = useState(() => loadList("terms", user, MAX_TERMS))
  const [ai, setAi] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState("")

  const close = useCallback(() => onOpenChange(false), [onOpenChange])

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

  // ── Global shortcut ────────────────────────────────────────────────────
  useEffect(() => {
    const onKeyDown = (event) => {
      const key = event.key.toLowerCase()
      if ((event.ctrlKey || event.metaKey) && !event.altKey && key === "k") {
        event.preventDefault()
        onOpenChange(!open)
        return
      }
      if (event.key === "Escape" && open) {
        event.preventDefault()
        if (helpOpen) setHelpOpen(false)
        else onOpenChange(false)
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [open, onOpenChange, helpOpen])

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
    const key = `${entry.type}:${entry.title}`
    const stored = loadList("recent", user, MAX_RECENTS)
    const next = [entry, ...stored.filter((r) => `${r.type}:${r.title}` !== key)].slice(0, MAX_RECENTS)
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

  // ── Debounced, abortable record search ────────────────────────────────
  useEffect(() => {
    if (!open) return
    const term = query.trim()
    const controller = new AbortController()
    let cancelled = false
    const timer = setTimeout(async () => {
      if (term.length < 2) {
        setRecords([])
        setLoading(false)
        setError("")
        return
      }
      setLoading(true)
      try {
        const data = await globalSearchRecords({
          q: term,
          limit: 5,
          house: house && house !== "All" ? house : undefined,
          signal: controller.signal,
        })
        if (cancelled) return
        setRecords(Array.isArray(data?.results) ? data.results : [])
        setError("")
      } catch {
        if (cancelled || controller.signal.aborted) return
        setRecords([])
        setError("Record search is unavailable right now. Navigation and quick actions still work.")
      } finally {
        if (!cancelled) setLoading(false)
      }
    }, term.length < 2 ? 0 : 220)
    return () => {
      cancelled = true
      clearTimeout(timer)
      controller.abort()
    }
  }, [query, open, house])

  // ── Natural-language question → existing assistant engine ─────────────
  const runAI = useCallback(async (question) => {
    const text = question.trim()
    if (text.length < 6) return
    setAiLoading(true)
    setAiError("")
    try {
      const data = await askAssistant(text, currentPage, null, house && house !== "All" ? house : null)
      setAi({
        question: text,
        answer: data?.answer || "",
        actions: Array.isArray(data?.actions) ? data.actions : [],
        table: data?.table || null,
        source: data?.source || "",
      })
      rememberTerm(text)
    } catch {
      setAiError("The assistant could not answer that just now. Record search is unaffected.")
    } finally {
      setAiLoading(false)
    }
  }, [currentPage, house, rememberTerm])

  useEffect(() => {
    if (!open) return
    const term = query.trim()
    if (!looksAnalytical(term)) return
    const timer = setTimeout(() => { runAI(term) }, 550)
    return () => clearTimeout(timer)
  }, [query, open, runAI])

  // ── Commands ───────────────────────────────────────────────────────────
  const pageItems = useMemo(
    () =>
      PAGE_COMMANDS.filter(canSee).map((command) => ({
        id: command.id,
        kind: "command",
        group: "Navigation",
        icon: command.icon,
        title: command.title,
        subtitle: command.hint,
        keywords: command.keywords,
        page: command.page,
        pinnable: true,
        run: () => onNavigate?.(command.page),
        recent: { type: "page", title: command.title, subtitle: command.hint, destination: { page: command.page } },
      })),
    [canSee, onNavigate]
  )

  const quickActions = useMemo(() => {
    const items = [
      {
        id: "action-theme",
        kind: "action",
        group: "Quick actions",
        icon: darkMode ? "☀" : "☾",
        title: darkMode ? "Switch to light theme" : "Switch to dark theme",
        subtitle: "Appearance only — data and filters stay as they are",
        keywords: "theme dark light appearance toggle",
        pinnable: true,
        run: () => onToggleTheme?.(),
      },
      {
        id: "action-assistant",
        kind: "action",
        group: "Quick actions",
        icon: "✦",
        title: "Open the AI Assistant",
        subtitle: "Ask a follow-up with the existing assistant",
        keywords: "ai assistant ask chat question",
        pinnable: true,
        run: () => window.dispatchEvent(new CustomEvent("assistant:open")),
      },
      {
        id: "action-help",
        kind: "action",
        group: "Quick actions",
        icon: "⌨",
        title: "Keyboard shortcuts",
        subtitle: "Every key the palette understands",
        keywords: "help shortcuts keys command help",
        run: () => setHelpOpen(true),
      },
      {
        id: "action-settings",
        kind: "action",
        group: "Quick actions",
        icon: "⚙",
        title: "Open Settings",
        subtitle: "Appearance, account and preferences",
        keywords: "settings preferences account",
        run: () => onNavigate?.("Settings"),
      },
      {
        id: "action-clear",
        kind: "action",
        group: "Quick actions",
        icon: "🧹",
        title: "Clear active page filters",
        subtitle: "Resets the current page's own filter state",
        keywords: "clear filters reset search",
        danger: true,
        run: () => window.dispatchEvent(new CustomEvent("assistant:clear-filters")),
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
        icon: "🏛",
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
        icon: "📅",
        title: "Clear financial-year filter",
        subtitle: `Currently ${fy} — return to every recorded year`,
        keywords: "financial year fy clear period",
        run: () => onFYChange?.(""),
      })
    }

    return items
  }, [darkMode, house, fy, onToggleTheme, onNavigate, onHouseChange, onFYChange])

  const contextItems = useMemo(() => {
    const items = []
    for (const command of (CONTEXT_BY_PAGE[currentPage] || []).filter(canSee)) {
      items.push({
        id: command.id,
        kind: "command",
        group: "On this page",
        icon: command.icon,
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
          icon: "📁",
          title: `Investigate project #${contextProjectId}`,
          subtitle: "Open the project drawer with its existing detail views",
          keywords: "investigate selected project open",
          run: () => onOpenProject?.(contextProjectId),
        },
        {
          id: "ctx-record-changes",
          kind: "action",
          group: "On this page",
          icon: "🕘",
          title: "View this project's recorded changes",
          subtitle: `Project #${contextProjectId} · recorded imports only`,
          keywords: "history changes previous current progress compare",
          run: () => window.dispatchEvent(new CustomEvent("open-project", { detail: { projectId: contextProjectId, tab: "changes" } })),
        },
        {
          id: "ctx-record-vendor",
          kind: "action",
          group: "On this page",
          icon: "🏢",
          title: "View associated vendors",
          subtitle: "Vendor Intelligence · payments recorded against works",
          keywords: "related vendor expenditure payments",
          run: () => onNavigate?.("Vendor Intelligence"),
        },
        {
          id: "ctx-record-ask",
          kind: "action",
          group: "On this page",
          icon: "✦",
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
        icon: "✦",
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
        icon: TYPE_META[record.type]?.icon || "•",
        title: record.title,
        subtitle: record.subtitle,
        meta: record.meta,
        keywords: "",
        recordType: record.type,
        run: () => {
          if (!record.destination) return
          const params = { ...(record.destination.params || {}) }
          const projectId = params.projectId
          delete params.projectId
          if (projectId) onOpenProject?.(projectId)
          else if (Object.keys(params).length) onDrillDown?.(record.destination.page, params)
          else onNavigate?.(record.destination.page)
        },
        recent: { type: record.type, title: record.title, subtitle: record.subtitle, destination: record.destination },
      })),
    [records, onNavigate, onDrillDown, onOpenProject]
  )

  const pinnedItems = useMemo(() => {
    const index = new Map()
    for (const item of [...contextItems, ...quickActions, ...pageItems]) index.set(item.id, item)
    return pins.map((id) => index.get(id)).filter(Boolean)
  }, [pins, contextItems, quickActions, pageItems])

  const recentItems = useMemo(
    () =>
      recents.map((entry) => ({
        id: `recent-${entry.type}-${entry.title}`,
        kind: "recent",
        group: "Recent activity",
        icon: TYPE_META[entry.type]?.icon || "🕘",
        title: entry.title,
        subtitle: entry.subtitle,
        keywords: "",
        recentId: `${entry.type}:${entry.title}`,
        run: () => {
          const destination = entry.destination || {}
          const params = { ...(destination.params || {}) }
          const projectId = params.projectId
          delete params.projectId
          if (projectId) onOpenProject?.(projectId)
          else if (Object.keys(params).length) onDrillDown?.(destination.page, params)
          else if (destination.page) onNavigate?.(destination.page)
        },
      })),
    [recents, onNavigate, onDrillDown, onOpenProject]
  )

  const termItems = useMemo(
    () =>
      terms.map((term) => ({
        id: `term-${term}`,
        kind: "term",
        group: "Recent searches",
        icon: "🕘",
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
        score: Math.max(matchScore(term, item.title), matchScore(term, item.keywords || "") - 10),
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
    return out
  }, [query, helpOpen, pinnedItems, recentItems, termItems, contextItems, quickActions, pageItems, recordItems, ai, aiItems, rank])

  const flat = useMemo(() => sections.flatMap((section) => section.items), [sections])
  const clampedActive = Math.min(active, Math.max(0, flat.length - 1))
  const activeItem = flat[clampedActive]

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

  const execute = useCallback(
    (item) => {
      if (!item) return
      if (item.danger && confirmId !== item.id) {
        setConfirmId(item.id)
        return
      }
      setConfirmId(null)
      if (item.recent) remember(item.recent)
      close()
      item.run?.()
    },
    [confirmId, remember, close]
  )

  const jumpGroup = (direction) => {
    if (!flat.length) return
    const currentSection = sections.findIndex((section) =>
      section.items.some((_, index) => sections
        .slice(0, sections.indexOf(section))
        .reduce((total, s) => total + s.items.length, 0) + index === clampedActive)
    )
    const nextIndex = Math.max(0, Math.min(sections.length - 1, currentSection + direction))
    let offset = 0
    for (let i = 0; i < nextIndex; i += 1) offset += sections[i].items.length
    setActive(offset)
  }

  const onKeyDown = (event) => {
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
    } else if (event.key === "Enter") {
      event.preventDefault()
      if (event.shiftKey && term.length >= 6) {
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
      inputRef.current?.focus()
    }
  }

  if (!open) return null

  let runningIndex = -1
  const activeId = flat.length ? `command-option-${clampedActive}` : undefined

  return (
    <div
      className={`fixed inset-0 z-[95] flex items-end justify-center sm:items-start sm:px-3 sm:pt-[10vh] ${darkMode ? "dark" : ""}`}
    >
      <div className="absolute inset-0 bg-black/45 dark:bg-black/70" onClick={close} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onKeyDown}
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
        className="pop-in-down relative flex max-h-[90dvh] w-full flex-col overflow-hidden rounded-t-2xl border border-[#e8dadc] bg-white shadow-soft-lg sm:max-h-[76vh] sm:max-w-3xl sm:rounded-2xl dark:border-[#351923] dark:bg-[#0d090b]"
      >
        <div className="mx-auto mt-2 h-1 w-10 rounded-full bg-[#e8dadc] sm:hidden dark:bg-[#351923]" />

        <div className="flex items-center gap-3 border-b border-[#f0e6e7] px-4 dark:border-[#2a141a]">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="h-4 w-4 flex-none text-[#7a6f74] dark:text-[#b9aeb2]">
            <circle cx="11" cy="11" r="8" />
            <path d="m21 21-4.3-4.3" />
          </svg>
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
            placeholder="Search projects, MPs, vendors, payments, states… or ask a question"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value)
              setActive(0)
              setConfirmId(null)
            }}
            className="h-12 min-w-0 flex-1 bg-transparent text-sm text-[#292326] outline-none placeholder:text-[#b9aeb2] dark:text-[#f5eff1] dark:placeholder:text-[#82767b]"
          />
          <span className="hidden flex-none items-center gap-1 sm:flex">
            <Kbd>Esc</Kbd>
          </span>
        </div>

        <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,1fr)_15rem]">
          <div ref={listRef} id="command-listbox" role="listbox" aria-label="Commands and records" className="max-h-[56vh] overflow-y-auto overscroll-contain py-1 lg:max-h-[58vh]">
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
              <div className="flex items-center gap-2 px-4 py-2.5 text-xs text-[#7a6f74] dark:text-[#b9aeb2]" role="status">
                <span className="h-3 w-3 animate-spin rounded-full border-2 border-[#e8dadc] border-t-[#c8102e] dark:border-[#351923] dark:border-t-[#e2a1ab]" />
                {aiLoading ? "Asking the assistant…" : "Searching records…"}
              </div>
            )}

            {helpOpen && (
              <div className="space-y-2 px-4 py-3 text-xs text-[#5e5257] dark:text-[#b9aeb2]">
                {[
                  ["Ctrl / ⌘ + K", "Open or close the palette"],
                  ["Esc", "Close (or leave this help)"],
                  ["↑ / ↓", "Move through results"],
                  ["Alt + ↑ / ↓", "Jump to the previous or next category"],
                  ["Enter", "Run the highlighted command"],
                  ["Shift + Enter", "Ask the typed question to the assistant"],
                  ["Tab", "Return focus to the search field"],
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
                <div className="px-4 pb-1 pt-3 text-[0.625rem] font-bold uppercase tracking-wider text-[#b9aeb2] dark:text-[#82767b]">
                  {section.title}
                </div>

                {section.answer && (
                  <div className="mx-3 mb-2 rounded-lg border border-[#f0e6e7] bg-[#faf7f6] p-2.5 text-xs leading-snug text-[#3a3135] dark:border-[#2a141a] dark:bg-[#140c10] dark:text-[#ece4e6]">
                    <p className="whitespace-pre-line">{section.answer.answer}</p>
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
                      onClick={() => execute(item)}
                      className={`group flex w-full cursor-pointer items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                        isActive
                          ? "bg-[#fdf2f3] shadow-[inset_2px_0_0_#c8102e] dark:bg-[#1a0f13] dark:shadow-[inset_2px_0_0_#8b2233]"
                          : "hover:bg-[#faf7f6] dark:hover:bg-[#140c10]"
                      }`}
                    >
                      <span aria-hidden="true" className={`flex h-8 w-8 flex-none items-center justify-center rounded-lg text-sm ${isActive ? "bg-[#c8102e] text-white dark:bg-[#5c1622]" : "bg-[#f5efef] text-[#7a6f74] dark:bg-[#170d11] dark:text-[#b9aeb2]"}`}>
                        {item.icon}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium text-[#292326] dark:text-[#f5eff1]">
                          <Highlight text={item.title} query={query} />
                        </span>
                        {item.subtitle && (
                          <span className="block truncate text-xs text-[#7a6f74] dark:text-[#b9aeb2]">
                            {confirmId === item.id ? "Press Enter again to confirm" : item.subtitle}
                          </span>
                        )}
                      </span>
                      {item.checked && <span aria-hidden="true" className="flex-none text-xs text-[#c8102e] dark:text-[#e2a1ab]">✓</span>}
                      {item.meta && <span className="flex-none text-xs font-medium text-[#7a6f74] dark:text-[#b9aeb2]">{item.meta}</span>}

                      {item.kind === "recent" || item.kind === "term" ? (
                        <button
                          type="button"
                          aria-label={item.kind === "term" ? "Remove this recent search" : "Remove from recent activity"}
                          onClick={(event) => {
                            event.stopPropagation()
                            if (item.kind === "term") {
                              setTerms((prev) => {
                                const next = prev.filter((t) => t !== item.recentTerm)
                                saveList("terms", user, next)
                                return next
                              })
                            } else {
                              removeRecent(item.recentId)
                            }
                          }}
                          className="flex-none rounded-sm p-1 text-[#b9aeb2] opacity-0 transition hover:bg-black/5 focus:opacity-100 group-hover:opacity-100 dark:hover:bg-white/10"
                        >
                          ✕
                        </button>
                      ) : item.pinnable ? (
                        <button
                          type="button"
                          aria-label={pinned ? "Unpin this command" : "Pin this command"}
                          onClick={(event) => {
                            event.stopPropagation()
                            togglePin(item)
                          }}
                          className={`flex-none rounded-sm p-1 transition hover:bg-black/5 dark:hover:bg-white/10 ${pinned ? "text-[#c8102e] opacity-100 dark:text-[#e2a1ab]" : "text-[#b9aeb2] opacity-0 focus:opacity-100 group-hover:opacity-100"}`}
                        >
                          {pinned ? "★" : "☆"}
                        </button>
                      ) : null}

                      {isActive && <span aria-hidden="true" className="flex-none text-xs text-[#c8102e] dark:text-[#e2a1ab]">↵</span>}
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
                <div className="mt-1 text-xs text-[#7a6f74] dark:text-[#b9aeb2]">
                  {query.trim()
                    ? "Try a project ID, a vendor, a constituency, or press Shift+Enter to ask the assistant."
                    : "Projects, MPs, vendors, payments, states, investigations and commands."}
                </div>
              </div>
            )}
          </div>

          {/* Desktop preview: what the highlighted row will open. */}
          <aside className="hidden border-l border-[#f0e6e7] px-4 py-4 lg:block dark:border-[#2a141a]" aria-hidden="true">
            {activeItem ? (
              <div className="space-y-2">
                <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-[#f5efef] text-base text-[#7a6f74] dark:bg-[#170d11] dark:text-[#b9aeb2]">{activeItem.icon}</span>
                <p className="text-sm font-bold leading-snug text-[#292326] dark:text-[#f5eff1]">{activeItem.title}</p>
                {activeItem.subtitle && <p className="text-xs leading-relaxed text-[#7a6f74] dark:text-[#b9aeb2]">{activeItem.subtitle}</p>}
                {activeItem.meta && <p className="font-mono text-xs font-semibold text-[#292326] dark:text-[#f5eff1]">{activeItem.meta}</p>}
                <p className="pt-2 text-[0.625rem] uppercase tracking-wider text-[#b9aeb2] dark:text-[#82767b]">{activeItem.group}</p>
                <p className="text-[0.6875rem] text-[#9a8b90]">Press ↵ to open. Records open their existing page or drawer.</p>
              </div>
            ) : (
              <p className="text-xs text-[#9a8b90]">Highlight a row to preview it here.</p>
            )}
          </aside>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[#f0e6e7] px-4 py-2 text-[0.6875rem] text-[#7a6f74] dark:border-[#2a141a] dark:text-[#b9aeb2]">
          <span className="flex flex-wrap items-center gap-3">
            <span className="flex items-center gap-1"><Kbd>↑</Kbd><Kbd>↓</Kbd> move</span>
            <span className="flex items-center gap-1"><Kbd>↵</Kbd> open</span>
            <span className="hidden items-center gap-1 sm:flex"><Kbd>⇧↵</Kbd> ask AI</span>
            <span className="hidden items-center gap-1 md:flex"><Kbd>⌥↑↓</Kbd> categories</span>
          </span>
          <span className="flex items-center gap-1"><Kbd>Ctrl</Kbd><Kbd>K</Kbd><span className="ml-1">anywhere</span></span>
        </div>
      </div>
    </div>
  )
}
