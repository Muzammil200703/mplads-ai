import { useState, useCallback } from "react"

const FAQ_DATA = [
  {
    q: "What is MPLADS?",
    a: "MPLADS (Members of Parliament Local Area Development Scheme) is a Indian government initiative that allows Members of Parliament to recommend development works in their constituencies with emphasis on the creation of durable community assets. Each MP is allocated funds annually to carry out development projects such as roads, schools, health centres, drinking water facilities, and other public infrastructure.",
  },
  {
    q: "What does this dashboard track?",
    a: "This dashboard monitors MPLADS project allocations, expenditure, and physical progress across all states and union territories of India. It tracks individual project status, financial utilization, fund disbursement, and completion rates. The platform also provides AI-driven risk analysis and anomaly detection to help auditors and administrators identify projects that may warrant closer review.",
  },
  {
    q: "Where does the project data come from?",
    a: "The project data is sourced from official MPLADS government records, including works data, expenditure data, and project status records. The dataset used by this platform contains approximately 83,623 monitored works. The data reflects the latest project records available to the platform.",
  },
  {
    q: "What is a sanctioned amount?",
    a: "A sanctioned amount is the officially approved budget allocated for a specific MPLADS project. It represents the maximum funds that can be spent on that particular work. This amount is determined during the project approval process and serves as the financial ceiling for the project.",
  },
  {
    q: "What is expenditure?",
    a: "Expenditure refers to the actual amount of money that has been spent or disbursed on a project so far. It represents real financial outflow from the MPLADS funds toward completing the sanctioned work. Expenditure is tracked against the sanctioned amount to measure fund utilization.",
  },
  {
    q: "What does expenditure % mean?",
    a: "Expenditure percentage is calculated as (Expenditure ÷ Sanctioned Amount) × 100. It shows what proportion of the approved budget has been spent. For example, if a project with a sanctioned amount of ₹10 lakh has an expenditure of ₹7 lakh, the expenditure percentage is 70%. This metric helps assess how much of the allocated budget has been utilized.",
  },
  {
    q: "What is physical progress?",
    a: "Physical progress (also referred to as completion percentage) represents the actual on-ground completion status of a project, measured as a percentage. It indicates how much of the physical work has been completed relative to the total planned work. For example, 50% physical progress means half of the actual construction or development work has been finished.",
  },
  {
    q: "How is a project's risk score calculated?",
    a: "The risk score is calculated using a combination of rule-based analysis and machine learning. The system evaluates multiple factors including expenditure relative to sanctioned amount, physical progress, the relationship between spending and progress, project age, and comparisons against similar projects. Each factor contributes risk points based on predefined rules. A machine learning model (Isolation Forest) provides an additional anomaly signal. The final risk score (0–100) combines these signals, and the risk level is assigned as High (≥60), Medium (≥30), or Low (>0).",
  },
  {
    q: "What does an anomaly mean?",
    a: "An anomaly in this dashboard indicates that a project's characteristics deviate significantly from expected patterns. This could mean unusual expenditure relative to progress, projects with high spending but low completion, projects that appear stalled, or other statistically unusual combinations. An anomaly is an analytical flag suggesting that the project may benefit from additional review. It is not a finding of wrongdoing or fraud.",
  },
  {
    q: "How are projects evaluated by the platform?",
    a: "The platform evaluates projects using multiple indicators rather than a single metric. These indicators can include financial utilization (expenditure relative to sanctioned amount), physical completion progress, the relationship between expenditure and progress, project status, project age and duration, and comparisons against similar projects within the same region, budget range, or financial year.\n\nThe platform provides three layers of analysis:\n• Project Monitoring — tracking allocation, expenditure, and progress data.\n• Risk Scoring — assigning a numerical risk level based on rule-based and statistical evaluation.\n• Anomaly Detection — identifying projects whose patterns are statistically unusual compared to peers.\n\nThese systems are designed to assist auditors and administrators in prioritizing their review. They provide analytical insights and flag projects for attention; they do not make final determinations about project quality, compliance, fraud, or wrongdoing.",
  },
  {
    q: "How frequently is the data updated?",
    a: "The dashboard reflects the latest project dataset available to the platform. The freshness of the data depends on when the underlying MPLADS source records are updated by the government. The platform ingests available project records and recalculates risk scores and anomaly flags when new data is loaded. There is no fixed automatic update schedule — data freshness is tied to the availability of updated source records.",
  },
  {
    q: "Does a high-risk score mean a project is fraudulent?",
    a: "No. A high-risk score is an analytical priority indicator, not evidence of fraud, corruption, or wrongdoing. It means that the project's characteristics (such as high expenditure with low progress, unusual spending patterns, or extended duration without completion) suggest it may warrant further human review. Many high-risk flagged projects may have legitimate explanations. The score is a tool for directing auditor attention, not a judgment.",
  },
  {
    q: "Is the AI risk score a final judgment on a project?",
    a: "No. The AI risk score is a decision-support and prioritization tool, not an authority or investigative body. It is generated by automated rule-based and statistical analysis of project data. A flagged project requires human verification, contextual review, and domain expertise before any conclusion is reached. The AI system helps identify where to look; it does not determine what the findings mean.",
  },
]

function ChevronIcon({ open }) {
  return (
    <svg
      className={`h-5 w-5 shrink-0 text-gray-400 transition-transform duration-300 ${
        open ? "rotate-180" : ""
      }`}
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden="true"
    >
      <path
        fillRule="evenodd"
        d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
        clipRule="evenodd"
      />
    </svg>
  )
}

function FaqItem({ item, isOpen, onToggle }) {
  return (
    <div
      className={`rounded-lg border transition-colors duration-200 ${
        isOpen
          ? "border-blue-200 bg-blue-50/50 dark:border-blue-800/50 dark:bg-blue-950/20"
          : "border-gray-200 bg-white hover:border-gray-300 dark:border-gray-700 dark:bg-[#0d090b] dark:hover:border-gray-600"
      }`}
    >
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-4 px-5 py-4 text-left"
        aria-expanded={isOpen}
      >
        <span
          className={`text-sm font-semibold leading-snug ${
            isOpen
              ? "text-blue-700 dark:text-blue-300"
              : "text-gray-800 dark:text-gray-200"
          }`}
        >
          {item.q}
        </span>
        <ChevronIcon open={isOpen} />
      </button>

      {/* Collapsible answer */}
      <div
        className="overflow-hidden transition-all duration-300 ease-in-out"
        style={{ maxHeight: isOpen ? "600px" : "0px" }}
      >
        <div className="px-5 pb-5 pt-0">
          <div className="border-t border-gray-100 dark:border-gray-700/60 pt-3">
            {item.a.split("\n").map((line, i) => {
              if (line.startsWith("• ")) {
                return (
                  <div key={i} className="flex gap-2 py-0.5">
                    <span className="text-blue-500 mt-0.5">•</span>
                    <span className="text-sm leading-relaxed text-gray-600 dark:text-gray-400">
                      {line.slice(2)}
                    </span>
                  </div>
                )
              }
              return (
                <p
                  key={i}
                  className="text-sm leading-relaxed text-gray-600 dark:text-gray-400"
                >
                  {line}
                </p>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

export default function FAQ() {
  const [openIndex, setOpenIndex] = useState(null)

  const handleToggle = useCallback(
    (idx) => {
      setOpenIndex((prev) => (prev === idx ? null : idx))
    },
    []
  )

  return (
    <div className="min-h-full bg-[#faf7f6] p-4 sm:p-6 text-[#292326] transition-colors duration-200 dark:bg-[#000000] dark:text-gray-100">
      <div className="mx-auto max-w-3xl space-y-6">
        {/* Header */}
        <div>
          <h2 className="text-2xl font-bold text-[#292326] dark:text-white">
            Frequently Asked Questions
          </h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Understand the data, evaluation criteria, and AI insights behind the
            MPLADS dashboard.
          </p>
        </div>

        {/* FAQ List */}
        <div className="space-y-2">
          {FAQ_DATA.map((item, idx) => (
            <FaqItem
              key={idx}
              item={item}
              isOpen={openIndex === idx}
              onToggle={() => handleToggle(idx)}
            />
          ))}
        </div>

        {/* Footer note */}
        <p className="text-center text-xs text-gray-400 dark:text-gray-500 pt-2">
          For additional support or questions not covered here, please contact
          your system administrator.
        </p>
      </div>
    </div>
  )
}
