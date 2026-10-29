"""探针分类体系。

每个探测器都属于一个分类,前端据此分组展示、按需勾选。分类只用于组织与
呈现,不参与评分:权重、mode 成员关系仍然由各协议的 config 决定。
"""

from __future__ import annotations

# 分类 id -> 展示信息。插入顺序即前端展示顺序(见 CATEGORY_ORDER)。
PROBE_CATEGORIES: dict[str, dict[str, str]] = {
    "basic": {
        "label": "基础与协议",
        "description": "连通、请求/响应协议规范、消息标识、响应形状",
    },
    "identity": {
        "label": "身份与真伪",
        "description": "模型身份自述、签名与行为指纹",
    },
    "capability": {
        "label": "能力完整性",
        "description": "知识、PDF、结构化输出、函数调用、长上下文",
    },
    "consistency": {
        "label": "一致性与完整性",
        "description": "模型一致性、流式完整性、token 字段对齐",
    },
    "billing": {
        "label": "计费与用量",
        "description": "usage 字段真实性、token 计费一致性",
    },
}

# 前端展示顺序;未知分类的探针排到最后(见 category_order_key)。
CATEGORY_ORDER = list(PROBE_CATEGORIES)


def category_label(cid: str) -> str:
    """返回分类的中文标签;未知分类原样返回 id,避免前端出现空白。"""
    info = PROBE_CATEGORIES.get(cid)
    return info["label"] if info else cid


def category_order_key(cid: str) -> int:
    """排序键:已知分类按 CATEGORY_ORDER,未知分类统一排到最后。"""
    try:
        return CATEGORY_ORDER.index(cid)
    except ValueError:
        return len(CATEGORY_ORDER)
