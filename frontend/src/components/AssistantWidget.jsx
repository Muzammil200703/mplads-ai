import { useEffect, useRef, useState } from "react"
import { useAuth } from "../context/AuthContext"
import {
  askAssistant, getAssistantSuggestions, getAssistantContextHelp, getAssistantQueryResults,
} from "../services/api"
import OutcomePredictionCard from "./OutcomePredictionCard"

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
        <div className="overflow-hidden rounded-lg border border-[#e8dadc] dark:border-[#351923]">
          <div className="max-h-[260px] overflow-auto">
            <table className="min-w-full border-collapse text-left text-[0.6875rem]">
              <thead className="sticky top-0 bg-[#f8f1f1] dark:bg-[#120b0e]">
                <tr>{table.columns.map((c, i) => <th key={i} className="whitespace-nowrap border-b border-[#e8dadc] px-2 py-1.5 font-bold dark:border-[#351923]">{c}</th>)}</tr>
              </thead>
              <tbody>
                {(table.rows || []).map((row, i) => (
                  <tr key={i} className="odd:bg-white even:bg-[#faf7f6] dark:odd:bg-[#0d090b] dark:even:bg-[#0d090b]">
                    {(row || []).map((cell, j) => <td key={j} className="whitespace-nowrap border-b border-[#e8dadc] px-2 py-1.5 align-top dark:border-[#351923]">{cell}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {table.caption && <p className="bg-[#faf7f6] px-2 py-1 text-[0.625rem] text-[#71666a] dark:bg-[#120b0e] dark:text-[#b9aeb2]">{table.caption}</p>}
        </div>
      )}

      {findings.length > 0 && (
        <section>
          <p className="mb-1 text-[0.6563rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">Findings</p>
          <div className="space-y-1.5">
            {findings.map((f, i) => (
              <div key={i} className="rounded-lg border border-[#e8dadc] bg-white p-2 dark:border-[#351923] dark:bg-[#000000]">
                <div className="flex items-start justify-between gap-2">
                  <p className="text-[0.75rem] font-semibold">{f.title}</p>
                  <span className="shrink-0 rounded-full bg-[#f8f1f1] px-1.5 py-0.5 text-[0.5938rem] font-bold uppercase text-[#292326] dark:bg-[#0d090b] dark:text-gray-200">{f.kind || "note"}</span>
                </div>
                <p className="mt-0.5 text-[0.6875rem] leading-snug text-[#71666a] dark:text-[#b9aeb2]">{f.detail}</p>
                {f.evidence && <p className="mt-1 text-[0.625rem] text-[#71666a] dark:text-[#b9aeb2]">Source: {f.evidence}</p>}
              </div>
            ))}
          </div>
        </section>
      )}

      {recommendations.length > 0 && (
        <section>
          <p className="mb-1 text-[0.6563rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">Recommended next steps</p>
          <div className="space-y-1.5">
            {recommendations.map((r, i) => (
              <div key={i} className="rounded-lg border border-[#e8dadc] bg-white p-2 dark:border-[#351923] dark:bg-[#000000]">
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
        <section className="rounded-lg bg-[#faf7f6] p-2 dark:bg-[#120b0e]">
          <p className="mb-0.5 text-[0.6563rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">Limitations</p>
          <ul className="list-disc space-y-0.5 pl-4 text-[0.6563rem] leading-snug text-[#71666a] dark:text-[#b9aeb2]">
            {limitations.map((item, i) => <li key={i}>{item}</li>)}
          </ul>
        </section>
      )}
    </div>
  )
}

/* ── Natural-language query block ────────────────────────────────────
   Shows what was actually executed and returned: applied filters, the
   generated SELECT (viewable + copyable), a compact chart, row count and
   timing, the full stored result on demand, CSV export and any execution
   error. Every number comes from the backend — nothing is derived here. */
const compactNumber = (value, money) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return "—"
  const abs = Math.abs(n)
  const short = abs >= 1e7 ? `${(n / 1e7).toFixed(2)} Cr`
    : abs >= 1e5 ? `${(n / 1e5).toFixed(2)} L`
      : abs >= 1e3 ? `${(n / 1e3).toFixed(1)}k`
        : `${Math.round(n * 100) / 100}`
  return money ? `₹${short}` : short
}

function QueryResult({ query, chart }) {
  const [copied, setCopied] = useState(false)
  const [full, setFull] = useState(null)
  const [loading, setLoading] = useState(false)
  const [showSql, setShowSql] = useState(false)

  if (!query) return null
  const status = query.status || "ok"
  const failed = status === "error"
  const unavailable = status === "unavailable"

  const copySql = async () => {
    try {
      await navigator.clipboard.writeText(query.sql || "")
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch { setCopied(false) }
  }

  const loadFull = async () => {
    if (!query.id || loading) return
    setLoading(true)
    try { setFull(await getAssistantQueryResults(query.id)) } catch { /* keep compact view */ }
    setLoading(false)
  }

  const exportCsv = async () => {
    if (!query.id) return
    setLoading(true)
    try {
      const data = full || await getAssistantQueryResults(query.id)
      const cols = data.columns || []
      const escape = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`
      const csv = [cols.map(escape).join(","), ...(data.rows || []).map((r) => r.map(escape).join(","))].join("\n")
      const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }))
      const a = document.createElement("a")
      a.href = url
      a.download = `mplads-query-${String(query.id).slice(0, 8)}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } catch { /* export is best-effort */ }
    setLoading(false)
  }

  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-[#e8dadc] dark:border-[#351923]">
      <div className="flex items-center justify-between gap-2 bg-[#f8f1f1] px-2 py-1.5 dark:bg-[#120b0e]">
        <p className="text-[0.6563rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">
          {failed ? "⚠ Query blocked"
            : unavailable ? "ℹ Historical data unavailable"
              : `Query · ${query.row_count ?? 0} row${query.row_count === 1 ? "" : "s"}${query.elapsed_ms ? ` · ${query.elapsed_ms} ms` : ""}`}
        </p>
        <div className="flex shrink-0 items-center gap-1">
          {query.sql && (
            <button
              onClick={copySql}
              className="rounded-sm px-1.5 py-0.5 text-[0.625rem] font-semibold text-[#292326] hover:bg-black/5 dark:text-gray-200 dark:hover:bg-white/10"
            >{copied ? "Copied" : "Copy SQL"}</button>
          )}
          {query.sql && (
            <button
              onClick={() => setShowSql((s) => !s)}
              className="rounded-sm px-1.5 py-0.5 text-[0.625rem] font-semibold text-[#c8102e] hover:bg-black/5 dark:text-blue-300 dark:hover:bg-white/10"
            >{showSql ? "Hide SQL" : "View SQL"}</button>
          )}
          {query.can_export && query.id && !failed && (
            <button
              onClick={exportCsv}
              disabled={loading}
              className="rounded-sm px-1.5 py-0.5 text-[0.625rem] font-semibold text-[#292326] hover:bg-black/5 disabled:opacity-40 dark:text-gray-200 dark:hover:bg-white/10"
            >CSV</button>
          )}
        </div>
      </div>

      {query.filters?.length > 0 && (
        <div className="flex flex-wrap gap-1 border-t border-[#e8dadc] px-2 py-1.5 dark:border-[#351923]">
          {query.filters.map((f, i) => (
            <span key={i} className="rounded-full bg-[#f8f1f1] px-1.5 py-0.5 text-[0.5938rem] font-semibold text-[#71666a] dark:bg-[#210a12] dark:text-[#e2a4b4]">
              {f.label}: {f.value}
            </span>
          ))}
        </div>
      )}

      {chart?.items?.length > 0 && !failed && (
        <div className="space-y-1 border-t border-[#e8dadc] px-2 py-2 dark:border-[#351923]">
          <p className="text-[0.5938rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">
            {chart.value_column} by {chart.label_column}
          </p>
          {chart.items.map((item, i) => {
            const max = Math.max(...chart.items.map((x) => Math.abs(Number(x.value) || 0)), 1)
            const width = Math.max(3, Math.round((Math.abs(Number(item.value) || 0) / max) * 100))
            return (
              <div key={i} className="flex items-center gap-2">
                <span className="w-[38%] shrink-0 truncate text-[0.625rem] text-[#292326] dark:text-gray-200" title={item.label}>{item.label}</span>
                <span className="h-2 flex-1 overflow-hidden rounded-full bg-[#f1e6e8] dark:bg-[#210a12]">
                  <span className="block h-full rounded-full bg-[#c8102e] dark:bg-blue-500" style={{ width: `${width}%` }} />
                </span>
                <span className="w-[24%] shrink-0 text-right text-[0.625rem] font-semibold text-[#292326] dark:text-gray-100">
                  {compactNumber(item.value, chart.money)}
                </span>
              </div>
            )
          })}
        </div>
      )}

      {showSql && query.sql && (
        <div className="border-t border-[#e8dadc] dark:border-[#351923]">
          <pre className="max-h-[180px] overflow-auto bg-[#faf7f6] p-2 text-[0.625rem] leading-relaxed text-[#292326] dark:bg-[#000000] dark:text-[#f5eff1]">
            {query.sql}
          </pre>
          <p className="bg-[#faf7f6] px-2 pb-1.5 text-[0.5938rem] text-[#71666a] dark:bg-[#000000] dark:text-[#82767b]">
            Read-only SELECT, validated and executed with a row cap and timeout.
          </p>
        </div>
      )}

      {query.truncated && !full && query.id && (
        <button
          onClick={loadFull}
          disabled={loading}
          className="block w-full border-t border-[#e8dadc] px-2 py-1.5 text-left text-[0.6563rem] font-semibold text-[#c8102e] hover:bg-[#faf7f6] disabled:opacity-50 dark:border-[#351923] dark:text-blue-300 dark:hover:bg-[#120b0e]"
        >
          {loading ? "Loading…" : `Show all ${query.row_count} rows`}
        </button>
      )}

      {full && (
        <div className="max-h-[240px] overflow-auto border-t border-[#e8dadc] dark:border-[#351923]">
          <table className="min-w-full border-collapse text-left text-[0.6875rem]">
            <thead className="sticky top-0 bg-[#f8f1f1] dark:bg-[#120b0e]">
              <tr>{(full.columns || []).map((c, i) => (
                <th key={i} className="whitespace-nowrap border-b border-[#e8dadc] px-2 py-1.5 font-bold dark:border-[#351923]">{c}</th>
              ))}</tr>
            </thead>
            <tbody>
              {(full.rows || []).map((row, i) => (
                <tr key={i} className="odd:bg-white even:bg-[#faf7f6] dark:odd:bg-[#0d090b] dark:even:bg-[#0d090b]">
                  {row.map((cell, j) => (
                    <td key={j} className="whitespace-nowrap border-b border-[#e8dadc] px-2 py-1.5 dark:border-[#351923]">{String(cell ?? "—")}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {failed && query.error && (
        <p className="border-t border-[#e8dadc] bg-red-50 px-2 py-1.5 text-[0.6563rem] leading-snug text-red-700 dark:border-[#351923] dark:bg-[#210a12] dark:text-red-300">
          {query.error}
        </p>
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
  house,
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

  /* ── Scroll-aware launcher (phones) ── a fixed button would keep covering
     right-aligned counts while the page scrolls, so it steps out of the way
     on downward scroll and returns as soon as the user stops or scrolls up.
     Desktop keeps the button permanently visible. */
  const [fabHidden, setFabHidden] = useState(false)
  useEffect(() => {
    let lastTop = null
    let idle
    const onScroll = (e) => {
      const el = e.target
      if (!(el instanceof HTMLElement)) return
      const top = el.scrollTop
      if (lastTop !== null && window.innerWidth < 1024) {
        if (top > lastTop + 8) setFabHidden(true)
        else if (top < lastTop - 8) setFabHidden(false)
      }
      lastTop = top
      clearTimeout(idle)
      idle = setTimeout(() => setFabHidden(false), 900)
    }
    document.addEventListener("scroll", onScroll, true)
    return () => { document.removeEventListener("scroll", onScroll, true); clearTimeout(idle) }
  }, [])
  useEffect(() => {
    let card = null
    let resizeObserver = null
    let frame = 0

    const measure = () => {
      frame = 0
      const nextCard = document.querySelector("[data-mplads-modal-card]")
      if (nextCard !== card) {
        resizeObserver?.disconnect()
        card = nextCard
        resizeObserver = card ? new ResizeObserver(scheduleMeasure) : null
        if (card) resizeObserver.observe(card)
      }

      const present = Boolean(card)
      setModalPresent((previous) => previous === present ? previous : present)
      if (!card) {
        setModalSync((previous) => previous === null ? previous : null)
        return
      }

      const rect = card.getBoundingClientRect()
      const top = Math.max(rect.top, 12)
      const bottom = Math.min(rect.bottom, window.innerHeight - 12)
      const height = bottom - top
      const next = height < 200 ? null : { top: Math.round(top), height: Math.round(height) }
      setModalSync((previous) => (
        previous?.top === next?.top && previous?.height === next?.height
          ? previous
          : next
      ))
    }

    const scheduleMeasure = () => {
      if (!frame) frame = requestAnimationFrame(measure)
    }
    const containsModalCard = (node) => node.nodeType === 1 && (
      node.matches?.("[data-mplads-modal-card]") || node.querySelector?.("[data-mplads-modal-card]")
    )
    const mutationObserver = new MutationObserver((records) => {
      if (records.some((record) => [...record.addedNodes, ...record.removedNodes].some(containsModalCard))) {
        scheduleMeasure()
      }
    })

    measure()
    mutationObserver.observe(document.body, { childList: true, subtree: true })
    window.addEventListener("resize", scheduleMeasure)
    window.addEventListener("scroll", scheduleMeasure, true)
    return () => {
      mutationObserver.disconnect()
      resizeObserver?.disconnect()
      window.removeEventListener("resize", scheduleMeasure)
      window.removeEventListener("scroll", scheduleMeasure, true)
      if (frame) cancelAnimationFrame(frame)
    }
  }, [])

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
      const res = await askAssistant(question, page, sessionRef.current, house)
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
        query: res.query || null,
        chart: res.chart || null,
        prediction: res.prediction || null,
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
        className={`assistant-fab fixed z-[70] flex h-13 w-13 items-center justify-center rounded-full bg-[#c8102e] text-xl text-white shadow-soft transition-[transform,opacity] duration-200 hover:scale-105 hover:bg-[#a50f29] active:scale-95 dark:bg-blue-600 dark:hover:bg-blue-500 ${modalPresent ? "!z-[85]" : ""} ${open || fabHidden ? "pointer-events-none opacity-0" : ""} ${fabHidden ? "translate-y-2" : ""}`}
        style={{ height: 52, width: 52 }}
      >
        {open ? "✕" : "✦"}
      </button>

      {/* Panel */}
      {open && (
        <div        className="pop-in-up fixed z-[70] flex h-[520px] max-h-[calc(100vh-6rem)] w-[380px] max-w-[calc(100vw-2.5rem)] flex-col overflow-hidden rounded-2xl border border-[#e8dadc] bg-white shadow-soft-lg dark:border-[#351923] dark:bg-[#000000]" style={{ right: "calc(1.25rem + env(safe-area-inset-right, 0px))", bottom: "calc(5rem + env(safe-area-inset-bottom, 0px))", transformOrigin: "bottom right", ...(modalSync ? { top: modalSync.top, bottom: "auto", height: modalSync.height, maxHeight: "none" } : {}), ...(modalPresent ? { zIndex: 85 } : {}) }}>
          {/* Header */}
          <div className="flex flex-none items-center gap-2.5 border-b border-[#e8dadc] bg-[#faf7f6] px-4 py-3 dark:border-[#351923] dark:bg-[#120b0e]">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#c8102e] text-sm text-white dark:bg-blue-600">✦</span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-bold text-[#292326] dark:text-[#f5eff1]">MPLADS AI Assistant</p>
              <p className="truncate text-[0.6875rem] text-[#71666a] dark:text-[#b9aeb2]">
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
              className="flex h-8 w-8 items-center justify-center rounded-full text-[#71666a] transition hover:bg-black/5 dark:text-[#b9aeb2] dark:hover:bg-white/10"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12h14"/></svg>
            </button>
            <button
              onClick={() => setShowHistory((s) => !s)}
              title="History"
              aria-label="History"
              className={`flex h-8 w-8 items-center justify-center rounded-full transition hover:bg-black/5 dark:hover:bg-white/10 ${showHistory ? "bg-black/5 text-[#292326] dark:bg-white/10 dark:text-[#f5eff1]" : "text-[#71666a] dark:text-[#b9aeb2]"}`}
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            </button>
            <button
              onClick={() => setOpen(false)}
              title="Close assistant"
              aria-label="Close assistant"
              className="flex h-8 w-8 items-center justify-center rounded-full text-[#71666a] transition hover:bg-black/5 dark:text-[#b9aeb2] dark:hover:bg-white/10"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
          </div>

          {/* History drawer */}
          {showHistory && (
            <div className="flex flex-none flex-col overflow-hidden border-b border-[#e8dadc] bg-[#faf7f6] dark:border-[#351923] dark:bg-[#120b0e]" style={{ maxHeight: "55%" }}>
              <div className="flex items-center justify-between px-4 pb-1.5 pt-2.5">
                <p className="text-[0.6875rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">Past conversations</p>
                {history.length > 0 && (
                  <button onClick={clearHistory} className="rounded-sm px-1.5 py-0.5 text-[0.6875rem] font-semibold text-red-600 transition hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/40">
                    Clear all
                  </button>
                )}
            </div>
              <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
                {history.length === 0 ? (
                  <p className="px-2 py-3 text-[0.75rem] text-[#71666a] dark:text-[#b9aeb2]">
                    No saved conversations yet. Chats are saved here automatically as you talk.
                  </p>
                ) : (
                  history.map((c) => (
                    <button
                      key={c.ts}
                      onClick={() => openConversation(c)}
                      className="mb-1 block w-full rounded-lg px-2.5 py-2 text-left transition hover:bg-black/5 dark:hover:bg-white/5"
                    >
                      <p className="truncate text-[0.78rem] font-semibold text-[#292326] dark:text-gray-100">{convTitle(c)}</p>
                      <p className="text-[0.6563rem] text-[#71666a] dark:text-[#b9aeb2]">
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
              <div className="anim-panel-in rounded-xl bg-[#f8f1f1] p-4 text-[0.8125rem] leading-snug text-[#292326] dark:bg-[#0d090b] dark:text-[#f5eff1]">
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
              <p className="px-1 text-[0.6875rem] font-semibold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">
                {contextChip}
              </p>
            )}
            {messages.length === 0 && !showHistory && contextHelp && (
              <button
                onClick={() => send("What does this page show?")}
                className="w-full rounded-xl border border-[#e8dadc] bg-white p-3 text-left transition hover:bg-[#f8f1f1] dark:border-[#351923] dark:bg-[#000000] dark:hover:bg-[#0d090b]">
                <p className="text-[0.6875rem] font-bold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">
                  📍 About this page
                </p>
                <p className="mt-1 line-clamp-2 text-[0.75rem] leading-snug text-[#292326] dark:text-gray-100">
                  {contextHelp}
                </p>
              </button>
            )}
            {messages.map((m, i) => (
              m.role === "system" ? (
                <p key={i} className="assistant-message rise-in text-center text-[0.6875rem] font-semibold uppercase tracking-wide text-[#71666a] dark:text-[#b9aeb2]">
                  {m.text}
                </p>
              ) : m.role === "user" ? (
                <div key={i} className="flex justify-end">
                  <p className="assistant-message rise-in max-w-[85%] rounded-2xl rounded-br-md bg-[#c8102e] px-3.5 py-2.5 text-[0.8125rem] text-white dark:bg-blue-600">
                    {m.text}
                  </p>
                </div>
              ) : (
                <div key={i} className="flex justify-start">
                  <div className="assistant-message rise-in max-w-[92%] rounded-2xl rounded-bl-md border border-[#e8dadc] bg-[#faf7f6] px-3.5 py-2.5 text-[#292326] dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100">
                    <AnswerBody text={m.text} />
                    <StructuredBlocks data={m.structured} />
                    <QueryResult query={m.query} chart={m.chart} />
                    <OutcomePredictionCard data={m.prediction} />
                    {m.retry && !busy && (
                      <button
                        onClick={() => send(m.retry)}
                        className="mt-2 rounded-full border border-[#c8102e] px-2.5 py-1 text-[0.6875rem] font-bold text-[#292326] hover:bg-[#f8f1f1] dark:border-blue-500 dark:text-blue-300 dark:hover:bg-[#120b0e]"
                      >
                        Retry
                      </button>
                    )}
                    {m.actions?.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5 border-t border-[#e8dadc] pt-2 dark:border-[#351923]">
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
                                : "rounded-full bg-[#c8102e] px-2.5 py-1 text-[0.6875rem] font-bold text-white transition hover:bg-[#a50f29] dark:bg-blue-600 dark:hover:bg-blue-500"}
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
                <div className="flex items-center gap-1.5 rounded-2xl border border-[#e8dadc] bg-[#faf7f6] px-3 py-2.5 dark:border-[#351923] dark:bg-[#0d090b]">
                  <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[#c8102e] dark:bg-blue-400" />
                  <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[#c8102e] dark:bg-blue-400" />
                  <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[#c8102e] dark:bg-blue-400" />
                </div>
              </div>
            )}
          </div>

          {/* Suggestions */}
          {suggestions.length > 0 && !busy && !showHistory && (
            <div className="flex flex-none flex-wrap gap-1.5 border-t border-[#e8dadc] px-3 pt-2.5 dark:border-[#351923]">
              {suggestions.slice(0, 5).map((s, i) => (
                <button
                  key={`${i}-${s}`}
                  onClick={() => send(s)}
                  className="rounded-full border border-[#e8dadc] bg-white px-2.5 py-1 text-[0.6875rem] font-semibold text-[#292326] transition hover:bg-[#f8f1f1] dark:border-[#351923] dark:bg-[#000000] dark:text-[#f5eff1] dark:hover:bg-[#0d090b]"
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
              className="assistant-input min-w-0 flex-1 resize-none overflow-y-auto rounded-2xl border border-[#e8dadc] bg-white px-3.5 py-2.5 text-[0.8125rem] outline-none focus:border-[#c8102e] dark:border-[#351923] dark:bg-[#0d090b] dark:text-gray-100 dark:focus:border-blue-500"
            />
            <button
              onClick={() => send()}
              disabled={busy || !input.trim()}
              aria-label="Send"
              className="flex h-9 w-9 flex-none items-center justify-center rounded-full bg-[#c8102e] text-white transition hover:bg-[#a50f29] disabled:opacity-40 dark:bg-blue-600 dark:hover:bg-blue-500"
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
