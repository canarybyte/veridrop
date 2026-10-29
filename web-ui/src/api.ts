/* Veridrop 前端 API 层
   所有接口同源、无需鉴权；错误统一抛 Error(可读 message)。 */

export const API_PREFIX = "/app/api";

/* ---------- 请求 / 响应 ---------- */

export type Protocol = "anthropic" | "openai" | "gemini";

export interface DetectRequest {
  credential_id: string;
  model: string;
  include_long_context: boolean;
  include_long_context_extreme: boolean;
  /** 主检测完成后追加深度诊断（13–25 项），默认 false。 */
  deep: boolean;
  /** 用户手动勾选的探针名，至少一个（后端会拒绝空数组）。 */
  probes: string[];
}

export interface DetectResponse {
  job_id: string;
  status: string;
}

/* ---------- 凭据 ---------- */

/** 后端只返回掩码，前端任何地方都不持有明文（新增/编辑时除外）。 */
export interface Credential {
  id: string;
  name: string;
  base_url: string;
  host: string;
  api_key_masked: string;
  created_at: string | null;
  updated_at: string | null;
  last_used_at: string | null;
}

export interface CredentialListResponse {
  items: Credential[];
}

export interface CreateCredentialRequest {
  name: string;
  base_url: string;
  api_key: string;
}

export interface UpdateCredentialRequest {
  name?: string;
  base_url?: string;
  api_key?: string;
}

/* ---------- 模型探测 ---------- */

export interface ModelsResponse {
  ok: boolean;
  auth_ok: boolean;
  models_endpoint_supported: boolean;
  raw_count: number;
  all_models: string[];
  by_protocol: Record<Protocol, string[]>;
  best_by_protocol: Record<Protocol, string | null>;
  status: number | null;
  error: string | null;
  note: string | null;
}

/* ---------- 探针目录 ---------- */

export interface ProbeCategory {
  id: string;
  label: string;
  description: string;
}

export interface ProbeCatalogItem {
  name: string;
  display_name: string;
  description: string;
  /** 所属分类 id，对应 ProbeCatalog.categories[].id。 */
  category: string;
  passive: boolean;
  weight: number;
  /** 当前 protocol/model 下是否可用（false 时前端禁用勾选）。 */
  applies: boolean;
}

export interface ProbeCatalog {
  protocol: Protocol;
  model: string | null;
  /** 展示顺序即数组顺序。 */
  categories: ProbeCategory[];
  probes: ProbeCatalogItem[];
}

/* ---------- 探针 / 结论 ---------- */

export type ProbeStatus = "pending" | "running" | "pass" | "fail" | "skip" | "error";
export type ProbeResultStatus = "pass" | "fail" | "skip" | "error";
export type JobVerdict = "passed" | "marginal" | "failed";
export type RiskLevel = "low" | "medium" | "high";

export interface DetectorDescriptor {
  name: string;
  display_name: string;
  weight: number;
  passive: boolean;
}

export interface PerformanceUsage {
  input_tokens: number;
  output_tokens: number;
}

export interface PerformancePayload {
  total_latency_ms: number;
  ttft_ms: number;
  request_count: number;
  backoff_events: number;
  usage: PerformanceUsage;
}

/* ---------- 深度诊断 ---------- */

export type DeepCheckStatus = "ok" | "warn" | "unknown" | "unsupported" | "error";
export type DeepStatus = "done" | "error";

export interface DeepCheck {
  id: string;
  title: string;
  status: DeepCheckStatus;
  value: string;
  summary: string;
  details?: unknown;
}

export interface DeepRateLimitMetrics {
  headers: Record<string, string>;
  status_429: number;
  retry_after_s: number | null;
  rpm_estimate: number | null;
  tpm_estimate: number | null;
  pool_guess: string;
  pool_evidence: string[];
}

export interface DeepReliabilityMetrics {
  samples: number;
  success: number;
  failed: number;
  success_rate: number | null;
  errors: Record<string, number>;
  window_s: number;
  burst: { concurrency: number; ok: number; failed: number; status_429: number };
  rate_limit: DeepRateLimitMetrics;
}

export interface DeepLatencyMetrics {
  ttft_ms: { min: number | null; median: number | null; p95: number | null };
  total_ms: { median: number | null };
  samples: number;
}

export interface DeepCacheMetrics {
  supported: boolean;
  hit_rate: number | null;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  input_tokens: number;
  ttl_5m: { supported: boolean; read_tokens: number | null };
  ttl_1h: { supported: boolean; read_tokens: number | null; error: string | null };
  billing_note: string;
  evidence: string[];
}

export interface DeepSignatureMetrics {
  supported: boolean;
  present: boolean;
  freshness: string;
  signatures_equal: boolean | null;
  roundtrip_accepted: boolean | null;
  evidence: string[];
}

export interface DeepInjectionMetrics {
  system_prompt_detected: boolean;
  injection_level: string;
  extra_input_tokens: number | null;
  evidence: string[];
}

export interface DeepPassthroughMetrics {
  level: string;
  honored: string[];
  dropped: string[];
  evidence: string[];
}

export interface DeepPricingMetrics {
  official: {
    input_per_1m: number | null;
    output_per_1m: number | null;
    currency: string;
    matched_model: string | null;
  };
  observed_usage: {
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
  };
  reference_cost_usd: number | null;
  usage_consistency: string;
  margin: null;
  margin_note: string;
}

export interface DeepRoutingMetrics {
  coding_plan: { verdict: string; confidence: number };
  distillation: { verdict: string; confidence: number };
  evidence: string[];
}

export interface DeepMetrics {
  reliability: DeepReliabilityMetrics;
  latency: DeepLatencyMetrics;
  cache: DeepCacheMetrics;
  signature: DeepSignatureMetrics;
  injection: DeepInjectionMetrics;
  passthrough: DeepPassthroughMetrics;
  pricing: DeepPricingMetrics;
  routing: DeepRoutingMetrics;
}

/** 单次深度诊断的完整报告（随历史报告落盘）。 */
export interface DeepReport {
  enabled: boolean;
  status: DeepStatus;
  error: string | null;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  protocol: string;
  model: string;
  disclaimer: string;
  metrics: DeepMetrics;
  checks: DeepCheck[];
}

/** 展示层需要的报告子集：实时流只拿到 deep_finished 里的指标，不含落盘元信息。 */
export interface DeepReportView {
  status: DeepStatus;
  error: string | null;
  duration_ms: number;
  metrics: DeepMetrics | null;
  disclaimer?: string;
}

/* ---------- SSE 事件 payload ---------- */

export interface QueuedPayload {
  job_id: string;
  protocol: string;
  target_model: string;
}

export interface RunStartedPayload {
  total: number;
  detectors: DetectorDescriptor[];
}

export interface ProbeStartedPayload {
  name: string;
  display_name: string;
  passive: boolean;
}

export interface ProbeFinishedPayload {
  name: string;
  display_name: string;
  weight: number;
  status: ProbeResultStatus;
  score: number;
  duration_ms: number | null;
  error: string | null;
  passive: boolean;
  skip_reason?: string;
}

export interface JobFinishedPayload {
  job_id: string;
  total_score: number;
  verdict: JobVerdict;
  risk_level: RiskLevel;
  summary: string;
  run_error: string | null;
  self_reported_identity: string | null;
  detected_brands: string[];
  duration_ms: number;
  performance: PerformancePayload;
  probes_total: number;
  probes_passed: number;
  probes_failed: number;
  report_id: string;
  /** 老后端不下发这两个字段，缺省视为未开启深度诊断。 */
  deep_enabled?: boolean;
  deep_status?: "running" | "skipped" | null;
}

export interface DeepStartedCheck {
  id: string;
  title: string;
}

export interface DeepStartedPayload {
  total: number;
  checks: DeepStartedCheck[];
}

/** 与 DeepCheck 同构：每完成一项推一次。 */
export type DeepProgressPayload = DeepCheck;

export interface DeepFinishedPayload {
  status: DeepStatus;
  error: string | null;
  duration_ms: number;
  /** 失败时后端可能给出空/缺省，展示层按 null 兜底。 */
  metrics: DeepMetrics | null;
  checks: DeepCheck[];
}

export interface JobErrorPayload {
  job_id: string;
  error: string;
}

export type DetectionEventType =
  | "queued"
  | "run_started"
  | "probe_started"
  | "probe_finished"
  | "job_finished"
  | "job_error"
  | "deep_started"
  | "deep_progress"
  | "deep_finished";

export type DetectionEvent =
  | { seq: number; ts: number; type: "queued"; payload: QueuedPayload }
  | { seq: number; ts: number; type: "run_started"; payload: RunStartedPayload }
  | { seq: number; ts: number; type: "probe_started"; payload: ProbeStartedPayload }
  | { seq: number; ts: number; type: "probe_finished"; payload: ProbeFinishedPayload }
  | { seq: number; ts: number; type: "job_finished"; payload: JobFinishedPayload }
  | { seq: number; ts: number; type: "job_error"; payload: JobErrorPayload }
  | { seq: number; ts: number; type: "deep_started"; payload: DeepStartedPayload }
  | { seq: number; ts: number; type: "deep_progress"; payload: DeepProgressPayload }
  | { seq: number; ts: number; type: "deep_finished"; payload: DeepFinishedPayload };

/* ---------- 错误处理 ---------- */

function detailEntryToText(entry: unknown): string {
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object") {
    const candidate = entry as { msg?: unknown; loc?: unknown };
    const msg = typeof candidate.msg === "string" ? candidate.msg : "";
    const loc = Array.isArray(candidate.loc)
      ? candidate.loc.filter((item) => typeof item === "string" || typeof item === "number").join(".")
      : "";
    if (msg && loc) return `${loc}: ${msg}`;
    if (msg) return msg;
    try {
      return JSON.stringify(entry);
    } catch {
      return String(entry);
    }
  }
  if (entry === null || entry === undefined) return "";
  return String(entry);
}

/** 把后端 detail（字符串 / 数组 / 对象）拼成可读错误文本。 */
export function extractDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map(detailEntryToText).filter(Boolean).join("; ");
  }
  return detailEntryToText(detail);
}

/** 把非 2xx 响应里的 detail（其实也可以是裸数组/字符串）转成可读错误。 */
function httpError(status: number, data: unknown): Error {
  const detail =
    data && typeof data === "object" && "detail" in data
      ? (data as { detail: unknown }).detail
      : data;
  const message = extractDetail(detail);
  return new Error(message || `请求失败（HTTP ${status}）`);
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw new Error("网络请求失败，请确认服务可访问。");
  }

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text) as unknown;
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    throw httpError(res.status, data);
  }

  return data as T;
}

export function getJSON<T>(url: string): Promise<T> {
  return request<T>(url);
}

/* ---------- 凭据接口 ---------- */

export function listCredentials(): Promise<CredentialListResponse> {
  return request<CredentialListResponse>(`${API_PREFIX}/credentials`);
}

export function createCredential(body: CreateCredentialRequest): Promise<Credential> {
  return request<Credential>(`${API_PREFIX}/credentials`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateCredential(
  id: string,
  body: UpdateCredentialRequest
): Promise<Credential> {
  return request<Credential>(`${API_PREFIX}/credentials/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

/** DELETE 成功返回 204 无响应体，不能走 JSON 解析。 */
export async function deleteCredential(id: string): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_PREFIX}/credentials/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new Error("网络请求失败，请确认服务可访问。");
  }
  if (res.ok) return;

  const text = await res.text();
  let data: unknown = text;
  if (text) {
    try {
      data = JSON.parse(text) as unknown;
    } catch {
      data = text;
    }
  }
  throw httpError(res.status, data);
}

/** 通过后端探活中转站的 GET /models；HTTP 错误会抛异常，探测失败则以 ok=false 返回。 */
export function fetchCredentialModels(id: string): Promise<ModelsResponse> {
  return request<ModelsResponse>(
    `${API_PREFIX}/credentials/${encodeURIComponent(id)}/models`,
    { method: "POST" }
  );
}

/** 拉取探针分类目录；model 可选，只影响各探针的 applies 字段。 */
export function getProbeCatalog(protocol: Protocol, model?: string): Promise<ProbeCatalog> {
  const search = new URLSearchParams();
  search.set("protocol", protocol);
  const trimmed = model?.trim();
  if (trimmed) search.set("model", trimmed);
  return request<ProbeCatalog>(`${API_PREFIX}/probes?${search.toString()}`);
}

export function submitDetection(body: DetectRequest): Promise<DetectResponse> {
  return request<DetectResponse>(`${API_PREFIX}/detect`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/* ---------- SSE 解析 ---------- */

const KNOWN_EVENT_TYPES: readonly DetectionEventType[] = [
  "queued",
  "run_started",
  "probe_started",
  "probe_finished",
  "job_finished",
  "job_error",
  "deep_started",
  "deep_progress",
  "deep_finished",
];

export function isKnownEventType(type: string): type is DetectionEventType {
  return (KNOWN_EVENT_TYPES as readonly string[]).includes(type);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** 解析单条 SSE data 字段；无法解析或类型不在契约内时返回 null。 */
export function parseDetectionEvent(raw: string, fallbackSeq: number): DetectionEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
  if (!isRecord(parsed)) return null;

  const type = typeof parsed.type === "string" ? parsed.type : "";
  if (!isKnownEventType(type)) return null;

  const seq = typeof parsed.seq === "number" ? parsed.seq : fallbackSeq;
  const ts = typeof parsed.ts === "number" ? parsed.ts : Date.now() / 1000;
  const payload = isRecord(parsed.payload) ? parsed.payload : {};

  // 运行时数据由后端保证结构，这里仅做形状归一化后交给类型系统。
  return { ...parsed, seq, ts, type, payload } as unknown as DetectionEvent;
}

/* ---------- 历史报告 ---------- */

export type ReportStatus = "queued" | "running" | "done" | "error";

export interface ReportSummary {
  id: string;
  protocol: Protocol;
  host: string;
  base_url: string;
  target_model: string;
  status: ReportStatus;
  total_score: number | null;
  verdict: JobVerdict | null;
  risk_level: RiskLevel | null;
  summary: string;
  created_at: string | null;
  finished_at: string | null;
  total_probes: number;
  passed_probes: number;
  failed_probes: number;
  /** 发起检测时使用的凭据；凭据被删除后可能为 null。 */
  credential_id: string | null;
  credential_name: string | null;
}

export interface ReportListResponse {
  items: ReportSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReportProbe {
  name: string;
  display_name: string;
  status: ProbeResultStatus;
  score: number;
  weight: number;
  duration_ms: number | null;
  error: string | null;
  details: Record<string, unknown> | null;
}

/** 历史报告里的性能字段按可选处理（兼容旧数据缺字段）。 */
export interface ReportPerformance {
  total_latency_ms?: number | null;
  ttft_ms?: number | null;
  request_count?: number | null;
  backoff_events?: number | null;
  usage?: {
    input_tokens?: number | null;
    output_tokens?: number | null;
  } | null;
}

/** 一次上游请求-响应对；流式时为合成后的 response + 原始事件。 */
export interface Exchange {
  id: number;
  probe_name: string;
  /** 该报告内递增序号。 */
  seq: number;
  request: Record<string, unknown> | null;
  response: Record<string, unknown> | null;
  /** 流式原始事件 [{event,data}] 或 chunk 列表。 */
  events: unknown[] | null;
  error: Record<string, unknown> | null;
  stream: boolean;
  latency_ms: number | null;
  status_code: number | null;
  created_at: string;
}

export interface ExchangeListResponse {
  items: Exchange[];
  total: number;
}

export interface ReportDetail {
  id: string;
  protocol: Protocol;
  base_url: string;
  host: string;
  target_model: string;
  status: ReportStatus;
  api_key_masked: string;
  total_score: number | null;
  verdict: JobVerdict | null;
  risk_level: RiskLevel | null;
  summary: string;
  run_error: string | null;
  self_reported_identity: string | null;
  detected_brands: string[];
  performance: ReportPerformance | null;
  report: Record<string, unknown> | null;
  probes: ReportProbe[];
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  total_probes: number;
  passed_probes: number;
  failed_probes: number;
  /** 发起检测时使用的凭据；凭据被删除后可能为 null。 */
  credential_id: string | null;
  credential_name: string | null;
  /** 该报告已采集的上游请求-响应对数量（旧数据可能缺失）。 */
  exchange_count?: number;
  /** 深度诊断报告；未开启时为 null。 */
  deep_report?: DeepReport | null;
  /** "done" | "error" | "skipped" | null（旧后端可能缺失）。 */
  deep_status?: string | null;
}

export interface ReportListParams {
  protocol?: Protocol;
  model?: string;
  verdict?: JobVerdict;
  minScore?: number;
  credential_id?: string;
  limit?: number;
  offset?: number;
}

export function listReports(params: ReportListParams = {}): Promise<ReportListResponse> {
  const search = new URLSearchParams();
  if (params.protocol) search.set("protocol", params.protocol);
  const model = params.model?.trim();
  if (model) search.set("model", model);
  if (params.verdict) search.set("verdict", params.verdict);
  if (typeof params.minScore === "number" && Number.isFinite(params.minScore)) {
    search.set("min_score", String(params.minScore));
  }
  if (params.credential_id) search.set("credential_id", params.credential_id);
  search.set("limit", String(params.limit ?? 20));
  search.set("offset", String(params.offset ?? 0));
  return request<ReportListResponse>(`${API_PREFIX}/reports?${search.toString()}`);
}

export function getReport(id: string): Promise<ReportDetail> {
  return request<ReportDetail>(`${API_PREFIX}/reports/${encodeURIComponent(id)}`);
}

export interface DeepOnlyResponse {
  job_id: string;
  status: string;
  source_report_id: string;
}

/** 对一条历史检测补跑深度诊断；返回新任务 id，前端用它接管控制台的事件流。 */
export function startReportDeep(reportId: string): Promise<DeepOnlyResponse> {
  return request<DeepOnlyResponse>(
    `${API_PREFIX}/reports/${encodeURIComponent(reportId)}/deep`,
    { method: "POST" }
  );
}

/** 按报告查询已采集的上游请求-响应对；probe 可选，用于按探针 name 过滤。 */
export function listReportExchanges(
  id: string,
  probe?: string
): Promise<ExchangeListResponse> {
  const search = new URLSearchParams();
  const probeName = probe?.trim();
  if (probeName) search.set("probe", probeName);
  const query = search.toString();
  return request<ExchangeListResponse>(
    `${API_PREFIX}/reports/${encodeURIComponent(id)}/exchanges${query ? `?${query}` : ""}`
  );
}
