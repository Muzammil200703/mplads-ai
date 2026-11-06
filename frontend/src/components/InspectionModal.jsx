import { useEffect, useRef, useState } from "react"
import ForensicsPanel from "./ForensicsPanel"

/* ═══════════════════════════════════════════════════════════════════
   INSPECTION MODAL — shared "AI Inspection & Forensic Summary" surface
   Rendered when a user selects/inspects a project from:
     • the AI Audit Command Center table (Inspect buttons)
     • the Projects page (row/card click → project selection interface)
   The page beneath stays mounted, so search/filter/pagination state is
   preserved when the modal closes.
   ═══════════════════════════════════════════════════════════════════ */

export function ModalShell({ title, onClose, children }) {
  // Closing animation: flip to .is-closing (fade + slight dip), then unmount
  // after 150ms. Reduced motion unmounts immediately. The ref guard makes
  // requestClose safe against double-invocation (Escape + backdrop click).
  const [closing, setClosing] = useState(false)
  const closingRef = useRef(false)
  const requestClose = () => {
    if (closingRef.current) return
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      onClose()
      return
    }
    closingRef.current = true
    setClosing(true)
    setTimeout(onClose, 150)
  }

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") requestClose() }
    window.addEventListener("keydown", onKey)
    // Lock background scroll while open (matches drawer/modal behaviour
    // elsewhere in the app); always restored on close.
    const prev = document.body.style.overflow
    document.body.style.overflow = "hidden"
    return () => {
      window.removeEventListener("keydown", onKey)
      document.body.style.overflow = prev
    }
  }, [onClose])

  return (
    <div
      className={`anim-overlay-in fixed inset-0 z-[75] flex items-start justify-center overflow-y-auto bg-black/50 p-3 backdrop-blur-2xs sm:p-6 ${closing ? "is-closing" : ""}`}
      onClick={requestClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="anim-card-in w-full max-w-2xl rounded-2xl border border-[#dde1e5] bg-white shadow-soft-lg dark:border-[#39434E] dark:bg-gray-900"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3 dark:border-gray-700">
          <h3 className="text-sm font-bold uppercase tracking-wide text-gray-700 dark:text-gray-200">{title}</h3>
          <button onClick={onClose} aria-label="Close" className="rounded-lg p-1.5 text-xl leading-none text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800">✕</button>
        </div>
        {children}
      </div>
    </div>
  )
}

export default function InspectionModal({ projectId, onClose, onOpenProject }) {
  return (
    <ModalShell onClose={onClose} title="AI Inspection & Forensic Summary">
      <div className="p-5">
        <ForensicsPanel projectId={projectId} onOpenProject={onOpenProject} />
      </div>
    </ModalShell>
  )
}
