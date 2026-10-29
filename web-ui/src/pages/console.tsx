import { useState } from "react";

import { BatchPanel } from "../components/batch-panel";
import { ConfigForm } from "../components/config-form";
import { DeepPanel } from "../components/deep-panel";
import { EventLog } from "../components/event-log";
import { HistoryPanel } from "../components/history-panel";
import { ProbeGrid } from "../components/probe-grid";
import { ScorePanel } from "../components/score-panel";
import { useDetectionStream } from "../hooks/use-detection-stream";
import type { BatchDetectItem } from "../api";
import { startReportDeep } from "../api";

export default function ConsolePage() {
  const [jobId, setJobId] = useState<string | null>(null);
  // 当前展示内容对应的历史记录 id（深测任务不是记录，需单独记住源记录）。
  const [reportId, setReportId] = useState<string | null>(null);
  const [credentialId, setCredentialId] = useState("");
  const [batchItems, setBatchItems] = useState<BatchDetectItem[] | null>(null);
  const [deepStarting, setDeepStarting] = useState(false);
  const [deepError, setDeepError] = useState<string | null>(null);
  // 中栏视图：探针 / 深度诊断，通过页签切换。
  const [centerTab, setCenterTab] = useState<"probes" | "deep">("probes");
  const {
    status,
    events,
    probes,
    jobResult,
    jobError,
    progress,
    deep,
  } = useDetectionStream(jobId);

  const busy = status === "connecting" || status === "running";

  const handleStart = (nextJobId: string) => {
    // jobId 变化会让 hook 先重置上一轮状态，再订阅新的任务流。
    setDeepError(null);
    setBatchItems(null);
    setReportId(nextJobId);
    setJobId(nextJobId);
  };

  // 批量：每个模型一个任务，默认展示第一个，其余在批量列表里切换。
  const handleStartBatch = (items: BatchDetectItem[]) => {
    if (items.length === 0) return;
    setDeepError(null);
    setBatchItems(items);
    setReportId(items[0].job_id);
    setJobId(items[0].job_id);
  };

  // 切换展示的任务（批量列表内点击）：不影响批量列表本身。
  const handleSelectBatchItem = (nextId: string) => {
    setDeepError(null);
    setReportId(nextId);
    setJobId(nextId);
  };

  // 选中历史记录：记下它的 report id，供“深度诊断”按钮使用。
  const handleSelectHistory = (nextId: string) => {
    setBatchItems(null);
    handleSelectBatchItem(nextId);
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
      setCenterTab("deep");
    } catch (cause: unknown) {
      setDeepError(cause instanceof Error ? cause.message : "深度诊断启动失败");
    } finally {
      setDeepStarting(false);
    }
  };

  return (
    <div className="page">
      <div className="console-layout">
        <aside className="panel console-col-left">
          <h2 className="panel-title">检测配置</h2>
          <p className="panel-hint">选择已保存的凭据与模型，再确认要跑的检测档位。</p>
          <ConfigForm
            busy={busy}
            onStart={handleStart}
            onStartBatch={handleStartBatch}
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
          {batchItems ? (
            <div className="console-right-block">
              <BatchPanel
                key={batchItems[0].job_id}
                items={batchItems}
                activeJobId={jobId}
                onSelect={handleSelectBatchItem}
                onClose={() => setBatchItems(null)}
              />
            </div>
          ) : null}
          {jobError ? (
            <div className="alert alert-danger" role="alert">
              检测失败：{jobError}
            </div>
          ) : null}
          <div className="console-right-block">
            <h2 className="panel-title">结论</h2>
            <p className="panel-hint">综合评分、风险等级与关键证据。</p>
            {reportId && status === "finished" && !busy ? (
              <div className="deep-trigger">
                <button
                  type="button"
                  className="probe-picker-btn probe-picker-btn-primary"
                  disabled={deepStarting}
                  onClick={handleStartDeep}
                >
                  {deepStarting
                    ? "正在启动深度诊断…"
                    : deep.report
                      ? "重新深度诊断"
                      : "深度诊断（13–25 项）"}
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
            <div className="probe-vendor-tabs" role="tablist" aria-label="中栏视图">
              <button
                type="button"
                role="tab"
                aria-selected={centerTab === "probes"}
                className={`probe-vendor-tab${centerTab === "probes" ? " is-active" : ""}`}
                onClick={() => setCenterTab("probes")}
              >
                探针 {progress.done}/{progress.total || "—"}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={centerTab === "deep"}
                className={`probe-vendor-tab${centerTab === "deep" ? " is-active" : ""}`}
                onClick={() => setCenterTab("deep")}
              >
                深度诊断{deep.active ? "" : "（未运行）"}
              </button>
            </div>

            {centerTab === "probes" ? (
              <>
                <p className="panel-hint">
                  探针会并发执行，结果按完成顺序回填；被跳过的探针也会保留在列表中。
                </p>
                <ProbeGrid probes={probes} progress={progress} />
              </>
            ) : deep.active ? (
              <DeepPanel
                checks={deep.checks}
                report={deep.report}
                status={deep.status}
                error={deep.error}
              />
            ) : (
              <p className="panel-hint">
                未运行。检测完成后可在上方“结论”区域手动开启深度诊断（13–25 项，短时采样）。
              </p>
            )}
          </div>

          <div className="console-right-block">
            <h2 className="panel-title">运行日志</h2>
            <EventLog events={events} />
          </div>
        </section>

        <aside className="panel console-col-right">
          {/* 探针选择器由 ConfigForm 通过 portal 渲染进这个挂载点。 */}
          <div className="console-right-block" id="probe-picker-slot" />
        </aside>
      </div>
    </div>
  );
}
