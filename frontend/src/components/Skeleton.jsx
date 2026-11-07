import React from "react"

/** Skeleton bar that pulses */
function Bar({ className = "" }) {
  return <div className={`animate-pulse rounded bg-gray-200 dark:bg-gray-700 ${className}`} />
}

/** Generic card skeleton */
function CardSkeleton() {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
      <Bar className="mb-3 h-4 w-1/3" />
      <Bar className="h-8 w-1/2" />
      <Bar className="mt-3 h-3 w-2/3" />
    </div>
  )
}

/** Page-level skeleton with header + cards grid */
export function PageSkeleton({ cards = 4, columns = 4 }) {
  return (
    <div className="min-h-full p-4 sm:p-6">
      <Bar className="mb-2 h-7 w-48" />
      <Bar className="mb-6 h-4 w-96 max-w-full" />
      <div className="grid grid-cols-1 gap-4 sm:gap-5" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {Array.from({ length: cards }).map((_, i) => (
          <CardSkeleton key={i} />
        ))}
      </div>
    </div>
  )
}

/** Table skeleton with rows */
export function TableSkeleton({ rows = 8 }) {
  return (
    <div className="space-y-3 p-4 sm:p-6">
      <Bar className="h-6 w-64" />
      <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
        {/* Header */}
        <div className="flex gap-4 border-b border-gray-100 dark:border-gray-700 pb-3 mb-3">
          <Bar className="h-4 w-20" />
          <Bar className="h-4 w-32" />
          <Bar className="h-4 w-24" />
          <Bar className="h-4 w-16" />
        </div>
        {/* Rows */}
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="flex items-center gap-4 py-3 border-b border-gray-50 dark:border-gray-800 last:border-0">
            <Bar className="h-4 w-16" />
            <Bar className="h-4 flex-1 max-w-[200px]" />
            <Bar className="h-4 w-24" />
            <Bar className="h-4 w-12" />
          </div>
        ))}
      </div>
    </div>
  )
}

/** Chart card skeleton */
export function ChartSkeleton() {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-[#0d090b]">
      <Bar className="mb-4 h-4 w-40" />
      <div className="flex items-end gap-3 h-32">
        {Array.from({ length: 6 }).map((_, i) => (
          <Bar key={i} className="flex-1 rounded-t" style={{ height: `${20 + Math.random() * 80}%` }} />
        ))}
      </div>
    </div>
  )
}
