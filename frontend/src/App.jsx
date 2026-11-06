import { useState, useEffect, lazy, Suspense, memo, useRef } from "react"
import Sidebar from "./components/Sidebar"
import AssistantWidget from "./components/AssistantWidget"
import TopBar from "./components/TopBar"
import { PageSkeleton } from "./components/Skeleton"
import { AuthProvider, useAuth } from "./context/AuthContext"
import { executeAssistantAction } from "./utils/executeActions"
import { invalidateAPICache } from "./services/api"

/* Application shell — single scroll-owner architecture:
   html/body never scroll; <main> is the only page-level scroll container
   (header and sidebar are viewport-fixed; overlays are viewport-bound).
   Page roots use min-h-full (not min-h-screen) so they size to <main>. */

const LoginView = lazy(() => import("./pages/Auth").then((m) => ({ default: m.LoginView })))
const SignupView = lazy(() => import("./pages/Auth").then((m) => ({ default: m.SignupView })))
const SavedProjectsPage = lazy(() => import("./pages/Workspace").then((m) => ({ default: m.SavedProjectsPage })))
const MyInvestigationsPage = lazy(() => import("./pages/Workspace").then((m) => ({ default: m.MyInvestigationsPage })))
const MyAuditCasesPage = lazy(() => import("./pages/Workspace").then((m) => ({ default: m.MyAuditCasesPage })))
const AdminPage = lazy(() => import("./pages/Workspace").then((m) => ({ default: m.AdminPage })))
const DataManagementPage = lazy(() => import("./pages/DataManagement"))
const MyVerificationsPage = lazy(() => import("./pages/MyVerifications"))
const MyDistrictPage = lazy(() => import("./pages/MyDistrict"))
const InquiriesPage = lazy(() => import("./pages/Inquiries"))
const VerificationQueuePage = lazy(() => import("./pages/VerificationQueue"))
const SettingsPage = lazy(() => import("./pages/Settings"))

// Branch addition: public landing pitch page before entering the app
const Landing = lazy(() => import("./pages/Landing"))

// Lazy-load page components — only the active page is loaded
const Overview = lazy(() => import("./pages/Overview"))
const Leaderboard = lazy(() => import("./pages/Leaderboard"))
const MPIntelligence = lazy(() => import("./pages/MPIntelligence"))
const Projects = lazy(() => import("./pages/Projects"))
const RiskCenter = lazy(() => import("./pages/RiskCenter"))
const Reports = lazy(() => import("./pages/Reports"))
const StateIntelligence = lazy(() => import("./pages/StateIntelligence"))
const AuditPriority = lazy(() => import("./pages/AuditPriority"))
const CompareProjects = lazy(() => import("./pages/CompareProjects"))
const FAQ = lazy(() => import("./pages/FAQ"))
const VendorIntelligence = lazy(() => import("./pages/VendorIntelligence"))
const AuditCenter = lazy(() => import("./pages/AuditCenter"))
const VendorNetwork = lazy(() => import("./pages/VendorNetwork"))
const VerifyPortal = lazy(() => import("./pages/VerifyPortal"))

/* ── Global scroll manager ─────────────────────────────────────────────
   The app shell (AppShell) has exactly one page-level scroll container:
   <main>. html/body never scroll, so the browser's default scroll-chaining
   targets the document and dies — which felt like "the wheel stops working"
   when the cursor happened to be over the fixed header or sidebar.

   This manager restores normal browser feel globally, without changing any
   visuals or behavior:
   • Wheel/trackpad over any chrome (header, sidebar, non-scrollable page
     regions) scrolls <main> — the same one predictable vertical context.
   • PageUp/PageDown/Home/End/Space scroll <main> (keyboard used to target
     the locked document scroller and do nothing).
   • While any overlay (drawer/modal) is open, wheel/keys are inert so the
     locked background stays put; the overlay's own panel keeps its native
     scrolling. Closing the overlay instantly restores wheel/keys to <main>.
   • Stray-lock guard: if a component ever leaves body overflow locked
     ("hidden") with no overlay open, unlock it — scroll lock state must
     never stick after a modal unmounts. */
function ScrollManager() {
  useEffect(() => {
    const mainScroller = () => document.querySelector("main")
    // Overlay detection: every real overlay in this app (project drawer,
    // modals, vendor profile, mobile backdrop) is a position:fixed DIV.
    // All app chrome is a fixed HEADER (top bar) or ASIDE (sidebars) —
    // excluded by tag. A translated-off-screen drawer fails the viewport-
    // intersection test, so a closed mobile sidebar never reads as an overlay.
    const overlayOpen = () => {
      return Array.from(document.querySelectorAll("div.fixed")).some((el) => {
        const cs = getComputedStyle(el)
        if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0) return false
        const r = el.getBoundingClientRect()
        if (r.width === 0 || r.height === 0) return false
        const vw = window.innerWidth, vh = window.innerHeight
        const intersects = r.left < vw * 0.9 && r.right > vw * 0.1 && r.top < vh * 0.9 && r.bottom > vh * 0.1
        return intersects
      })
    }

    const KEY_SCROLL_KEYS = new Set(["PageUp", "PageDown", "Home", "End", " "])
    const onKey = (e) => {
      if (e.ctrlKey || e.altKey || e.metaKey) return
      const t = e.target
      if (t instanceof HTMLElement && t.matches("input, textarea, select")) return
      if (!KEY_SCROLL_KEYS.has(e.key)) return
      if (overlayOpen()) return
      const el = mainScroller()
      if (!el) return
      const page = el.clientHeight * 0.9
      let next = null
      if (e.key === "PageDown" || e.key === " ") next = el.scrollTop + page
      else if (e.key === "PageUp") next = el.scrollTop - page
      else if (e.key === "Home") next = 0
      else if (e.key === "End") next = el.scrollHeight
      if (next === null) return
      e.preventDefault()
      el.scrollTo({ top: Math.max(0, Math.min(el.scrollHeight - el.clientHeight, next)), behavior: "auto" })
    }

    const onWheel = (e) => {
      // Never hijack native scrolling inside an existing scrollable region
      // (main, overlay panels, tables, dropdowns): the browser handles it.
      let node = e.target instanceof Element ? e.target : null
      while (node && node !== document.body) {
        if (node instanceof HTMLElement) {
          const cs = getComputedStyle(node)
          const canScrollY = /(auto|scroll|overlay)/.test(cs.overflowY)
          if (canScrollY && node.scrollHeight > node.clientHeight + 1) return
        }
        node = node.parentElement
      }
      // Cursor over chrome / inert page surface → scroll the main region.
      if (overlayOpen()) return // background stays locked while an overlay is up
      const el = mainScroller()
      if (!el) return
      el.scrollTop += e.deltaY
    }

    document.addEventListener("wheel", onWheel, { passive: true })
    document.addEventListener("keydown", onKey)
    if (typeof window !== "undefined") window.__scrollManagerActive = true // dev fingerprint

    // Stray-lock guard: body must never stay overflow:hidden with no overlay
    let unlockTimer = null
    const watchdog = () => {
      if (overlayOpen()) return
      if (document.body.style.overflow === "hidden") {
        clearTimeout(unlockTimer)
        unlockTimer = setTimeout(() => {
          if (!overlayOpen() && document.body.style.overflow === "hidden") {
            document.body.style.overflow = ""
            console.warn("[ScrollManager] released stray body scroll lock")
          }
        }, 80)
      }
    }
    const mo = new MutationObserver(watchdog)
    mo.observe(document.body, { attributes: true, attributeFilter: ["style"], subtree: false })

    return () => {
      document.removeEventListener("wheel", onWheel)
      document.removeEventListener("keydown", onKey)
      mo.disconnect()
      clearTimeout(unlockTimer)
    }
  }, [])

  return null
}

function useIsMobile() {
  const [isMobile, setIsMobile] = useState(() =>
    typeof window !== "undefined" ? window.innerWidth < 1024 : false
  )
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 1023px)")
    const handler = (e) => setIsMobile(e.matches)
    mq.addEventListener("change", handler)
    setIsMobile(mq.matches)
    return () => mq.removeEventListener("change", handler)
  }, [])
  return isMobile
}

function AppShell() {
  const { user, loading: authLoading } = useAuth()
  ScrollManager()
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [darkMode, setDarkMode] = useState(false)
  // Default to main Overview dashboard so all options (Reports, Leaderboard, etc.) are immediately visible
  const [currentPage, setCurrentPage] = useState(() => {
    if (typeof window !== "undefined") {
      if (window.location.hash === "#signin") return "Sign in"
      if (window.location.hash === "#landing") return "Landing"
    }
    return "Overview"
  })
  // Global search — independent from project search
  const [globalSearchQuery, setGlobalSearchQuery] = useState("")
  // Project search — only set when navigating to Projects
  const [projectSearchQuery, setProjectSearchQuery] = useState("")
  const [drillDownParams, setDrillDownParams] = useState(null)
  const [selectedFY, setSelectedFY] = useState("")
  // Shared House scope: Overview and State Intelligence are two views of the
  // same portfolio, so switching pages must not silently change the House.
  const [selectedHouse, setSelectedHouse] = useState("All")

  const isMobile = useIsMobile()
  // Start closed: the drawer must never cover page content on load — it opens
  // via the menu button (or Ctrl+B) and closes on navigate/backdrop.
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false)
  const [assistantOpen, setAssistantOpen] = useState(false)
  // MP Intelligence page context ("this MP" for the assistant): id of the MP
  // whose detail view is currently open, or null.
  const [assistantMpId, setAssistantMpId] = useState(null)
  // State Intelligence page context: the state name currently drilled into.
  const [assistantState, setAssistantState] = useState(null)

  const handleNavigate = (page) => {
    setCurrentPage(page)
    if (isMobile) setMobileDrawerOpen(false)
  }

  const toggleSidebar = () => {
    if (isMobile) {
      setMobileDrawerOpen((prev) => !prev)
    } else {
      setSidebarCollapsed((previous) => !previous)
    }
  }

  const closeMobileDrawer = () => setMobileDrawerOpen(false)

  // Keyboard shortcut: Ctrl+B or [ to toggle sidebar
  useEffect(() => {
    const handler = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "b") {
        e.preventDefault()
        toggleSidebar()
      }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [isMobile])

  // Listen for navigate-to-project events from any component (e.g. RiskCenter modal)
  useEffect(() => {
    const handler = (e) => {
      const query = e.detail?.query
      if (query) {
        setProjectSearchQuery(query)
        setCurrentPage("Projects")
        if (isMobile) setMobileDrawerOpen(false)
      }
    }
    window.addEventListener("navigate-to-project", handler)
    return () => window.removeEventListener("navigate-to-project", handler)
  }, [isMobile])

  // Global search submit: only navigate when user explicitly selects a result
  const handleGlobalSearchNavigate = (page, params) => {
    setGlobalSearchQuery("")
    if (params?.keyword) {
      setProjectSearchQuery(params.keyword)
    }
    setCurrentPage(page)
    if (isMobile) setMobileDrawerOpen(false)
  }

  const handleDrillDown = (page, params) => {
    setDrillDownParams(params || null)
    setCurrentPage(page)
    if (isMobile) setMobileDrawerOpen(false)
  }

  // Open a project drawer from a workspace page: switch to Projects, then
  // dispatch once the page is mounted (retry covers the lazy chunk load).
  const openProjectFromWorkspace = (projectId, tab) => {
    setCurrentPage("Projects")
    if (isMobile) setMobileDrawerOpen(false)
    const dispatch = () => window.dispatchEvent(new CustomEvent("open-project", { detail: { projectId, tab } }))
    setTimeout(dispatch, 400)
    setTimeout(dispatch, 1200)
  }

  // Context for the AI Assistant: which project the user is looking at
  // ("project:<id>" page tag) so "why is this risky?" resolves correctly.
  // Sources: Projects' drawer (open-project), Risk Center / Audit Priority /
  // Compare Projects / Project Detail modals (assistant-project-context).
  // Context clears when the details surface close (explicit null events) or
  // the page changes. Names ride along for display; ids stay authoritative.
  const [assistantProjectId, setAssistantProjectId] = useState(null)
  useEffect(() => {
    const fromProjects = (e) => setAssistantProjectId(e.detail?.projectId || null)
    const fromDetailModals = (e) => setAssistantProjectId(e.detail?.projectId || null)
    window.addEventListener("open-project", fromProjects)
    window.addEventListener("assistant-project-context", fromDetailModals)
    return () => {
      window.removeEventListener("open-project", fromProjects)
      window.removeEventListener("assistant-project-context", fromDetailModals)
    }
  }, [])
  useEffect(() => {
    if (currentPage !== "Projects") setAssistantProjectId(null)
  }, [currentPage])

  // Display names for the assistant's project-context indicator. Ids remain
  // the only authoritative key (backend re-resolves everything); this map is
  // purely cosmetic and bounded.
  const [assistantProjectNames, setAssistantProjectNames] = useState({})
  useEffect(() => {
    const remember = (e) => {
      const { projectId, projectName } = e.detail || {}
      if (projectId && projectName) {
        setAssistantProjectNames((prev) => (prev[projectId] === projectName ? prev : { ...prev, [projectId]: projectName }))
      }
    }
    window.addEventListener("assistant-project-context", remember)
    return () => window.removeEventListener("assistant-project-context", remember)
  }, [])
  const getAssistantProjectName = (pid) => (pid ? assistantProjectNames[pid] || null : null)

  // ── Assistant action execution (JARVIS control plane) ────────────────
  // The assistant returns structured, backend-validated actions; they are
  // executed here through the app's EXISTING mechanisms (handleNavigate /
  // handleDrillDown / openProjectFromWorkspace) — no second router.
  // clear_filters / refresh_data are broadcast as events; filter pages
  // reset their OWN existing filter state on "assistant:clear-filters"
  // and re-run their existing loads on "assistant:refresh".
  const executeAssistant = (action) => {
    return executeAssistantAction(action, {
      navigate: handleNavigate,
      drillDown: handleDrillDown,
      openProject: openProjectFromWorkspace,
      clearFilters: () => window.dispatchEvent(new CustomEvent("assistant:clear-filters")),
      refreshData: () => {
        invalidateAPICache("")
        window.dispatchEvent(new CustomEvent("assistant:refresh"))
      },
      hasPermission: (page) => {
        // Defense-in-depth only — the backend already role-gates every
        // action. Guests never reach workspace-only pages.
        const GUEST_PAGES = new Set([
          "Overview", "Projects", "Risk Center", "AI Audit Center", "Vendor Network",
          "Ground Truth Verification", "Reports", "Leaderboard", "State Intelligence",
          "Audit Priority", "Compare Projects", "FAQ", "Vendor Intelligence", "Settings",
          "MP Intelligence",
        ])
        if (user) return true
        return GUEST_PAGES.has(page)
      },
    })
  }

  // Page state preservation: track which pages have been visited so we keep them mounted
  const visitedPages = useRef(new Set(["Overview"]))
  // Add current page to visited set synchronously (not in useEffect)
  if (!visitedPages.current.has(currentPage)) {
    visitedPages.current.add(currentPage)
  }

  const renderPage = () => {
    // Auth pages render standalone (no sidebar nav entry, still inside shell)
    if (currentPage === "Sign in") return <LoginView onSwitch={() => setCurrentPage("Sign up")} />
    if (currentPage === "Sign up") return <SignupView onSwitch={() => setCurrentPage("Sign in")} />

    const pages = [
      { key: "Overview", el: <Overview darkMode={darkMode} onDrillDown={handleDrillDown} fy={selectedFY} house={selectedHouse} onHouseChange={setSelectedHouse} /> },
      { key: "Projects", el: <Projects projectSearchQuery={projectSearchQuery} onClearProjectSearch={() => setProjectSearchQuery("")} drillDownParams={drillDownParams} onClearDrillDown={() => setDrillDownParams(null)} fy={selectedFY} onNavigate={handleNavigate} /> },
      { key: "Risk Center", el: <RiskCenter drillDownParams={drillDownParams} onClearDrillDown={() => setDrillDownParams(null)} fy={selectedFY} /> },
      { key: "AI Audit Center", el: <AuditCenter onOpenProject={openProjectFromWorkspace} fy={selectedFY} /> },
      { key: "Vendor Network", el: <VendorNetwork /> },
      { key: "Ground Truth Verification", el: <VerifyPortal /> },
      { key: "Reports", el: <Reports fy={selectedFY} /> },
      { key: "Leaderboard", el: <Leaderboard darkMode={darkMode} /> },
      { key: "State Intelligence", el: <StateIntelligence darkMode={darkMode} house={selectedHouse} onHouseChange={setSelectedHouse} onNavigateToProjects={(state) => handleDrillDown("Projects", { state })} onContextChange={setAssistantState} fy={selectedFY} drillDownParams={drillDownParams} onClearDrillDown={() => setDrillDownParams(null)} /> },
      { key: "MP Intelligence", el: <MPIntelligence darkMode={darkMode} drillDownParams={drillDownParams} onClearDrillDown={() => setDrillDownParams(null)} fy={selectedFY} onContextChange={setAssistantMpId} /> },
      { key: "Audit Priority", el: <AuditPriority fy={selectedFY} drillDownParams={drillDownParams} onClearDrillDown={() => setDrillDownParams(null)} /> },
      { key: "Compare Projects", el: <CompareProjects fy={selectedFY} drillDownParams={drillDownParams} onClearDrillDown={() => setDrillDownParams(null)} /> },
      { key: "FAQ", el: <FAQ /> },
      { key: "Vendor Intelligence", el: <VendorIntelligence onOpenProject={openProjectFromWorkspace} drillDownParams={drillDownParams} onClearDrillDown={() => setDrillDownParams(null)} /> },
      { key: "Settings", el: <SettingsPage darkMode={darkMode} onThemeToggle={() => setDarkMode((previous) => !previous)} /> },
      { key: "Saved Projects", el: <SavedProjectsPage onOpenProject={openProjectFromWorkspace} /> },
      { key: "My Investigations", el: <MyInvestigationsPage onOpenProject={openProjectFromWorkspace} /> },
      { key: "My Audit Cases", el: <MyAuditCasesPage onOpenProject={openProjectFromWorkspace} /> },
      { key: "My Verifications", el: <MyVerificationsPage onOpenProject={openProjectFromWorkspace} /> },
      { key: "My District", el: <MyDistrictPage onOpenProject={openProjectFromWorkspace} /> },
      { key: "Inquiries", el: <InquiriesPage onOpenProject={openProjectFromWorkspace} /> },
      { key: "Evidence Queue", el: <VerificationQueuePage onOpenProject={openProjectFromWorkspace} /> },
      { key: "Administration", el: <AdminPage /> },
      { key: "Data Management", el: <DataManagementPage /> },
    ]
    return (
      <Suspense fallback={<PageSkeleton cards={4} columns={4} />}>
        {pages.map(({ key, el }) => (
          <div
            key={key}
            className="rise-refresh"
            style={{ display: key === currentPage ? "block" : "none" }}
          >
            {visitedPages.current.has(key) ? el : null}
          </div>
        ))}
      </Suspense>
    )
  }

  // Keep hash in sync so refresh returns to auth or landing page when intended
  useEffect(() => {
    if (currentPage === "Sign in") window.location.hash = "signin"
    else if (currentPage === "Landing") window.location.hash = "landing"
    else if (window.location.hash === "#signin" || window.location.hash === "#landing") window.location.hash = ""
  }, [currentPage])

  // After successful sign-in/sign-up, leave the auth page
  useEffect(() => {
    if (user && (currentPage === "Sign in" || currentPage === "Sign up")) {
      setCurrentPage("Overview")
    }
  }, [user]) // eslint-disable-line react-hooks/exhaustive-deps

  const getMainMargin = () => {
    if (isMobile) return "ml-0"
    return sidebarCollapsed ? "ml-16" : "ml-60"
  }

  // Branch flow: the public Landing page renders standalone (no app chrome)
  if (currentPage === "Landing") {
    return (
      <Suspense fallback={<div className="min-h-screen bg-slate-900 flex items-center justify-center text-slate-400">Loading...</div>}>
        <Landing onLaunch={() => setCurrentPage("Overview")} />
        <AssistantWidget currentPage={currentPage} />
      </Suspense>
    )
}

return (
    <div
      className={`h-[100dvh] overflow-hidden ${
        darkMode
          ? "dark bg-[#0a0a0c] text-[#f3f4f6]"
          : "bg-[#f9f9ff] text-[#151c27]"
      }`}
    >
      {isMobile && mobileDrawerOpen && (
        <div
          className="fixed inset-0 z-[55] bg-black/50 transition-opacity"
          onClick={closeMobileDrawer}
        />
      )}

      <Sidebar
        collapsed={isMobile ? false : sidebarCollapsed}
        currentPage={currentPage}
        onNavigate={handleNavigate}
        darkMode={darkMode}
        isMobile={isMobile}
        isOpen={mobileDrawerOpen}
        onClose={closeMobileDrawer}
        onSupportClick={() => setAssistantOpen(true)}
      />

      <TopBar
        collapsed={isMobile ? false : sidebarCollapsed}
        onMenuClick={toggleSidebar}
        currentPage={currentPage}
        onNavigate={handleNavigate}
        searchQuery={globalSearchQuery}
        onSearchChange={setGlobalSearchQuery}
        onNavigateToResult={handleGlobalSearchNavigate}
        isMobile={isMobile}
        selectedFY={selectedFY}
        onFYChange={setSelectedFY}
      />

      <main
        className={`h-full overflow-x-auto overflow-y-auto pt-[64px] lg:pt-[72px] transition-[margin] duration-300 ease-in-out ${getMainMargin()}`}
      >
        {renderPage()}
      </main>

      <AssistantWidget
        currentPage={currentPage}
        contextProjectId={assistantProjectId}
        contextMpId={assistantMpId}
        contextState={assistantState}
        contextProjectName={getAssistantProjectName(assistantProjectId)}
        onNavigate={handleNavigate}
        onOpenProject={openProjectFromWorkspace}
        onExecuteAction={executeAssistant}
        open={assistantOpen}
        onOpenChange={setAssistantOpen}
      />
    </div>
  )
}function App() {
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  )
}

export default App
    