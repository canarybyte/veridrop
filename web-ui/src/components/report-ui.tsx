import type {
  DetectionMode,
  JobVerdict,
  ProbeResultStatus,
  ReportStatus,
  RiskLevel,
} from "../api";

export const MODE_LABEL: Record<DetectionMode, string> = {
  quick: "快速",
  standard: "标准",
  full: "完整",
};

export const VERDICT_LABEL: Record<JobVerdict, string> = {
  passed: "通过",
  marginal: "边缘",
  failed: "未通过",
};

const VERDICT_CHIP: Record<JobVerdict, string> = {
  passed: "chip-success",
  marginal: "chip-warning",
  failed: "chip-danger",
};

export const RISK_LABEL: Record<RiskLevel, string> = {
  low: "低风险",
  medium: "中风险",
  high: "高风险",
};

const RISK_CHIP: Record<RiskLevel, string> = {
  low: "chip-success",
  medium: "chip-warning",
  high: "chip-danger",
};

const STATUS_LABEL: Record<ReportStatus, string> = {
  queued: "排队中",
  running: "检测中",
  done: "已完成",
  error: "失败",
};

const STATUS_CHIP: Record<ReportStatus, string> = {
  queued: "chip-muted",
  running: "chip-primary",
  done: "chip-success",
  error: "chip-danger",
};

const PROBE_STATUS_LABEL: Record<ProbeResultStatus, string> = {
  pass: "通过",
  fail: "未通过",
  skip: "跳过",
  error: "执行错误",
};

const PROBE_STATUS_CHIP: Record<ProbeResultStatus, string> = {
  pass: "chip-success",
  fail: "chip-danger",
  skip: "chip-muted",
  error: "chip-danger",
};

/** ISO 8601 字符串按本地时区格式化；无法解析时原样返回。 */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function VerdictChip({ verdict }: { verdict: JobVerdict | null }) {
  if (!verdict) return <span className="muted">—</span>;
  return <span className={`chip ${VERDICT_CHIP[verdict]}`}>{VERDICT_LABEL[verdict]}</span>;
}

export function RiskChip({ risk }: { risk: RiskLevel | null }) {
  if (!risk) return <span className="muted">—</span>;
  return <span className={`chip ${RISK_CHIP[risk]}`}>{RISK_LABEL[risk]}</span>;
}

export function StatusChip({ status }: { status: ReportStatus }) {
  return (
    <span className={`chip ${STATUS_CHIP[status]}`}>
      <span className="chip-dot" aria-hidden="true" />
      {STATUS_LABEL[status]}
    </span>
  );
}

export function ProbeStatusChip({ status }: { status: ProbeResultStatus }) {
  return <span className={`chip ${PROBE_STATUS_CHIP[status]}`}>{PROBE_STATUS_LABEL[status]}</span>;
}
