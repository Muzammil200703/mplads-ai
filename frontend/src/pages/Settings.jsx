/* ═══════════════════════════════════════════════════════════
   Settings page — application preferences.
   Currently: theme. More preferences can be added
   as new sections below without changing the page structure.
   ═══════════════════════════════════════════════════════════ */

function SettingsShell({ children }) {
  return (
    <div className="mx-auto max-w-[60rem] space-y-4 pt-4 sm:space-y-5 sm:pt-6">
      <div>
        <h1 className="text-xl font-bold text-[#292326] sm:text-2xl dark:text-[#f5eff1]">Settings</h1>
        <p className="mt-1 text-sm text-[#71666a] dark:text-[#b9aeb2]">
          Application preferences. Changes apply immediately and are saved on this device.
        </p>
      </div>
      {children}
    </div>
  )
}

export default function Settings({ darkMode, onThemeToggle }) {
  return (
    <SettingsShell>
      {/* THEME */}
      <section className="rounded-xl border border-[#e8dadc] bg-white p-4 shadow-sm sm:p-5 dark:border-[#351923] dark:bg-[#000000]">
        <div className="mb-4">
          <h2 className="text-lg font-bold text-[#292326] dark:text-[#f5eff1]">Theme</h2>
          <p className="mt-0.5 text-sm text-[#71666a] dark:text-[#b9aeb2]">
            Choose the appearance for the application.
          </p>
        </div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {[
            { label: "Light", description: "Use a bright background", value: false },
            { label: "Dark", description: "Use a darker background", value: true },
          ].map((option) => {
            const selected = darkMode === option.value
            return (
              <button
                key={option.label}
                onClick={() => selected || onThemeToggle?.()}
                aria-pressed={selected}
                className={`rounded-lg border px-3 py-3 text-left transition ${
                  selected
                    ? "border-[#c8102e] bg-[#f8f1f1] ring-2 ring-[#c8102e]/10 dark:border-blue-500 dark:bg-[#0d090b]"
                    : "border-[#e8dadc] bg-white hover:border-[#71666a]/50 dark:border-[#351923] dark:bg-[#000000] dark:hover:border-[#b9aeb2]/50"
                }`}
              >
                <span className="flex items-center gap-2">
                  <span
                    className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2 ${
                      selected ? "border-[#c8102e] dark:border-blue-400" : "border-[#e8dadc] dark:border-[#82767b]"
                    }`}
                  >
                    {selected && <span className="h-2 w-2 rounded-full bg-[#c8102e] dark:bg-blue-400" />}
                  </span>
                  <span className="text-sm font-semibold text-[#292326] dark:text-[#f5eff1]">{option.label}</span>
                </span>
                <span className="mt-1 block pl-6 text-xs text-[#71666a] dark:text-[#b9aeb2]">{option.description}</span>
              </button>
            )
          })}
        </div>
      </section>
    </SettingsShell>
  )
}
