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

  const handleBlur = useCallback((e) => {
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
      className="fixed left-0 right-0 top-0 z-[70] h-[64px] lg:h-[72px] border-b border-[#dde1e5] bg-[#f4f5f6] text-[#30343A] dark:border-[#39434E] dark:bg-[#171b20] dark:text-[#f1f3f5] transition-colors duration-300"
    >
      <div
        className={`
flex h-full min-w-0 items-center gap-1.5 px-2 sm:gap-3 sm:px-4 lg:gap-4 lg:px-5 transition-all duration-300 ${
          isMobile || collapsed ? "" : "pl-60"
        }
        `}
        style={{
          paddingLeft: isMobile ? "1.25rem" : collapsed ? "0.75rem" : undefined,
        }}
      >
        {/* MENU BUTTON */}
        <button
          onClick={onMenuClick}
          className="
            flex h-10 w-10 shrink-0 items-center
            justify-center rounded-lg
            border border-[#dde1e5]
            bg-[#eceff2]
            text-[#30343A]
            transition-all duration-200
            hover:bg-[#eceff2]
            hover:border-[#46566a]
            hover:shadow-sm
            active:scale-95
            dark:border-[#39434E]
            dark:bg-[#222831]
            dark:text-[#f1f3f5]
            dark:hover:bg-[#2c3540]
            dark:hover:border-[#7c8795]
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
                      ? `bg-[#eceff2] font-semibold text-[#30343A] dark:bg-[#222831] dark:text-white`
                      : `text-[#69717c] hover:bg-[#eceff2] hover:text-[#30343A] dark:text-[#cbd2d9] dark:hover:bg-[#222831] dark:hover:text-white`
                  }
                `}
              >
                {item}
                {active && (
                  <span
                    className="
                      absolute bottom-0 left-2 right-2
                      h-[2px] rounded-full
                      bg-[#46566a]
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
              text-[#7c8795]
              dark:text-[#aab3bd]
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
              h-10 w-full rounded-lg
              border border-[#dde1e5]
              bg-white
              pl-9 pr-3
              text-sm text-[#30343A]
              outline-none
              placeholder:text-[#aab3bd]
              focus:border-[#46566a] focus:ring-1 focus:ring-[#46566a]
              dark:border-[#39434E] dark:bg-[#222831] dark:text-[#f1f3f5]
              dark:placeholder:text-[#69717c]
              dark:focus:border-[#7c8795] dark:focus:ring-[#7c8795]
            "
          />

          {/* ── Recent Searches Dropdown ── */}
          {showHistory && (
            <div
              className="
                pop-in-down
                absolute left-0 right-0 top-full z-[80] mt-1
                overflow-hidden rounded-xl
                border border-[#dde1e5]
                bg-white
                shadow-soft
                dark:border-[#39434E] dark:bg-[#222831]
              "
            >
              {historyCleared ? (
                <div className="px-4 py-3 text-xs text-[#aab3bd] dark:text-[#69717c]">
                  History cleared
                </div>
              ) : history.length === 0 ? (
                <div className="px-4 py-3 text-xs text-[#aab3bd] dark:text-[#69717c]">
                  No recent searches
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between border-b border-[#eceff2] px-4 py-2 dark:border-[#39434E]">
                    <span className="text-[0.6875rem] font-semibold uppercase tracking-wide text-[#aab3bd] dark:text-[#69717c]">
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
                            text-left text-sm text-[#2c3540]
                            transition-colors
                            hover:bg-[#f4f5f6]
                            dark:text-[#cbd2d9]
                            dark:hover:bg-[#2c3540]
                          "
                        >
                          <span className="shrink-0 text-[#aab3bd] dark:text-[#69717c]">
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
              text-[#7c8795] dark:text-[#69717c]
            "
          >
            FY
          </span>
          <select
            value={selectedFY || ""}
            onChange={(e) => onFYChange && onFYChange(e.target.value)}
            className="
              h-10 rounded-lg
              border border-[#dde1e5] bg-white
              px-3 text-sm font-medium text-[#30343A]
              outline-none
              focus:border-[#46566a] focus:ring-1 focus:ring-[#46566a]
              dark:border-[#39434E] dark:bg-[#222831] dark:text-[#f1f3f5]
              dark:focus:border-[#7c8795] dark:focus:ring-[#7c8795]
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
