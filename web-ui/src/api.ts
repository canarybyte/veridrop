/* Veridrop 前端 API 层
   所有接口同源、无需鉴权；错误统一抛 Error(可读 message)。 */

export const API_PREFIX = "/app/api";

/* ---------- 请求 / 响应 ---------- */

export type Protocol = "anthropic" | "openai" | "gemini";
export type DetectionMode = "quick" | "standard" | "full";

export interface DetectRequest {
  credential_id: string;
  model: string;
  mode: DetectionMode;
  include_long_context: boolean;
  include_long_context_extreme: boolean;
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

/* ---------- SSE 事件 payload ---------- */

export interface QueuedPayload {
  job_id: string;
  protocol: string;
  target_model: string;
  mode: string;
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
  | "job_error";

export type DetectionEvent =
  | { seq: number; ts: number; type: "queued"; payload: QueuedPayload }
  | { seq: number; ts: number; type: "run_started"; payload: RunStartedPayload }
  | { seq: number; ts: number; type: "probe_started"; payload: ProbeStartedPayload }
  | { seq: number; ts: number; type: "probe_finished"; payload: ProbeFinishedPayload }
  | { seq: number; ts: number; type: "job_finished"; payload: JobFinishedPayload }
  | { seq: number; ts: number; type: "job_error"; payload: JobErrorPayload };

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
  mode: DetectionMode;
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

export interface ReportDetail {
  id: string;
  protocol: Protocol;
  base_url: string;
  host: string;
  target_model: string;
  mode: DetectionMode;
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
