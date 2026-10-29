import { useEffect, useRef, useState } from "react";

import type {
  DetectionEvent,
  JobVerdict,
  ProbeResultStatus,
} from "../api";
import { formatClock, formatDuration, formatScore } from "../lib/format";

interface EventLogProps {
  events: DetectionEvent[];
}

const PROBE_STATUS_LABEL: Record<ProbeResultStatus, string> = {
  pass: "通过",
  fail: "未通过",
  skip: "已跳过",
  error: "执行错误",
};

const VERDICT_LABEL: Record<JobVerdict, string> = {
  passed: "通过",
  marginal: "边缘",
  failed: "未通过",
};

interface LogLine {
  tone: "muted" | "primary" | "success" | "danger";
  text: string;
  error: string | null;
}

function lineOf(event: DetectionEvent): LogLine {
  switch (event.type) {
    case "queued":
      return {
        tone: "muted",
        text: `任务已入队 · ${event.payload.target_model || event.payload.protocol}`,
        error: null,
      };
    case "run_started":
      return {
        tone: "primary",
        text: `开始运行，共 ${event.payload.total} 个探针`,
        error: null,
      };
    case "probe_started":
      return {
        tone: "primary",
        text: `开始探针「${event.payload.display_name}」`,
        error: null,
      };
    case "probe_finished": {
      const { display_name, status, score, duration_ms, error } = event.payload;
      const tail = [
        PROBE_STATUS_LABEL[status],
        `得分 ${formatScore(score)}`,
        formatDuration(duration_ms),
      ]
        .filter(Boolean)
        .join(" · ");
      return {
        tone:
          status === "pass"
            ? "success"
            : status === "fail" || status === "error"
              ? "danger"
              : "muted",
        text: `探针「${display_name}」${tail}`,
        error,
      };
    }
    case "job_finished":
      return {
        tone: event.payload.verdict === "passed" ? "success" : "danger",
        text: `检测完成：总分 ${formatScore(event.payload.total_score)}，结论 ${
          VERDICT_LABEL[event.payload.verdict]
        }`,
        error: event.payload.run_error,
      };
    case "job_error":
      return {
        tone: "danger",
        text: "检测任务异常结束",
        error: event.payload.error,
      };
    case "deep_started":
      return {
        tone: "primary",
        text: `深度诊断开始，共 ${event.payload.total} 项`,
        error: null,
      };
    case "deep_progress":
      return {
        tone:
          event.payload.status === "ok"
            ? "success"
            : event.payload.status === "error"
              ? "danger"
              : "muted",
        text: `深度诊断「${event.payload.title || event.payload.id}」：${
          event.payload.value || event.payload.status
        }`,
        error: null,
      };
    case "deep_finished":
      return {
        tone: event.payload.status === "error" ? "danger" : "success",
        text: `深度诊断${
          event.payload.status === "error" ? "失败" : "完成"
        } · ${event.payload.checks.length} 项`,
        error: event.payload.error,
      };
  }
}

export function EventLog({ events }: EventLogProps) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<number[]>([]);
  const listRef = useRef<HTMLOListElement | null>(null);

  const visible = events.slice(-200);

  useEffect(() => {
    if (!open) return;
    const node = listRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [open, visible.length]);

  const toggleExpanded = (seq: number) => {
    setExpanded((prev) =>
      prev.includes(seq) ? prev.filter((item) => item !== seq) : [...prev, seq]
    );
  };

  return (
    <section className="log-block">
      <div className="log-head">
        <h3 className="log-title">运行日志</h3>
        <span className="chip chip-muted">{visible.length}</span>
        <button
          type="button"
          className="btn btn-ghost log-toggle"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          {open ? "收起" : "展开"}
        </button>
      </div>

      {open ? (
        visible.length === 0 ? (
          <div className="empty-state log-empty">
            <p className="page-subtitle">还没有事件，提交任务后开始记录。</p>
          </div>
        ) : (
          <ol className="log-list" ref={listRef}>
            {visible.map((event) => {
              const line = lineOf(event);
              const canExpand = Boolean(line.error);
              const isExpanded = expanded.includes(event.seq);
              return (
                <li className={`log-row log-${line.tone}`} key={event.seq}>
                  <span className="log-time">{formatClock(event.ts)}</span>
                  <div className="log-body">
                    <div className="log-text">
                      {line.text}
                      {canExpand ? (
                        <button
                          type="button"
                          className="log-expand"
                          onClick={() => toggleExpanded(event.seq)}
                          aria-expanded={isExpanded}
                        >
                          {isExpanded ? "收起错误" : "查看错误"}
                        </button>
                      ) : null}
                    </div>
                    {canExpand && isExpanded ? (
                      <pre className="log-error">{line.error}</pre>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ol>
        )
      ) : null}
    </section>
  );
}
