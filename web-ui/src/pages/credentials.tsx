import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import type { Credential } from "../api";
import { deleteCredential, listCredentials, updateCredential } from "../api";
import { CredentialForm } from "../components/credential-form";
import { formatTimestamp } from "../components/report-ui";

const NAME_MAX = 64;
const COLUMN_COUNT = 6;

interface Notice {
  kind: "success" | "error";
  text: string;
}

function credentialNotFound(message: string): boolean {
  return /credential not found/i.test(message);
}

function duplicateName(message: string): boolean {
  return /already exists/i.test(message);
}

export default function CredentialsPage() {
  const [items, setItems] = useState<Credential[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [renameSaving, setRenameSaving] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);

  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await listCredentials();
      setItems(data.items);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "加载凭据列表失败。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 操作提示条自动消失，避免一直占位。
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const handleSaved = (created: Credential) => {
    setItems((prev) => [created, ...prev.filter((item) => item.id !== created.id)]);
    setShowForm(false);
    setEditingId(null);
    setNotice({ kind: "success", text: `已创建凭据「${created.name}」。` });
  };

  const startRename = (item: Credential) => {
    setEditingId(item.id);
    setEditingName(item.name);
    setRenameError(null);
    setConfirmingId(null);
  };

  const cancelRename = () => {
    setEditingId(null);
    setEditingName("");
    setRenameError(null);
  };

  const saveRename = async (item: Credential) => {
    if (renameSaving) return;
    const name = editingName.trim();
    if (!name) {
      setRenameError("名称不能为空。");
      return;
    }
    if (name.length > NAME_MAX) {
      setRenameError(`名称最多 ${NAME_MAX} 个字符。`);
      return;
    }
    if (name === item.name) {
      cancelRename();
      return;
    }

    setRenameSaving(true);
    setRenameError(null);
    try {
      const updated = await updateCredential(item.id, { name });
      setItems((prev) => prev.map((row) => (row.id === updated.id ? updated : row)));
      cancelRename();
      setNotice({ kind: "success", text: `已重命名为「${updated.name}」。` });
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "";
      if (duplicateName(message)) {
        setRenameError("名称已存在");
      } else if (credentialNotFound(message)) {
        setRenameError("凭据不存在，可能已被删除。");
        void load();
      } else {
        setRenameError(message || "重命名失败。");
      }
    } finally {
      setRenameSaving(false);
    }
  };

  const confirmDelete = async (item: Credential) => {
    if (deletingId) return;
    setDeletingId(item.id);
    try {
      await deleteCredential(item.id);
      setItems((prev) => prev.filter((row) => row.id !== item.id));
      setConfirmingId(null);
      setNotice({ kind: "success", text: `已删除凭据「${item.name}」，历史报告不受影响。` });
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "";
      if (credentialNotFound(message)) {
        setItems((prev) => prev.filter((row) => row.id !== item.id));
        setConfirmingId(null);
        setNotice({ kind: "error", text: "凭据已不存在，可能已被其他页面删除。" });
      } else {
        setNotice({ kind: "error", text: message || "删除凭据失败。" });
      }
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="page-title">凭据管理</h1>
          <p className="page-subtitle">
            API Key 加密保存，仅显示掩码；删除凭据不会影响历史报告。
          </p>
        </div>
        <button
          type="button"
          className="btn btn-primary"
          aria-expanded={showForm}
          onClick={() => setShowForm((value) => !value)}
        >
          {showForm ? "收起" : "新建凭据"}
        </button>
      </div>

      {notice ? (
        <div
          className={
            notice.kind === "success"
              ? "alert credentials-notice is-success"
              : "alert alert-danger credentials-notice"
          }
          role={notice.kind === "error" ? "alert" : "status"}
        >
          {notice.text}
        </div>
      ) : null}

      {showForm ? (
        <section className="panel credentials-form-panel">
          <h2 className="panel-title">新建凭据</h2>
          <p className="panel-hint">保存后只保留掩码，明文不会回显。</p>
          <CredentialForm onSaved={handleSaved} onCancel={() => setShowForm(false)} />
        </section>
      ) : null}

      <section className="panel credentials-list-panel">
        {error ? (
          <div className="alert alert-danger credentials-error" role="alert">
            <p>加载凭据列表失败：{error}</p>
            <button type="button" className="btn btn-ghost" onClick={() => void load()}>
              重试
            </button>
          </div>
        ) : null}

        {!error && loading ? (
          <div className="reports-table-wrap">
            <table className="reports-table credentials-table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>中转站</th>
                  <th>API Key</th>
                  <th>最近使用</th>
                  <th>更新时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {Array.from({ length: 3 }, (_, index) => (
                  <tr key={`skeleton-${index}`} className="reports-skeleton-row">
                    <td colSpan={COLUMN_COUNT}>
                      <span className="reports-skeleton" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {!error && !loading && items.length === 0 ? (
          <div className="empty-state reports-empty">
            <p className="page-subtitle">
              还没有凭据，去控制台新建或点右上按钮。
            </p>
            <div className="reports-empty-actions">
              <Link className="btn btn-ghost" to="/">
                去控制台
              </Link>
              <button type="button" className="btn btn-primary" onClick={() => setShowForm(true)}>
                新建凭据
              </button>
            </div>
          </div>
        ) : null}

        {!error && !loading && items.length > 0 ? (
          <div className="reports-table-wrap">
            <table className="reports-table credentials-table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>中转站</th>
                  <th>API Key</th>
                  <th>最近使用</th>
                  <th>更新时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const editing = editingId === item.id;
                  const confirming = confirmingId === item.id;
                  const deleting = deletingId === item.id;
                  return (
                    <tr key={item.id} className="credentials-row">
                      <td>
                        {editing ? (
                          <span className="credentials-rename">
                            <input
                              className="input credentials-rename-input"
                              type="text"
                              value={editingName}
                              maxLength={NAME_MAX}
                              autoComplete="off"
                              spellCheck={false}
                              aria-label={`重命名 ${item.name}`}
                              disabled={renameSaving}
                              autoFocus
                              onChange={(event) => setEditingName(event.target.value)}
                              onKeyDown={(event) => {
                                if (event.key === "Enter") {
                                  event.preventDefault();
                                  void saveRename(item);
                                }
                                if (event.key === "Escape") {
                                  event.preventDefault();
                                  cancelRename();
                                }
                              }}
                            />
                            {renameError ? (
                              <span className="credentials-rename-error" role="alert">
                                {renameError}
                              </span>
                            ) : null}
                          </span>
                        ) : (
                          <span className="credentials-name" title={item.name}>
                            {item.name}
                          </span>
                        )}
                      </td>
                      <td>
                        <span className="credentials-host" title={item.base_url}>
                          {item.host || "—"}
                        </span>
                      </td>
                      <td>
                        <code className="credentials-key">{item.api_key_masked || "—"}</code>
                      </td>
                      <td className="credentials-time">
                        {item.last_used_at ? (
                          formatTimestamp(item.last_used_at)
                        ) : (
                          <span className="muted">未使用</span>
                        )}
                      </td>
                      <td className="credentials-time">{formatTimestamp(item.updated_at)}</td>
                      <td>
                        {editing ? (
                          <div className="credentials-actions">
                            <button
                              type="button"
                              className="btn btn-primary credentials-action-btn"
                              disabled={renameSaving}
                              onClick={() => void saveRename(item)}
                            >
                              {renameSaving ? "保存中…" : "保存"}
                            </button>
                            <button
                              type="button"
                              className="btn btn-ghost credentials-action-btn"
                              disabled={renameSaving}
                              onClick={cancelRename}
                            >
                              取消
                            </button>
                          </div>
                        ) : confirming ? (
                          <div className="credentials-actions">
                            <span className="credentials-confirm-text">确认删除？</span>
                            <button
                              type="button"
                              className="btn btn-ghost credentials-action-btn is-danger"
                              disabled={deleting}
                              onClick={() => void confirmDelete(item)}
                            >
                              {deleting ? "删除中…" : "确认"}
                            </button>
                            <button
                              type="button"
                              className="btn btn-ghost credentials-action-btn"
                              disabled={deleting}
                              onClick={() => setConfirmingId(null)}
                            >
                              取消
                            </button>
                          </div>
                        ) : (
                          <div className="credentials-actions">
                            <Link className="btn btn-ghost credentials-action-btn" to="/">
                              去检测
                            </Link>
                            <button
                              type="button"
                              className="btn btn-ghost credentials-action-btn"
                              onClick={() => startRename(item)}
                            >
                              重命名
                            </button>
                            <button
                              type="button"
                              className="btn btn-ghost credentials-action-btn is-danger"
                              onClick={() => {
                                cancelRename();
                                setConfirmingId(item.id);
                              }}
                            >
                              删除
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
