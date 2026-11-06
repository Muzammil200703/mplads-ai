import { useEffect, useRef, useState } from "react"
import { useAuth } from "../context/AuthContext"
import { askAssistant, getAssistantSuggestions, getAssistantContextHelp } from "../services/api"

/* ═══════════════════════════════════════════════════════════════════
   MPLADS AI ASSISTANT — universal role-aware widget (floating panel)
   One assistant for every role: general MPLADS help + app guidance +
   role-gated data answers. Role enforcement lives on the server.
   Now with persistent multi-conversation history (localStorage), a
   New Chat reset that never deletes history, and a clean composer.
   ═══════════════════════════════════════════════════════════════════ */

const ROLE_SUGGESTIONS = {
  guest: [
    "What is MPLADS?",
    "Open Projects",
    "What is Ground Verification?",
    "How do I search for a project?",
  ],
  citizen: [
    "How do I submit project evidence?",
    "What happens after I upload a photo?",
    "What is a risk score?",
    "Open Ground Verification",
  ],
  field_verifier: [
    "Show my submissions",
    "How does verification work?",
    "What does a risk score mean?",
    "Who verifies citizen evidence?",
  ],
  district_authority: [
    "Show my pending inquiries",
    "How do I respond to an inquiry?",
    "What is an inquiry?",
    "How do I search for a project?",
  ],
  analyst: [
    "Show high-risk projects",
    "Which states have the most projects?",
    "Show projects in Telangana",
    "What is a risk score?",
  ],
  auditor: [
    "Open Audit Priority",
    "Show high-risk projects",
    "Open AI Audit Center",
    "What does P1 mean?",
  ],
  admin: [
    "What should I investigate today?",
    "Which projects have high risk?",
    "How does the inquiry workflow work?",
    "Find unusual spending.",
  ],
}

/* ── Conversation history (localStorage) ────────────────────────────
   Bounded to 25 conversations × 60 messages each. Stores messages,
   timestamps, the project context in play, and executed-action results
   so reopening restores a faithful, useful transcript. */
const HIST_KEY = "mplads_assistant_history_v1"
const HIST_MAX_CONVS = 25
const HIST_MAX_MSGS = 60

/** Module-scope so event handlers stay free of impure-in-render lint. */
const newSessionId = () =>
  `asst-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

function loadHistory() {
  try {
    const raw = localStorage.getItem(HIST_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr.slice(0, HIST_MAX_CONVS) : []
  } catch {
    return []
  }
}
function persistHistory(convs) {
  try {
    localStorage.setItem(HIST_KEY, JSON.stringify(convs.slice(0, HIST_MAX_CONVS)))
  } catch { /* storage full/unavailable — history is best-effort */ }
}
function convTitle(conv) {
  const proj = conv.projectId ? ` — Project #${conv.projectId}` : ""
  const firstUser = (conv.messages || []).find((m) => m.role === "user")
  if (firstUser) {
    const t = firstUser.text.replace(/\s+/g, " ").trim()
    if (t) return (t.length > 42 ? t.slice(0, 42) + "…" : t) + proj
  }
  return `Conversation — ${new Date(conv.ts).toLocaleDateString()}${proj}`
}

/** Renders **bold** and bullet lines from the engine's markdown-lite answers. */
function AnswerBody({ text }) {
  const lines = String(text || "").split("\n")
  return (
    <div className="space-y-1">
      {lines.map((line, i) => {
        if (!line.trim()) return <div key={i} className="h-1" />
        const bolded = line.split(/(\*\*[^*]+\*\*)/g).map((part, j) =>
          part.startsWith("**") && part.endsWith("**")
            ? <strong key={j} className="font-bold">{part.slice(2, -2)}</strong>
            : part
        )
        if (line.trim().startsWith("### ")) {
          return <p key={i} className="mt-1 text-[0.8125rem] font-bold leading-snug">{line.slice(4)}</p>
        }
        if (line.trim().startsWith("•")) {
          return <p key={i} className="pl-2 text-[0.8125rem] leading-snug">{bolded}</p>
        }
        return <p key={i} className="text-[0.8125rem] leading-snug">{bolded}</p>
      })}
    </div>
  )
}

/* Structured payload from Assistant 2.0. Kept visually subordinate to the
   narrative answer: a compact table first, then findings, then actions the
   user can take. Every string comes from the backend; nothing is derived in
   the browser. */
function StructuredBlocks({ data }) {
  if (!data) return null
  const table = data.table
  const findings = Array.isArray(data.findings) ? data.findings : []
  const recommendations = Array.isArray(data.recommendations) ? data.recommendations : []
  const limitations = Array.isArray(data.limitations) ? data.limitations : []
  const hasRows = table && Array.isArray(table.columns) && table.columns.length > 0
  if (!hasRows && findings.length === 0 && recommendations.length === 0 && limitations.length === 0) return null
  return (
    <div className="mt-3 space-y-3">
      {hasRows && (
        <div className="overflow-hidden rounded-lg border border-[#dde1e5] dark:border-[#39434E]">
          <div className="max-h-[260px] overflow-auto">
            <table className="min-w-full border-collapse text-left text-[0.6875rem]">
              <thead className="sticky top-0 bg-[#eceff2] dark:bg-[#1c2127]">
                <tr>{table.columns.map((c, i) => <th key={i} className="whitespace-nowrap border-b border-[#dde1e5] px-2 py-1.5 font-bold dark:border-[#39434E]">{c}</th>)}</tr>
              </thead>
              <tbody>
                {(table.rows || []).map((row, i) => (
                  <tr key={i} className="odd:bg-white even:bg-[#f4f5f6] dark:odd:bg-[#222831] dark:even:bg-[#222831]">
                    {(row || []).map((cell, j) => <td key={j} className="whitespace-nowrap border-b border-[#dde1e5] px-2 py-1.5 align-top dark:border-[#39434E]">{cell}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {table.caption && <p className="bg-[#f4f5f6] px-2 py-1 text-[0.625rem] text-[#69717c] dark:bg-[#1c2127] dark:text-[#aab3bd]">{table.caption}</p>}
        </div>
      )}

      {findings.length > 0 && (
        <section>
          <p className="mb-1 text-[0.6563rem] font-bold uppercase tracking-wide text-[#69717c] dark:text-[#aab3bd]">Findings</p>
          <div className="space-y-1.5">
            {findings.map((f, i) => (
              <div key={i} className="rounded-lg border border-[#dde1e5] bg-white p-2 dark:border-[#39434E] dark:bg-[#171b20]">
                <div className="flex items-start justify-between gap-2">
                  <p className="text-[0.75rem] font-semibold">{f.title}</p>
                  <span className="shrink-0 rounded-full bg-[#eceff2] px-1.5 py-0.5 text-[0.5938rem] font-bold uppercase text-[#30343A] dark:bg-[#222831] dark:text-gray-200">{f.kind || "note"}</span>
                </div>
                <p className="mt-0.5 text-[0.6875rem] leading-snug text-[#69717c] dark:text-[#aab3bd]">{f.detail}</p>
                {f.evidence && <p className="mt-1 text-[0.625rem] text-[#69717c] dark:text-[#aab3bd]">Source: {f.evidence}</p>}
              </div>
            ))}
          </div>
        </section>
      )}

      {recommendations.length > 0 && (
        <section>
          <p className="mb-1 text-[0.6563rem] font-bold uppercase tracking-wide text-[#69717c] dark:text-[#aab3bd]">Recommended next steps</p>
          <div className="space-y-1.5">
            {recommendations.map((r, i) => (
              <div key={i} className="rounded-lg border border-[#dde1e5] bg-white p-2 dark:border-[#39434E] dark:bg-[#171b20]">
                <p className="text-[0.75rem] font-semibold">{r.category} — {r.title}</p>
                <p className="mt-0.5 text-[0.6875rem] leading-snug"><span className="font-semibold">Why:</span> {r.why}</p>
                <p className="mt-0.5 text-[0.6875rem] leading-snug"><span className="font-semibold">Record:</span> {r.evidence}</p>
                <p className="mt-0.5 text-[0.6875rem] leading-snug"><span className="font-semibold">Missing:</span> {r.missing}</p>
                <p className="mt-0.5 text-[0.6875rem] leading-snug"><span className="font-semibold">Action:</span> {r.action}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {limitations.length > 0 && (
        <section className="rounded-lg bg-[#f4f5f6] p-2 dark:bg-[#1c2127]">
          <p className="mb-0.5 text-[0.6563rem] font-bold uppercase tracking-wide text-[#69717c] dark:text-[#aab3bd]">Limitations</p>
          <ul className="list-disc space-y-0.5 pl-4 text-[0.6563rem] leading-snug text-[#69717c] dark:text-[#aab3bd]">
            {limitations.map((item, i) => <li key={i}>{item}</li>)}
          </ul>
        </section>
      )}
    </div>
  )
}

export default function AssistantWidget({
  currentPage,
  contextProjectId,
  contextMpId,
  contextProjectName,
  onNavigate,
  onOpenProject,
  onExecuteAction,
  open: controlledOpen,
  onOpenChange,
  contextState,
}) {
  const { user } = useAuth()

  // Controlled when the app passes open/onOpenChange (e.g. header/support
  // entry points), uncontrolled otherwise — the floating button keeps working
  // standalone.
  const [internalOpen, setInternalOpen] = useState(false)

  const open = controlledOpen ?? internalOpen

  const setOpen = (next) => {
    const value = typeof next === "function" ? next(open) : next

    if (controlledOpen !== undefined) {
      onOpenChange?.(value)
    } else {
      setInternalOpen(value)
    }
  }

  const [messages, setMessages] = useState([]) // {role, text, actions?, exec?, ts}
  const [input, setInput] = useState("")
  const [busy, setBusy] = useState(false)
  const [suggestions, setSuggestions] = useState([])
  const [contextHelp, setContextHelp] = useState(null)
  const [showHistory, setShowHistory] = useState(false)
  const [history, setHistory] = useState(loadHistory)
  const scrollRef = useRef(null)
  const inputRef = useRef(null)
  const sessionRef = useRef(null)
  const [lastProjectId, setLastProjectId] = useState(null)

  /* ── Modal height sync ── when a Project Details modal is open, stretch the
     panel to exactly the modal card's height and align its bottom edge, so the
     two read as one side-by-side pair (assistant content scrolls internally).
     Inline style only while synced — closing the modal restores the stock 520px
     bottom-anchored panel. Reacts to content growth/shrink via ResizeObserver,
     overlay scrolling, and window resize; the poll catches the card mounting
     after the panel is already open. */
  const [modalSync, setModalSync] = useState(null) // {top, height} | null
  const [modalPresent, setModalPresent] = useState(false) // project modal on screen (raises FAB + panel above its overlay)
  useEffect(() => {
    const measure = () => {
      const card = document.querySelector("[data-mplads-modal-card]")
      setModalPresent(!!card)
      if (!card) { setModalSync(null); return }
      const r = card.getBoundingClientRect()
      const m = 12
      const top = Math.max(r.top, m)
      const bottom = Math.min(r.bottom, window.innerHeight - m)
      const h = bottom - top
      if (h < 200) { setModalSync(null); return }
      setModalSync({ top: Math.round(top), height: Math.round(h) })
    }
    measure()
    const card = document.querySelector("[data-mplads-modal-card]")
    const ro = card ? new ResizeObserver(measure) : null
    if (card) ro.observe(card)
    window.addEventListener("resize", measure)
    window.addEventListener("scroll", measure, true)
    const t = setInterval(measure, 400)
    return () => { if (ro) ro.disconnect(); window.removeEventListener("resize", measure); window.removeEventListener("scroll", measure, true); clearInterval(t) }
  }, [open, lastProjectId, modalPresent])

  /* ── Side-by-side signal ── Project Details shifts left only while this
     panel is open (desktop widths, enforced by CSS media query). Exposed
     as a class on <html> (initial state for late-mounted modals) and an
     event (live toggle). */
  useEffect(() => {
    document.documentElement.classList.toggle("assistant-panel-open", open)
    window.dispatchEvent(new CustomEvent("assistant-panel", { detail: { open } }))
    return () => {
      document.documentElement.classList.remove("assistant-panel-open")
      window.dispatchEvent(new CustomEvent("assistant-panel", { detail: { open: false } }))
    }
  }, [open])

  const roleKey = !user ? "guest" : (user.role in ROLE_SUGGESTIONS ? user.role : "guest")

  /* ── Persistence ── the current transcript is saved whenever it changes
     and on panel close, keyed by the conversation record created on open. */
  const convMetaRef = useRef(null) // {ts, projectId}
  const messagesRef = useRef(messages)
  useEffect(() => { messagesRef.current = messages }, [messages])

  const saveNow = () => {
    const msgs = messagesRef.current
    if (!convMetaRef.current || msgs.length === 0) return
    const meta = convMetaRef.current
    setHistory((prev) => {
      const entry = { ts: meta.ts, projectId: meta.projectId || null, messages: msgs.slice(-HIST_MAX_MSGS) }
      const rest = prev.filter((c) => c.ts !== meta.ts)
      const next = [entry, ...rest].slice(0, HIST_MAX_CONVS)
      persistHistory(next)
      return next
    })
  }

  // "✦ Ask AI" inside the Project Details modal (and any other surface that
  // broadcasts assistant:open) opens this panel; the project context itself
  // arrives through App via the assistant-project-context event.
  useEffect(() => {
    const openit = () => setOpen(true)
    window.addEventListener("assistant:open", openit)
    return () => window.removeEventListener("assistant:open", openit)
  }, [])

  // A project modal opened while this panel is already up: re-seed to the NEW
  // project context (same path as the open-effect, without closing). A subtle
  // system note records the switch so the transcript stays coherent.
  const ctxSwitchRef = useRef(null)
  useEffect(() => {
    if (!open || !contextProjectId) {
      ctxSwitchRef.current = null
      return
    }
    const prev = ctxSwitchRef.current
    const switched = prev !== null && prev !== contextProjectId
    ctxSwitchRef.current = contextProjectId
    saveNow()
    setMessages([])
    setSuggestions(ROLE_SUGGESTIONS[roleKey] || ROLE_SUGGESTIONS.guest)
    setContextHelp(null)
    setShowHistory(false)
    sessionRef.current = newSessionId()
    setLastProjectId(contextProjectId)
    convMetaRef.current = { ts: Date.now(), projectId: contextProjectId }
    if (switched) {
      setMessages([{ role: "system", text: `Project context updated: ${contextProjectName || `#${contextProjectId}`}` }])
    }
    const page = `project:${contextProjectId}`
    getAssistantSuggestions(page)
      .then((d) => { if (d?.general?.length) setSuggestions(d.general.slice(0, 5)) })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contextProjectId])

  // Fresh conversation per opened session; suggestions follow role + page.
  // Runs after paint (setTimeout 0) so the open-transition renders first.
  useEffect(() => {
    if (!open) return
    const t = setTimeout(() => {
      saveNow()
      setMessages([])
      setSuggestions(ROLE_SUGGESTIONS[roleKey] || ROLE_SUGGESTIONS.guest)
      setContextHelp(null)
      setShowHistory(false)
      sessionRef.current = newSessionId()
      setLastProjectId(contextProjectId || null)
      convMetaRef.current = { ts: Date.now(), projectId: contextProjectId || null }
      inputRef.current?.focus()
      const page = contextProjectId ? `project:${contextProjectId}` : contextMpId ? `mp:${contextMpId}` : currentPage
      if (page) {
        getAssistantSuggestions(page)
          .then((d) => { if (d?.general?.length) setSuggestions(d.general.slice(0, 5)) })
          .catch(() => {})
        if (!page.startsWith("project:")) {
          getAssistantContextHelp(page)
            .then((d) => { if (d?.help) setContextHelp(d.help) })
            .catch(() => {})
        } else {
          setContextHelp(null)
        }
      }
    }, 0)
    return () => clearTimeout(t)
    // deps intentionally: re-running on currentPage/contextProjectId re-seeds
    // suggestions for the new context; saveNow/roleKey are stable-enough refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, roleKey, currentPage, contextProjectId, contextMpId])

  useEffect(() => {
    if (open && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [open, messages, busy, showHistory])

  const send = async (questionRaw) => {
    const question = String(questionRaw || input).trim()
    if (!question || busy) return
    setInput("")
    setSuggestions([])
    setMessages((m) => [...m, { role: "user", text: question, ts: Date.now() }])
    setBusy(true)
    try {
      // Project context: live drawer first, then the conversation's own
      // project (restored history keeps asking about the same project).
      const ctxPid = contextProjectId || lastProjectId
      const page = ctxPid ? `project:${ctxPid}` : contextMpId ? `mp:${contextMpId}` : currentPage
      const res = await askAssistant(question, page, sessionRef.current)
      if (res.project_id) {
        setLastProjectId(res.project_id)
        if (convMetaRef.current && !convMetaRef.current.projectId) {
          convMetaRef.current = { ...convMetaRef.current, projectId: res.project_id }
        }
      }

      const exec = []
      for (const a of res.actions || []) {
        if (!a.auto || !onExecuteAction) continue
        const status = onExecuteAction(a)
        exec.push({ label: a.label, status })
      }
      setMessages((m) => [...m, {
        role: "assistant", text: res.answer, actions: res.actions || [], exec,
        structured: {
          table: res.table || null,
          findings: res.findings || [],
          recommendations: res.recommendations || [],
          limitations: res.limitations || [],
          intent: res.intent || "help",
        },
        ts: Date.now(),
      }])
      // Auto-navigation should reveal the page, but an analytical answer must
      // stay on screen. Close only for short navigation-only responses.
      const navigationOnly = (res.actions || []).every((a) => a.action === "navigate")
        && !res.table && !(res.findings || []).length && !(res.recommendations || []).length
        && String(res.answer || "").length < 420
      if (exec.some((e) => e.status === "ok") && navigationOnly) {
        setTimeout(() => setOpen(false), 900)
      }
    } catch {
      setMessages((m) => [...m, {
        role: "assistant",
        text: "The assistant is temporarily unavailable. Please try again in a moment.",
        retry: question,
        ts: Date.now(),
      }])
    } finally {
      setBusy(false)
      // Persist once the exchange completes (covers quick close-after-send).
      setTimeout(saveNow, 50)
    }
  }

  const runAction = (a) => {
    // Manual chips (auto:false suggestions, or repeat-after-close) go through
    // the same validated executor — never ad-hoc handling.
    if (onExecuteAction) {
      const status = onExecuteAction(a)
      if (status === "ok") {
        setOpen(false)
        return
      }
    }
    // Fallbacks preserve the pre-existing behaviors if no executor is wired.
    if (a.action === "open_project" && onOpenProject) {
      onOpenProject(parseInt(a.target, 10))
      setOpen(false)
    } else if (a.action === "navigate" && onNavigate) {
      onNavigate(a.target)
      setOpen(false)
    }
  }

  const startNewChat = () => {
    saveNow()                       // current transcript → history first
    setMessages([])
    setSuggestions(ROLE_SUGGESTIONS[roleKey] || ROLE_SUGGESTIONS.guest)
    sessionRef.current = newSessionId()
    // New Chat re-anchors to the CURRENT page context (drawer MP/state) and
    // drops the discussed-project memory — multi-turn stays but the old
    // conversation's subject doesn't leak across chats.
    setLastProjectId(contextProjectId || null)
    convMetaRef.current = { ts: Date.now(), projectId: contextProjectId || null }
    setShowHistory(false)
    setInput("")
    setTimeout(() => inputRef.current?.focus(), 60)
  }

  const openConversation = (conv) => {
    setMessages((conv.messages || []).map((m) => ({ ...m })))
    sessionRef.current = newSessionId()
    convMetaRef.current = { ts: conv.ts, projectId: conv.projectId || null }
    setLastProjectId(conv.projectId || null)
    setShowHistory(false)
    setTimeout(() => inputRef.current?.focus(), 60)
  }

  const clearHistory = () => {
    if (!window.confirm("Delete ALL saved assistant conversations? This cannot be undone.")) return
    setHistory([])
    persistHistory([])
    setShowHistory(false)
  }

  // Auto-grow composer up to 4 rows, never beyond (and no resize grip).
  const autoGrow = (el) => {
    if (!el) return
    el.style.height = "auto"
    el.style.height = Math.min(el.scrollHeight, 96) + "px"
  }

  const ph = contextProjectId
    ? "Ask anything about this project…"
    : contextMpId
      ? "Ask anything about this MP…"
      : contextState
        ? "Ask anything about this state…"
        : "Ask anything about MPLADS…"

  // Context chip: what the assistant currently "sees" — project drawer, MP
  // detail, named state (State Intelligence), or the plain page.
  const contextChip = contextProjectId
    ? `📁 Project #${contextProjectId}${contextProjectName ? ` — ${contextProjectName}` : ""}`
    : contextMpId
      ? `👤 MP context active`
      : contextState
        ? `🗺 State: ${contextState}`
        : null

  return (
    <>
      {/* Launcher */}
      <button
        onClick={() => setOpen((o) => !o)}
        aria-label="MPLADS AI Assistant"
        title="MPLADS AI Assistant"
        className={`fixed bottom-5 right-5 z-[70] flex h-13 w-13 items-center justify-center rounded-full bg-[#46566a] text-xl text-white shadow-soft transition-[transform,opacity] duration-200 hover:scale-105 hover:bg-[#37455a] active:scale-95 dark:bg-blue-600 dark:hover:bg-blue-500 ${modalPresent ? "!z-[85]" : ""} ${open ? "pointer-events-none opacity-0" : ""}`}
        style={{ height: 52, width: 52 }}
      >
        {open ? "✕" : "✦"}
      </button>

      {/* Panel */}
      {open && (
        <div        className="pop-in-up fixed bottom-20 right-5 z-[70] flex h-[520px] max-h-[calc(100vh-6rem)] w-[380px] max-w-[calc(100vw-2.5rem)] flex-col overflow-hidden rounded-2xl border border-[#dde1e5] bg-white shadow-soft-lg dark:border-[#39434E] dark:bg-[#171b20]" style={{ transformOrigin: "bottom right", ...(modalSync ? { top: modalSync.top, bottom: "auto", height: modalSync.height, maxHeight: "none" } : {}), ...(modalPresent ? { zIndex: 85 } : {}) }}>
          {/* Header */}
          <div className="flex flex-none items-center gap-2.5 border-b border-[#dde1e5] bg-[#f4f5f6] px-4 py-3 dark:border-[#39434E] dark:bg-[#1c2127]">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#46566a] text-sm text-white dark:bg-blue-600">✦</span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-bold text-[#30343A] dark:text-[#f1f3f5]">MPLADS AI Assistant</p>
              <p className="truncate text-[0.6875rem] text-[#69717c] dark:text-[#aab3bd]">
                {lastProjectId
                  ? `Project: ${contextProjectName || `#${lastProjectId}`}`
                  : user
                    ? `For: ${user.name.split(" ")[0]} (${user.role.replace("_", " ")})`
                    : "Public help — sign in for more"}
              </p>
            </div>
            <button
              onClick={startNewChat}
              title="New Chat (keeps history)"
              aria-label="New Chat"
              className="flex h-8 w-8 items-center justify-center rounded-full text-[#69717c] transition hover:bg-black/5 dark:text-[#aab3bd] dark:hover:bg-white/10"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>
            </button>
            <button
              onClick={() => setShowHistory((s) => !s)}
              title="History"
              aria-label="History"
              className={`flex h-8 w-8 items-center justify-center rounded-full transition hover:bg-black/5 dark:hover:bg-white/10 ${showHistory ? "bg-black/5 text-[#30343A] dark:bg-white/10 dark:text-[#f1f3f5]" : "text-[#69717c] dark:text-[#aab3bd]"}`}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            </button>
            <button
              onClick={() => setOpen(false)}
              title="Close assistant"
              aria-label="Close assistant"
              className="flex h-8 w-8 items-center justify-center rounded-full text-[#69717c] transition hover:bg-black/5 dark:text-[#aab3bd] dark:hover:bg-white/10"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
          </div>

          {/* History drawer */}
          {showHistory && (
            <div className="flex flex-none flex-col overflow-hidden border-b border-[#dde1e5] bg-[#f4f5f6] dark:border-[#39434E] dark:bg-[#1c2127]" style={{ maxHeight: "55%" }}>
              <div className="flex items-center justify-between px-4 pb-1.5 pt-2.5">
                <p className="text-[0.6875rem] font-bold uppercase tracking-wide text-[#69717c] dark:text-[#aab3bd]">Past conversations</p>
                {history.length > 0 && (
                  <button onClick={clearHistory} className="rounded px-1.5 py-0.5 text-[0.6875rem] font-semibold text-red-600 transition hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/40">
                    Clear all
                  </button>
                )}
            </div>
              <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
                {history.length === 0 ? (
                  <p className="px-2 py-3 text-[0.75rem] text-[#69717c] dark:text-[#aab3bd]">
                    No saved conversations yet. Chats are saved here automatically as you talk.
                  </p>
                ) : (
                  history.map((c) => (
                    <button
                      key={c.ts}
                      onClick={() => openConversation(c)}
                      className="mb-1 block w-full rounded-lg px-2.5 py-2 text-left transition hover:bg-black/5 dark:hover:bg-white/5"
                    >
                      <p className="truncate text-[0.78rem] font-semibold text-[#30343A] dark:text-gray-100">{convTitle(c)}</p>
                      <p className="text-[0.6563rem] text-[#69717c] dark:text-[#aab3bd]">
                        {new Date(c.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                        {c.projectId ? ` · Project #${c.projectId}` : ""}
                        {` · ${c.messages?.length || 0} messages`}
                      </p>
                    </button>
                  ))
                )}
              </div>
            </div>
          )}

          {/* Messages */}
          <div ref={scrollRef} className="assistant-scroll min-h-0 flex-1 space-y-3.5 overflow-y-auto px-4 py-4">
            {messages.length === 0 && !showHistory && (
              <div className="rounded-xl bg-[#eceff2] p-4 text-[0.8125rem] leading-snug text-[#30343A] dark:bg-[#222831] dark:text-[#f1f3f5]">
                {contextProjectId
                  ? `Project context loaded (#${contextProjectId}). Ask me anything about this project — its risk, spending, timeline, vendor, peers, data quality — or say "generate an audit brief".`
                  : contextMpId
                    ? "MP context loaded. Ask about this MP's works, spending, completion rate — or any portfolio question."
                    : contextState
                      ? `State context loaded (${contextState}). Ask for a summary, its projects, or its high-risk works.`
                      : "Namaste 🙏 I can answer questions about MPLADS, guide you through this portal, and — depending on your role — pull up live project, risk, vendor and verification data. All answers come from real portal data; nothing is invented."}
              </div>
            )}
            {messages.length === 0 && !showHistory && contextChip && (
              <p className="px-1 text-[0.6875rem] font-semibold uppercase tracking-wide text-[#69717c] dark:text-[#aab3bd]">
                {contextChip}
              </p>
            )}
            {messages.length === 0 && !showHistory && contextHelp && (
              <button
                onClick={() => send("What does this page show?")}
                className="w-full rounded-xl border border-[#dde1e5] bg-white p-3 text-left transition hover:bg-[#eceff2] dark:border-[#39434E] dark:bg-[#171b20] dark:hover:bg-[#222831]">
                <p className="text-[0.6875rem] font-bold uppercase tracking-wide text-[#69717c] dark:text-[#aab3bd]">
                  📍 About this page
                </p>
                <p className="mt-1 line-clamp-2 text-[0.75rem] leading-snug text-[#30343A] dark:text-gray-100">
                  {contextHelp}
                </p>
              </button>
            )}
            {messages.map((m, i) => (
              m.role === "system" ? (
                <p key={i} className="rise-in text-center text-[0.6875rem] font-semibold uppercase tracking-wide text-[#69717c] dark:text-[#aab3bd]">
                  {m.text}
                </p>
              ) : m.role === "user" ? (
                <div key={i} className="flex justify-end">
                  <p className="rise-in max-w-[85%] rounded-2xl rounded-br-md bg-[#46566a] px-3.5 py-2.5 text-[0.8125rem] text-white dark:bg-blue-600">
                    {m.text}
                  </p>
                </div>
              ) : (
                <div key={i} className="flex justify-start">
                  <div className="rise-in max-w-[92%] rounded-2xl rounded-bl-md border border-[#dde1e5] bg-[#f4f5f6] px-3.5 py-2.5 text-[#30343A] dark:border-[#39434E] dark:bg-[#222831] dark:text-gray-100">
                    <AnswerBody text={m.text} />
                    <StructuredBlocks data={m.structured} />
                    {m.retry && !busy && (
                      <button
                        onClick={() => send(m.retry)}
                        className="mt-2 rounded-full border border-[#46566a] px-2.5 py-1 text-[0.6875rem] font-bold text-[#30343A] hover:bg-[#eceff2] dark:border-blue-500 dark:text-blue-300 dark:hover:bg-[#1c2127]"
                      >
                        Retry
                      </button>
                    )}
                    {m.actions?.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5 border-t border-[#dde1e5] pt-2 dark:border-[#39434E]">
                        {m.actions.map((a, j) => {
                          const ex = m.exec?.[j]
                          const done = ex && ex.status === "ok"
                          const failed = ex && ex.status && ex.status !== "ok" && ex.status !== "rejected"
                          return (
                            <button
                              key={j}
                              onClick={() => runAction(a)}
                              className={done
                                ? "flex items-center gap-1 rounded-full bg-green-600/10 px-2.5 py-1 text-[0.6875rem] font-bold text-green-700 dark:text-green-300"
                                : "rounded-full bg-[#46566a] px-2.5 py-1 text-[0.6875rem] font-bold text-white transition hover:bg-[#37455a] dark:bg-blue-600 dark:hover:bg-blue-500"}
                            >
                              {done ? "✓ " : failed ? "⚠ " : a.action === "open_project" ? "🗂 " : "→ "}{a.label}
                            </button>
                          )
                        })}
                      </div>
                    )}
                  </div>
                </div>
              )
            ))}
            {busy && (
              <div className="flex justify-start">
                <div className="flex items-center gap-1.5 rounded-2xl border border-[#dde1e5] bg-[#f4f5f6] px-3 py-2.5 dark:border-[#39434E] dark:bg-[#222831]">
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-[#46566a] dark:bg-blue-400" style={{ animationDelay: "0ms" }} />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-[#46566a] dark:bg-blue-400" style={{ animationDelay: "120ms" }} />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-[#46566a] dark:bg-blue-400" style={{ animationDelay: "240ms" }} />
                </div>
              </div>
            )}
          </div>

          {/* Suggestions */}
          {suggestions.length > 0 && !busy && !showHistory && (
            <div className="flex flex-none flex-wrap gap-1.5 border-t border-[#dde1e5] px-3 pt-2.5 dark:border-[#39434E]">
              {suggestions.slice(0, 5).map((s, i) => (
                <button
                  key={`${i}-${s}`}
                  onClick={() => send(s)}
                  className="rounded-full border border-[#dde1e5] bg-white px-2.5 py-1 text-[0.6875rem] font-semibold text-[#30343A] transition hover:bg-[#eceff2] dark:border-[#39434E] dark:bg-[#171b20] dark:text-[#f1f3f5] dark:hover:bg-[#222831]"
                >
                  {s}
                </button>
              ))}
            </div>
          )}

          {/* Input — Enter sends, Shift+Enter newline; no resize grip */}
          <div className="flex flex-none items-end gap-2 p-3">
            <textarea
              ref={(el) => { inputRef.current = el; autoGrow(el) }}
              value={input}
              onChange={(e) => { setInput(e.target.value); autoGrow(e.target) }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault()
                  send()
                }
              }}
              rows={1}
              placeholder={ph}
              maxLength={500}
              className="assistant-input min-w-0 flex-1 resize-none overflow-y-auto rounded-2xl border border-[#dde1e5] bg-white px-3.5 py-2.5 text-[0.8125rem] outline-none focus:border-[#46566a] dark:border-[#39434E] dark:bg-[#222831] dark:text-gray-100 dark:focus:border-blue-500"
            />
            <button
              onClick={() => send()}
              disabled={busy || !input.trim()}
              aria-label="Send"
              className="flex h-9 w-9 flex-none items-center justify-center rounded-full bg-[#46566a] text-white transition hover:bg-[#37455a] disabled:opacity-40 dark:bg-blue-600 dark:hover:bg-blue-500"
            >
              ➤
            </button>
            {busy && <span className="sr-only">Loading…</span>}
          </div>
        </div>
      )}
    </>
  )
}