const API_URL = import.meta.env.VITE_API_URL !== undefined 
  ? import.meta.env.VITE_API_URL 
  // Local development uses the same port as start_backend.bat/start_app.py.
  // Keeping this aligned prevents auth calls such as /auth/signup from
  // being sent to an unrelated/stale local service.
  : (import.meta.env.PROD ? "https://mplads-ai-optimized.onrender.com" : "http://127.0.0.1:8000")

// ═══════════════ AUTHENTICATION SESSION ═══════════════
// Persistent JWT session in localStorage; attached to every request.
const TOKEN_KEY = "mplads_token"
export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) } catch { return null }
}
export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch { /* storage unavailable */ }
}
export function clearToken() { setToken(null) }

// ═══════════════ CLIENT-SIDE API CACHE ═══════════════
// TTL-based cache to avoid refetching filter/static data on every page mount
const _apiCache = new Map()
function cacheGet(key, ttlMs = 60000) {
  const entry = _apiCache.get(key)
  if (entry && (Date.now() - entry.ts) < ttlMs) return entry.data
  _apiCache.delete(key)
  return undefined
}
function cacheSet(key, data) {
  _apiCache.set(key, { data, ts: Date.now() })
}
function cacheInvalidate(prefix) {
  for (const key of _apiCache.keys()) {
    if (key.startsWith(prefix)) _apiCache.delete(key)
  }
}

// ═══════════════ BACKEND CONNECTION STATUS (single source of truth) ═══════════════
// Every request that goes through the shared transport reports into this bus.
//   • Any successful HTTP exchange (2xx) marks the backend reachable —
//     including a retry that succeeds after a cold start — and records the
//     time of that success.
//   • A failure marks it unreachable ONLY when there is no fresh success:
//     either nothing has ever succeeded (initial contact failed) or every
//     request has been failing for longer than SUCCESS_GRACE_MS. This keeps a
//     partial failure (one optional endpoint down while the page's other
//     requests succeed) from ever raising the global "backend unreachable"
//     banner, while a genuine sustained outage still shows it.
// Consumers subscribe via onBackendStatus() instead of each page keeping its
// own private "is the server up" boolean, so the state can never contradict
// itself across pages and a success anywhere clears a stale failure without
// a page refresh.
const SUCCESS_GRACE_MS = 15000
let _backendReachable = null // null = not yet probed by any request
let _lastSuccessAt = 0
const _backendStatusListeners = new Set()

function _reportBackendStatus(reachable) {
  if (reachable) {
    _lastSuccessAt = Date.now()
    if (_backendReachable !== true) {
      _backendReachable = true
      _emitBackendStatus()
    }
  } else {
    // Drop the failure while a confirmed reachable state is still fresh —
    // the failed endpoint surfaces its own section-level error instead.
    if (_backendReachable === true && Date.now() - _lastSuccessAt < SUCCESS_GRACE_MS) return
    if (_backendReachable !== false) {
      _backendReachable = false
      _emitBackendStatus()
    }
  }
}

function _emitBackendStatus() {
  for (const listener of _backendStatusListeners) {
    try {
      listener(_backendReachable)
    } catch { /* listener errors never break the transport */ }
  }
}

/** Subscribe to backend connectivity changes. Returns an unsubscribe fn. */
export function onBackendStatus(listener) {
  _backendStatusListeners.add(listener)
  return () => _backendStatusListeners.delete(listener)
}

/** Current confirmed connectivity without subscribing. */
export function getBackendStatus() {
  return _backendReachable
}

// ═══════════════ REQUEST TRANSPORT ═══════════════
// Render's free tier can 503/429 briefly while the instance wakes up or when
// a cold-start burst hits the platform edge. Transient failures are retried
// with a short backoff so a waking backend is treated as "starting", never as
// a permanent configuration error.
const TRANSIENT_STATUS = new Set([429, 502, 503, 504])
const TRANSIENT_RETRIES = 2
const TRANSIENT_BASE_DELAY_MS = 800

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function request(endpoint, options = {}, _attempt = 0) {
  const url = `${API_URL}${endpoint}`
  const token = getToken()

  // Only attach Content-Type when a body is actually sent. A bodyless GET
  // with "Content-Type: application/json" is NOT CORS-safelisted, so it
  // forces a preflight OPTIONS before every request — wasteful round-trips
  // that Render's free tier can throttle (429) during cold-start bursts,
  // which then blocks the real request in the browser.
  const headers = {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers || {}),
  }
  if (
    options.body !== undefined &&
    options.body !== null &&
    !(options.body instanceof FormData) &&
    !headers["Content-Type"]
  ) {
    headers["Content-Type"] = "application/json"
  }

  let response
  try {
    response = await fetch(url, { ...options, headers })
  } catch (networkError) {
    // fetch() rejects on network failures (offline, DNS, aborted connection).
    // AbortError must always propagate so callers can cancel live search.
    if (networkError && networkError.name === "AbortError") throw networkError
    if (_attempt < TRANSIENT_RETRIES) {
      await delay(TRANSIENT_BASE_DELAY_MS * (_attempt + 1))
      return request(endpoint, options, _attempt + 1)
    }
    _reportBackendStatus(false)
    throw networkError
  }

  // Any successful HTTP exchange proves the backend is up. This runs before
  // the retry check so that even the intermediate 2xx of a request whose
  // caller still throws elsewhere marks connectivity truthfully.
  if (response.ok) {
    _reportBackendStatus(true)
  }

  if (response.status === 401 && getToken()) {
    // Session expired — clear so the UI falls back to signed-out state.
    clearToken()
    window.dispatchEvent(new CustomEvent("auth-expired"))
  }

  // Retry transient platform failures (Render cold start 503, edge 429
  // throttle, gateway 502/504) before surfacing an error to the UI.
  if (TRANSIENT_STATUS.has(response.status) && _attempt < TRANSIENT_RETRIES) {
    await delay(TRANSIENT_BASE_DELAY_MS * (_attempt + 1))
    return request(endpoint, options, _attempt + 1)
  }

  if (!response.ok) {
    let errorDetail = `API request failed: ${response.status}`
    try {
      const errJson = await response.json()
      if (errJson && errJson.detail) {
        errorDetail = typeof errJson.detail === "string"
          ? errJson.detail
          : JSON.stringify(errJson.detail)
      }
    } catch { /* ignore JSON parse errors */ }
    // Exhausted retries on transient statuses (503/429/502/504) mean the
    // backend really did not answer — report degraded. Non-transient 4xx/5xx
    // (401/404/422…) come from a *reachable* server and must not flip the
    // global connectivity state; the failed endpoint surfaces its own error
    // in its section.
    if (TRANSIENT_STATUS.has(response.status)) {
      _reportBackendStatus(false)
    }
    throw new Error(errorDetail)
  }

  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

function buildQuery(params = {}) {
  const q = new URLSearchParams()
  Object.entries(params).forEach(([key, val]) => {
    if (val !== undefined && val !== null && val !== "" && val !== "all") {
      q.append(key, val)
    }
  })
  const qs = q.toString()
  return qs ? `?${qs}` : ""
}

// ═══════════════ REQUEST DEDUPLICATION ═══════════════
// Simultaneous identical GETs share one network request.
const _inflight = new Map()

/**
 * Cached + deduplicated GET helper.
 * - Returns cached data when a fresh entry exists (TTL).
 * - Coalesces concurrent calls for the same key into a single request.
 * - Caches only successful, non-empty payloads; errors propagate uncached.
 * Use "data_"-prefixed keys for anything that must be invalidated by
 * uploadCSV()/triggerSync() (they call cacheInvalidate("data_")).
 */
async function cachedGet(key, ttlMs, fetcher) {
  const cached = cacheGet(key, ttlMs)
  if (cached !== undefined) return cached
  if (_inflight.has(key)) return _inflight.get(key)
  const promise = (async () => {
    try {
      const data = await fetcher()
      if (data !== undefined && data !== null) cacheSet(key, data)
      return data
    } finally {
      _inflight.delete(key)
    }
  })()
  _inflight.set(key, promise)
  return promise
}

export async function healthCheck() {
  return request("/health")
}

/**
 * Official eSAKSHI-style per-house dashboard metrics (allocated, expenditure,
 * fund utilization, expenditure rate, MPs, completed/pending works,
 * ongoing-work payments) + dataset snapshot dates.
 */
export async function getDashboardHouse(house, fy) {
  const qs = buildQuery({ house, fy })
  return request(`/dashboard/house${qs}`)
}

export async function getStates() {
  const cached = cacheGet("states", 300000) // 5 min cache
  if (cached !== undefined) return cached
  const data = await request("/filters/states")
  if (data) cacheSet("states", data)
  return data
}

export async function getConstituencies(state) {
  if (!state) return []
  const cacheKey = `cons_${state}`
  const cached = cacheGet(cacheKey, 300000) // 5 min cache
  if (cached !== undefined) return cached
  try {
    const data = await request(`/filters/constituencies?state=${encodeURIComponent(state)}`)
    if (data) cacheSet(cacheKey, data)
    return data
  } catch {
    try {
      const data = await request(`/filters/districts?state=${encodeURIComponent(state)}`)
      if (data) cacheSet(cacheKey, data)
      return data
    } catch {
      try {
        const data = await request(`/search/projects?state=${encodeURIComponent(state)}&skip=0&limit=500`)
        const set = new Set()
        if (data && Array.isArray(data.results)) {
          data.results.forEach((p) => { if (p.constituency) set.add(p.constituency) })
        }
        const result = Array.from(set).sort()
        cacheSet(cacheKey, result)
        return result
      } catch {
        return []
      }
    }
  }
}

export async function getFYs() {
  const cached = cacheGet("fys", 300000) // 5 min cache
  if (cached !== undefined) return cached
  try {
    const data = await request("/filters/fys")
    if (data) cacheSet("fys", data)
    return data
  } catch {
    const fallback = [
      { fy: "2023-24", count: 8562 },
      { fy: "2024-25", count: 19203 },
      { fy: "2025-26", count: 50274 },
      { fy: "2026-27", count: 5192 },
    ]
    cacheSet("fys", fallback)
    return fallback
  }
}

// Keep backward-compatible alias
export const getDistricts = getConstituencies

// Allow pages to invalidate cache when data changes
export function invalidateAPICache(prefix) {
  cacheInvalidate(prefix || "")
}

export async function getCategories() {
  const cached = cacheGet("categories", 300000)
  if (cached !== undefined) return cached
  const data = await request("/filters/categories")
  if (data) cacheSet("categories", data)
  return data
}

export async function getDashboardOverview(params = {}) {
  // Same success-only cache as the sibling dashboard aggregations (states /
  // anomalies / early-warning use 2 min): re-mounts serve from cache instead of
  // re-hitting the backend, and rejected requests are never cached, so a
  // cold-start blip cannot stick as zeros.
  return cachedGet(`dash_overview_${buildQuery(params)}`, 120000, () =>
    request(`/dashboard/overview${buildQuery(params)}`))
}

export async function getDashboardStates(params = {}) {
  // Cache state dashboard data for 2 minutes (expensive aggregation)
  const cacheKey = `dash_states_${buildQuery(params)}`
  const cached = cacheGet(cacheKey, 120000)
  if (cached !== undefined) return cached
  const data = await request(`/dashboard/states${buildQuery(params)}`)
  if (data) cacheSet(cacheKey, data)
  return data
}

/**
 * State-level aggregates for the Overview India choropleth. Returns all 36
 * states/UTs with ISO codes plus explicit availability flags, so the map can
 * distinguish a recorded zero from an unavailable value.
 */
export async function getStateMapMetrics(params = {}) {
  return cachedGet(`state_map_v2_${buildQuery(params)}`, 120000, () =>
    request(`/dashboard/state-map${buildQuery(params)}`))
}

export async function getDashboardMPs(params = {}) {
  return request(`/dashboard/mps${buildQuery(params)}`)
}

export async function getDashboardConstituencies(params = {}) {
  return request(`/dashboard/constituencies${buildQuery(params)}`)
}

export async function getDashboardFinancials() {
  return request("/dashboard/financials")
}

export async function getDashboardProjectTypes() {
  return request("/dashboard/project-types")
}

/* ── MP Intelligence: MP-level work/financial views (All/LS/RS) ── */
export async function getMPIntelligence(params = {}) {
  return cachedGet(`mpi_${buildQuery(params)}`, 60000, () =>
    request(`/mp-intelligence${buildQuery(params)}`))
}

export async function getMPIntelligenceDetail(mpId, worksLimit = 400) {
  return cachedGet(`mpi_detail_${mpId}_${worksLimit}`, 120000, () =>
    request(`/mp-intelligence/${mpId}?works_limit=${worksLimit}`))
}

export async function getAnomaliesSummary(params = {}) {
  const cacheKey = `anom_summary_${buildQuery(params)}`
  const cached = cacheGet(cacheKey, 120000) // 2 min cache
  if (cached !== undefined) return cached
  const data = await request(`/dashboard/anomalies-summary${buildQuery(params)}`)
  if (data) cacheSet(cacheKey, data)
  return data
}

export async function refreshAnomaliesSummary() {
  return request("/dashboard/anomalies-summary/refresh", { method: "POST" })
}

export async function getEarlyWarning(params = {}) {
  const cacheKey = `early_warning_${buildQuery(params)}`
  const cached = cacheGet(cacheKey, 120000) // 2 min cache
  if (cached !== undefined) return cached
  const data = await request(`/dashboard/early-warning${buildQuery(params)}`)
  if (data) cacheSet(cacheKey, data)
  return data
}

export async function getProjects(params = {}) {
  return cachedGet(`data_projects_${buildQuery(params)}`, 30000, () =>
    request(`/projects${buildQuery(params)}`))
}

export async function searchProjects(params = {}) {
  const { signal, ...queryParams } = params
  const options = signal ? { signal } : {}
  // Abortable calls (live search-as-you-type) bypass the cache;
  // plain calls get a short TTL so paging back is instant.
  if (signal) return request(`/search/projects${buildQuery(queryParams)}`, options)
  return cachedGet(`data_search_${buildQuery(queryParams)}`, 15000, () =>
    request(`/search/projects${buildQuery(queryParams)}`))
}

export async function getProjectTimeline(projectId) {
  // Timeline derives from the static dataset — long cache.
  return cachedGet(`data_tl_${projectId}`, 300000, async () => {
    try {
      return await request(`/projects/${projectId}/timeline`)
    } catch { return null }
  })
}

export async function getProjectActivity(projectId) {
  // Expenditure activity derives from the static dataset — long cache.
  return cachedGet(`data_act_${projectId}`, 300000, async () => {
    try {
      return await request(`/projects/${projectId}/expenditure-activity`)
    } catch { return null }
  })
}

export async function getProjectDetail(projectId) {
  return cachedGet(`data_detail_${projectId}`, 60000, () =>
    request(`/projects/${projectId}`))
}

export async function getAnomalies(params = {}) {
  return cachedGet(`data_anom_${buildQuery(params)}`, 30000, () =>
    request(`/anomalies${buildQuery(params)}`))
}

export async function getProjectRisk(projectId) {
  return request(`/ai/risk/${projectId}`)
}

export async function getRiskyProjects(limit = 10) {
  return request(`/ai/risky-projects?limit=${limit}`)
}

export async function getAIInsights() {
  return request("/ai/insights")
}

export async function getAINarrativeInsights(params = {}) {
  return request(`/ai/narrative-insights${buildQuery(params)}`)
}

export async function getStateInsights(state) {
  if (!state) return null
  return request(`/ai/state-insights/${encodeURIComponent(state)}`)
}

export async function getMPSummary(params = {}) {
  return request(`/mp-summary${buildQuery(params)}`)
}

export async function getRecommendedWorks(params = {}) {
  return request(`/recommended-works${buildQuery(params)}`)
}

export async function getExpenditures(params = {}) {
  return request(`/expenditures${buildQuery(params)}`)
}

export async function getVendorIntelligence(params = {}) {
  const query = buildQuery(params)
  return cachedGet(`vendor_intelligence_${query}`, 300000, () => request(`/vendor-intelligence${query}`))
}

export async function getVendorProfile(vendorKey) {
  return cachedGet(`vendor_profile_${vendorKey}`, 300000, () =>
    request(`/vendor-intelligence/${encodeURIComponent(vendorKey)}`))
}

// ═══════════════ AI AUDIT & VERIFICATION (SIH 26102) ═══════════════

export async function getAuditQueue(params = {}) {
  return request(`/ai/audit-queue${buildQuery(params)}`)
}

export async function getInspectionBundle(projectId) {
  return request(`/ai/inspection/${projectId}`)
}

export async function detectCostAnomaly(params = {}) {
  return request(`/ai/detect-cost-anomaly${buildQuery(params)}`)
}

export async function getVendorNetwork(params = {}) {
  // 2-minute cache + in-flight dedupe so remounts don't refetch the full
  // cluster aggregation (matches the vendor-intelligence caching pattern).
  return cachedGet(`vendor_network_${buildQuery(params)}`, 120000, () => request(`/ai/vendor-network${buildQuery(params)}`))
}

export async function verifyProjectLookup(projectId) {
  return request(`/ai/verify/project/${projectId}`)
}

export async function submitVerificationReport({ projectId, status, lat, lon, reporterName, note, inquiryId, file }) {
  const form = new FormData()
  if (file) form.append("file", file)
  const qs = buildQuery({ project_id: projectId, status, lat, lon, reporter_name: reporterName, note, inquiry_id: inquiryId })
  return request(`/ai/verify/report${qs}`, { method: "POST", body: form })
}

export async function verifyImage({ file, projectId, store = true }) {
  const form = new FormData()
  form.append("file", file)
  const qs = buildQuery({ project_id: projectId, store })
  return request(`/ai/verify-image${qs}`, { method: "POST", body: form })
}

export async function recordAuditAction({ projectId, action, note }) {
  const qs = buildQuery({ project_id: projectId, action, note })
  return request(`/ai/audit-action${qs}`, { method: "POST" })
}

export async function getAuditActions(projectId) {
  return request(`/ai/audit-actions/${projectId}`)
}

export async function getCompletedWorks(params = {}) {
  return request(`/completed-works${buildQuery(params)}`)
}

export async function getStateIntelligence(params = {}) {
  const cacheKey = `state_intel_v2_${buildQuery(params)}`
  const cached = cacheGet(cacheKey, 120000) // 2 min cache
  if (cached !== undefined) return cached
  const data = await request(`/dashboard/state-intelligence${buildQuery(params)}`)
  if (data) cacheSet(cacheKey, data)
  return data
}

// State Intelligence drill-down details — derived from static dataset
export async function getStateDetails(state) {
  return cachedGet(`state_det_${state}`, 300000, async () => {
    try {
      return await request(`/dashboard/state-details/${encodeURIComponent(state)}`)
    } catch { return null }
  })
}

export async function getAuditPriority(params = {}) {
  return cachedGet(`data_audit_prio_${buildQuery(params)}`, 60000, () =>
    request(`/audit-priority${buildQuery(params)}`))
}

export async function getAuditPrioritySummary(params = {}) {
  return cachedGet(`data_audit_prio_sum_${buildQuery(params)}`, 60000, () =>
    request(`/audit-priority/summary${buildQuery(params)}`))
}

export async function getSimilarProjects(projectId, limit = 5) {
  return cachedGet(`data_sim_${projectId}_${limit}`, 300000, () =>
    request(`/projects/${projectId}/similar?limit=${limit}`))
}

/* ── Forensic tools (Project Forensic Mode · DNA/Twins · Stress Test) ──
   Backed by backend/forensic_api.py. 300s cache matches the similar-
   projects cache; stress-test bypasses the cache so each simulation
   reflects the live inputs, though the current-snapshot shape is
   deterministic for a given project. */

export async function getForensicBundle(projectId) {
  return cachedGet(`forensic_${projectId}`, 300000, () =>
    request(`/forensic-tools/${projectId}`))
}

export async function getProjectDNA(projectId) {
  return cachedGet(`dna_${projectId}`, 300000, () =>
    request(`/forensic-tools/dna/${projectId}`))
}

export async function getProjectStressTest(projectId, payload) {
  return request(`/forensic-tools/stress-test/${projectId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  })
}

export async function getAnomalyAnalytics(params = {}) {
  const cacheKey = `anomaly_analytics_${buildQuery(params)}`
  const cached = cacheGet(cacheKey, 120000) // 2 min cache for expensive analytics
  if (cached !== undefined) return cached
  const data = await request(`/anomalies/analytics${buildQuery(params)}`)
  if (data) cacheSet(cacheKey, data)
  return data
}

export async function getScatterData(params = {}) {
  return request(`/anomalies/scatter-data${buildQuery(params)}`)
}

export async function getOverview() {
  return getDashboardOverview()
}

// Feature 1: AI Risk Explanation
export async function getRiskExplanation(projectId) {
  // Derived deterministically from stored risk results — safe to cache.
  return cachedGet(`data_rex_${projectId}`, 300000, () =>
    request(`/ai/risk-explanation/${projectId}`))
}

// Feature 3: Anomaly Explanation
export async function getAnomalyExplanation(projectId) {
  return cachedGet(`data_aex_${projectId}`, 300000, () =>
    request(`/ai/anomaly-explanation/${projectId}`))
}

// Feature 4: Benchmarking
export async function getBenchmarking(params = {}) {
  return cachedGet(`data_bench_${buildQuery(params)}`, 300000, () =>
    request(`/ai/benchmarking${buildQuery(params)}`))
}

// ═══════════════ AUDIT INTELLIGENCE & INVESTIGATION ═══════════════

// Evidence gap detection — "What evidence is missing?"
export async function getEvidenceGaps(projectId) {
  return cachedGet(`data_eg_${projectId}`, 300000, () =>
    request(`/ai/evidence-gaps/${projectId}`))
}

// Anomaly dimension explorer
export async function getAnomalyExplorer(projectId) {
  return cachedGet(`data_axp_${projectId}`, 300000, () =>
    request(`/ai/anomaly-explorer/${projectId}`))
}

// Peer benchmarking against comparable projects (statistical comparison)
export async function getPeerBenchmark(projectId, params = {}) {
  return cachedGet(`data_peer_${projectId}_${buildQuery(params)}`, 120000, () =>
    request(`/projects/${projectId}/peer-benchmark${buildQuery(params)}`))
}

// What-if simulation — recomputes risk on hypothetical values (never persisted)
export async function simulateRisk(projectId, payload) {
  return request(`/ai/simulate/${projectId}`, {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

// Structured audit case + escalation draft
export async function getAuditCase(projectId) {
  return cachedGet(`data_case_${projectId}`, 300000, () =>
    request(`/ai/audit-case/${projectId}`))
}

// Investigation workspace (persisted workflow state) — NOT cached, always fresh
export async function getInvestigation(projectId) {
  return request(`/investigations/${projectId}`)
}

export async function startInvestigation(projectId) {
  return request(`/investigations/${projectId}/start`, { method: "POST" })
}

export async function updateInvestigation(projectId, payload) {
  return request(`/investigations/${projectId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  })
}

export async function updateEvidenceItem(projectId, itemKey, status) {
  return request(`/investigations/${projectId}/items/${encodeURIComponent(itemKey)}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  })
}

export async function updateFindingReview(projectId, findingKey, payload) {
  return request(`/investigations/${projectId}/findings/${encodeURIComponent(findingKey)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  })
}

export async function addFindingAction(projectId, findingKey) {
  return request(`/investigations/${projectId}/findings/${encodeURIComponent(findingKey)}/action`, {
    method: "POST",
  })
}

export async function getInvestigationReport(projectId) {
  return request(`/investigations/${projectId}/report`)
}

export async function getActiveInvestigations() {
  return request("/investigations/active")
}

// Data freshness / sync
export async function getDataFreshness() {
  const cached = cacheGet("data_freshness", 60000) // 1 min cache
  if (cached !== undefined) return cached
  try {
    const data = await request("/api/data/freshness")
    if (data) cacheSet("data_freshness", data)
    return data
  } catch {
    return { connected: false, message: "Unable to check data freshness" }
  }
}

export async function getDataStats() {
  const cached = cacheGet("data_stats", 300000) // 5 min cache
  if (cached !== undefined) return cached
  try {
    const data = await request("/api/data/stats")
    if (data) cacheSet("data_stats", data)
    return data
  } catch {
    return null
  }
}

export async function getDataProviders() {
  const cached = cacheGet("data_providers", 600000) // 10 min cache
  if (cached !== undefined) return cached
  try {
    const data = await request("/api/data/providers")
    if (data) cacheSet("data_providers", data)
    return data
  } catch {
    return []
  }
}

export async function triggerSync(source = "github") {
  cacheInvalidate("")
  return request(`/api/data/sync?source=${source}`, { method: "POST" })
}

export async function uploadCSV(file, delimiter = ",") {
  const formData = new FormData()
  formData.append("file", file)
  formData.append("delimiter", delimiter)
  cacheInvalidate("data_")
  const data = await request("/api/data/upload", {
    method: "POST",
    headers: {},
    body: formData,
  })
  // A successful upload may also change project/risk data on the server.
  cacheInvalidate("")
  return data
}

// Export cache invalidation for use after data mutations
export { cacheInvalidate }

// ═══════════════ AUTHENTICATION API ═══════════════

export async function signup(payload) {
  const data = await request("/auth/signup", { method: "POST", body: JSON.stringify(payload) })
  setToken(data.token)
  return data
}

export async function login(email, password) {
  const data = await request("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) })
  setToken(data.token)
  return data
}

export function logout() {
  clearToken()
}

export async function fetchMe() {
  return request("/auth/me")
}

export async function forgotPassword(email) {
  return request("/auth/forgot-password", { method: "POST", body: JSON.stringify({ email }) })
}

export async function resetPassword(token, newPassword) {
  return request("/auth/reset-password", { method: "POST", body: JSON.stringify({ token, new_password: newPassword }) })
}

export async function listUsers() {
  return request("/auth/users")
}

export async function updateUser(userId, payload) {
  return request(`/auth/users/${userId}`, { method: "PATCH", body: JSON.stringify(payload) })
}

export async function createUser(payload) {
  return request("/auth/users", { method: "POST", body: JSON.stringify(payload) })
}

// ═══════════════ ROLE PORTALS (field verifier / district authority / inquiries) ═══════════════

export async function getMyVerifications() {
  return request("/ai/verify/mine")
}

export async function getDistrictProjects(limit = 200) {
  return request(`/auth/me/district/projects?limit=${limit}`)
}

export async function getDistrictSummary() {
  return request("/auth/me/district/summary")
}

export async function getMyDistrictInquiries() {
  return request("/auth/me/inquiries")
}

export async function getAllInquiries(status) {
  const q = status ? `?status=${encodeURIComponent(status)}` : ""
  return request(`/ai/inquiries${q}`)
}

export async function closeInquiry(inquiryId) {
  return request(`/ai/inquiries/${inquiryId}/close`, { method: "POST" })
}

// ═══════════════ MPLADS AI ASSISTANT ═══════════════

export async function askAssistant(question, page, sessionId) {
  return request("/assistant/ask", {
    method: "POST",
    body: JSON.stringify({ question, page: page || null, session_id: sessionId || null }),
  })
}

export async function getAssistantSuggestions(page) {
  const qs = page && !page.startsWith("project:") ? `?page=${encodeURIComponent(page)}` : ""
  return request(`/assistant/suggestions${qs}`)
}

export async function getAssistantContextHelp(page) {
  return request(`/assistant/context-help?page=${encodeURIComponent(page)}`)
}

// ═══════════════ CITIZEN EVIDENCE REVIEW (verifier/auditor) ═══════════════

export async function getEvidenceQueue(reviewStatus = "pending") {
  return request(`/ai/evidence/queue?review_status=${encodeURIComponent(reviewStatus)}`)
}

export async function reviewEvidence(reportId, decision, note) {
  const params = new URLSearchParams({ decision })
  if (note) params.set("note", note)
  return request(`/ai/evidence/${reportId}/review?${params.toString()}`, { method: "POST" })
}

export async function getAllEvidence() {
  return request("/ai/evidence/all")
}

// ═══════════════ WORKSPACE API (saved projects / investigations / cases) ═══════════════

export async function getSavedProjects() {
  return request("/auth/me/saved-projects")
}

export async function saveProject(projectId) {
  return request(`/auth/me/saved-projects/${projectId}`, { method: "POST" })
}

export async function unsaveProject(projectId) {
  return request(`/auth/me/saved-projects/${projectId}`, { method: "DELETE" })
}

export async function getMyInvestigations() {
  return request("/auth/me/investigations")
}

export async function createMyInvestigation(projectId) {
  return request(`/auth/me/investigations/${projectId}`, { method: "POST" })
}

export async function updateMyInvestigation(projectId, status) {
  return request(`/auth/me/investigations/${projectId}`, { method: "PATCH", body: JSON.stringify({ status }) })
}

export async function getMyAuditCases() {
  return request("/auth/me/audit-cases")
}

export async function saveAuditCase(projectId) {
  return request(`/auth/me/audit-cases/${projectId}`, { method: "POST" })
}

export async function updateCaseStatus(projectId, status) {
  return request(`/auth/me/audit-cases/${projectId}`, { method: "PATCH", body: JSON.stringify({ status }) })
}

export async function getAllInvestigations() {
  return request("/auth/admin/investigations")
}

// ═══════════════ ADMIN DATA MANAGEMENT (CSV ingestion) ═══════════════
// Admin-only. The backend re-checks the role on every call.

export async function getDataOverview() {
  return request("/admin/data/overview")
}

export async function getImportHistory(limit = 50) {
  return request(`/admin/data/imports?limit=${limit}`)
}

export async function getImportDetail(batchId) {
  return request(`/admin/data/imports/${batchId}`)
}

export async function uploadDataFiles(files, { datasetType, house } = {}) {
  const form = new FormData()
  for (const f of files) form.append("files", f)
  if (datasetType) form.append("dataset_type", datasetType)
  if (house) form.append("house", house)
  return request("/admin/data/uploads", { method: "POST", body: form })
}

export async function revalidateImport(batchId, { datasetType, house } = {}) {
  const form = new FormData()
  if (datasetType) form.append("dataset_type", datasetType)
  if (house) form.append("house", house)
  return request(`/admin/data/imports/${batchId}/preview`, { method: "POST", body: form })
}

export async function commitImport(batchId, { datasetType, house } = {}) {
  const form = new FormData()
  if (datasetType) form.append("dataset_type", datasetType)
  if (house) form.append("house", house)
  return request(`/admin/data/imports/${batchId}/commit`, { method: "POST", body: form })
}

export async function rollbackImport(batchId) {
  return request(`/admin/data/imports/${batchId}/rollback`, { method: "POST" })
}

export async function refreshImport(batchId) {
  return request(`/admin/data/imports/${batchId}/refresh`, { method: "POST" })
}

export async function discardImport(batchId) {
  return request(`/admin/data/imports/${batchId}`, { method: "DELETE" })
}
