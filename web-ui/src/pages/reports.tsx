import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { Credential, JobVerdict, Protocol, ReportSummary } from "../api";
import { listCredentials, listReports } from "../api";
import {
  RiskChip,
  StatusChip,
  VerdictChip,
  formatTimestamp,
} from "../components/report-ui";

const PAGE_SIZE = 20;

interface Filters {
  protocol: Protocol | "";
  verdict: JobVerdict | "";
  minScore: string;
  model: string;
  credential: string;
}

const EMPTY_FILTERS: Filters = {
  protocol: "",
  verdict: "",
  minScore: "",
  model: "",
  credential: "",
};

const COLUMN_COUNT = 9;

function scoreText(score: number | null): string {
  return score === null ? "—" : score.toFixed(1);
}

function isDatabaseUnavailable(message: string): boolean {
  return message.trim().toLowerCase() === "database unavailable";
}

export default function ReportsPage() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<ReportSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [credentialsFailed, setCredentialsFailed] = useState(false);

  // 凭据下拉只用于筛选，加载失败时不阻塞报告列表。
  useEffect(() => {
    let active = true;
    listCredentials()
      .then((response) => {
        if (!active) return;
        setCredentials(response.items);
        setCredentialsFailed(false);
      })
      .catch(() => {
        if (!active) return;
        setCredentialsFailed(true);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    setItems([]);
    setTotal(0);

    // 输入过滤条件时做短暂防抖，避免每敲一个字符就请求一次。
    const timer = window.setTimeout(() => {
      const rawMinScore = filters.minScore.trim();
      const minScore = rawMinScore === "" ? undefined : Number(rawMinScore);
      const validMinScore =
        typeof minScore === "number" && Number.isFinite(minScore) ? minScore : undefined;

      listReports({
        protocol: filters.protocol || undefined,
        verdict: filters.verdict || undefined,
        model: filters.model.trim() || undefined,
        minScore: validMinScore,
        credential_id: filters.credential || undefined,
        limit: PAGE_SIZE,
        offset,
      })
        .then((response) => {
          if (!active) return;
          setItems(response.items);
          setTotal(response.total);
          setLoading(false);
        })
        .catch((cause: unknown) => {
          if (!active) return;
          setLoading(false);
          setError(cause instanceof Error ? cause.message : "加载报告列表失败。");
        });
    }, 300);

    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [
    filters.protocol,
    filters.verdict,
    filters.minScore,
    filters.model,
    filters.credential,
    offset,
    reloadKey,
  ]);

  const updateFilter = (patch: Partial<Filters>) => {
    setFilters((prev) => ({ ...prev, ...patch }));
    setOffset(0);
  };

  const resetFilters = () => {
    setFilters(EMPTY_FILTERS);
    setOffset(0);
  };

  const hasFilters = Boolean(
    filters.protocol ||
      filters.verdict ||
      filters.minScore.trim() ||
      filters.model.trim() ||
      filters.credential
  );

  const openReport = (id: string) => {
    navigate(`/reports/${id}`);
  };

  const rangeStart = items.length > 0 ? offset + 1 : 0;
  const rangeEnd = offset + items.length;
  const databaseDown = error !== null && isDatabaseUnavailable(error);

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1 className="page-title">历史报告</h1>
          <p className="page-subtitle">查看已完成的检测报告，按协议、凭据、结论与评分筛选。</p>
        </div>
        <Link className="btn btn-ghost" to="/">
          去控制台检测
        </Link>
      </div>

      <section className="panel reports-filter-panel">
        <div className="reports-filters">
          <label className="field">
            <span className="field-label">协议</span>
            <select
              className="input"
              value={filters.protocol}
              onChange={(event) =>
                updateFilter({ protocol: event.target.value as Filters["protocol"] })
              }
            >
              <option value="">全部</option>
              <option value="anthropic">anthropic</option>
              <option value="openai">openai</option>
              <option value="gemini">gemini</option>
            </select>
          </label>

          <label className="field">
            <span className="field-label">凭据</span>
            <select
              className="input"
              aria-label="按凭据筛选"
              value={filters.credential}
              onChange={(event) => updateFilter({ credential: event.target.value })}
            >
              <option value="">全部</option>
              {credentials.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
            {credentialsFailed ? (
              <span className="field-hint">凭据列表加载失败，暂时只能按全部筛选。</span>
            ) : null}
          </label>

          <label className="field">
            <span className="field-label">结论</span>
            <select
              className="input"
              value={filters.verdict}
              onChange={(event) =>
                updateFilter({ verdict: event.target.value as Filters["verdict"] })
              }
            >
              <option value="">全部</option>
              <option value="passed">passed</option>
              <option value="marginal">marginal</option>
              <option value="failed">failed</option>
            </select>
          </label>

          <label className="field">
            <span className="field-label">最低分</span>
            <input
              className="input"
              type="number"
              min={0}
              max={100}
              step={0.1}
              value={filters.minScore}
              placeholder="不限"
              onChange={(event) => updateFilter({ minScore: event.target.value })}
            />
          </label>

          <label className="field">
            <span className="field-label">模型</span>
            <input
              className="input"
              type="text"
              value={filters.model}
              placeholder="如 gpt-4o-mini"
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => updateFilter({ model: event.target.value })}
            />
            <span className="field-hint">模型名（精确匹配）</span>
          </label>
        </div>

        {hasFilters ? (
          <div className="reports-filter-actions">
            <button type="button" className="btn btn-ghost" onClick={resetFilters}>
              清除筛选
            </button>
          </div>
        ) : null}
      </section>

      <section className="panel reports-list-panel">
        {error ? (
          <div className="alert alert-danger reports-error" role="alert">
            <p>
              {databaseDown
                ? "数据库暂不可用，暂时无法读取历史报告，请稍后再试。"
                : `加载报告列表失败：${error}`}
            </p>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setReloadKey((value) => value + 1)}
            >
              重试
            </button>
          </div>
        ) : null}

        {!error && loading ? (
          <div className="reports-table-wrap">
            <table className="reports-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>中转站</th>
                  <th>凭据</th>
                  <th>模型</th>
                  <th>总分</th>
                  <th>结论</th>
                  <th>风险</th>
                  <th>探针</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {Array.from({ length: 5 }, (_, index) => (
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
              {total > 0
                ? "当前页没有数据，可回到第一页查看。"
                : hasFilters
                  ? "没有符合筛选条件的检测记录。"
                  : "还没有检测记录，去控制台跑一次。"}
            </p>
            <div className="reports-empty-actions">
              {total > 0 && offset > 0 ? (
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setOffset(0)}
                >
                  回到第一页
                </button>
              ) : null}
              {hasFilters ? (
                <button type="button" className="btn btn-ghost" onClick={resetFilters}>
                  清除筛选
                </button>
              ) : null}
              <Link className="btn btn-primary" to="/">
                去控制台
              </Link>
            </div>
          </div>
        ) : null}

        {!error && !loading && items.length > 0 ? (
          <div className="reports-table-wrap">
            <table className="reports-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>中转站</th>
                  <th>凭据</th>
                  <th>模型</th>
                  <th>总分</th>
                  <th>结论</th>
                  <th>风险</th>
                  <th>探针</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.id}
                    className="reports-row"
                    role="link"
                    tabIndex={0}
                    aria-label={`查看 ${item.host} ${item.target_model} 的检测报告`}
                    onClick={() => openReport(item.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        openReport(item.id);
                      }
                    }}
                  >
                    <td className="reports-time">{formatTimestamp(item.created_at)}</td>
                    <td>
                      <span className="reports-host" title={item.base_url}>
                        {item.host || "—"}
                      </span>
                    </td>
                    <td>
                      {item.credential_name ? (
                        <span className="reports-credential" title={item.credential_name}>
                          {item.credential_name}
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <span className="reports-model">{item.target_model}</span>
                    </td>
                    <td className="reports-num">{scoreText(item.total_score)}</td>
                    <td>
                      <VerdictChip verdict={item.verdict} />
                    </td>
                    <td>
                      <RiskChip risk={item.risk_level} />
                    </td>
                    <td className="reports-num">
                      {item.passed_probes}/{item.total_probes}
                    </td>
                    <td>
                      <StatusChip status={item.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {!error && total > 0 ? (
          <div className="reports-pagination">
            <span className="reports-range">
              {items.length > 0
                ? `第 ${rangeStart}–${rangeEnd} 条，共 ${total} 条`
                : `共 ${total} 条`}
            </span>
            <div className="reports-pager">
              <button
                type="button"
                className="btn btn-ghost"
                disabled={loading || offset === 0}
                onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
              >
                上一页
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={loading || offset + PAGE_SIZE >= total}
                onClick={() => setOffset((value) => value + PAGE_SIZE)}
              >
                下一页
              </button>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
