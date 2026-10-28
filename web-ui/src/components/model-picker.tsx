import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ModelsResponse, Protocol } from "../api";
import { fetchCredentialModels } from "../api";

const PROTOCOL_ORDER: Protocol[] = ["anthropic", "openai", "gemini"];

const PROTOCOL_LABELS: Record<Protocol, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  gemini: "Gemini",
};

const MANUAL_SUGGESTIONS = [
  "claude-sonnet-4-6",
  "claude-haiku-4-5",
  "claude-opus-4-7",
  "gpt-4o-mini",
  "gpt-4o",
  "o3-mini",
  "gemini-2.5-pro",
  "gemini-2.5-flash",
];

const MAX_RENDERED_MODELS = 200;

/** 模型 ID → 协议推测（/models 不可用时用于给手动输入的模型一个提示）。 */
export function guessProtocol(model: string): Protocol {
  const value = model.trim().toLowerCase();
  if (value.startsWith("gemini")) return "gemini";
  if (
    value.startsWith("gpt-") ||
    value.startsWith("o1") ||
    value.startsWith("o3") ||
    value.startsWith("o4")
  ) {
    return "openai";
  }
  return "anthropic";
}

function bestOf(data: ModelsResponse, protocol: Protocol): string | null {
  const list = data.by_protocol[protocol] ?? [];
  const best = data.best_by_protocol[protocol] ?? null;
  return best && list.includes(best) ? best : null;
}

export interface ModelPickerProps {
  credentialId: string;
  selectedModel: string;
  onSelectModel: (model: string) => void;
  disabled?: boolean;
}

export function ModelPicker({
  credentialId,
  selectedModel,
  onSelectModel,
  disabled = false,
}: ModelPickerProps) {
  const [models, setModels] = useState<ModelsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeProtocol, setActiveProtocol] = useState<Protocol>("anthropic");
  const [query, setQuery] = useState("");
  const [manual, setManual] = useState(false);
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    requestRef.current += 1;
    const seq = requestRef.current;
    setLoading(true);
    setError(null);
    setModels(null);
    try {
      const data = await fetchCredentialModels(credentialId);
      if (requestRef.current !== seq) return;
      setModels(data);

      if (data.models_endpoint_supported === false) {
        // 中转站不暴露 /models：直接退回手动输入，仍可以正常发起检测。
        setManual(true);
        return;
      }

      const preferred = PROTOCOL_ORDER.find(
        (protocol) => Boolean(data.best_by_protocol[protocol])
      );
      const fallback = PROTOCOL_ORDER.find(
        (protocol) => (data.by_protocol[protocol] ?? []).length > 0
      );
      const protocol = preferred ?? fallback ?? "anthropic";
      setActiveProtocol(protocol);
      onSelectModel(bestOf(data, protocol) ?? "");
    } catch (cause) {
      if (requestRef.current !== seq) return;
      setError(cause instanceof Error ? cause.message : "获取模型列表失败。");
    } finally {
      if (requestRef.current === seq) setLoading(false);
    }
  }, [credentialId, onSelectModel]);

  useEffect(() => {
    setManual(false);
    setQuery("");
    setActiveProtocol("anthropic");
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    const list = models?.by_protocol[activeProtocol] ?? [];
    const keyword = query.trim().toLowerCase();
    if (!keyword) return list;
    return list.filter((item) => item.toLowerCase().includes(keyword));
  }, [models, activeProtocol, query]);

  const shown = filtered.slice(0, MAX_RENDERED_MODELS);
  const best = models ? bestOf(models, activeProtocol) : null;
  const endpointUnsupported = models !== null && models.models_endpoint_supported === false;
  const probeFailed = models !== null && !models.ok;

  const handleProtocolChange = (protocol: Protocol) => {
    setActiveProtocol(protocol);
    setQuery("");
    if (!models || !selectedModel) return;
    const list = models.by_protocol[protocol] ?? [];
    if (list.includes(selectedModel)) return;
    onSelectModel(bestOf(models, protocol) ?? "");
  };

  const handleManualToggle = (next: boolean) => {
    setManual(next);
    setQuery("");
    if (!next && models) {
      const list = models.by_protocol[activeProtocol] ?? [];
      if (selectedModel && !list.includes(selectedModel)) {
        onSelectModel(bestOf(models, activeProtocol) ?? "");
      }
    }
  };

  const manualHint = selectedModel.trim()
    ? `按模型名推测协议：${PROTOCOL_LABELS[guessProtocol(selectedModel)]}（仅用于提示，检测以实际模型为准）`
    : "不知道模型名时，可以先从常用值里挑一个。";

  return (
    <div className="field model-picker">
      <div className="model-head">
        <span className="field-label">模型</span>
        <button
          type="button"
          className="btn btn-ghost model-refresh"
          disabled={loading || disabled}
          onClick={() => void load()}
        >
          {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {loading ? (
        <div className="model-loading" aria-hidden="true">
          <span className="skeleton-line" />
          <span className="skeleton-line skeleton-line-short" />
          <span className="skeleton-line" />
        </div>
      ) : null}

      {!loading && error ? (
        <p className="form-error" role="alert">
          获取模型列表失败：{error}
        </p>
      ) : null}

      {!loading && models && endpointUnsupported ? (
        <div className="alert alert-warning" role="status">
          <p>该中转站没有暴露 /models 接口，已切换为手动输入模型。</p>
          {models.error ? <p className="model-detail">{models.error}</p> : null}
          {models.note ? <p className="model-detail">{models.note}</p> : null}
        </div>
      ) : null}

      {!loading && models && !endpointUnsupported && probeFailed ? (
        <div className="alert alert-warning" role="status">
          <p>
            模型列表探测未成功
            {models.status ? `（HTTP ${models.status}）` : ""}
            ，可刷新重试或手动输入模型。
          </p>
          {models.error ? <p className="model-detail">{models.error}</p> : null}
          {models.note ? <p className="model-detail">{models.note}</p> : null}
        </div>
      ) : null}

      <label className="check-row">
        <input
          type="checkbox"
          checked={manual}
          disabled={disabled}
          onChange={(event) => handleManualToggle(event.target.checked)}
        />
        <span>手动输入模型</span>
      </label>

      {manual ? (
        <>
          <input
            className="input"
            type="text"
            value={selectedModel}
            placeholder="例如 claude-sonnet-4-6"
            list="manual-model-suggestions"
            autoComplete="off"
            spellCheck={false}
            disabled={disabled}
            onChange={(event) => onSelectModel(event.target.value)}
          />
          <datalist id="manual-model-suggestions">
            {MANUAL_SUGGESTIONS.map((item) => (
              <option key={item} value={item} />
            ))}
          </datalist>
          <span className="field-hint">{manualHint}</span>
        </>
      ) : null}

      {!manual && models && !endpointUnsupported ? (
        <>
          <div className="model-tabs" role="tablist" aria-label="按协议筛选模型">
            {PROTOCOL_ORDER.map((protocol) => {
              const count = models.by_protocol[protocol]?.length ?? 0;
              const active = protocol === activeProtocol;
              return (
                <button
                  key={protocol}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  className={`model-tab${active ? " is-active" : ""}`}
                  onClick={() => handleProtocolChange(protocol)}
                >
                  {PROTOCOL_LABELS[protocol]}
                  <span className="model-tab-count">{count}</span>
                </button>
              );
            })}
          </div>

          <input
            className="input"
            type="search"
            aria-label="搜索模型"
            value={query}
            placeholder="搜索模型（不区分大小写）"
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setQuery(event.target.value)}
          />

          {shown.length > 0 ? (
            <div className="model-list" role="radiogroup" aria-label="选择模型">
              {shown.map((item) => (
                <label
                  key={item}
                  className={`model-option${item === selectedModel ? " is-selected" : ""}`}
                >
                  <input
                    type="radio"
                    name="credential-model"
                    value={item}
                    checked={item === selectedModel}
                    disabled={disabled}
                    onChange={() => onSelectModel(item)}
                  />
                  <span className="model-name">{item}</span>
                  {item === best ? <span className="chip chip-primary">建议</span> : null}
                </label>
              ))}
            </div>
          ) : (
            <div className="model-empty">
              {filtered.length === 0
                ? query.trim()
                  ? "没有匹配的模型，换个关键词或手动输入。"
                  : "该协议下暂无模型，可切换 tab 或手动输入。"
                : "暂无模型。"}
            </div>
          )}

          {filtered.length > shown.length ? (
            <span className="field-hint">共 {filtered.length} 个，仅显示前 {MAX_RENDERED_MODELS} 个。</span>
          ) : null}
        </>
      ) : null}

      {selectedModel.trim() ? (
        <div className="model-selected">
          已选：<code>{selectedModel.trim()}</code>
        </div>
      ) : null}
    </div>
  );
}
