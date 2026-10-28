import type { ProbeStatus } from "../api";
import type {
  DetectionProgress,
  ProbeState,
} from "../hooks/use-detection-stream";
import { formatDuration, formatScore } from "../lib/format";

interface ProbeGridProps {
  probes: ProbeState[];
  progress: DetectionProgress;
}

const STATUS_META: Record<ProbeStatus, { label: string; chip: string }> = {
  pending: { label: "待运行", chip: "chip-muted" },
  running: { label: "运行中", chip: "chip-primary" },
  pass: { label: "通过", chip: "chip-success" },
  fail: { label: "未通过", chip: "chip-danger" },
  error: { label: "执行错误", chip: "chip-danger" },
  skip: { label: "已跳过", chip: "chip-muted" },
};

const CARD_CLASS: Record<ProbeStatus, string> = {
  pending: "",
  running: "is-running",
  pass: "is-pass",
  fail: "is-fail",
  error: "is-fail",
  skip: "is-skip",
};

function ProbeCard({ probe }: { probe: ProbeState }) {
  const meta = STATUS_META[probe.status];
  const className = ["probe-card", CARD_CLASS[probe.status]]
    .filter(Boolean)
    .join(" ");

  return (
    <article className={className}>
      <div className="probe-card-header">
        <span className="probe-card-title">{probe.displayName}</span>
        <span className={`chip ${meta.chip}`}>
          <span className="chip-dot" aria-hidden="true" />
          {meta.label}
        </span>
      </div>
      <p className="probe-card-desc">
        <span className="probe-card-name">{probe.name}</span>
        {probe.status === "skip" && probe.skipReason
          ? ` · ${probe.skipReason}`
          : ""}
        {probe.status === "running" ? " · 正在等待模型响应…" : ""}
      </p>
      <div className="probe-card-foot">
        <span>得分 {formatScore(probe.score)}</span>
        <span aria-hidden="true">·</span>
        <span>{formatDuration(probe.durationMs)}</span>
        {probe.passive ? <span className="chip chip-muted">被动</span> : null}
      </div>
    </article>
  );
}

export function ProbeGrid({ probes, progress }: ProbeGridProps) {
  const complete =
    progress.total > 0 &&
    progress.done >= progress.total &&
    probes.every((probe) => probe.status !== "pending");

  return (
    <div className="stack">
      {complete ? (
        <div className="probe-banner" role="status">
          <span className="probe-banner-icon" aria-hidden="true">
            ✓
          </span>
          全部探针跑测完成 · 共 {progress.total} 项，通过 {progress.passed} 项
        </div>
      ) : null}

      {probes.length === 0 ? (
        <div className="empty-state">
          <p className="page-subtitle">
            填写左侧配置并点击「开始检测」，探针会逐个出现在这里。
          </p>
        </div>
      ) : (
        <div className="probe-grid">
          {probes.map((probe) => (
            <ProbeCard key={probe.name} probe={probe} />
          ))}
        </div>
      )}
    </div>
  );
}
