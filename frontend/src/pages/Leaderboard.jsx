import { useEffect, useState } from "react"
import { getStateIntelligence } from "../services/api"
import { formatMoney } from "../utils/format"

function computeScore(s) {
  const util = Number(s.utilization_percentage) || 0
  const progress = Number(s.average_completion_percentage) || 0
  const total = Number(s.total_projects) || 1
  const risky = Number(s.high_risk_projects) || 0
  const riskRatio = Math.min(risky / total, 1)
  const safetyScore = (1 - riskRatio) * 100
  const raw = util * 0.4 + progress * 0.4 + safetyScore * 0.2
  return Math.round(Math.min(Math.max(raw, 0), 100))
}

const MEDALS_LIGHT = ["#C8102E", "#E78D96", "#F2B7BD"]
const MEDALS_DARK = ["#8F1838", "#A32346", "#D33B62"]
const MEDAL_INK_LIGHT = ["#ffffff", "#292326", "#292326"]
const MEDAL_INK_DARK = ["#ffffff", "#ffffff", "#ffffff"]

export default function Leaderboard({ darkMode }) {
  const [states, setStates] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    setLoading(true)
    getStateIntelligence({})
      .then((data) => {
        if (!active) return
        const list = Array.isArray(data) ? data : (data?.states || [])
        const scored = list
          .map((s) => ({ ...s, _score: computeScore(s) }))
          .sort((a, b) => b._score - a._score)
        setStates(scored)
      })
      .catch(() => setStates([]))
      .finally(() => active && setLoading(false))
    return () => { active = false }
  }, [])

  const pageBg = darkMode ? "text-[#f5eff1]" : "text-[#292326]"
  const cardBg = darkMode ? "bg-[#0d090b] border-[#351923]" : "bg-white border-[#e8dadc]"
  const mutedText = darkMode ? "text-[#b9aeb2]" : "text-[#71666a]"

  return (
    <div className={`mx-auto max-w-5xl space-y-6 px-4 pt-6 pb-10 sm:px-6 sm:pt-8 lg:px-8 lg:pt-10 ${pageBg}`}>
      <div>
        <h1 className="text-2xl sm:text-3xl font-bold">State Transparency Leaderboard</h1>
        <p className={`mt-2 text-sm ${mutedText}`}>
          States ranked by a composite score of fund utilization, physical progress, and audit safety.
        </p>
      </div>

      {loading && (
        <div className={`rounded-xl border p-8 text-center ${cardBg} ${mutedText}`}>
          Loading rankings...
        </div>
      )}

      {!loading && states.length === 0 && (
        <div className={`rounded-xl border p-8 text-center ${cardBg} ${mutedText}`}>
          No data available.
        </div>
      )}

      <div className="space-y-4">
        {states.map((s, i) => (
          <div
            key={s.state}
            className={`rounded-xl border p-4 shadow-sm transition-all duration-200 hover:shadow-md hover:-translate-y-0.5 ${cardBg}`}
          >
            <div className="flex items-center gap-4">
              <div
                className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full font-bold text-white"
                style={{ backgroundColor: i < 3 ? (darkMode ? MEDALS_DARK : MEDALS_LIGHT)[i] : (darkMode ? "#120b0e" : "#e8dadc"), color: i < 3 ? (darkMode ? MEDAL_INK_DARK : MEDAL_INK_LIGHT)[i] : undefined }}
              >
                {i + 1}
              </div>
              <div className="min-w-0 flex-1">
                <p className="font-bold text-base">{s.state}</p>
                <p className={`text-xs ${mutedText}`}>
                  {formatMoney ? formatMoney(s.total_sanctioned_amount) : s.total_sanctioned_amount} sanctioned &middot; {s.total_projects} works
                </p>
              </div>
              <div className="text-right">
                <p className="text-2xl font-bold font-mono">{s._score}</p>
                <p className={`text-[0.6875rem] uppercase tracking-wide ${mutedText}`}>Trust Score</p>
              </div>
            </div>
            <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
              <div
                className="h-full rounded-full bg-gradient-to-r from-blue-500 to-blue-600 transition-all duration-700"
                style={{ width: `${s._score}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
