import { useEffect, useMemo, useState } from "react";

import type {
  DeepCheck,
  DeepReportView,
  DeepStatus,
  DetectionEvent,
  DetectionEventType,
  DetectorDescriptor,
  JobFinishedPayload,
  ProbeFinishedPayload,
  ProbeStatus,
} from "../api";
import { parseDetectionEvent } from "../api";

export type StreamStatus = "idle" | "connecting" | "running" | "finished" | "error";

/** 深度诊断在控制台侧的阶段。 */
export type DeepStatusView = "idle" | "running" | "done" | "error";

/** 列表项带 pending 标记：占位项（尚未回填结论）用于计算进度。 */
export interface DeepCheckState extends DeepCheck {
  pending: boolean;
}

export interface DeepState {
  /** 是否应展示深度诊断 UI；老后端不会置为 true。 */
  active: boolean;
  status: DeepStatusView;
  total: number;
  checks: DeepCheckState[];
  report: DeepReportView | null;
  error: string | null;
}

export interface ProbeState {
  name: string;
  displayName: string;
  passive: boolean;
  weight: number;
  status: ProbeStatus;
  score: number | null;
  durationMs: number | null;
  error: string | null;
  skipReason: string | null;
}

export interface DetectionProgress {
  done: number;
  total: number;
  passed: number;
  failed: number;
}

export interface UseDetectionStreamResult {
  status: StreamStatus;
  events: DetectionEvent[];
  probes: ProbeState[];
  jobResult: JobFinishedPayload | null;
  jobError: string | null;
  progress: DetectionProgress;
  reconnecting: boolean;
  deep: DeepState;
}

interface StreamState {
  status: StreamStatus;
  events: DetectionEvent[];
  probes: ProbeState[];
  total: number;
  jobResult: JobFinishedPayload | null;
  jobError: string | null;
  reconnecting: boolean;
  deep: DeepState;
}

const LOG_LIMIT = 200;

const SSE_EVENT_TYPES: DetectionEventType[] = [
  "queued",
  "run_started",
  "probe_started",
  "probe_finished",
  "job_finished",
  "job_error",
  "deep_started",
  "deep_progress",
  "deep_finished",
];

const EMPTY_DEEP: DeepState = {
  active: false,
  status: "idle",
  total: 0,
  checks: [],
  report: null,
  error: null,
};

const EMPTY_STATE: StreamState = {
  status: "idle",
  events: [],
  probes: [],
  total: 0,
  jobResult: null,
  jobError: null,
  reconnecting: false,
  deep: EMPTY_DEEP,
};

function probeFromDescriptor(detector: DetectorDescriptor): ProbeState {
  return {
    name: detector.name,
    displayName: detector.display_name,
    passive: Boolean(detector.passive),
    weight: detector.weight,
    status: "pending",
    score: null,
    durationMs: null,
    error: null,
    skipReason: null,
  };
}

function upsertProbe(
  probes: ProbeState[],
  name: string,
  patch: Partial<ProbeState> & Pick<ProbeState, "displayName">
): ProbeState[] {
  const index = probes.findIndex((probe) => probe.name === name);
  if (index === -1) {
    return [
      ...probes,
      {
        name,
        passive: false,
        weight: 1,
        status: "pending",
        score: null,
        durationMs: null,
        error: null,
        skipReason: null,
        ...patch,
      },
    ];
  }
  return probes.map((probe, current) =>
    current === index ? { ...probe, ...patch } : probe
  );
}

function probePatchFromFinished(
  payload: ProbeFinishedPayload
): Partial<ProbeState> & Pick<ProbeState, "displayName"> {
  return {
    displayName: payload.display_name || payload.name,
    passive: Boolean(payload.passive),
    weight: typeof payload.weight === "number" ? payload.weight : 1,
    status: payload.status,
    score: typeof payload.score === "number" ? payload.score : null,
    durationMs:
      typeof payload.duration_ms === "number" ? payload.duration_ms : null,
    error: payload.error ?? null,
    skipReason: payload.skip_reason ?? null,
  };
}

function upsertDeepCheck(
  checks: DeepCheckState[],
  next: DeepCheckState
): DeepCheckState[] {
  const index = checks.findIndex((check) => check.id === next.id);
  if (index === -1) return [...checks, next];
  return checks.map((check, current) =>
    current === index ? { ...check, ...next } : check
  );
}

function deepCheckFromProgress(
  payload: DeepCheck
): DeepCheckState {
  return {
    id: String(payload.id),
    title: payload.title || String(payload.id),
    status: payload.status,
    value: payload.value ?? "",
    summary: payload.summary ?? "",
    details: payload.details,
    pending: false,
  };
}

function reduceEvent(state: StreamState, event: DetectionEvent): StreamState {
  const events = [...state.events, event].slice(-LOG_LIMIT);

  switch (event.type) {
    case "queued":
      return {
        ...state,
        events,
        status: state.status === "finished" || state.status === "error" ? state.status : "running",
      };

    case "run_started": {
      const detectors = Array.isArray(event.payload.detectors) ? event.payload.detectors : [];
      const probes = detectors.map(probeFromDescriptor);
      const total =
        typeof event.payload.total === "number" ? event.payload.total : probes.length;
      return { ...state, events, probes, total, status: "running" };
    }

    case "probe_started":
      return {
        ...state,
        events,
        status: "running",
        probes: upsertProbe(state.probes, event.payload.name, {
          displayName: event.payload.display_name || event.payload.name,
          passive: Boolean(event.payload.passive),
          status: "running",
        }),
      };

    case "probe_finished":
      return {
        ...state,
        events,
        status: "running",
        probes: upsertProbe(
          state.probes,
          event.payload.name,
          probePatchFromFinished(event.payload)
        ),
      };

    case "job_finished": {
      // 开启深度诊断时主检测只是第一阶段：连接保持、状态停留在运行中。
      const deepEnabled = event.payload.deep_enabled === true;
      return {
        ...state,
        events,
        status: deepEnabled ? "running" : "finished",
        reconnecting: false,
        jobResult: event.payload,
        deep: deepEnabled
          ? { ...state.deep, active: true, status: "running" }
          : state.deep,
      };
    }

    case "job_error": {
      const message = event.payload.error || "检测任务异常结束。";
      return {
        ...state,
        events,
        status: "error",
        reconnecting: false,
        jobError: message,
        deep: state.deep.active
          ? { ...state.deep, status: "error", error: state.deep.error ?? message }
          : state.deep,
      };
    }

    case "deep_started": {
      const list = Array.isArray(event.payload.checks) ? event.payload.checks : [];
      const checks: DeepCheckState[] = list.map((item) => ({
        id: String(item.id),
        title: item.title || String(item.id),
        status: "unknown",
        value: "等待中",
        summary: "",
        pending: true,
      }));
      return {
        ...state,
        events,
        status: "running",
        deep: {
          active: true,
          status: "running",
          total:
            typeof event.payload.total === "number" && event.payload.total > 0
              ? event.payload.total
              : checks.length,
          checks,
          report: null,
          error: null,
        },
      };
    }

    case "deep_progress":
      return {
        ...state,
        events,
        status: "running",
        deep: {
          ...state.deep,
          active: true,
          status: "running",
          checks: upsertDeepCheck(
            state.deep.checks,
            deepCheckFromProgress(event.payload)
          ),
        },
      };

    case "deep_finished": {
      const deepStatus: DeepStatus =
        event.payload.status === "error" ? "error" : "done";
      const incoming = Array.isArray(event.payload.checks)
        ? event.payload.checks
        : [];
      const checks: DeepCheckState[] =
        incoming.length > 0
          ? incoming.map((check) => ({ ...check, pending: false }))
          : state.deep.checks.map((check) => ({ ...check, pending: false }));
      return {
        ...state,
        events,
        status: "finished",
        reconnecting: false,
        deep: {
          active: true,
          status: deepStatus,
          total: checks.length > 0 ? checks.length : state.deep.total,
          checks,
          report: {
            status: deepStatus,
            error: event.payload.error ?? null,
            duration_ms:
              typeof event.payload.duration_ms === "number"
                ? event.payload.duration_ms
                : 0,
            metrics: event.payload.metrics ?? null,
          },
          error: event.payload.error ?? null,
        },
      };
    }

    default:
      return { ...state, events };
  }
}

/**
 * 订阅一次检测任务的事件流。
 * 口径：total 取 run_started.total；done 统计所有 probe_finished（含 skip），
 * 这样结束时 done 能到 total，右栏显示 N/N。
 */
export function useDetectionStream(jobId: string | null): UseDetectionStreamResult {
  const [state, setState] = useState<StreamState>(EMPTY_STATE);

  useEffect(() => {
    if (!jobId) {
      setState(EMPTY_STATE);
      return;
    }

    setState({ ...EMPTY_STATE, status: "connecting" });

    let lastSeq = 0;
    let disposed = false;
    const source = new EventSource(
      `/app/api/detect/${encodeURIComponent(jobId)}/events`
    );

    const handleEvent = (type: DetectionEventType) => (message: Event) => {
      if (disposed) return;
      const raw = (message as MessageEvent<string>).data;
      const event = parseDetectionEvent(raw, lastSeq + 1);
      if (!event || event.type !== type) return;
      if (event.seq <= lastSeq) return;
      lastSeq = event.seq;

      setState((prev) => reduceEvent(prev, event));

      // 开启深度诊断时，job_finished 之后还有一段 deep 事件流，先不关闭连接。
      if (event.type === "job_finished" && event.payload.deep_enabled !== true) {
        disposed = true;
        source.close();
      } else if (event.type === "deep_finished" || event.type === "job_error") {
        disposed = true;
        source.close();
      }
    };

    const listeners = SSE_EVENT_TYPES.map((type) => {
      const listener = handleEvent(type) as unknown as EventListener;
      source.addEventListener(type, listener);
      return [type, listener] as const;
    });

    source.onopen = () => {
      if (disposed) return;
      setState((prev) => ({
        ...prev,
        status:
          prev.status === "finished" || prev.status === "error" ? prev.status : "running",
        reconnecting: false,
      }));
    };

    source.onerror = () => {
      if (disposed) return;
      // EventSource 会自动重连；未收到终态前保持运行态，只提示正在重连。
      setState((prev) => {
        if (prev.status === "finished" || prev.status === "error") return prev;
        return {
          ...prev,
          status: prev.events.length > 0 ? "running" : "connecting",
          reconnecting: true,
        };
      });
    };

    return () => {
      disposed = true;
      listeners.forEach(([type, listener]) => {
        source.removeEventListener(type, listener);
      });
      source.close();
    };
  }, [jobId]);

  const progress = useMemo<DetectionProgress>(() => {
    let done = 0;
    let passed = 0;
    let failed = 0;
    for (const probe of state.probes) {
      if (probe.status === "pass") {
        done += 1;
        passed += 1;
      } else if (probe.status === "fail" || probe.status === "error") {
        done += 1;
        failed += 1;
      } else if (probe.status === "skip") {
        done += 1;
      }
    }
    return {
      done,
      total: state.total > 0 ? state.total : state.probes.length,
      passed,
      failed,
    };
  }, [state.probes, state.total]);

  return {
    status: state.status,
    events: state.events,
    probes: state.probes,
    jobResult: state.jobResult,
    jobError: state.jobError,
    progress,
    reconnecting: state.reconnecting,
    deep: state.deep,
  };
}
