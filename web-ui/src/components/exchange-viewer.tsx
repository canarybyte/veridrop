import { useEffect, useMemo, useState } from "react";
import type { MouseEvent } from "react";

import type { Exchange } from "../api";
import { formatTimestamp } from "./report-ui";
import { formatDuration } from "../lib/format";

/** events 可能很多，超过该数量时默认只渲染前若干条。 */
const EVENT_RENDER_LIMIT = 200;

function stringify(value: unknown): string {
  if (value === undefined || value === null) return "null";
  try {
    return JSON.stringify(value, null, 2) ?? "null";
  } catch {
    return String(value);
  }
}

/** 复制按钮：自行管理“已复制/复制失败”的短暂反馈，不向父级冒泡。 */
function CopyButton({ text, label = "复制 JSON" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 1600);
    return () => window.clearTimeout(timer);
  }, [state]);

  const handleCopy = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    try {
      const clipboard = navigator.clipboard;
      if (!clipboard || typeof clipboard.writeText !== "function") {
        setState("failed");
        return;
      }
      clipboard.writeText(text).then(
        () => setState("copied"),
        () => setState("failed")
      );
    } catch {
      setState("failed");
    }
  };

  return (
    <button type="button" className="copy-btn" onClick={handleCopy}>
      {state === "copied" ? "已复制" : state === "failed" ? "复制失败" : label}
    </button>
  );
}

interface JsonBlockProps {
  title: string;
  value: unknown;
  /** 默认展开（响应 body 用）。 */
  defaultOpen?: boolean;
}

/** 可折叠的 JSON 块；value 为 null 时显示“无”占位。 */
function JsonBlock({ title, value, defaultOpen = false }: JsonBlockProps) {
  const hasValue = value !== null && value !== undefined;
  return (
    <details className="exchange-block" open={defaultOpen}>
      <summary className="exchange-block-summary">
        <span className="exchange-block-title">{title}</span>
        {hasValue ? <CopyButton text={stringify(value)} /> : null}
      </summary>
      {hasValue ? (
        <pre className="code json-block">{stringify(value)}</pre>
      ) : (
        <p className="exchange-placeholder">无</p>
      )}
    </details>
  );
}

/** 流式事件块：超过上限时默认只渲染前 200 条并提供“显示全部”。 */
function EventsBlock({ events }: { events: unknown[] | null }) {
  const [expanded, setExpanded] = useState(false);
  const total = Array.isArray(events) ? events.length : 0;
  const visible = Array.isArray(events)
    ? expanded
      ? events
      : events.slice(0, EVENT_RENDER_LIMIT)
    : null;

  return (
    <details className="exchange-block">
      <summary className="exchange-block-summary">
        <span className="exchange-block-title">流式事件（{total} 条）</span>
        {Array.isArray(events) ? <CopyButton text={stringify(events)} /> : null}
      </summary>
      {visible === null ? (
        <p className="exchange-placeholder">无</p>
      ) : (
        <>
          <pre className="code json-block">{stringify(visible)}</pre>
          {!expanded && total > EVENT_RENDER_LIMIT ? (
            <div className="exchange-events-more">
              <span className="muted small">
                已渲染前 {EVENT_RENDER_LIMIT} 条，共 {total} 条。
              </span>
              <button
                type="button"
                className="btn btn-ghost exchange-small-btn"
                onClick={() => setExpanded(true)}
              >
                显示全部
              </button>
            </div>
          ) : null}
        </>
      )}
    </details>
  );
}

function ExchangeCard({ exchange, index }: { exchange: Exchange; index: number }) {
  const showEvents = exchange.stream || exchange.events !== null;
  return (
    <article className="exchange-card">
      <header className="exchange-card-head">
        <div className="exchange-card-meta">
          <span className="exchange-seq">#{exchange.seq || index + 1}</span>
          <span className={exchange.stream ? "chip chip-primary" : "chip chip-muted"}>
            {exchange.stream ? "流式" : "非流式"}
          </span>
          <span className="exchange-meta-item">
            耗时 {formatDuration(exchange.latency_ms)}
          </span>
          {exchange.status_code !== null ? (
            <span className="exchange-meta-item">状态码 {exchange.status_code}</span>
          ) : null}
          <span className="exchange-meta-item">{formatTimestamp(exchange.created_at)}</span>
        </div>
        <CopyButton text={stringify(exchange)} label="复制整条 JSON" />
      </header>
      <div className="exchange-card-body">
        <JsonBlock title="请求 body" value={exchange.request} />
        <JsonBlock title="响应 body" value={exchange.response} defaultOpen />
        {showEvents ? <EventsBlock events={exchange.events} /> : null}
        {exchange.error !== null ? (
          <JsonBlock title="错误" value={exchange.error} />
        ) : null}
      </div>
    </article>
  );
}

export interface ExchangeDrawerProps {
  /** 探针 name（契约字段）。 */
  probeName: string;
  probeDisplayName: string;
  exchanges: Exchange[];
  onClose: () => void;
}

/** 右侧抽屉：按 seq 展示某探针的全部上游请求-响应对。 */
export function ExchangeDrawer({
  probeName,
  probeDisplayName,
  exchanges,
  onClose,
}: ExchangeDrawerProps) {
  const sorted = useMemo(
    () => [...exchanges].sort((a, b) => a.seq - b.seq),
    [exchanges]
  );

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  return (
    <div className="drawer-root">
      <div className="drawer-overlay" onClick={onClose} aria-hidden="true" />
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`探针 ${probeDisplayName} 的输出`}
      >
        <header className="drawer-head">
          <div className="drawer-head-text">
            <h2 className="drawer-title">
              探针：{probeDisplayName}（{probeName}）
            </h2>
            <p className="drawer-subtitle">共 {sorted.length} 条上游请求-响应对</p>
          </div>
          <button
            type="button"
            className="drawer-close"
            onClick={onClose}
            aria-label="关闭"
          >
            ×
          </button>
        </header>
        <div className="drawer-body">
          {sorted.length === 0 ? (
            <p className="muted">暂无输出。</p>
          ) : (
            sorted.map((exchange, index) => (
              <ExchangeCard
                key={`${exchange.id}-${exchange.seq}-${index}`}
                exchange={exchange}
                index={index}
              />
            ))
          )}
        </div>
      </aside>
    </div>
  );
}
