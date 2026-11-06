import { useEffect, useRef } from "react"
import { useAuth } from "../context/AuthContext"

const ICON_COL = "flex h-6 w-6 shrink-0 items-center justify-center text-base leading-none"

const NAV_SVG = {
  "map-pin": <><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" /><circle cx="12" cy="10" r="3" /></>,
  bookmark: <path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z" />,
  search: <><circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" /></>,
  briefcase: <><path d="M16 20V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" /><rect width="20" height="14" x="2" y="6" rx="2" /></>,
  "user-check": <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="m16 11 2 2 4-4" /></>,
  database: <><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" /><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3" /></>,
  trophy: <><path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6" /><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18" /><path d="M4 22h16" /><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22" /><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22" /><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z" /></>,
  shield: <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />,
  headphones: <><path d="M3 18v-6a9 9 0 0 1 18 0v6" /><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z" /></>,
}

function IconFor({ icon }) {
  const paths = NAV_SVG[icon]
  if (paths) {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="h-[18px] w-[18px]"
        aria-hidden="true"
      >
        {paths}
      </svg>
    )
  }
  return icon
}

function NavRow({ icon, label, href, active, onNavigate, collapsed, badge, onClick }) {
  return (
    <button
      key={label}
      type="button"
      onClick={onClick || (() => href && onNavigate(href))}
      title={collapsed ? label : ""}
      className={`
        group relative flex h-11 min-h-11 w-full flex-none items-center gap-3 rounded px-3
        transition-colors duration-200
        ${collapsed ? "justify-center" : "justify-start"}
        ${active
          ? "bg-[#eceff2] text-[#30343A] dark:bg-[#222831] dark:text-white"
          : "text-[#69717c] hover:bg-[#eceff2] hover:text-[#30343A] dark:text-[#cbd2d9] dark:hover:bg-[#222831] dark:hover:text-white"}
        `}
    >
      {active && (
        <span className="fade-in-200 absolute right-0 top-1/2 h-8 w-1 -translate-y-1/2 rounded-l bg-[#46566a]" aria-hidden="true" />
      )}
      <span className={ICON_COL}>
        <IconFor icon={icon} />
      </span>
      <span
        className={`
          flex min-w-0 items-center text-sm font-medium transition-all duration-300
          ${collapsed ? "w-0 overflow-hidden opacity-0" : "w-auto opacity-100"}
        `}
      >
        <span className="min-w-0 truncate">{label}</span>
        {badge && (
          <span
            title="Beta feature"
            className="ml-1.5 flex-none rounded-full border border-amber-200 bg-amber-100 px-[5px] py-px text-[9px] font-bold leading-none tracking-wide text-amber-800 dark:border-amber-400/20 dark:bg-amber-400/10 dark:text-amber-300"
          >
            {badge}
          </span>
        )}
      </span>
    </button>
  )
}

function SectionHeading({ label, collapsed }) {
  return (
    <div
      className={`
        flex h-8 min-h-8 flex-none items-end pl-12 pr-3 pb-1 text-[0.625rem] font-bold uppercase tracking-wider
        text-[#69717c]/60 dark:text-[#aab3bd]/60
        ${collapsed ? "hidden" : ""}
      `}
    >
      {label}
    </div>
  )
}

function Sidebar({
  collapsed,
  currentPage,
  onNavigate,
  isMobile,
  isOpen,
  onClose,
  onSupportClick,
}) {
  const { user, hasRole, can } = useAuth()
  // Auto-hiding scrollbar: a container-level class on the scroll element
  // (no React re-render per scroll event). One passive listener per nav;
  // thumb fades out ~900ms after scrolling stops.
  const navRef = useRef(null)
  useEffect(() => {
    const el = navRef.current
    if (!el) return
    let hideTimer
    const onScroll = () => {
      el.classList.add("sidebar-scrolling")
      clearTimeout(hideTimer)
      hideTimer = setTimeout(() => el.classList.remove("sidebar-scrolling"), 900)
    }
    el.addEventListener("scroll", onScroll, { passive: true })
    return () => {
      el.removeEventListener("scroll", onScroll)
      clearTimeout(hideTimer)
    }
  }, [])
  const mainNav = [
    { name: "Overview", href: "Overview", icon: "▦" },
    { name: "Projects", href: "Projects", icon: "▤" },
    { name: "Risk Center", href: "Risk Center", icon: "⚠" },
    { name: "State Intelligence", href: "State Intelligence", icon: "map-pin" },
    { name: "MP Intelligence", href: "MP Intelligence", icon: "user-check" },
    { name: "Reports", href: "Reports", icon: "▣" },
    { name: "Leaderboard", href: "Leaderboard", icon: "trophy" },
    { name: "Compare Projects", href: "Compare Projects", icon: "⚖" },
  ]
  // Audit + vendor sections are ANALYST-tier features — hidden from the
  // lateral field roles (they never inherit the analyst workspace).
  const analystTier = user ? hasRole("analyst") : true
  // Audit section — grouped separately below a divider (detection +
  // prioritization), same NavRow grid as the main tabs.
  const auditNav = analystTier
    ? [
        { name: "AI Audit Center", href: "AI Audit Center", icon: "🛡️", badge: "NEW" },
        { name: "Audit Priority", href: "Audit Priority", icon: "🎯" },
      ]
    : []
  // Vendor section — grouped separately below a divider (same NavRow grid,
  // same icons/labels/badges; only the grouping differs).
  const vendorNav = analystTier
    ? [
        { name: "Vendor Network", href: "Vendor Network", icon: "🕸️", badge: "BETA" },
        { name: "Vendor Intelligence", href: "Vendor Intelligence", icon: "briefcase", badge: "BETA" },
      ]
    : []
  const bottomNav = [
    ...(can("verification:submit") || !user
      ? [{ label: "Ground Verification", href: "Ground Truth Verification", icon: "✅" }]
      : []),
    ...(can("inquiry:respond")
      ? [{ label: "My District", href: "My District", icon: "map-pin" }]
      : []),
    { label: "Settings", href: "Settings", icon: "⚙" },
    { label: "Support", href: null, icon: "headphones" },
    { label: "FAQ", href: "FAQ", icon: "❓" },
  ]
  const workspaceNav = []
  if (user && hasRole("analyst")) {
    workspaceNav.push({ name: "Saved Projects", href: "Saved Projects", icon: "bookmark" })
    workspaceNav.push({ name: "My Investigations", href: "My Investigations", icon: "search" })
  }
  if (user && hasRole("auditor")) {
    workspaceNav.push({ name: "My Audit Cases", href: "My Audit Cases", icon: "briefcase" })
  }
  if (user && can("inquiry:review")) {
    workspaceNav.push({ name: "Inquiries", href: "Inquiries", icon: "✉️" })
  }
  if (user && can("verification:read_own")) {
    workspaceNav.push({
      name: user.role === "citizen" ? "My Evidence" : "My Verifications",
      href: "My Verifications",
      icon: "✅",
    })
  }
  if (user && can("verification:review")) {
    workspaceNav.push({ name: "Evidence Queue", href: "Evidence Queue", icon: "📥" })
  }
  if (user && hasRole("admin")) {
    workspaceNav.push({ name: "Administration", href: "Administration", icon: "shield" })
    workspaceNav.push({ name: "Data Management", href: "Data Management", icon: "database" })
  }

  if (isMobile) {
    return (
      <>
        <aside className={`fixed left-0 top-0 z-[60] flex h-screen min-h-0 w-64 flex-col border-r border-[#dde1e5] bg-[#f4f5f6] text-[#30343A] dark:border-[#39434E] dark:bg-[#171b20] dark:text-[#f1f3f5] transition-transform duration-300 ease-in-out px-4 ${isOpen ? "translate-x-0" : "-translate-x-full"}`}>
          <div className="flex min-w-0 flex-none items-center gap-3 pb-4 pt-1">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center overflow-hidden rounded-full bg-[#2c3540] text-lg leading-none text-white dark:bg-[#39434E]">🏛</div>
            <div className="min-w-0 overflow-hidden">
              <h1 className="whitespace-nowrap text-lg font-bold text-[#30343A] dark:text-[#f1f3f5]">MPLADS Insight</h1>
              <p className="whitespace-nowrap text-xs text-[#69717c] dark:text-[#aab3bd]">Auditor Portal</p>
            </div>
          </div>
          <nav ref={navRef} className="sidebar-scroll min-h-0 flex-1 overflow-y-auto overflow-x-hidden pb-2">
            <div className="flex flex-col gap-1">
              {mainNav.map((item) => <NavRow key={item.name} icon={item.icon} label={item.name} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} badge={item.badge} collapsed={collapsed} />)}
              {auditNav.length > 0 && <div className="my-2 border-t border-[#dde1e5] dark:border-[#39434E]" role="separator" aria-label="Audit section" />}
              {auditNav.map((item) => <NavRow key={item.name} icon={item.icon} label={item.name} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} badge={item.badge} collapsed={collapsed} />)}
              {vendorNav.length > 0 && <div className="my-2 border-t border-[#dde1e5] dark:border-[#39434E]" role="separator" aria-label="Vendor section" />}
              {vendorNav.map((item) => <NavRow key={item.name} icon={item.icon} label={item.name} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} badge={item.badge} collapsed={collapsed} />)}
            {workspaceNav.length > 0 && (
              <>
                <SectionHeading label="Workspace" collapsed={collapsed} />
                {workspaceNav.map((item) => <NavRow key={item.name} icon={item.icon} label={item.name} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} collapsed={collapsed} />)}
              </>
            )}
              <div className="mt-2 flex flex-col gap-1 border-t border-[#dde1e5] pt-2 dark:border-[#39434E]">
                {bottomNav.map((item) => <NavRow key={item.label} icon={item.icon} label={item.label} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} onClick={item.label === "Support" ? onSupportClick : undefined} collapsed={collapsed} />)}
              </div>
            </div>
          </nav>
        </aside>
      </>
    )
  }

  return (
    <aside className={`fixed left-0 top-0 z-30 hidden h-screen min-h-0 flex-col border-r border-[#dde1e5] bg-[#f4f5f6] text-[#30343A] dark:border-[#39434E] dark:bg-[#171b20] dark:text-[#f1f3f5] transition-all duration-300 ease-in-out lg:flex ${collapsed ? "w-16 px-2" : "w-60 px-4"}`}>
      <div className={`flex min-w-0 flex-none items-center pb-4 pt-1 ${collapsed ? "justify-center gap-0" : "gap-3"}`}>
        <div className="flex h-10 w-10 shrink-0 items-center justify-center overflow-hidden rounded-full bg-[#2c3540] text-lg leading-none text-white dark:bg-[#39434E]">🏛</div>
        <div className={`overflow-hidden transition-all duration-300 ${collapsed ? "w-0 opacity-0" : "w-auto opacity-100"}`}>
          <h1 className="whitespace-nowrap text-lg font-bold text-[#30343A] dark:text-[#f1f3f5]">MPLADS Insight</h1>
          <p className="whitespace-nowrap text-xs text-[#69717c] dark:text-[#aab3bd]">Auditor Portal</p>
        </div>
      </div>
      <nav ref={navRef} className="sidebar-scroll min-h-0 flex-1 overflow-y-auto overflow-x-hidden pb-2">
        <div className="flex flex-col gap-1">
          {mainNav.map((item) => <NavRow key={item.name} icon={item.icon} label={item.name} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} badge={item.badge} collapsed={collapsed} />)}
          <div className="my-2 border-t border-[#dde1e5] dark:border-[#39434E]" role="separator" aria-label="Audit section" />
          {auditNav.map((item) => <NavRow key={item.name} icon={item.icon} label={item.name} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} badge={item.badge} collapsed={collapsed} />)}
          {auditNav.length > 0 && <div className="my-2 border-t border-[#dde1e5] dark:border-[#39434E]" role="separator" aria-label="Vendor section" />}
          {vendorNav.map((item) => <NavRow key={item.name} icon={item.icon} label={item.name} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} badge={item.badge} collapsed={collapsed} />)}
          {workspaceNav.length > 0 && (
            <>
              <SectionHeading label="Workspace" collapsed={collapsed} />
              {workspaceNav.map((item) => <NavRow key={item.name} icon={item.icon} label={item.name} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} collapsed={collapsed} />)}
            </>
          )}
          <div className="mt-2 flex flex-col gap-1 border-t border-[#dde1e5] pt-2 dark:border-[#39434E]">
            {bottomNav.map((item) => <NavRow key={item.label} icon={item.icon} label={item.label} href={item.href} active={currentPage === item.href} onNavigate={onNavigate} onClick={item.label === "Support" ? onSupportClick : undefined} collapsed={collapsed} />)}
          </div>
        </div>
      </nav>
    </aside>
  )
}

export default Sidebar
