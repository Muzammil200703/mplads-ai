import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react"
import { useAuth } from "../context/AuthContext"
import { globalSearchRecords } from "../services/api"

/* Global command palette (⌘K / Ctrl+K).
   One interface over the app that ALREADY exists: pages come from the same
   page registry the sidebar drives, records come from the backend, and every
   selection is executed through the shell's own handlers (onNavigate /
   onDrillDown / onOpenProject). No second router, no parallel navigation.

   Accessibility: WAI-ARIA combobox + listbox, aria-activedescendant, modal
   focus handling (focus starts in the input and Tab is contained), Escape
   closes, and every row is a real <button> so it works with a screen reader
   and without a keyboard shortcut. */

const RECENT_KEY = "mplads_command_recent_v1"
const RECENT_LIMIT = 8

const TYPE_ICONS = {
  project: "▤",
  mp: "👤",
  vendor: "🏢",
  constituency: "📍",
  state: "🗺",
  investigation: "🔍",
  page: "▦",
  action: "⚡",
}

// Navigation commands — page keys mirror App.jsx exactly.
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

// Quick actions backed by workflows that already exist.
const ACTION_COMMANDS = [
  { id: "action-assistant", title: "Ask the AI Assistant", icon: "✦", hint: "Open the assistant panel", keywords: "ai assistant ask chat question", access: "guest", action: "assistant" },
  { id: "action-field", title: "Create a field inspection", icon: "🧭", hint: "Start in the Ground Verification portal", keywords: "inspection field create verify", access: "guest", action: "navigate", page: "Ground Truth Verification" },
  { id: "action-investigation", title: "Open a new investigation", icon: "🔍", hint: "Continue in Audit Priority", keywords: "investigation new case open", access: "analyst", action: "navigate", page: "Audit Priority" },
  { id: "action-compare", title: "Compare projects", icon: "⚖", hint: "Pick two works side by side", keywords: "compare projects difference", access: "guest", action: "navigate", page: "Compare Projects" },
  { id: "action-investigations", title: "View recent investigations", icon: "🗂", hint: "Your investigation workspace", keywords: "recent investigations cases", access: "analyst", action: "navigate", page: "My Investigations" },
]

// Context commands: only what the current page can actually do.
const CONTEXT_COMMANDS = {
  Projects: [
    { id: "ctx-projects-compare", title: "Compare these projects", icon: "⚖", hint: "Open Compare Projects", keywords: "compare projects", action: "navigate", page: "Compare Projects" },
    { id: "ctx-projects-audit", title: "Review in the AI Audit Center", icon: "🛡", hint: "Automated audit workspace", keywords: "audit review risk", access: "analyst", action: "navigate", page: "AI Audit Center" },
  ],
  "Risk Center": [
    { id: "ctx-risk-priority", title: "Open Audit Priority", icon: "🎯", hint: "Rank the flagged works", keywords: "risk priority audit", access: "analyst", action: "navigate", page: "Audit Priority" },
    { id: "ctx-risk-audit", title: "Review in the AI Audit Center", icon: "🛡", hint: "Automated audit workspace", keywords: "audit review", access: "analyst", action: "navigate", page: "AI Audit Center" },
  ],
  "Vendor Intelligence": [
    { id: "ctx-vendor-network", title: "Open the Vendor Network", icon: "🕸", hint: "Relationship graph", keywords: "vendor network relationships", action: "navigate", page: "Vendor Network" },
  ],
  "Vendor Network": [
    { id: "ctx-vendor-intel", title: "Open Vendor Intelligence", icon: "🏢", hint: "Performance and risk detail", keywords: "vendor intelligence performance", action: "navigate", page: "Vendor Intelligence" },
  ],
}

function readRecent() {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]")
    return Array.isArray(parsed) ? parsed.filter((r) => r && r.type && r.title).slice(0, RECENT_LIMIT) : []
  } catch {
    return []
  }
}

function persistRecent(list) {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_LIMIT)))
  } catch {
    /* storage unavailable — recents simply stay in memory */
  }
}

// Recent activity lives in a tiny external store: pages/projects are
// remembered from effects, and remembering must not schedule a React state
// update from inside an effect body.
let recentSnapshot = readRecent()
const recentListeners = new Set()
const recentStore = {
  subscribe(listener) {
    recentListeners.add(listener)
    return () => recentListeners.delete(listener)
  },
  getSnapshot() {
    return recentSnapshot
  },
  remember(entry) {
    if (!entry || !entry.title) return
    const key = `${entry.type}:${entry.title}`
    const next = [entry, ...recentSnapshot.filter((r) => `${r.type}:${r.title}` !== key)].slice(0, RECENT_LIMIT)
    recentSnapshot = next
    persistRecent(next)
    recentListeners.forEach((listener) => listener())
  },
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

function Kbd({ children }) {
  return (
    <kbd className="rounded border border-[#e8dadc] bg-[#faf7f6] px-1.5 py-0.5 font-sans text-[0.625rem] font-semibold text-[#7a6f74] dark:border-[#351923] dark:bg-[#140c10] dark:text-[#b9aeb2]">
      {children}
    </kbd>
  )
}

export default function CommandPalette({
  open,
  onOpenChange,
  currentPage,
  house,
  onNavigate,
  onDrillDown,
  onOpenProject,
}) {
  const { user, hasRole, can } = useAuth()
  const inputRef = useRef(null)
  const listRef = useRef(null)
  const [query, setQuery] = useState("")
  const [records, setRecords] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [active, setActive] = useState(0)
  const recent = useSyncExternalStore(recentStore.subscribe, recentStore.getSnapshot)

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

  // ── Global shortcut: Ctrl+K (Windows/Linux) and Cmd+K (macOS) ─────────
  useEffect(() => {
    const onKeyDown = (event) => {
      const key = event.key.toLowerCase()
      if ((event.ctrlKey || event.metaKey) && !event.altKey && key === "k") {
        // Always take over the chord: browsers use Ctrl+K for their own
        // search bar, which would otherwise swallow the shortcut.
        event.preventDefault()
        onOpenChange(!open)
        return
      }
      if (event.key === "Escape" && open) {
        event.preventDefault()
        onOpenChange(false)
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [open, onOpenChange])

  // ── Open/close lifecycle: reset, focus, lock page scroll ──────────────
  useEffect(() => {
    if (!open) return
    const previous = document.activeElement
    document.body.style.overflow = "hidden"
    const focusTimer = setTimeout(() => inputRef.current?.focus(), 30)
    return () => {
      clearTimeout(focusTimer)
      document.body.style.overflow = ""
      if (previous instanceof HTMLElement) previous.focus?.()
    }
  }, [open])

  // ── Which project is "current"? The last one opened anywhere in the app.
  const lastProjectId = useMemo(() => {
    const project = recent.find((entry) => entry.type === "project" && entry.destination?.projectId)
    return project?.destination?.projectId ?? null
  }, [recent])

  // ── Recent activity: pages visited and projects opened ────────────────
  useEffect(() => {
    if (!currentPage || ["Sign in", "Sign up", "Landing"].includes(currentPage)) return
    recentStore.remember({ type: "page", title: currentPage, subtitle: "Page", destination: { page: currentPage } })
  }, [currentPage])

  useEffect(() => {
    const remember = (event) => {
      const projectId = event.detail?.projectId
      if (!projectId) return
      recentStore.remember({
        type: "project",
        title: `Project #${projectId}`,
        subtitle: "Project",
        destination: { page: "Projects", projectId },
      })
    }
    window.addEventListener("open-project", remember)
    return () => window.removeEventListener("open-project", remember)
  }, [])

  // ── Debounced, abortable record search ───────────────────────────────
  useEffect(() => {
    if (!open) return
    const term = query.trim()
    const controller = new AbortController()
    let cancelled = false
    const timer = setTimeout(async () => {
      if (term.length < 2) {
        setRecords([])
        setLoading(false)
        setError(null)
        return
      }
      setLoading(true)
      try {
        const data = await globalSearchRecords({
          q: term,
          limit: 6,
          house: house && house !== "All" ? house : undefined,
          signal: controller.signal,
        })
        if (cancelled) return
        setRecords(Array.isArray(data?.results) ? data.results : [])
        setError(null)
      } catch {
        if (cancelled || controller.signal.aborted) return
        setRecords([])
        setError("Record search is unavailable right now. Navigation commands still work.")
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

  const openDestination = useCallback(
    (destination) => {
      if (!destination) return
      // The backend may express "open this project's drawer" either at the
      // top level or inside params — accept both, never leak projectId into
      // the page's own filter params.
      const projectId = destination.projectId ?? destination.params?.projectId
      if (projectId) {
        onOpenProject?.(projectId)
        return
      }
      const params = { ...(destination.params || {}) }
      delete params.projectId
      if (Object.keys(params).length > 0 && destination.page) {
        onDrillDown?.(destination.page, params)
        return
      }
      if (destination.page) onNavigate?.(destination.page)
    },
    [onOpenProject, onDrillDown, onNavigate]
  )

  const pages = useMemo(
    () =>
      PAGE_COMMANDS.filter(canSee).map((command) => ({
        id: command.id,
        group: "Pages",
        icon: command.icon,
        title: command.title,
        subtitle: command.hint,
        keywords: command.keywords,
        run: () => onNavigate?.(command.page),
        recent: { type: "page", title: command.title, subtitle: command.hint, destination: { page: command.page } },
      })),
    [canSee, onNavigate]
  )

  const actions = useMemo(
    () =>
      ACTION_COMMANDS.filter(canSee).map((command) => ({
        id: command.id,
        group: "Actions",
        icon: command.icon,
        title: command.title,
        subtitle: command.hint,
        keywords: command.keywords,
        run: () => {
          if (command.action === "assistant") {
            window.dispatchEvent(new CustomEvent("assistant:open"))
            return
          }
          onNavigate?.(command.page)
        },
        recent: { type: "action", title: command.title, subtitle: command.hint, destination: { page: command.page } },
      })),
    [canSee, onNavigate]
  )

  const contextItems = useMemo(() => {
    const items = []
    const contextual = (CONTEXT_COMMANDS[currentPage] || []).filter(canSee)
    for (const command of contextual) {
      items.push({
        id: command.id,
        group: "On this page",
        icon: command.icon,
        title: command.title,
        subtitle: command.hint,
        keywords: command.keywords,
        run: () => onNavigate?.(command.page),
        recent: { type: "action", title: command.title, subtitle: command.hint, destination: { page: command.page } },
      })
    }
    if (currentPage === "Projects" && lastProjectId) {
      items.push({
        id: "ctx-project-history",
        group: "On this page",
        icon: "🕘",
        title: "View this project's change history",
        subtitle: `Project #${lastProjectId} · recorded import changes`,
        keywords: "history changes timeline import project",
        run: () =>
          window.dispatchEvent(
            new CustomEvent("open-project", { detail: { projectId: lastProjectId, tab: "changes" } })
          ),
      })
      items.push({
        id: "ctx-project-inspection",
        group: "On this page",
        icon: "🧭",
        title: "Start a field inspection",
        subtitle: `Project #${lastProjectId} · Ground Verification`,
        keywords: "inspection field verify project",
        run: () => onNavigate?.("Ground Truth Verification"),
      })
    }
    return items
  }, [currentPage, canSee, onNavigate, lastProjectId])

  const recordItems = useMemo(
    () =>
      records.map((record) => ({
        id: `record-${record.id}`,
        group: "Records",
        icon: TYPE_ICONS[record.type] || "•",
        title: record.title,
        subtitle: record.subtitle,
        meta: record.meta,
        type: record.type,
        run: () => openDestination(record.destination),
        recent: {
          type: record.type,
          title: record.title,
          subtitle: record.subtitle,
          destination: record.destination,
        },
      })),
    [records, openDestination]
  )

  // ── Assemble visible rows (with ranking) ─────────────────────────────
  const sections = useMemo(() => {
    const term = query.trim()
    const rank = (items) => {
      if (!term) return items
      return items
        .map((item) => ({
          item,
          score: Math.max(matchScore(term, item.title), matchScore(term, item.keywords || "") - 10),
        }))
        .filter((entry) => entry.score > 0)
        .sort((a, b) => b.score - a.score)
        .map((entry) => entry.item)
    }

    const out = []
    if (!term) {
      const recents = recent
        .map((entry) => ({
          id: `recent-${entry.type}-${entry.title}`,
          group: "Recent",
          icon: TYPE_ICONS[entry.type] || "🕘",
          title: entry.title,
          subtitle: entry.subtitle,
          run: () => openDestination(entry.destination),
          recent: entry,
        }))
        .slice(0, 5)
      if (recents.length) out.push({ title: "Recent", items: recents })
      out.push({ title: "Pages", items: pages })
      out.push({ title: "Actions", items: actions })
      if (contextItems.length) out.push({ title: "On this page", items: contextItems })
      return out
    }

    const rankedContext = rank(contextItems)
    if (rankedContext.length) out.push({ title: "On this page", items: rankedContext })
    const rankedActions = rank(actions)
    if (rankedActions.length) out.push({ title: "Actions", items: rankedActions })
    const rankedPages = rank(pages)
    if (rankedPages.length) out.push({ title: "Pages", items: rankedPages })
    if (recordItems.length) out.push({ title: "Records", items: recordItems })
    return out
  }, [query, recent, pages, actions, contextItems, recordItems, openDestination])

  const flat = useMemo(() => sections.flatMap((section) => section.items), [sections])
  // Derived, not stored: the highlight can never point past a shrinking list.
  const clampedActive = Math.min(active, Math.max(0, flat.length - 1))

  useEffect(() => {
    if (!open || !listRef.current) return
    const node = listRef.current.querySelector(`[data-index="${clampedActive}"]`)
    node?.scrollIntoView({ block: "nearest" })
  }, [clampedActive, open])

  const execute = useCallback(
    (item) => {
      if (!item) return
      if (item.recent) recentStore.remember(item.recent)
      close()
      item.run?.()
    },
    [close]
  )

  const onListKeyDown = (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault()
      setActive((prev) => (flat.length ? (Math.min(prev, flat.length - 1) + 1) % flat.length : 0))
    } else if (event.key === "ArrowUp") {
      event.preventDefault()
      setActive((prev) =>
        flat.length ? (Math.min(prev, flat.length - 1) - 1 + flat.length) % flat.length : 0
      )
    } else if (event.key === "Enter") {
      event.preventDefault()
      execute(flat[clampedActive])
    } else if (event.key === "Home" && event.target === inputRef.current) {
      event.preventDefault()
      setActive(0)
    } else if (event.key === "End" && event.target === inputRef.current) {
      event.preventDefault()
      setActive(Math.max(0, flat.length - 1))
    } else if (event.key === "Tab") {
      // Focus containment: the dialog owns the keyboard while open.
      event.preventDefault()
      inputRef.current?.focus()
    }
  }

  if (!open) return null

  let runningIndex = -1
  const activeId = flat.length ? `command-option-${clampedActive}` : undefined

  return (
    <div className="fixed inset-0 z-[95] flex items-start justify-center px-3 pt-[8vh] sm:pt-[12vh]">
      <div
        className="absolute inset-0 bg-black/45 dark:bg-black/70"
        onClick={close}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onListKeyDown}
        className="pop-in-down relative flex w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-[#e8dadc] bg-white shadow-soft dark:border-[#351923] dark:bg-[#0d090b]"
      >
        <div className="flex items-center gap-3 border-b border-[#f0e6e7] px-4 dark:border-[#2a141a]">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
            className="h-4 w-4 flex-none text-[#7a6f74] dark:text-[#b9aeb2]"
          >
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
            aria-label="Search pages, records and actions"
            autoComplete="off"
            spellCheck="false"
            placeholder="Search pages, projects, MPs, vendors, states…"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value)
              setActive(0)
            }}
            className="h-12 min-w-0 flex-1 bg-transparent text-sm text-[#292326] outline-none placeholder:text-[#b9aeb2] dark:text-[#f5eff1] dark:placeholder:text-[#82767b]"
          />
          <span className="hidden flex-none items-center gap-1 sm:flex">
            <Kbd>Esc</Kbd>
          </span>
        </div>

        <div
          ref={listRef}
          id="command-listbox"
          role="listbox"
          aria-label="Commands and records"
          className="max-h-[58vh] overflow-y-auto overscroll-contain py-1"
        >
          {error && (
            <div
              role="status"
              className="mx-3 mt-2 rounded-lg border border-[#f0d6d9] bg-[#fdf2f3] px-3 py-2 text-xs text-[#8a2b36] dark:border-[#3a1b22] dark:bg-[#1a0f13] dark:text-[#e2a1ab]"
            >
              {error}
            </div>
          )}

          {loading && (
            <div className="flex items-center gap-2 px-4 py-3 text-xs text-[#7a6f74] dark:text-[#b9aeb2]" role="status">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-[#e8dadc] border-t-[#c8102e] dark:border-[#351923] dark:border-t-[#e2a1ab]" />
              Searching records…
            </div>
          )}

          {sections.map((section) => (
            <div key={section.title} role="group" aria-label={section.title}>
              <div className="px-4 pb-1 pt-3 text-[0.625rem] font-bold uppercase tracking-wider text-[#b9aeb2] dark:text-[#82767b]">
                {section.title}
              </div>
              {section.items.map((item) => {
                runningIndex += 1
                const index = runningIndex
                const isActive = index === clampedActive
                return (
                  <button
                    key={item.id}
                    id={`command-option-${index}`}
                    data-index={index}
                    role="option"
                    type="button"
                    aria-selected={isActive}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => execute(item)}
                    className={`flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                      isActive
                        ? "bg-[#fdf2f3] shadow-[inset_2px_0_0_#c8102e] dark:bg-[#1a0f13] dark:shadow-[inset_2px_0_0_#8b2233]"
                        : "hover:bg-[#faf7f6] dark:hover:bg-[#140c10]"
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className={`flex h-8 w-8 flex-none items-center justify-center rounded-lg text-sm ${
                        isActive
                          ? "bg-[#c8102e] text-white dark:bg-[#5c1622]"
                          : "bg-[#f5efef] text-[#7a6f74] dark:bg-[#170d11] dark:text-[#b9aeb2]"
                      }`}
                    >
                      {item.icon}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-[#292326] dark:text-[#f5eff1]">
                        {item.title}
                      </span>
                      {item.subtitle && (
                        <span className="block truncate text-xs text-[#7a6f74] dark:text-[#b9aeb2]">
                          {item.subtitle}
                        </span>
                      )}
                    </span>
                    {item.meta && (
                      <span className="flex-none text-xs font-medium text-[#7a6f74] dark:text-[#b9aeb2]">
                        {item.meta}
                      </span>
                    )}
                    {isActive && (
                      <span aria-hidden="true" className="flex-none text-xs text-[#c8102e] dark:text-[#e2a1ab]">
                        ↵
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          ))}

          {!loading && !error && flat.length === 0 && (
            <div className="px-4 py-10 text-center" role="status">
              <div className="text-sm font-medium text-[#292326] dark:text-[#f5eff1]">
                {query.trim() ? `No matches for “${query.trim()}”` : "Start typing to search"}
              </div>
              <div className="mt-1 text-xs text-[#7a6f74] dark:text-[#b9aeb2]">
                {query.trim()
                  ? "Try a project ID, an MP or constituency name, or a vendor."
                  : "Pages, projects, MPs, vendors, constituencies, states and investigations."}
              </div>
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[#f0e6e7] px-4 py-2 text-[0.6875rem] text-[#7a6f74] dark:border-[#2a141a] dark:text-[#b9aeb2]">
          <span className="flex items-center gap-3">
            <span className="flex items-center gap-1">
              <Kbd>↑</Kbd>
              <Kbd>↓</Kbd> to navigate
            </span>
            <span className="flex items-center gap-1">
              <Kbd>↵</Kbd> to open
            </span>
          </span>
          <span className="flex items-center gap-1">
            <Kbd>Ctrl</Kbd>
            <Kbd>K</Kbd>
            <span className="ml-1">anywhere</span>
          </span>
        </div>
      </div>
    </div>
  )
}
