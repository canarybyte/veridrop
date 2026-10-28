import { useState } from "react";
import type { FormEvent } from "react";

import type { Credential } from "../api";
import { createCredential } from "../api";

export interface CredentialFormProps {
  onSaved: (credential: Credential) => void;
  onCancel: () => void;
}

const NAME_MAX = 64;

export function CredentialForm({ onSaved, onCancel }: CredentialFormProps) {
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;

    const trimmedName = name.trim();
    const trimmedBaseUrl = baseUrl.trim();
    const trimmedKey = apiKey.trim();

    if (!trimmedName) {
      setError("请填写凭据名称。");
      return;
    }
    if (trimmedName.length > NAME_MAX) {
      setError(`名称最多 ${NAME_MAX} 个字符。`);
      return;
    }
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

    setError(null);
    setSaving(true);
    try {
      const created = await createCredential({
        name: trimmedName,
        base_url: trimmedBaseUrl,
        api_key: trimmedKey,
      });
      // 明文只在提交时存在一次，成功后立即从组件状态里抹掉。
      setName("");
      setBaseUrl("");
      setApiKey("");
      setShowKey(false);
      onSaved(created);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "保存凭据失败。";
      setError(
        /credential name already exists/i.test(message)
          ? "名称已存在，请换一个名称。"
          : message
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="credential-form" onSubmit={handleSubmit} noValidate>
      <label className="field" htmlFor="credential-name">
        <span className="field-label">名称</span>
        <input
          id="credential-name"
          className="input"
          type="text"
          value={name}
          maxLength={NAME_MAX}
          placeholder="例如 主力中转站"
          autoComplete="off"
          spellCheck={false}
          autoFocus
          onChange={(event) => setName(event.target.value)}
        />
        <span className="field-hint">必填，1–{NAME_MAX} 个字符，用于在控制台里区分凭据。</span>
      </label>

      <label className="field" htmlFor="credential-base-url">
        <span className="field-label">Base URL</span>
        <input
          id="credential-base-url"
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
        <label className="field-label" htmlFor="credential-api-key">
          API Key
        </label>
        <span className="key-field">
          <input
            id="credential-api-key"
            className="input"
            type={showKey ? "text" : "password"}
            value={apiKey}
            placeholder="sk-..."
            autoComplete="new-password"
            spellCheck={false}
            onChange={(event) => setApiKey(event.target.value)}
          />
          <button
            type="button"
            className="btn btn-ghost key-toggle"
            aria-pressed={showKey}
            onClick={() => setShowKey((value) => !value)}
          >
            {showKey ? "隐藏" : "显示"}
          </button>
        </span>
        <span className="field-hint">保存后不可再查看明文，控制台只显示掩码。</span>
      </div>

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="form-actions">
        <button className="btn btn-primary" type="submit" disabled={saving}>
          {saving ? "保存中…" : "保存凭据"}
        </button>
        <button
          className="btn btn-ghost"
          type="button"
          disabled={saving}
          onClick={onCancel}
        >
          取消
        </button>
      </div>
    </form>
  );
}
