import { useEffect, useMemo, useRef, useState } from "react";

import type {
  DetectionMode,
  ProbeCatalog,
  ProbeCatalogItem,
  Protocol,
} from "../api";
import { getProbeCatalog } from "../api";

const MODE_LABELS: Record<DetectionMode, string> = {
  quick: "快速",
  standard: "标准",
  full: "完整",
};

/** model 变化只会影响 applies，稍作防抖避免连续请求。 */
const FETCH_DEBOUNCE_MS = 300;

const OTHER_GROUP_ID = "__other";

interface ProbeGroup {
  id: string;
  label: string;
  description: string;
  items: ProbeCatalogItem[];
}

function formatWeight(weight: number): string {
  if (!Number.isFinite(weight)) return "—";
  return String(Math.round(weight * 100) / 100);
}

/** 只覆盖部分模式时才展示徽标；覆盖全部模式视为默认可用，不额外占位。 */
function partialModes(modes: DetectionMode[]): string | null {
  if (!Array.isArray(modes) || modes.length === 0 || modes.length >= 3) return null;
  return modes.map((item) => MODE_LABELS[item] ?? item).join(" · ");
}

export interface ProbePickerProps {
  protocol: Protocol;
  model: string;
  mode: DetectionMode;
  /** null 表示跟随模式模板；数组表示用户显式选择。 */
  value: string[] | null;
  onChange: (next: string[]) => void;
  disabled?: boolean;
}

export function ProbePicker({
  protocol,
  model,
  mode,
  value,
  onChange,
  disabled = false,
}: ProbePickerProps) {
  const [catalog, setCatalog] = useState<ProbeCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
  const [expanded, setExpanded] = useState(true);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(
    () => new Set()
  );
  const requestRef = useRef(0);

  useEffect(() => {
    const seq = requestRef.current + 1;
    requestRef.current = seq;
    const timer = window.setTimeout(() => {
      setLoading(true);
      setError(null);
      getProbeCatalog(protocol, model.trim() || undefined)
        .then((data) => {
          if (requestRef.current !== seq) return;
          setCatalog(data);
        })
        .catch((cause: unknown) => {
          if (requestRef.current !== seq) return;
          setCatalog(null);
          setError(cause instanceof Error ? cause.message : "未知错误");
        })
        .finally(() => {
          if (requestRef.current === seq) setLoading(false);
        });
    }, FETCH_DEBOUNCE_MS);

    return () => window.clearTimeout(timer);
  }, [protocol, model, retryNonce]);

  // 未自定义时直接展示当前模式模板，切换模式会自动跟随。
  const selectedNames = value ?? (catalog ? catalog.defaults[mode] ?? [] : []);
  const selectedSet = useMemo(() => new Set(selectedNames), [selectedNames]);

  const total = catalog?.probes.length ?? 0;
  const selectedCount = catalog
    ? catalog.probes.reduce(
        (count, item) => (selectedSet.has(item.name) ? count + 1 : count),
        0
      )
    : selectedSet.size;

  const groups = useMemo<ProbeGroup[]>(() => {
    if (!catalog) return [];
    const byCategory = new Map<string, ProbeCatalogItem[]>();
    for (const item of catalog.probes) {
      const key = item.category || OTHER_GROUP_ID;
      const existing = byCategory.get(key);
      if (existing) existing.push(item);
      else byCategory.set(key, [item]);
    }

    const result: ProbeGroup[] = [];
    for (const category of catalog.categories) {
      const items = byCategory.get(category.id);
      if (!items || items.length === 0) continue;
      result.push({
        id: category.id,
        label: category.label,
        description: category.description,
        items,
      });
      byCategory.delete(category.id);
    }

    const leftovers = Array.from(byCategory.values()).flat();
    if (leftovers.length > 0) {
      result.push({
        id: OTHER_GROUP_ID,
        label: "其他",
        description: "未归入预设分类的探针。",
        items: leftovers,
      });
    }
    return result;
  }, [catalog]);

  const applySelection = (next: Set<string>) => {
    if (!catalog) return;
    onChange(
      catalog.probes.filter((item) => next.has(item.name)).map((item) => item.name)
    );
  };

  const toggleProbe = (name: string, checked: boolean) => {
    const next = new Set(selectedSet);
    if (checked) next.add(name);
    else next.delete(name);
    applySelection(next);
  };

  const applyTemplate = () => {
    if (!catalog) return;
    onChange([...(catalog.defaults[mode] ?? [])]);
  };

  const selectAllAvailable = () => {
    if (!catalog) return;
    applySelection(new Set(catalog.probes.filter((item) => item.applies).map((item) => item.name)));
  };

  const clearAll = () => onChange([]);

  const selectGroup = (group: ProbeGroup) => {
    const next = new Set(selectedSet);
    for (const item of group.items) {
      if (item.applies) next.add(item.name);
    }
    applySelection(next);
  };

  const clearGroup = (group: ProbeGroup) => {
    const next = new Set(selectedSet);
    for (const item of group.items) next.delete(item.name);
    applySelection(next);
  };

  const toggleGroup = (groupId: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  };

  return (
    <div className="field probe-picker">
      <div className="probe-picker-head">
        <span className="field-label">探针选择</span>
        <span className="probe-picker-count">
          {`已选 ${selectedCount}`}
          {catalog ? ` / ${total}` : ""}
        </span>
        <button
          type="button"
          className="probe-picker-collapse"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "收起" : "展开"}
        </button>
      </div>

      {expanded ? (
        <>
          <div className="probe-picker-toolbar">
            <button
              type="button"
              className="probe-picker-btn probe-picker-btn-primary"
              disabled={disabled || !catalog}
              onClick={applyTemplate}
            >
              按 {mode} 模板
            </button>
            <button
              type="button"
              className="probe-picker-btn"
              disabled={disabled || !catalog}
              onClick={selectAllAvailable}
            >
              全选可用
            </button>
            <button
              type="button"
              className="probe-picker-btn"
              disabled={disabled}
              onClick={clearAll}
            >
              清空
            </button>
          </div>

          {loading ? (
            <div className="probe-picker-loading" aria-hidden="true">
              <span className="skeleton-line" />
              <span className="skeleton-line skeleton-line-short" />
              <span className="skeleton-line" />
            </div>
          ) : null}

          {!loading && error ? (
            <div className="probe-picker-error" role="alert">
              <span>探针目录加载失败：{error}</span>
              <button
                type="button"
                className="probe-picker-btn"
                disabled={disabled}
                onClick={() => setRetryNonce((current) => current + 1)}
              >
                重试
              </button>
            </div>
          ) : null}

          {catalog && groups.length > 0 ? (
            <div className="probe-picker-scroll">
              {groups.map((group) => {
                const open = !collapsedGroups.has(group.id);
                const groupSelected = group.items.reduce(
                  (count, item) => (selectedSet.has(item.name) ? count + 1 : count),
                  0
                );
                return (
                  <section key={group.id} className="probe-group">
                    <button
                      type="button"
                      className="probe-group-head"
                      aria-expanded={open}
                      title={group.description}
                      onClick={() => toggleGroup(group.id)}
                    >
                      <span className="probe-group-caret" aria-hidden="true">
                        {open ? "▾" : "▸"}
                      </span>
                      <span className="probe-group-label">{group.label}</span>
                      <span className="probe-group-count">{`${groupSelected}/${group.items.length}`}</span>
                    </button>

                    {open ? (
                      <div className="probe-group-body">
                        <div className="probe-group-actions">
                          <button
                            type="button"
                            className="probe-group-link"
                            disabled={disabled}
                            onClick={() => selectGroup(group)}
                          >
                            全选
                          </button>
                          <button
                            type="button"
                            className="probe-group-link"
                            disabled={disabled}
                            onClick={() => clearGroup(group)}
                          >
                            清空
                          </button>
                        </div>

                        {group.items.map((item) => {
                          const checked = selectedSet.has(item.name);
                          const modesText = partialModes(item.modes);
                          return (
                            <label
                              key={item.name}
                              className={`probe-picker-item${checked ? " is-selected" : ""}${
                                item.applies ? "" : " is-disabled"
                              }`}
                              title={item.applies ? undefined : "当前模型不适用"}
                            >
                              <input
                                type="checkbox"
                                checked={checked}
                                disabled={disabled || !item.applies}
                                onChange={(event) =>
                                  toggleProbe(item.name, event.target.checked)
                                }
                              />
                              <span className="probe-picker-item-main">
                                <span className="probe-picker-item-title">
                                  <span className="probe-picker-item-name">
                                    {item.display_name || item.name}
                                  </span>
                                  <span className="probe-picker-item-weight">
                                    {`权重 ${formatWeight(item.weight)}`}
                                  </span>
                                </span>
                                {item.description ? (
                                  <span className="probe-picker-item-desc">
                                    {item.description}
                                  </span>
                                ) : null}
                                {item.passive || modesText || !item.applies ? (
                                  <span className="probe-picker-item-tags">
                                    {item.passive ? (
                                      <span className="probe-picker-tag probe-picker-tag-passive">
                                        被动
                                      </span>
                                    ) : null}
                                    {modesText ? (
                                      <span className="probe-picker-tag">
                                        {`仅 ${modesText}`}
                                      </span>
                                    ) : null}
                                    {!item.applies ? (
                                      <span className="probe-picker-tag probe-picker-tag-disabled">
                                        当前模型不适用
                                      </span>
                                    ) : null}
                                  </span>
                                ) : null}
                              </span>
                            </label>
                          );
                        })}
                      </div>
                    ) : null}
                  </section>
                );
              })}
            </div>
          ) : null}

          {!loading && !error && catalog && groups.length === 0 ? (
            <div className="probe-picker-empty">该协议下暂无探针。</div>
          ) : null}

          <p className="probe-picker-note">
            自定义选择时总分会按所选探针重新加权，不能与官方基线直接比较。
          </p>
        </>
      ) : (
        <span className="field-hint">
          {value === null
            ? `当前按 ${MODE_LABELS[mode]} 模式模板执行，展开可自定义探针。`
            : `当前为自定义探针（${selectedCount} 项），展开可修改。`}
        </span>
      )}
    </div>
  );
}
