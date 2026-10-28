import { useState } from "react";
import type { FormEvent } from "react";

import type { DetectionMode, Protocol } from "../api";
import { submitDetection } from "../api";

interface ProtocolOption {
  value: Protocol;
  label: string;
  modelHint: string;
  models: string[];
}

const PROTOCOLS: ProtocolOption[] = [
  {
    value: "anthropic",
    label: "Anthropic",
    modelHint: "claude-* 形式，例如 claude-sonnet-4-6",
    models: ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"],
  },
  {
    value: "openai",
    label: "OpenAI",
    modelHint: "gpt-* 形式，例如 gpt-4o-mini",
    models: ["gpt-4o-mini", "gpt-4o", "o3-mini"],
  },
  {
    value: "gemini",
    label: "Gemini",
    modelHint: "gemini-* 形式，例如 gemini-2.5-flash",
    models: ["gemini-2.5-pro", "gemini-2.5-flash"],
  },
];

interface ModeOption {
  value: DetectionMode;
  label: string;
  desc: string;
}

const MODES: ModeOption[] = [
  { value: "quick", label: "快速", desc: "只跑核心探针，几十秒出结论。" },
  { value: "standard", label: "标准", desc: "覆盖主要探针，兼顾耗时与可信度。" },
  { value: "full", label: "完整", desc: "全量探针，含长上下文等重型检测。" },
];

export interface ConfigFormProps {
  busy: boolean;
  onStart: (jobId: string) => void;
}

export function ConfigForm({ busy, onStart }: ConfigFormProps) {
  const [protocol, setProtocol] = useState<Protocol>("anthropic");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [model, setModel] = useState("");
  const [mode, setMode] = useState<DetectionMode>("standard");
  const [includeLongContext, setIncludeLongContext] = useState(false);
  const [includeExtreme, setIncludeExtreme] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const activeProtocol = PROTOCOLS.find((item) => item.value === protocol) ?? PROTOCOLS[0];
  const activeMode = MODES.find((item) => item.value === mode) ?? MODES[1];
  const pending = busy || submitting;

  const handleProtocolChange = (next: Protocol) => {
    setProtocol(next);
    // 切换协议后模型名格式不同，直接清空避免残留不匹配的值。
    setModel("");
    setError(null);
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pending) return;

    const trimmedBaseUrl = baseUrl.trim();
    const trimmedModel = model.trim();
    const trimmedKey = apiKey.trim();

    if (!trimmedBaseUrl) {
      setError("请填写 Base URL。");
      return;
    }
    if (!/^https?:\/\//i.test(trimmedBaseUrl)) {
      setError("Base URL 需要以 http:// 或 https:// 开头。");
      return;
    }
    if (!trimmedKey) {
      setError("请填写 API Key。");
      return;
    }
    if (!trimmedModel) {
      setError(`请填写模型名（${activeProtocol.modelHint}）。`);
      return;
    }

    setError(null);
    setSubmitting(true);
    try {
      const response = await submitDetection({
        protocol,
        base_url: trimmedBaseUrl,
        api_key: trimmedKey,
        model: trimmedModel,
        mode,
        include_long_context: includeLongContext,
        include_long_context_extreme: includeExtreme,
      });
      if (!response.job_id) {
        throw new Error("服务未返回任务 ID。");
      }
      onStart(response.job_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提交检测任务失败。");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="stack" onSubmit={handleSubmit} noValidate>
      <div className="field">
        <span className="field-label">协议</span>
        <div className="seg" role="group" aria-label="选择协议">
          {PROTOCOLS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={`seg-btn${item.value === protocol ? " is-active" : ""}`}
              aria-pressed={item.value === protocol}
              onClick={() => handleProtocolChange(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="field-hint">{activeProtocol.modelHint}</span>
      </div>

      <label className="field">
        <span className="field-label">Base URL</span>
        <input
          className="input"
          type="url"
          value={baseUrl}
          placeholder="https://api.example.com/v1"
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setBaseUrl(event.target.value)}
        />
      </label>

      <div className="field">
        <label className="field-label" htmlFor="detect-api-key">
          API Key
        </label>
        <span className="key-field">
          <input
            id="detect-api-key"
            className="input"
            type={showKey ? "text" : "password"}
            value={apiKey}
            placeholder="sk-..."
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setApiKey(event.target.value)}
          />
          <button
            type="button"
            className="btn btn-ghost key-toggle"
            onClick={() => setShowKey((value) => !value)}
          >
            {showKey ? "隐藏" : "显示"}
          </button>
        </span>
      </div>

      <label className="field">
        <span className="field-label">模型</span>
        <input
          className="input"
          type="text"
          value={model}
          placeholder={activeProtocol.models[0] ?? "model-name"}
          list={`model-options-${protocol}`}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setModel(event.target.value)}
        />
        <datalist id={`model-options-${protocol}`}>
          {activeProtocol.models.map((item) => (
            <option key={item} value={item} />
          ))}
        </datalist>
      </label>

      <div className="field">
        <span className="field-label">检测模式</span>
        <div className="seg" role="group" aria-label="选择检测模式">
          {MODES.map((item) => (
            <button
              key={item.value}
              type="button"
              className={`seg-btn${item.value === mode ? " is-active" : ""}`}
              aria-pressed={item.value === mode}
              onClick={() => setMode(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="field-hint">{activeMode.desc}</span>
      </div>

      <div className="field">
        <span className="field-label">扩展检测</span>
        <label className="check-row">
          <input
            type="checkbox"
            checked={includeLongContext}
            onChange={(event) => {
              setIncludeLongContext(event.target.checked);
              if (!event.target.checked) setIncludeExtreme(false);
            }}
          />
          <span>长上下文探针</span>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={includeExtreme}
            onChange={(event) => {
              setIncludeExtreme(event.target.checked);
              if (event.target.checked) setIncludeLongContext(true);
            }}
          />
          <span>超长上下文（extreme）</span>
        </label>
      </div>

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}

      <button className="btn btn-primary btn-block" type="submit" disabled={pending}>
        {pending ? "检测进行中…" : "开始检测"}
      </button>
    </form>
  );
}
