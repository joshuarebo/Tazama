/**
 * The only place the console talks to the API.
 *
 * Two things worth knowing:
 *
 *  - **Auth is a bearer token in a header**, obtained from `/auth/login`. There is no
 *    cookie, so there is no CSRF surface; the token is kept in memory and mirrored to
 *    `sessionStorage` so a page refresh does not log you out. `localStorage` would persist
 *    it across browser restarts, which is worse for a shared NOC workstation.
 *  - **Errors are problem+json** (RFC 9457). `ApiError` carries the `detail` the API wrote,
 *    which is written to be shown to a person, so the UI can display it verbatim.
 */

const BASE = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");
const TOKEN_KEY = "tazama.token";
const ROLE_KEY = "tazama.role";

export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly correlationId?: string;

  constructor(status: number, type: string, detail: string, correlationId?: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
    this.correlationId = correlationId;
  }
}

function readToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    // Private mode or blocked storage: the session simply does not survive a refresh.
    return null;
  }
}

function writeToken(token: string, role: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token);
    window.sessionStorage.setItem(ROLE_KEY, role);
  } catch {
    /* ignore: an unstored token still works for this page view */
  }
}

export function currentRole(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(ROLE_KEY);
  } catch {
    return null;
  }
}

export function signOut(): void {
  try {
    window.sessionStorage.removeItem(TOKEN_KEY);
    window.sessionStorage.removeItem(ROLE_KEY);
  } catch {
    /* ignore */
  }
}

export async function login(username: string, password: string): Promise<string> {
  const response = await fetch(`${BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const body = await response.json();
  if (!response.ok) {
    throw new ApiError(response.status, body.type ?? "", body.detail ?? "Sign-in failed.");
  }
  writeToken(body.access_token, body.role);
  return body.role as string;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = readToken();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const body = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new ApiError(
      response.status,
      body.type ?? "",
      body.detail ?? `Request failed with ${response.status}.`,
      body.correlationId,
    );
  }
  return body as T;
}

/* ----------------------------------------------------------------- shapes */

export type Meta = {
  version: string;
  app_mode: string;
  display_tz: string;
  demo_banner: boolean;
  ai_available: boolean;
  vector_backend: string;
  teams_dry_run: boolean;
  map_tile_url: string;
  roles: string[];
  capabilities: Record<string, string[]>;
  features: Record<string, boolean>;
};

export type Overview = {
  live_incidents: number;
  by_priority: Record<string, number>;
  by_cause: Record<string, number>;
  by_region: { region: string; incidents: number; affected_subs: number }[];
  suppressed: number;
  affected_subs: number;
  complaints_24h: number;
  complaints_correlated: number;
  open_notifications: number;
  data_window_end: string | null;
};

export type IncidentSummary = {
  id: string;
  title: string;
  status: string;
  priority: string;
  cause: string | null;
  region: string;
  primary_node_id: string | null;
  primary_node_kind: string | null;
  detected_at: string;
  started_at: string | null;
  resolved_at: string | null;
  affected_cells: number;
  affected_sites: number;
  affected_subs: number;
  enterprise_accounts: number;
  complaint_count: number;
  sla_risk: number | null;
  suppressed_reason: string | null;
  duration_minutes: number | null;
};

export type MapPoint = {
  cell_id: string;
  site_id: string;
  name: string;
  lat: number;
  lon: number;
  tech: string;
  region: string;
  subscribers_est: number;
};

export type Evidence = { kind: string; ref: string; note: string; weight: number };

export type Decision = {
  occurred_at: string;
  actor: string;
  action: string;
  outcome: string;
  confidence: number | null;
  rule_version: string | null;
  evidence: Evidence[];
};

export type IncidentDetail = {
  incident: IncidentSummary;
  cells: MapPoint[];
  alarms: {
    id: string;
    node_id: string;
    alarm_type: string;
    severity: string;
    raised_at: string;
    cleared_at: string | null;
    is_flapping: boolean;
  }[];
  complaints: {
    id: string;
    received_at: string;
    channel: string;
    category: string | null;
    language: string | null;
    urgency: number | null;
    cell_id: string | null;
    text_preview: string;
  }[];
  kpi_series: { kpi: string; cell_id: string; points: Record<string, number | string | null>[] }[];
  decisions: Decision[];
  similar: {
    id: string;
    title: string;
    cause: string | null;
    region: string;
    detected_at: string;
    status: string;
    affected_subs: number;
  }[];
  maintenance: Record<string, unknown>[];
  notifications: Record<string, unknown>[];
  recovery: Record<string, unknown>[];
};

export type Page<T> = { items: T[]; total: number; limit: number; offset: number };

export type ComplaintRow = {
  id: string;
  received_at: string;
  channel: string;
  language: string | null;
  region: string | null;
  cell_id: string | null;
  site_id: string | null;
  site_confidence: number | null;
  category: string | null;
  status: string;
  incident_id: string | null;
  text_preview: string;
  text_length: number;
};

export type CoverageRow = {
  question: string;
  resolved: number;
  total: number;
  pct: number;
  detail: string;
};

export type Coverage = {
  measured_at: string;
  rows: CoverageRow[];
  time_alignment: CoverageRow[];
  warnings: string[];
  complaint_to_cell_pct: number;
};

export type QueryResult = {
  columns: { name: string; masked: boolean }[];
  rows: (string | number | null)[][];
  row_count: number;
  truncated: boolean;
  duration_ms: number;
  executed_sql: string;
  warnings: string[];
};

export type CareLookup = {
  found: boolean;
  subscriber: {
    token: string;
    region: string;
    segment: string;
    is_enterprise: boolean;
    sla_tier: string | null;
    opt_out: boolean;
    is_roaming: boolean;
    home_cell_id: string | null;
    last_cell_id: string | null;
    last_seen_at: string | null;
    msisdn_format: string | null;
  } | null;
  serving_cell: MapPoint | null;
  known_issue: {
    incident_id: string;
    title: string;
    priority: string;
    status: string;
    cause: string | null;
    detected_at: string;
    affected_subs: number;
    say_to_customer: string;
    eta_note: string;
    notification_sent: boolean;
  } | null;
  complaints: {
    id: string;
    received_at: string;
    channel: string;
    category: string | null;
    status: string;
    incident_id: string | null;
    text_preview: string;
  }[];
  care_calls: {
    id: string;
    started_at: string;
    duration_s: number;
    reason_code: string;
    reason_text: string | null;
  }[];
  notes: string[];
};

export type AnalysisWindow = { start: string; end: string; hours: number };

export type RateSeries = {
  scope: string;
  window: AnalysisWindow;
  metric: string;
  bucket_starts: string[];
  counts: number[];
  rate_per_1k: number[];
  deseasonalised: number[];
  subscribers_est: number;
  per_subscribers: number;
  gaps: number;
  n_points: number;
  caveats: string[];
};

export type LeadLag = {
  scope: string;
  kpi: string;
  window: AnalysisWindow;
  lags: number[];
  correlations: (number | null)[];
  best_lag_hours: number | null;
  best_correlation: number | null;
  kpi_leads: boolean;
  zero_lag_correlation: number | null;
  n_points: number;
  caveats: string[];
  reading: string;
};

export type CorrelationMatrix = {
  window: AnalysisWindow;
  region: string | null;
  labels: string[];
  pearson: (number | null)[][];
  spearman: (number | null)[][];
  top: { left: string; right: string; pearson: number | null; spearman: number | null }[];
  n_points: number;
  caveats: string[];
};

export type Drivers = {
  window: AnalysisWindow;
  region: string | null;
  drivers: { kpi: string; score: number; rank: number; n_points: number; caveats: string[] }[];
};

export type Pca = {
  window: AnalysisWindow;
  region: string | null;
  labels: string[];
  explained_variance_ratio: number[];
  cumulative_variance: number[];
  loadings: number[][];
  dropped_labels: string[];
  n_points: number;
  caveats: string[];
};

export type BusyCell = {
  cell_id: string;
  name: string;
  region: string;
  tech: string;
  complaints: number;
};

export type DigestPreview = {
  report_id: string;
  kind: string;
  title: string;
  generated_at: string;
  window_start: string;
  window_end: string;
  display_tz: string;
  headline: string[][];
  incidents: {
    incident_id: string;
    title: string;
    priority: string;
    status: string;
    cause: string | null;
    region: string;
    affected_subs: number;
    affected_cells: number;
    complaint_count: number;
    detected_at: string;
    duration_minutes: number | null;
    suppressed_reason: string | null;
  }[];
  sections: { heading: string; lines: string[] }[];
  caveats: string[];
  is_demo: boolean;
  card: Record<string, unknown>;
  card_bytes: number;
  dry_run: boolean;
  webhook_configured: boolean;
  delivery_note: string;
};

/* --------------------------------------------------------------- endpoints */

export const getMeta = () => api<Meta>("/api/v1/meta");
export const getOverview = () => api<Overview>("/api/v1/incidents/overview");
export const getIncidents = (query = "") =>
  api<Page<IncidentSummary>>(`/api/v1/incidents${query ? `?${query}` : ""}`);
export const getIncident = (id: string) => api<IncidentDetail>(`/api/v1/incidents/${id}`);
export const getIncidentMap = () => api<MapPoint[]>("/api/v1/incidents/map");
export const getComplaints = (query = "") =>
  api<Page<ComplaintRow>>(`/api/v1/complaints${query ? `?${query}` : ""}`);
export const getCoverage = () => api<Coverage>("/api/v1/discovery/join-coverage");
export const getTables = () => api<string[]>("/api/v1/discovery/tables");
export const getCareSamples = (limit = 8) => api<string[]>(`/api/v1/care/samples?limit=${limit}`);
export const careLookup = (identifier: string) =>
  api<CareLookup>("/api/v1/care/lookup", {
    method: "POST",
    body: JSON.stringify({ identifier }),
  });
export const getInsightsWindow = (days = 14) =>
  api<AnalysisWindow>(`/api/v1/insights/window?days=${days}`);
export const getComplaintRate = (query = "") =>
  api<RateSeries>(`/api/v1/insights/complaint-rate${query ? `?${query}` : ""}`);
export const getLeadLag = (query: string) => api<LeadLag>(`/api/v1/insights/lead-lag?${query}`);
export const getCorrelations = (query = "") =>
  api<CorrelationMatrix>(`/api/v1/insights/correlations${query ? `?${query}` : ""}`);
export const getDrivers = (query = "") =>
  api<Drivers>(`/api/v1/insights/drivers${query ? `?${query}` : ""}`);
export const getPca = (query = "") => api<Pca>(`/api/v1/insights/pca${query ? `?${query}` : ""}`);
export const getBusiestCells = (limit = 12) =>
  api<BusyCell[]>(`/api/v1/insights/busiest-cells?limit=${limit}`);
export const getDigest = (kind: "daily" | "weekly") =>
  api<DigestPreview>(`/api/v1/reports/digest?kind=${kind}`);
export const runQuery = (sql: string) =>
  api<QueryResult>("/api/v1/discovery/query", {
    method: "POST",
    body: JSON.stringify({ sql }),
  });
