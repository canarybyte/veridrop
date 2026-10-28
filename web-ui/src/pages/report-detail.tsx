import { Fragment, useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { Link, useParams } from "react-router-dom";

import type {
  JobVerdict,
  ReportDetail,
  ReportPerformance,
  RiskLevel,
} from "../api";
import { getReport } from "../api";
import {
  MODE_LABEL,
  ProbeStatusChip,
  RiskChip,
  StatusChip,
  VerdictChip,
  formatTimestamp,
} from "../components/report-ui";
import { clampScore, formatDuration } from "../lib/format";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** performance 优先用顶层字段，缺失时逐项回落 report.performance。 */
function pickPerformance(detail: ReportDetail): ReportPerformance | null {
  const nested =
    isRecord(detail.report) && isRecord(detail.report.performance)
      ? (detail.report.performance as ReportPerformance)
      : null;
  const top = detail.performance;
  if (!top) return nested;
  if (!nested) return top;
  return {
    total_latency_ms: top.total_latency_ms ?? nested.total_latency_ms,
    ttft_ms: top.ttft_ms ?? nested.ttft_ms,
    request_count: top.request_count ?? nested.request_count,
    backoff_events: top.backoff_events ?? nested.backoff_events,
    usage: top.usage ?? nested.usage,
  };
}

/** 耗时：finished-started，缺失时回落性能字段 total_latency_ms。 */
function resolveDuration(
  detail: ReportDetail,
  performance: ReportPerformance | null
): number | null {
  const started = detail.started_at ? new Date(detail.started_at).getTime() : NaN;
  const finished = detail.finished_at ? new Date(detail.finished_at).getTime() : NaN;
  if (Number.isFinite(started) && Number.isFinite(finished) && finished >= started) {
    return finished - started;
  }
  return numOrNull(performance?.total_latency_ms);
}

function ringTone(
  verdict: JobVerdict | null,
  risk: RiskLevel | null
): "" | "is-success" | "is-warn" | "is-fail" {
  if (risk === "high") return "is-fail";
  if (verdict === "passed") return "is-success";
  if (verdict === "marginal") return "is-warn";
  if (verdict === "failed") return "is-fail";
  return "";
}

function truncate(text: string, limit: number): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function stringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "null";
  } catch {
    return String(value);
  }
}

function IdentityText({ text }: { text: string | null }) {
  const value = (text ?? "").trim();
  if (!value) return <span className="muted">未上报</span>;
  if (value.length <= 160) return <span className="report-identity-text">{value}</span>;
  return (
    <details className="report-identity">
      <summary>
        <span className="report-identity-text">{truncate(value, 160)}</span>
        <span className="report-identity-more">展开全部</span>
      </summary>
      <pre className="code report-json">{value}</pre>
    </details>
  );
}

function BackLink() {
  return (
    <Link className="report-back" to="/reports">
      ← 回到历史报告
    </Link>
  );
}

export default function ReportDetailPage() {
  const { id } = useParams<{ id: string }>();
  const reportId = id ?? "";
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!reportId) {
      setLoading(false);
      setNotFound(true);
      return;
    }
    let active = true;
    setLoading(true);
    setNotFound(false);
    setError(null);
    setDetail(null);

    getReport(reportId)
      .then((response) => {
        if (!active) return;
        setDetail(response);
        setLoading(false);
      })
      .catch((cause: unknown) => {
        if (!active) return;
        setLoading(false);
        const message = cause instanceof Error ? cause.message : "";
        if (/^report not found$/i.test(message.trim())) {
          setNotFound(true);
          return;
        }
        setError(message || "加载报告失败。");
      });

    return () => {
      active = false;
    };
  }, [reportId, reloadKey]);

  if (loading) {
    return (
      <div className="page">
        <BackLink />
        <section className="panel">
          <h2 className="panel-title">报告详情</h2>
          <p className="panel-hint">正在加载报告…</p>
          <div className="reports-skeleton-block">
            {Array.from({ length: 4 }, (_, index) => (
              <span key={`loading-${index}`} className="reports-skeleton" />
            ))}
          </div>
        </section>
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="page">
        <BackLink />
        <section className="panel">
          <div className="empty-state">
            <h2 className="page-title">报告不存在或已过期</h2>
            <p className="page-subtitle">
              该报告可能已被清理，或链接中的 ID 不正确。
            </p>
            <div className="reports-empty-actions">
              <Link className="btn btn-primary" to="/reports">
                返回历史报告
              </Link>
            </div>
          </div>
        </section>
      </div>
    );
  }

  if (error || !detail) {
    return (
      <div className="page">
        <BackLink />
        <section className="panel">
          <div className="alert alert-danger reports-error" role="alert">
            <p>{error ?? "加载报告失败。"}</p>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setReloadKey((value) => value + 1)}
            >
              重试
            </button>
          </div>
        </section>
      </div>
    );
  }

  const performance = pickPerformance(detail);
  const duration = resolveDuration(detail, performance);
  const requestCount = numOrNull(performance?.request_count);
  const ttft = numOrNull(performance?.ttft_ms);
  const inputTokens = numOrNull(performance?.usage?.input_tokens);
  const outputTokens = numOrNull(performance?.usage?.output_tokens);
  const scoreValue = numOrNull(detail.total_score);
  const scoreLabel = scoreValue === null ? "—" : scoreValue.toFixed(1);
  const ringScore = scoreValue === null ? 0 : clampScore(scoreValue);
  const tone = ringTone(detail.verdict, detail.risk_level);
  // 失败作业可能缺少这些字段，做一层运行时兜底，避免详情页崩溃。
  const probes = Array.isArray(detail.probes) ? detail.probes : [];
  const brands = Array.isArray(detail.detected_brands) ? detail.detected_brands : [];
  const passedProbes = numOrNull(detail.passed_probes) ?? 0;
  const failedProbes = numOrNull(detail.failed_probes) ?? 0;
  const skippedProbes = probes.filter((probe) => probe.status === "skip").length;
  const totalFromSummary = numOrNull(detail.total_probes);
  const totalProbes =
    totalFromSummary !== null && totalFromSummary > 0 ? totalFromSummary : probes.length;

  return (
    <div className="page">
      <BackLink />

      <div className="page-header">
        <div>
          <h1 className="page-title">
            {detail.host} · {detail.target_model} ·{" "}
            {MODE_LABEL[detail.mode] ?? detail.mode}
          </h1>
          <p className="page-subtitle">
            协议 <code className="code">{detail.protocol}</code> · 报告 ID{" "}
            <code className="code">{detail.id}</code>
          </p>
        </div>
        <div className="report-badges">
          <VerdictChip verdict={detail.verdict} />
          <RiskChip risk={detail.risk_level} />
          <StatusChip status={detail.status} />
        </div>
      </div>

      <section className="panel report-detail-header">
        <div className="report-detail-score">
          <div
            className={["score-ring", "report-detail-ring", tone]
              .filter(Boolean)
              .join(" ")}
            style={{ "--score": String(ringScore) } as CSSProperties}
            role="img"
            aria-label={`综合评分 ${scoreLabel}`}
          >
            <span className="score-ring-value">{scoreLabel}</span>
          </div>
          <div className="report-detail-score-caption">
            <span className="report-detail-score-label">综合评分</span>
            <span className="muted small">
              {scoreValue === null ? "未生成评分" : "满分 100"}
            </span>
          </div>
        </div>

        <dl className="report-meta-grid">
          <div className="report-meta-item">
            <dt>综合评分</dt>
            <dd className="report-meta-value">{scoreLabel}</dd>
          </div>
          <div className="report-meta-item">
            <dt>结论</dt>
            <dd className="report-meta-value">
              <VerdictChip verdict={detail.verdict} />
            </dd>
          </div>
          <div className="report-meta-item">
            <dt>风险等级</dt>
            <dd className="report-meta-value">
              <RiskChip risk={detail.risk_level} />
            </dd>
          </div>
          <div className="report-meta-item">
            <dt>检测时间</dt>
            <dd className="report-meta-value">{formatTimestamp(detail.created_at)}</dd>
          </div>
          <div className="report-meta-item">
            <dt>耗时</dt>
            <dd className="report-meta-value">{formatDuration(duration)}</dd>
          </div>
          <div className="report-meta-item">
            <dt>请求数</dt>
            <dd className="report-meta-value">
              {requestCount === null ? "—" : requestCount}
            </dd>
          </div>
          <div className="report-meta-item">
            <dt>TTFT</dt>
            <dd className="report-meta-value">{formatDuration(ttft)}</dd>
          </div>
          <div className="report-meta-item">
            <dt>Token 用量</dt>
            <dd className="report-meta-value is-mono">
              {inputTokens === null && outputTokens === null
                ? "—"
                : `${inputTokens ?? "—"} in / ${outputTokens ?? "—"} out`}
            </dd>
          </div>
          <div className="report-meta-item">
            <dt>API Key</dt>
            <dd className="report-meta-value is-mono">{detail.api_key_masked || "—"}</dd>
          </div>
        </dl>
      </section>

      <section className="panel">
        <h2 className="panel-title">身份与诊断</h2>
        <p className="panel-hint">模型自报身份、检测到的非官方品牌与综合摘要。</p>

        <div className="report-detail-fields">
          <div className="info-row">
            <span className="info-label">模型身份</span>
            <span className="info-value">
              <IdentityText text={detail.self_reported_identity} />
            </span>
          </div>

          <div className="info-row">
            <span className="info-label">非官方品牌</span>
            <span className="info-value">
              {brands.length > 0 ? (
                <>
                  <span className="chip-list">
                    {brands.map((brand) => (
                      <span className="chip chip-danger" key={brand}>
                        {brand}
                      </span>
                    ))}
                  </span>
                  <p className="report-brand-warning">检测到非官方后端品牌。</p>
                </>
              ) : (
                <span className="muted">未检测到</span>
              )}
            </span>
          </div>

          <div className="info-row">
            <span className="info-label">诊断摘要</span>
            <span className="info-value">{detail.summary || "无额外摘要。"}</span>
          </div>
        </div>

        {detail.run_error ? (
          <div className="alert alert-danger report-run-error" role="alert">
            运行错误：{detail.run_error}
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">探针结果</h2>
          <div className="report-probe-stats">
            <span className="chip chip-success">通过 {passedProbes}</span>
            <span className="chip chip-danger">失败 {failedProbes}</span>
            <span className="chip chip-muted">跳过 {skippedProbes}</span>
            <span className="chip chip-muted">总计 {totalProbes}</span>
          </div>
        </div>
        <p className="panel-hint">失败或执行错误的探针可展开查看原始 details。</p>

        {probes.length === 0 ? (
          <div className="empty-state">
            <p className="page-subtitle">
              {detail.run_error
                ? "本次任务未能产出探针结果，请查看上方运行错误。"
                : "该报告没有探针明细。"}
            </p>
          </div>
        ) : (
          <div className="reports-table-wrap">
            <table className="reports-table report-probe-table">
              <thead>
                <tr>
                  <th>探针</th>
                  <th>状态</th>
                  <th>得分</th>
                  <th>权重</th>
                  <th>耗时</th>
                  <th>错误</th>
                </tr>
              </thead>
              <tbody>
                {probes.map((probe) => {
                  const expandable = probe.status === "fail" || probe.status === "error";
                  const probeScore =
                    typeof probe.score === "number" ? probe.score.toFixed(1) : "—";
                  return (
                    <Fragment key={probe.name}>
                      <tr>
                        <td>
                          <span className="report-probe-name">{probe.display_name}</span>
                          <span className="report-probe-id">{probe.name}</span>
                        </td>
                        <td>
                          <ProbeStatusChip status={probe.status} />
                        </td>
                        <td className="reports-num">{probeScore}</td>
                        <td className="reports-num">{probe.weight}</td>
                        <td className="reports-num">{formatDuration(probe.duration_ms)}</td>
                        <td>
                          <span
                            className="report-probe-error"
                            title={probe.error ?? undefined}
                          >
                            {probe.error ? truncate(probe.error, 120) : "—"}
                          </span>
                        </td>
                      </tr>
                      {expandable ? (
                        <tr className="report-details-row">
                          <td colSpan={6}>
                            <details className="report-details">
                              <summary>查看 details</summary>
                              <pre className="code report-json">{stringify(probe.details)}</pre>
                            </details>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
