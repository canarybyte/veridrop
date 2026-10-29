import type { DeepCheck, DeepCheckStatus, DeepReportView } from "../api";
import { formatDuration } from "../lib/format";

export type DeepPanelStatus = "idle" | "running" | "done" | "error";

/** 实时流里的占位项带 pending 标记；落盘报告里的 check 不带。 */
export type DeepPanelCheck = DeepCheck & { pending?: boolean };

interface DeepPanelProps {
  checks: DeepPanelCheck[];
  /** 实时阶段只有指标子集，历史报告给完整 DeepReport（结构兼容）。 */
  report: DeepReportView | null;
  status: DeepPanelStatus;
  error: string | null;
}

const STATUS_META: Record<DeepCheckStatus, { label: string; chip: string }> = {
  ok: { label: "ok", chip: "chip-success" },
  warn: { label: "warn", chip: "chip-warning" },
  unknown: { label: "unknown", chip: "chip-muted" },
  unsupported: { label: "unsupported", chip: "chip-muted" },
  error: { label: "error", chip: "chip-danger" },
};

const DEFAULT_DISCLAIMER =
  "深度诊断基于短时采样与启发式推断，仅用于辅助判断，不等同官方计费或官方声明。";

const PANEL_STATUS_META: Record<DeepPanelStatus, { label: string; chip: string }> = {
  idle: { label: "待开始", chip: "chip-muted" },
  running: { label: "诊断中", chip: "chip-primary" },
  done: { label: "已完成", chip: "chip-success" },
  error: { label: "诊断失败", chip: "chip-danger" },
};

function stringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "null";
  } catch {
    return String(value);
  }
}

/** 比率既可能是 0–1 也可能是百分数，按量级兜底。 */
function formatRate(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const percent = value <= 1 ? value * 100 : value;
  return `${percent.toFixed(1)}%`;
}

function formatMs(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)} ms`;
}

function formatUnitPrice(
  value: number | null | undefined,
  currency?: string
): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const unit = currency || "USD";
  return `${value.toFixed(value < 0.01 ? 4 : 2)} ${unit}/1M`;
}

function formatCost(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `$${value.toFixed(value < 0.01 ? 4 : 2)}`;
}

export function DeepPanel({ checks, report, status, error }: DeepPanelProps) {
  const total = checks.length;
  const done = checks.filter((check) => !check.pending).length;
  const percent = total > 0 ? Math.min(100, (done / total) * 100) : 0;
  const panelStatus = PANEL_STATUS_META[status];
  const disclaimer = report?.disclaimer || DEFAULT_DISCLAIMER;
  const errorText = error || report?.error || "";

  const metrics = report?.metrics ?? null;
  const reliability = metrics?.reliability ?? null;
  const latency = metrics?.latency ?? null;
  const cache = metrics?.cache ?? null;
  const pricing = metrics?.pricing ?? null;

  return (
    <div className="deep-panel">
      <div className="panel-head deep-head">
        <h2 className="panel-title deep-title">深度诊断（13–25 项）</h2>
        <div className="deep-head-chips">
          <span className="chip chip-muted">启发式结论</span>
          <span className={`chip ${panelStatus.chip}`}>
            <span className="chip-dot" aria-hidden="true" />
            {panelStatus.label}
          </span>
          {total > 0 ? (
            <span className="chip chip-muted">
              {done}/{total}
            </span>
          ) : null}
          {report && report.duration_ms > 0 ? (
            <span className="chip chip-muted">{formatDuration(report.duration_ms)}</span>
          ) : null}
        </div>
      </div>

      <p className="panel-hint">
        主检测完成后追加的短时采样：逐项给出结论、证据与指标，可展开查看原始 details。
      </p>
      <p className="deep-disclaimer">{disclaimer}</p>

      {status === "running" ? (
        <div className="deep-progress">
          <div className="progress-head">
            <span className="small">诊断进度</span>
            <span className="small muted">
              {done}/{total || "—"}
            </span>
          </div>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${percent}%` }} />
          </div>
        </div>
      ) : null}

      {status === "error" || (errorText && status !== "running") ? (
        <div className="alert alert-danger deep-error" role="alert">
          深度诊断失败：{errorText || "未知错误"}
        </div>
      ) : null}

      {total === 0 ? (
        <div className="empty-state deep-empty">
          <p className="page-subtitle">
            {status === "running" ? "正在准备 13 项诊断…" : "暂无深度诊断结果。"}
          </p>
        </div>
      ) : (
        <div className="reports-table-wrap deep-table-wrap">
          <table className="reports-table deep-table">
            <thead>
              <tr>
                <th>编号</th>
                <th>检查项</th>
                <th>状态</th>
                <th>结论</th>
              </tr>
            </thead>
            <tbody>
              {checks.map((check) => {
                const meta = STATUS_META[check.status] ?? STATUS_META.unknown;
                const hasDetails =
                  check.details !== undefined && check.details !== null;
                return (
                  <tr key={check.id}>
                    <td className="deep-id">{check.id}</td>
                    <td className="deep-check-title">{check.title || "—"}</td>
                    <td>
                      <span className={`chip ${check.pending ? "chip-muted" : meta.chip}`}>
                        <span className="chip-dot" aria-hidden="true" />
                        {check.pending ? "等待中" : meta.label}
                      </span>
                    </td>
                    <td className="deep-value-cell">
                      <span className="deep-value">
                        {check.pending ? "等待中" : check.value || "—"}
                      </span>
                      {check.summary ? (
                        <span className="deep-summary">{check.summary}</span>
                      ) : null}
                      {hasDetails ? (
                        <details className="deep-details">
                          <summary>查看 details</summary>
                          <pre className="json-block">{stringify(check.details)}</pre>
                        </details>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {metrics ? (
        <div className="deep-overview">
          <div className="deep-metrics">
            <div className="deep-metric-card">
              <span className="deep-metric-label">短时成功率</span>
              <span className="deep-metric-value">
                {formatRate(reliability?.success_rate)}
              </span>
              <span className="deep-metric-sub">
                {reliability
                  ? `样本 ${reliability.samples} · 失败 ${reliability.failed} · 窗口 ${reliability.window_s}s`
                  : "—"}
              </span>
            </div>

            <div className="deep-metric-card">
              <span className="deep-metric-label">TTFT 中位数</span>
              <span className="deep-metric-value">
                {formatMs(latency?.ttft_ms?.median)}
              </span>
              <span className="deep-metric-sub">
                {latency
                  ? `P95 ${formatMs(latency.ttft_ms?.p95)} · 样本 ${latency.samples}`
                  : "—"}
              </span>
            </div>

            <div className="deep-metric-card">
              <span className="deep-metric-label">缓存命中率</span>
              <span className="deep-metric-value">
                {cache?.supported ? formatRate(cache.hit_rate) : "不支持"}
              </span>
              <span className="deep-metric-sub">
                {cache
                  ? `读 ${cache.cache_read_tokens} · 写 ${cache.cache_creation_tokens}`
                  : "—"}
              </span>
            </div>
          </div>

          <div className="deep-pricing">
            <div className="info-row">
              <span className="info-label">官方参考价</span>
              <span className="info-value">
                {pricing
                  ? `${formatUnitPrice(
                      pricing.official?.input_per_1m,
                      pricing.official?.currency
                    )} 入 / ${formatUnitPrice(
                      pricing.official?.output_per_1m,
                      pricing.official?.currency
                    )} 出`
                  : "—"}
                {pricing?.official?.matched_model ? (
                  <span className="muted small">
                    {" "}
                    · {pricing.official.matched_model}
                  </span>
                ) : null}
              </span>
            </div>

            <div className="info-row">
              <span className="info-label">参考成本</span>
              <span className="info-value">
                {formatCost(pricing?.reference_cost_usd)}
              </span>
            </div>

            <div className="info-row">
              <span className="info-label">利润率</span>
              <span className="info-value">
                <span className="deep-margin">无法计算（缺少中转站标价）</span>
              </span>
            </div>

            {pricing?.margin_note ? (
              <p className="deep-note">{pricing.margin_note}</p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
