import { useEffect, useMemo, useState } from "react";

import type {
  DetectionEvent,
  DetectionEventType,
  DetectorDescriptor,
  JobFinishedPayload,
  ProbeFinishedPayload,
  ProbeStatus,
} from "../api";
import { parseDetectionEvent } from "../api";

export type StreamStatus = "idle" | "connecting" | "running" | "finished" | "error";

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
}

interface StreamState {
  status: StreamStatus;
  events: DetectionEvent[];
  probes: ProbeState[];
  total: number;
  jobResult: JobFinishedPayload | null;
  jobError: string | null;
  reconnecting: boolean;
}

const LOG_LIMIT = 200;

const SSE_EVENT_TYPES: DetectionEventType[] = [
  "queued",
  "run_started",
  "probe_started",
  "probe_finished",
  "job_finished",
  "job_error",
];

const EMPTY_STATE: StreamState = {
  status: "idle",
  events: [],
  probes: [],
  total: 0,
  jobResult: null,
  jobError: null,
  reconnecting: false,
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

    case "job_finished":
      return {
        ...state,
        events,
        status: "finished",
        reconnecting: false,
        jobResult: event.payload,
      };

    case "job_error":
      return {
        ...state,
        events,
        status: "error",
        reconnecting: false,
        jobError: event.payload.error || "检测任务异常结束。",
      };

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

      if (event.type === "job_finished" || event.type === "job_error") {
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
  };
}
