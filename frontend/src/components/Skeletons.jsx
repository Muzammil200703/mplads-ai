/* ═══════════════════════════════════════════════════════════
   Shared skeleton loaders — subtle, layout-stable placeholders.
   Each skeleton mirrors the real content's footprint so pages
   don't jump when data arrives.
   ═══════════════════════════════════════════════════════════ */

export function Shimmer({ className = "" }) {
  return (
    <div
      className={`animate-pulse rounded-sm bg-gray-200/80 dark:bg-[#120b0e]/80 ${className}`}
      aria-hidden="true"
    />
  )
}

/** Row strip matching the Projects / Risk Center desktop table rows. */
export function TableRowsSkeleton({ rows = 8, cols = 6, className = "" }) {
  return (
    <div className={`divide-y divide-gray-100 dark:divide-gray-700/60 ${className}`}>
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-4 px-4 py-3.5">
          {Array.from({ length: cols }).map((_, c) => (
            <Shimmer
              key={c}
              className={`h-4 ${c === 0 ? "w-10" : c === 1 ? "flex-1" : c % 2 ? "w-24" : "w-20"}`}
            />
          ))}
        </div>
      ))}
    </div>
  )
}

/** Grid of card placeholders (KPI cards, summary cards). */
export function CardsSkeleton({ count = 4, className = "" }) {
  return (
    <div className={`grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4 ${className}`}>
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]"
        >
          <Shimmer className="h-3 w-24" />
          <Shimmer className="mt-3 h-7 w-28" />
          <Shimmer className="mt-3 h-3 w-32" />
        </div>
      ))}
    </div>
  )
}

/** Mobile card list placeholder. */
export function MobileCardsSkeleton({ rows = 5 }) {
  return (
    <div className="space-y-3 lg:hidden">
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]"
        >
          <div className="flex items-center justify-between">
            <Shimmer className="h-4 w-16" />
            <Shimmer className="h-5 w-14 rounded-full" />
          </div>
          <Shimmer className="mt-3 h-4 w-3/4" />
          <Shimmer className="mt-2 h-3 w-1/2" />
        </div>
      ))}
    </div>
  )
}
