import { useCallback, useEffect, useState } from "react";

import type { JobVerdict, ReportSummary } from "../api";
import { listReports } from "../api";

const HISTORY_LIMIT = 20;

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

function formatTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export interface HistoryPanelProps {
  /** 当前控制台正在展示的任务 id，用于高亮。 */
  activeJobId: string | null;
  /** 当前选中的凭据；为空时无法按凭据过滤，只能看全部。 */
  credentialId: string;
  /** 选中历史记录：由父组件把 id 交给流式 hook，回放该次检测的全部事件。 */
  onSelect: (jobId: string) => void;
  /** 变化时重新拉取列表（例如一次检测结束后）。 */
  refreshKey: string;
  /** 运行中禁止切换历史，避免两个任务的事件流互相覆盖。 */
  disabled: boolean;
}

export function HistoryPanel({
  activeJobId,
  credentialId,
  onSelect,
  refreshKey,
  disabled,
}: HistoryPanelProps) {
  const [scope, setScope] = useState<"current" | "all">("current");
  const [items, setItems] = useState<ReportSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 「当前凭据」范围下必须先选凭据，未选时不展示任何记录（不回退到全部）。
  const needsCredential = scope === "current" && !credentialId;
  const credentialFilter = scope === "current" ? credentialId : undefined;

  const load = useCallback(() => {
    if (needsCredential) {
      setItems([]);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    listReports({ limit: HISTORY_LIMIT, credential_id: credentialFilter })
      .then((data) => setItems(data.items))
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : "未知错误");
      })
      .finally(() => setLoading(false));
  }, [needsCredential, credentialFilter]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  return (
    <div className="history-panel">
      <div className="panel-head">
        <h2 className="panel-title">历史记录</h2>
        <button
          type="button"
          className="probe-picker-btn"
          onClick={load}
          disabled={loading}
        >
          刷新
        </button>
      </div>
      <div className="probe-vendor-tabs" role="tablist" aria-label="历史范围">
        <button
          type="button"
          role="tab"
          aria-selected={scope === "current"}
          className={`probe-vendor-tab${scope === "current" ? " is-active" : ""}`}
          onClick={() => setScope("current")}
        >
          当前凭据
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={scope === "all"}
          className={`probe-vendor-tab${scope === "all" ? " is-active" : ""}`}
          onClick={() => setScope("all")}
        >
          全部凭据
        </button>
      </div>
      <p className="panel-hint">
        {needsCredential
          ? "请先在上方选择凭据，再查看它的历史记录。"
          : scope === "current"
            ? "只显示当前凭据的检测记录，点击一条即可加载到控制台。"
            : "显示全部凭据的检测记录，点击一条即可加载到控制台。"}
      </p>

      {error ? (
        <div className="alert alert-danger" role="alert">
          历史记录加载失败：{error}
        </div>
      ) : null}

      {!error && !loading && items.length === 0 && !needsCredential ? (
        <p className="field-hint">还没有历史检测记录。</p>
      ) : null}

      <ul className="history-list">
        {items.map((item) => {
          const active = item.id === activeJobId;
          return (
            <li key={item.id}>
              <button
                type="button"
                className={`history-item${active ? " is-active" : ""}`}
                disabled={disabled}
                aria-current={active ? "true" : undefined}
                onClick={() => onSelect(item.id)}
              >
                <span className="history-item-top">
                  <span className="history-item-model">{item.target_model}</span>
                  <span className="history-item-score">
                    {item.total_score === null ? "—" : Math.round(item.total_score)}
                  </span>
                </span>
                <span className="history-item-meta">
                  <span>{item.credential_name ?? item.host}</span>
                  <span>{formatTime(item.created_at)}</span>
                </span>
                <span className="history-item-meta">
                  <span>{`探针 ${item.passed_probes}/${item.total_probes}`}</span>
                  {item.verdict ? (
                    <span className={`chip ${VERDICT_CHIP[item.verdict]}`}>
                      {VERDICT_LABEL[item.verdict]}
                    </span>
                  ) : (
                    <span className="chip chip-muted">{item.status}</span>
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
