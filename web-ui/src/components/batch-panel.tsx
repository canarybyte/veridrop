import { useEffect, useState } from "react";

import type { BatchDetectItem, DetectStatus, JobVerdict } from "../api";
import { getDetectStatus } from "../api";

const POLL_MS = 2000;

const STATUS_LABEL: Record<DetectStatus["status"], string> = {
  queued: "排队中",
  running: "运行中",
  done: "完成",
  error: "失败",
};

const VERDICT_LABEL: Record<JobVerdict, string> = {
  passed: "通过",
  marginal: "偏差",
  failed: "未通过",
};

const VERDICT_CHIP: Record<JobVerdict, string> = {
  passed: "chip-success",
  marginal: "chip-warning",
  failed: "chip-danger",
};

function isTerminal(status: DetectStatus | undefined): boolean {
  return status?.status === "done" || status?.status === "error";
}

export interface BatchPanelProps {
  items: BatchDetectItem[];
  /** 当前控制台正在展示的任务 id，用于高亮。 */
  activeJobId: string | null;
  /** 点击某个模型：控制台切换到该任务的事件流（与历史记录行为一致）。 */
  onSelect: (jobId: string) => void;
  onClose: () => void;
}

export function BatchPanel({ items, activeJobId, onSelect, onClose }: BatchPanelProps) {
  const [statuses, setStatuses] = useState<Record<string, DetectStatus>>({});
  const allDone = items.every((item) => isTerminal(statuses[item.job_id]));
  const doneCount = items.filter((item) => isTerminal(statuses[item.job_id])).length;

  // 所有任务结束后停止轮询；轮询间隔内只拉取状态，不占用 SSE 连接。
  useEffect(() => {
    if (allDone) return;
    let cancelled = false;
    const delay = Object.keys(statuses).length === 0 ? 0 : POLL_MS;
    const timer = window.setTimeout(async () => {
      const results = await Promise.all(
        items.map((item) => getDetectStatus(item.job_id).catch(() => null))
      );
      if (cancelled) return;
      setStatuses((prev) => {
        const next = { ...prev };
        for (const result of results) {
          if (result) next[result.job_id] = result;
        }
        return next;
      });
    }, delay);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [items, statuses, allDone]);

  return (
    <div className="batch-panel">
      <div className="panel-head">
        <h2 className="panel-title">批量检测</h2>
        <span className="field-hint">
          {doneCount}/{items.length} 完成
        </span>
        <button type="button" className="probe-picker-btn" onClick={onClose}>
          关闭
        </button>
      </div>
      <p className="panel-hint">每个模型是独立任务，点击一行可查看它的探针进度与结论。</p>

      <ul className="history-list">
        {items.map((item) => {
          const status = statuses[item.job_id];
          const active = item.job_id === activeJobId;
          const state = status?.status ?? "queued";
          return (
            <li key={item.job_id}>
              <button
                type="button"
                className={`history-item${active ? " is-active" : ""}`}
                aria-current={active ? "true" : undefined}
                onClick={() => onSelect(item.job_id)}
              >
                <span className="history-item-top">
                  <span className="history-item-model">{item.model}</span>
                  <span className="history-item-score">
                    {status?.total_score == null ? "—" : Math.round(status.total_score)}
                  </span>
                </span>
                <span className="history-item-meta">
                  <span>{status?.error ? status.error : "\u00a0"}</span>
                  {status?.verdict ? (
                    <span className={`chip ${VERDICT_CHIP[status.verdict]}`}>
                      {VERDICT_LABEL[status.verdict]}
                    </span>
                  ) : (
                    <span className="chip chip-muted">{STATUS_LABEL[state]}</span>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
