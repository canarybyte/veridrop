import { useState } from "react";

import { ConfigForm } from "../components/config-form";
import { DeepPanel } from "../components/deep-panel";
import { EventLog } from "../components/event-log";
import { HistoryPanel } from "../components/history-panel";
import { ProbeGrid } from "../components/probe-grid";
import { ScorePanel } from "../components/score-panel";
import type { StreamStatus } from "../hooks/use-detection-stream";
import { useDetectionStream } from "../hooks/use-detection-stream";
import type { JobFinishedPayload } from "../api";
import { startReportDeep } from "../api";

function headerChip(
  status: StreamStatus,
  reconnecting: boolean,
  jobResult: JobFinishedPayload | null
): { label: string; chip: string } {
  if (reconnecting && (status === "connecting" || status === "running")) {
    return { label: "正在重连…", chip: "chip-warning" };
  }
  switch (status) {
    case "idle":
      return { label: "待开始", chip: "chip-muted" };
    case "connecting":
      return { label: "连接中", chip: "chip-primary" };
    case "running":
      return { label: "检测中", chip: "chip-primary" };
    case "finished":
      if (jobResult?.verdict === "passed") {
        return { label: "检测完成", chip: "chip-success" };
      }
      if (jobResult?.verdict === "marginal") {
        return { label: "存在偏差", chip: "chip-warning" };
      }
      return { label: "检测未通过", chip: "chip-danger" };
    case "error":
      return { label: "检测失败", chip: "chip-danger" };
  }
}

export default function ConsolePage() {
  const [jobId, setJobId] = useState<string | null>(null);
  // 当前展示内容对应的历史记录 id（深测任务不是记录，需单独记住源记录）。
  const [reportId, setReportId] = useState<string | null>(null);
  const [credentialId, setCredentialId] = useState("");
  const [deepStarting, setDeepStarting] = useState(false);
  const [deepError, setDeepError] = useState<string | null>(null);
  const {
    status,
    events,
    probes,
    jobResult,
    jobError,
    progress,
    reconnecting,
    deep,
  } = useDetectionStream(jobId);

  const busy = status === "connecting" || status === "running";
  const badge =
    deep.active && deep.status === "running" && status === "running"
      ? { label: "深度诊断中", chip: "chip-primary" }
      : headerChip(status, reconnecting, jobResult);

  const handleStart = (nextJobId: string) => {
    // jobId 变化会让 hook 先重置上一轮状态，再订阅新的任务流。
    setDeepError(null);
    setReportId(nextJobId);
    setJobId(nextJobId);
  };

  // 选中历史记录：记下它的 report id，供“深度诊断”按钮使用。
  const handleSelectHistory = (nextId: string) => {
    setDeepError(null);
    setReportId(nextId);
    setJobId(nextId);
  };

  // 对当前记录补跑深度诊断：控制台切到新深测任务的事件流，
  // reportId 保持为原记录，便于深测结束后再次判断。
  const handleStartDeep = async () => {
    if (!reportId) return;
    setDeepStarting(true);
    setDeepError(null);
    try {
      const response = await startReportDeep(reportId);
      setJobId(response.job_id);
    } catch (cause: unknown) {
      setDeepError(cause instanceof Error ? cause.message : "深度诊断启动失败");
    } finally {
      setDeepStarting(false);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="page-title">控制台</h1>
          <p className="page-subtitle">
            发起一次中转站检测，实时查看逐项探针进度与最终结论。
          </p>
        </div>
        <span className={`chip ${badge.chip}`}>
          <span className="chip-dot" aria-hidden="true" />
          {badge.label}
        </span>
      </div>

      <div className="console-layout">
        <aside className="panel console-col-left">
          <h2 className="panel-title">检测配置</h2>
          <p className="panel-hint">选择已保存的凭据与模型，再确认要跑的检测档位。</p>
          <ConfigForm
            busy={busy}
            onStart={handleStart}
            onCredentialChange={setCredentialId}
          />
          <HistoryPanel
            activeJobId={jobId}
            credentialId={credentialId}
            onSelect={handleSelectHistory}
            refreshKey={`${jobId ?? ""}:${status}:${credentialId}`}
            disabled={busy}
          />

        </aside>

        <section className="panel console-col-center">
          {jobError ? (
            <div className="alert alert-danger" role="alert">
              检测失败：{jobError}
            </div>
          ) : null}
          <div className="console-right-block">
            <h2 className="panel-title">结论</h2>
            <p className="panel-hint">综合评分、风险等级与关键证据。</p>
            {reportId && status === "finished" && !busy && !deep.report ? (
              <div className="deep-trigger">
                <button
                  type="button"
                  className="probe-picker-btn probe-picker-btn-primary"
                  disabled={deepStarting}
                  onClick={handleStartDeep}
                >
                  {deepStarting ? "正在启动深度诊断…" : "深度诊断（13–25 项）"}
                </button>
                <span className="field-hint">
                  基于这次检测的探针结果，补跑短时采样的深度诊断，结果写回该历史记录。
                </span>
                {deepError ? (
                  <div className="alert alert-danger" role="alert">
                    {deepError}
                  </div>
                ) : null}
              </div>
            ) : null}
            <ScorePanel
              progress={progress}
              probes={probes}
              jobResult={jobResult}
              status={status}
            />
          </div>
          <div className="console-right-block">
            <h2 className="panel-title">运行日志</h2>
            <EventLog events={events} />
          </div>
          <div className="console-right-block">
            <div className="panel-head">
              <h2 className="panel-title">探针</h2>
              <span className={`chip ${busy ? "chip-primary" : "chip-muted"}`}>
                {progress.done}/{progress.total || "—"}
              </span>
            </div>
            <p className="panel-hint">
              探针会并发执行，结果按完成顺序回填；被跳过的探针也会保留在列表中。
            </p>
            <ProbeGrid probes={probes} progress={progress} />
          </div>
        </section>

        <aside className="panel console-col-right">
          {/* 探针选择器由 ConfigForm 通过 portal 渲染进这个挂载点。 */}
          <div className="console-right-block" id="probe-picker-slot" />
        </aside>
      </div>

      {deep.active ? (
        <section className="panel deep-console-panel">
          <DeepPanel
            checks={deep.checks}
            report={deep.report}
            status={deep.status}
            error={deep.error}
          />
        </section>
      ) : null}
    </div>
  );
}
