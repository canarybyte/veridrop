import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";

import type { Credential, DetectionMode } from "../api";
import { listCredentials, submitDetection } from "../api";
import { CredentialForm } from "./credential-form";
import { ModelPicker, guessProtocol } from "./model-picker";
import { ProbePicker } from "./probe-picker";

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
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [credentialsLoading, setCredentialsLoading] = useState(true);
  const [credentialsError, setCredentialsError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [showCredentialForm, setShowCredentialForm] = useState(false);
  const [model, setModel] = useState("");
  const [mode, setMode] = useState<DetectionMode>("standard");
  const [selectedProbes, setSelectedProbes] = useState<string[] | null>(null);
  const [includeLongContext, setIncludeLongContext] = useState(false);
  const [includeExtreme, setIncludeExtreme] = useState(false);
  const [deepEnabled, setDeepEnabled] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pending = busy || submitting;
  const selectedCredential = credentials.find((item) => item.id === selectedId) ?? null;
  const activeMode = MODES.find((item) => item.value === mode) ?? MODES[1];
  const trimmedModel = model.trim();
  const protocol = guessProtocol(trimmedModel);
  const canStart = Boolean(selectedId) && Boolean(trimmedModel);
  const longContextProbeSelected = selectedProbes?.includes("long_context") ?? false;

  // 探针集合随协议变化，切到其他协议时自定义选择失效，回到“按模式模板”。
  const previousProtocol = useRef(protocol);
  useEffect(() => {
    if (previousProtocol.current === protocol) return;
    previousProtocol.current = protocol;
    setSelectedProbes(null);
  }, [protocol]);

  // 显式选中 long_context 时自动打开长上下文开关；用户仍可手动关闭。
  useEffect(() => {
    if (longContextProbeSelected) setIncludeLongContext(true);
  }, [longContextProbeSelected]);

  const loadCredentials = useCallback(async () => {
    setCredentialsLoading(true);
    setCredentialsError(null);
    try {
      const data = await listCredentials();
      setCredentials(data.items);
      // 选中的凭据被删掉时同步回空态。
      setSelectedId((current) =>
        current && !data.items.some((item) => item.id === current) ? "" : current
      );
    } catch (cause) {
      setCredentialsError(cause instanceof Error ? cause.message : "获取凭据列表失败。");
    } finally {
      setCredentialsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCredentials();
  }, [loadCredentials]);

  const handleCredentialChange = (nextId: string) => {
    setSelectedId(nextId);
    // 凭据变了，旧模型一定不属于新凭据，直接清空，由 ModelPicker 重新拉列表。
    setModel("");
    setError(null);
  };

  const handleCredentialSaved = (created: Credential) => {
    setCredentials((prev) => [created, ...prev.filter((item) => item.id !== created.id)]);
    setSelectedId(created.id);
    setModel("");
    setShowCredentialForm(false);
    setError(null);
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pending) return;

    if (!selectedId) {
      setError("请先选择或新建凭据。");
      return;
    }
    if (!trimmedModel) {
      setError("请选择或手动填写模型。");
      return;
    }
    if (selectedProbes !== null && selectedProbes.length === 0) {
      setError("至少选择一个探针。");
      return;
    }

    setError(null);
    setSubmitting(true);
    try {
      const response = await submitDetection({
        credential_id: selectedId,
        model: trimmedModel,
        mode,
        include_long_context: includeLongContext,
        include_long_context_extreme: includeExtreme,
        deep: deepEnabled,
        probes: selectedProbes,
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
      <div className="field credential-section">
        <div className="panel-head credential-head">
          <span className="field-label">凭据</span>
          <div className="credential-actions">
            <Link className="text-link small" to="/credentials">
              凭据管理
            </Link>
            <button
              type="button"
              className="btn btn-ghost credential-new-btn"
              aria-expanded={showCredentialForm}
              onClick={() => setShowCredentialForm((value) => !value)}
            >
              {showCredentialForm ? "收起" : "新建凭据"}
            </button>
          </div>
        </div>

        {credentialsLoading ? (
          <div className="model-loading" aria-hidden="true">
            <span className="skeleton-line" />
          </div>
        ) : (
          <select
            className="input credential-select"
            aria-label="选择凭据"
            value={selectedId}
            disabled={pending}
            onChange={(event) => handleCredentialChange(event.target.value)}
          >
            <option value="">请选择凭据…</option>
            {credentials.map((item) => (
              <option key={item.id} value={item.id}>
                {`${item.name} · ${item.host} · ${item.api_key_masked}`}
              </option>
            ))}
          </select>
        )}

        {credentialsError ? (
          <div className="alert alert-danger credential-error" role="alert">
            <p>{credentialsError}</p>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void loadCredentials()}
            >
              重试
            </button>
          </div>
        ) : null}

        {!credentialsLoading && !credentialsError && credentials.length === 0 ? (
          <div className="credential-empty">还没有凭据，先新建一个。</div>
        ) : null}

        {showCredentialForm ? (
          <CredentialForm
            onSaved={handleCredentialSaved}
            onCancel={() => setShowCredentialForm(false)}
          />
        ) : null}

        {selectedCredential ? (
          <div className="credential-meta">
            <div className="info-row">
              <span className="info-label">Base URL</span>
              <span className="info-value">{selectedCredential.base_url}</span>
            </div>
            <div className="info-row">
              <span className="info-label">API Key</span>
              <span className="info-value">
                <code>{selectedCredential.api_key_masked}</code>
              </span>
            </div>
            <span className="field-hint">API Key 已加密保存，控制台只显示掩码，无法查看明文。</span>
          </div>
        ) : null}
      </div>

      {selectedId ? (
        <ModelPicker
          key={selectedId}
          credentialId={selectedId}
          selectedModel={model}
          onSelectModel={setModel}
          disabled={pending}
        />
      ) : (
        <div className="field">
          <span className="field-label">模型</span>
          <div className="placeholder-field" aria-hidden="true" />
          <span className="field-hint">选择凭据后会自动拉取可用模型。</span>
        </div>
      )}

      <div className="field">
        <span className="field-label">检测模式</span>
        <div className="seg" role="group" aria-label="选择检测模式">
          {MODES.map((item) => (
            <button
              key={item.value}
              type="button"
              className={`seg-btn${item.value === mode ? " is-active" : ""}`}
              aria-pressed={item.value === mode}
              disabled={pending}
              onClick={() => setMode(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="field-hint">{activeMode.desc}</span>
      </div>

      <ProbePicker
        protocol={protocol}
        model={trimmedModel}
        mode={mode}
        value={selectedProbes}
        onChange={setSelectedProbes}
        disabled={pending}
      />

      <div className="field">
        <span className="field-label">扩展检测</span>
        <label className="check-row">
          <input
            type="checkbox"
            checked={includeLongContext}
            disabled={pending}
            onChange={(event) => {
              setIncludeLongContext(event.target.checked);
              if (!event.target.checked) setIncludeExtreme(false);
            }}
          />
          <span>长上下文探针</span>
        </label>
        {longContextProbeSelected && !includeLongContext ? (
          <span className="field-hint">
            已选中 long_context 探针，关闭此开关后后端仍会按探针选择启用长上下文。
          </span>
        ) : null}
        <label className="check-row">
          <input
            type="checkbox"
            checked={includeExtreme}
            disabled={pending}
            onChange={(event) => {
              setIncludeExtreme(event.target.checked);
              if (event.target.checked) setIncludeLongContext(true);
            }}
          />
          <span>超长上下文（extreme）</span>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={deepEnabled}
            disabled={pending}
            onChange={(event) => setDeepEnabled(event.target.checked)}
          />
          <span>深度诊断（13–25 项）</span>
        </label>
        <span className="field-hint">
          额外约 20+ 次请求、短时采样，默认关闭；结论为启发式，不等同官方计费。
        </span>
      </div>

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}

      <button
        className="btn btn-primary btn-block"
        type="submit"
        disabled={pending || !canStart}
      >
        {pending ? "检测进行中…" : "开始检测"}
      </button>
      {!selectedId ? (
        <span className="field-hint">请先选择或新建凭据，才能开始检测。</span>
      ) : !trimmedModel ? (
        <span className="field-hint">请选择或手动填写模型。</span>
      ) : null}
    </form>
  );
}
