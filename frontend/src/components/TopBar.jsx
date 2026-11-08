import { getFYs } from "../services/api"
import ProfileMenu from "./ProfileMenu"
import { useEffect, useState, useRef, useCallback } from "react"

// ─── Search History (localStorage) ──────────────────────────────────
const HISTORY_KEY = "mplads_recent_searches"
const MAX_HISTORY = 10

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function saveHistory(history) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history))
  } catch {
    // ignore
  }
}

function addToHistory(query) {
  const trimmed = query.trim()
  if (!trimmed) return loadHistory()
  const history = loadHistory().filter((h) => h !== trimmed)
  history.unshift(trimmed)
  const sliced = history.slice(0, MAX_HISTORY)
  saveHistory(sliced)
  return sliced
}

function clearHistory() {
  try {
    localStorage.removeItem(HISTORY_KEY)
  } catch {
    // ignore
  }
  return []
}

// ─── TopBar Component ──────────────────────────────────────────────
function TopBar({
  collapsed,
  onMenuClick,
  currentPage,
  onNavigate,
  searchQuery,
  onSearchChange,
  onNavigateToResult,
  isMobile,
  selectedFY,
  onFYChange,
  onOpenCommandPalette,
}) {
  const [availableFYs, setAvailableFYs] = useState([])
  const [history, setHistory] = useState(loadHistory)
  const [showHistory, setShowHistory] = useState(false)
  const [historyCleared, setHistoryCleared] = useState(false)
  const inputRef = useRef(null)
  const historyRef = useRef(null)

  const handleSearchChange = (e) => {
    onSearchChange(e.target.value)
    // Hide history when user starts typing
    if (e.target.value.length > 0) setShowHistory(false)
  }

  const handleSearchSubmit = useCallback(() => {
    if (searchQuery && searchQuery.trim()) {
      const updated = addToHistory(searchQuery.trim())
      setHistory(updated)
      setShowHistory(false)
      onNavigateToResult("Projects", { keyword: searchQuery.trim() })
    }
  }, [searchQuery, onNavigateToResult])

  const handleHistoryClick = useCallback(
    (term) => {
      onSearchChange(term)
      // Move to top of history
      const updated = addToHistory(term)
      setHistory(updated)
      setShowHistory(false)
      onNavigateToResult("Projects", { keyword: term })
    },
    [onSearchChange, onNavigateToResult]
  )

  const handleClearHistory = useCallback((e) => {
    e.stopPropagation()
    const updated = clearHistory()
    setHistory(updated)
    setHistoryCleared(true)
    // Brief flash then reset so the "cleared" message disappears
    setTimeout(() => setHistoryCleared(false), 1200)
  }, [])

  const handleFocus = useCallback(() => {
    // Show history when focused and input is empty
    const h = loadHistory()
    setHistory(h)
    if ((!searchQuery || !searchQuery.trim()) && h.length > 0) {
      setShowHistory(true)
    }
  }, [searchQuery])

  const handleBlur = useCallback(() => {
    // Delay hiding so click on history item registers first
    setTimeout(() => {
      if (!historyRef.current?.contains(document.activeElement)) {
        setShowHistory(false)
      }
    }, 150)
  }, [])

  // Close dropdown on outside click
  useEffect(() => {
    if (!showHistory) return
    const handler = (e) => {
      if (
        historyRef.current &&
        !historyRef.current.contains(e.target) &&
        e.target !== inputRef.current
      ) {
        setShowHistory(false)
      }
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [showHistory])

  useEffect(() => {
    getFYs()
      .then((data) => {
        if (Array.isArray(data) && data.length > 0 && data[0] && typeof data[0].fy === "string") {
          setAvailableFYs(data)
        } else {
          setAvailableFYs([
            { fy: "2023-24", count: 8562 },
            { fy: "2024-25", count: 19203 },
            { fy: "2025-26", count: 50274 },
            { fy: "2026-27", count: 5192 },
          ])
        }
      })
      .catch(() => {
        setAvailableFYs([
          { fy: "2023-24", count: 8562 },
          { fy: "2024-25", count: 19203 },
          { fy: "2025-26", count: 50274 },
          { fy: "2026-27", count: 5192 },
        ])
      })
  }, [])

  const navigation = [
    "Projects",
    "Risk Center",
    "Audit Priority",
    "State Intelligence",
    "Vendor Intelligence",
  ]

  return (
    <header
      className="fixed left-0 right-0 top-0 z-[70] h-[64px] lg:h-[72px] border-b border-[#e8dadc] bg-[#faf7f6] text-[#292326] dark:border-[#351923] dark:bg-[#000000] dark:text-[#f5eff1] transition-colors duration-300"
    >
      <div
        className={`flex h-full min-w-0 items-center gap-2 px-3 transition-all duration-300 sm:gap-3 sm:px-4 lg:gap-4 lg:px-5 ${
          collapsed ? "lg:pl-3" : ""
        }`}
      >
        {/* MENU BUTTON */}
        <button
          onClick={onMenuClick}
          className="
            flex h-10 w-10 shrink-0 items-center
            justify-center rounded-full
            border border-[#e8dadc]
            bg-[#f8f1f1]
            text-[#292326]
            transition-all duration-200
            hover:bg-[#f8f1f1]
            hover:border-[#c8102e]
            hover:shadow-sm
            active:scale-95
            dark:border-[#351923]
            dark:bg-[#0d090b]
            dark:text-[#f5eff1]
            dark:hover:bg-[#120b0e]
            dark:hover:border-[#8b7d81]
          "
          aria-label="Toggle sidebar"
          title="Toggle sidebar (Ctrl+B)"
        >
          <span className="text-xl leading-none font-bold">☰</span>
        </button>

        {/* NAVIGATION */}
        <nav className="hidden shrink-0 items-center gap-1 xl:gap-1.5 xl:flex">
          {navigation.map((item) => {
            const active = currentPage === item
            return (
              <button
                key={item}
                onClick={() => onNavigate(item)}
                className={`
                  relative whitespace-nowrap
                  rounded-md px-2.5 py-1.5
                  text-[0.8125rem] font-medium
                  transition-all duration-150
                  ${
                    active
                      ? `bg-[#f8f1f1] font-semibold text-[#292326] dark:bg-[#3a0d1c] dark:text-white`
                      : `text-[#71666a] hover:bg-[#f8f1f1] hover:text-[#292326] dark:text-[#cdc1c5] dark:hover:bg-[#0d090b] dark:hover:text-white`
                  }
                `}
              >
                {item}
                {active && (
                  <span
                    className="
                      absolute bottom-0 left-2 right-2
                      h-[2px] rounded-full
                      bg-[#c8102e]
                    "
                  />
                )}
              </button>
            )
          })}
        </nav>

        {/* SEARCH + HISTORY DROPDOWN — takes all horizontal space left between
            the (left-aligned) navigation and the right-hand FY / sign-in cluster */}
        <div className="relative min-w-0 flex-1" ref={historyRef}>
          <span
            className="
              pointer-events-none absolute inset-y-0 left-3
              flex items-center
              text-[#7a6f74]
              dark:text-[#b9aeb2]
            "
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-[1.125rem] w-[1.125rem]"
              aria-hidden="true"
            >
              <circle cx="11" cy="11" r="8" />
              <path d="m21 21-4.3-4.3" />
            </svg>
          </span>

          <input
            ref={inputRef}
            type="text"
            placeholder={isMobile ? "Search..." : "Search projects, states, constituency, MP..."}
            value={searchQuery}
            onChange={handleSearchChange}
            onFocus={handleFocus}
            onBlur={handleBlur}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault()
                handleSearchSubmit()
              }
              if (e.key === "Escape") {
                setShowHistory(false)
                inputRef.current?.blur()
              }
            }}
            className="
              h-10 w-full min-w-0 rounded-lg
              border border-[#e8dadc]
              bg-white
              pl-9 pr-14
              text-sm text-[#292326]
              outline-none
              placeholder:text-[#b9aeb2]
              focus:border-[#c8102e] focus:ring-1 focus:ring-[#c8102e]
              dark:border-[#351923] dark:bg-[#0d090b] dark:text-[#f5eff1]
              dark:placeholder:text-[#82767b]
              dark:focus:border-[#8b7d81] dark:focus:ring-[#8b7d81]
            "
          />

          {/* Command palette trigger — the visible, mobile-friendly entry
              point for the same interface Ctrl/Cmd+K opens. */}
          {onOpenCommandPalette && (
            <button
              type="button"
              onClick={() => {
                setShowHistory(false)
                onOpenCommandPalette()
              }}
              aria-label="Open command palette"
              title="Command palette (Ctrl+K)"
              className="
                absolute right-2 top-1/2 flex h-6 min-w-6 -translate-y-1/2 items-center justify-center gap-1
                rounded-md border border-[#e8dadc] bg-[#faf7f6] px-1.5
                text-[0.625rem] font-semibold leading-none text-[#7a6f74]
                transition-colors
                hover:border-[#c8102e] hover:text-[#c8102e]
                dark:border-[#351923] dark:bg-[#140c10] dark:text-[#b9aeb2]
                dark:hover:border-[#8b7d81] dark:hover:text-[#f5eff1]
              "
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                aria-hidden="true"
                className="h-3 w-3"
              >
                <path d="M9 9V6a3 3 0 1 0-3 3h3Zm0 0v6m0-6h6m-6 6H6a3 3 0 1 0 3 3v-3Zm6-6h3a3 3 0 1 0-3-3v3Zm0 0v6m0 0h3a3 3 0 1 1-3 3v-3Z" />
              </svg>
              <span>K</span>
            </button>
          )}

          {/* ── Recent Searches Dropdown ── */}
          {showHistory && (
            <div
              className="
                pop-in-down
                absolute left-0 right-0 top-full z-[80] mt-1
                overflow-hidden rounded-xl
                border border-[#e8dadc]
                bg-white
                shadow-soft
                dark:border-[#351923] dark:bg-[#0d090b]
              "
            >
              {historyCleared ? (
                <div className="px-4 py-3 text-xs text-[#b9aeb2] dark:text-[#82767b]">
                  History cleared
                </div>
              ) : history.length === 0 ? (
                <div className="px-4 py-3 text-xs text-[#b9aeb2] dark:text-[#82767b]">
                  No recent searches
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between border-b border-[#f8f1f1] px-4 py-2 dark:border-[#351923]">
                    <span className="text-[0.6875rem] font-semibold uppercase tracking-wide text-[#b9aeb2] dark:text-[#82767b]">
                      Recent Searches
                    </span>
                    <button
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={handleClearHistory}
                      className="
                        text-[0.6875rem] font-medium text-[#e14b4b]
                        transition-colors hover:text-[#c43c3c]
                        dark:text-[#ef9494] dark:hover:text-[#f7c9c9]
                      "
                    >
                      Clear History
                    </button>
                  </div>
                  <ul className="max-h-[280px] overflow-y-auto py-1">
                    {history.map((term, i) => (
                      <li key={`${term}-${i}`}>
                        <button
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={() => handleHistoryClick(term)}
                          className="
                            flex w-full items-center gap-3
                            px-4 py-2.5
                            text-left text-sm text-[#120b0e]
                            transition-colors
                            hover:bg-[#faf7f6]
                            dark:text-[#cdc1c5]
                            dark:hover:bg-[#120b0e]
                          "
                        >
                          <span className="shrink-0 text-[#b9aeb2] dark:text-[#82767b]">
                            🕒
                          </span>
                          <span className="truncate">{term}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          )}
        </div>

        {/* FISCAL YEAR */}
        <div
          className="
            hidden items-center gap-1.5
            whitespace-nowrap
            2xl:flex
          "
        >
          <span
            className="
              text-[0.6875rem] font-semibold uppercase tracking-wide
              text-[#7a6f74] dark:text-[#82767b]
            "
          >
            FY
          </span>
          <select
            value={selectedFY || ""}
            onChange={(e) => onFYChange && onFYChange(e.target.value)}
            className="
              h-10 rounded-lg
              border border-[#e8dadc] bg-white
              px-3 text-sm font-medium text-[#292326]
              outline-none
              focus:border-[#c8102e] focus:ring-1 focus:ring-[#c8102e]
              dark:border-[#351923] dark:bg-[#0d090b] dark:text-[#f5eff1]
              dark:focus:border-[#8b7d81] dark:focus:ring-[#8b7d81]
            "
          >
            <option value="">All FY</option>
            {availableFYs.map((item) => (
              <option key={item.fy} value={item.fy}>
                {item.fy} ({item.count.toLocaleString("en-IN")})
              </option>
            ))}
          </select>
        </div>

        {/* PROFILE / SIGN-IN */}
        <ProfileMenu onNavigate={onNavigate} />
      </div>
    </header>
  )
}

export default TopBar
