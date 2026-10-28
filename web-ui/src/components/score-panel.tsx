import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { Link } from "react-router-dom";

import type { JobFinishedPayload, JobVerdict, RiskLevel } from "../api";
import type {
  DetectionProgress,
  ProbeState,
  StreamStatus,
} from "../hooks/use-detection-stream";
import { clampScore, formatDuration, formatScore } from "../lib/format";

interface ScorePanelProps {
  progress: DetectionProgress;
  probes: ProbeState[];
  jobResult: JobFinishedPayload | null;
  status: StreamStatus;
}

const VERDICT_LABEL: Record<JobVerdict, string> = {
  passed: "通过",
  marginal: "边缘",
  failed: "未通过",
};

const VERDICT_CHIP: Record<JobVerdict, string> = {
  passed: "chip-success",
  marginal: "chip-warning",
  failed: "chip-danger",
};

const CONSISTENCY: Record<JobVerdict, { label: string; chip: string }> = {
  passed: { label: "接入正常", chip: "chip-success" },
  marginal: { label: "存在偏差", chip: "chip-warning" },
  failed: { label: "未通过", chip: "chip-danger" },
};

const RISK: Record<RiskLevel, { label: string; chip: string }> = {
  low: { label: "低风险", chip: "chip-success" },
  medium: { label: "中风险", chip: "chip-warning" },
  high: { label: "高风险", chip: "chip-danger" },
};

function gradeOf(score: number): { label: string; chip: string } {
  if (score >= 85) return { label: "优秀", chip: "chip-success" };
  if (score >= 70) return { label: "通过", chip: "chip-success" };
  if (score >= 50) return { label: "基本合格", chip: "chip-warning" };
  return { label: "未达标", chip: "chip-danger" };
}

function ringTone(result: JobFinishedPayload): string {
  if (result.risk_level === "high") return "danger";
  if (result.verdict === "passed") return "success";
  if (result.verdict === "marginal") return "warn";
  return "danger";
}

function truncate(text: string, limit: number): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function ScorePanel({ progress, probes, jobResult, status }: ScorePanelProps) {
  const [animatedScore, setAnimatedScore] = useState(0);

  useEffect(() => {
    if (!jobResult) {
      setAnimatedScore(0);
      return;
    }
    const target = clampScore(jobResult.total_score);
    let frame = 0;
    const startedAt = performance.now();

    const tick = (now: number) => {
      const elapsed = Math.min(1, (now - startedAt) / 900);
      const eased = 1 - Math.pow(1 - elapsed, 3);
      setAnimatedScore(target * eased);
      if (elapsed < 1) {
        frame = requestAnimationFrame(tick);
      }
    };

    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [jobResult]);

  const finished = Boolean(jobResult);
  const rounded = finished ? Math.round(animatedScore) : 0;
  const tone = jobResult ? ringTone(jobResult) : null;
  const grade = jobResult ? gradeOf(jobResult.total_score) : null;
  const percent =
    progress.total > 0 ? Math.min(100, (progress.done / progress.total) * 100) : 0;
  const failedProbes = probes.filter(
    (probe) => probe.status === "fail" || probe.status === "error"
  );

  return (
    <div className="stack score-panel">
      <div className="score-block">
        <div
          className={`score-ring${tone ? ` is-${tone}` : ""}`}
          style={{ "--score": String(finished ? rounded : 0) } as CSSProperties}
          role="img"
          aria-label={finished ? `综合得分 ${rounded}` : "尚未评分"}
        >
          <span className="score-ring-value">{finished ? rounded : "—"}</span>
        </div>
        <div className="score-meta">
          {jobResult ? (
            <>
              <span className={`chip ${VERDICT_CHIP[jobResult.verdict]}`}>
                {VERDICT_LABEL[jobResult.verdict]}
              </span>
              <span className={`chip ${RISK[jobResult.risk_level].chip}`}>
                {RISK[jobResult.risk_level].label}
              </span>
              {grade ? (
                <span className={`chip ${grade.chip}`}>总分级：{grade.label}</span>
              ) : null}
              <p className="muted small">
                综合评分 {formatScore(jobResult.total_score)} / 100
              </p>
            </>
          ) : (
            <>
              <span className="chip chip-muted">
                {status === "running" || status === "connecting" ? "计算中" : "未评分"}
              </span>
              <p className="muted small">完成检测后生成结论。</p>
            </>
          )}
        </div>
      </div>

      {!finished ? (
        <div className="progress">
          <div className="progress-head">
            <span className="small">完成进度</span>
            <span className="small muted">
              {progress.done}/{progress.total || "—"}
            </span>
          </div>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${percent}%` }} />
          </div>
          <p className="small muted">
            {status === "idle"
              ? "尚未开始检测。"
              : status === "error"
                ? "检测中断，未生成评分。"
                : "正在并发跑测，评分将随探针完成逐步更新。"}
          </p>
        </div>
      ) : null}

      {jobResult ? (
        <div className="score-details">
          <div className="info-row">
            <span className="info-label">模型身份</span>
            <span className="info-value">
              {jobResult.self_reported_identity || "未上报"}
            </span>
          </div>

          <div className="info-row">
            <span className="info-label">一致性 / 接入</span>
            <span className="info-value">
              <span className={`chip ${CONSISTENCY[jobResult.verdict].chip}`}>
                {CONSISTENCY[jobResult.verdict].label}
              </span>
            </span>
          </div>

          <div className="info-row">
            <span className="info-label">非官方品牌</span>
            <span className="info-value">
              {jobResult.detected_brands.length > 0 ? (
                <span className="chip-list">
                  {jobResult.detected_brands.map((brand) => (
                    <span className="chip chip-danger" key={brand}>
                      {brand}
                    </span>
                  ))}
                </span>
              ) : (
                <span className="muted">未检测到</span>
              )}
            </span>
          </div>

          <div className="info-row">
            <span className="info-label">性能</span>
            <span className="info-value small">
              总耗时 {formatDuration(jobResult.duration_ms)} · 请求{" "}
              {jobResult.performance.request_count} 次 · TTFT{" "}
              {formatDuration(jobResult.performance.ttft_ms)}
            </span>
          </div>

          <div className="info-row">
            <span className="info-label">探针</span>
            <span className="info-value small">
              通过 {jobResult.probes_passed} / {jobResult.probes_total}
              {jobResult.probes_failed > 0
                ? ` · 失败 ${jobResult.probes_failed}`
                : ""}
            </span>
          </div>

          <div className="score-summary">
            <span className="info-label">诊断摘要</span>
            <p>{jobResult.summary || "无额外摘要。"}</p>
          </div>

          {jobResult.run_error ? (
            <p className="form-error">运行错误：{jobResult.run_error}</p>
          ) : null}

          {failedProbes.length > 0 ? (
            <div className="score-summary">
              <span className="info-label">失败探针</span>
              <ul className="diag-list">
                {failedProbes.map((probe) => (
                  <li key={probe.name}>
                    <span className="diag-name">{probe.displayName}</span>
                    <span className="diag-error">
                      {probe.error ? truncate(probe.error, 80) : "无错误详情"}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <Link className="btn btn-primary btn-block" to={`/reports/${jobResult.report_id}`}>
            查看完整报告
          </Link>
        </div>
      ) : null}

      {!jobResult && failedProbes.length > 0 ? (
        <div className="score-summary">
          <span className="info-label">已失败的探针</span>
          <ul className="diag-list">
            {failedProbes.map((probe) => (
              <li key={probe.name}>
                <span className="diag-name">{probe.displayName}</span>
                <span className="diag-error">
                  {probe.error ? truncate(probe.error, 80) : "无错误详情"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {!jobResult && progress.total === 0 && status !== "idle" ? (
        <p className="muted small">正在获取探针清单…</p>
      ) : null}
    </div>
  );
}
