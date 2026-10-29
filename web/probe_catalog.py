"""探针目录:把某协议的探测器元数据整理成前端可直接渲染的结构。

按协议懒加载对应包,读取探测器类上的 name/display_name/description/category
等元数据,输出分类与探针清单(含 applies 适用标记)。探针由用户手动勾选,
本模块不提供任何默认选择集合,也不发起任何上游请求。
"""

from __future__ import annotations

import importlib

from relay_detector.core.detectors_base import BaseDetector, PassiveDetector
from relay_detector.core.probes import (
    CATEGORY_ORDER,
    PROBE_CATEGORIES,
    category_order_key,
)


_SUPPORTED_PROTOCOLS = ("anthropic", "openai", "gemini")


def _load_protocol(protocol: str) -> list[BaseDetector]:
    """懒加载协议包,返回全部探测器实例。"""
    if protocol not in _SUPPORTED_PROTOCOLS:
        raise ValueError(f"unsupported protocol: {protocol}")

    package = importlib.import_module(f"relay_detector.protocols.{protocol}")
    # build_detectors(None):拿到全部探测器实例。
    return package.build_detectors(None)


def build_catalog(protocol: str, model: str | None) -> dict:
    """构建某协议的探针目录。

    ``model`` 为空表示「未指定模型」,此时所有探针的 applies 一律为 True;
    指定模型时按探测器的 applies_to() 判定该模型下是否可用。
    """
    detectors = _load_protocol(protocol)

    categories = [
        {
            "id": cid,
            "label": PROBE_CATEGORIES[cid]["label"],
            "description": PROBE_CATEGORIES[cid]["description"],
        }
        for cid in CATEGORY_ORDER
    ]

    probes = []
    for d in sorted(
        detectors, key=lambda item: (category_order_key(item.category), item.name)
    ):
        probes.append(
            {
                "name": d.name,
                "display_name": d.display_name,
                "description": d.description,
                "category": d.category,
                "passive": isinstance(d, PassiveDetector),
                "weight": float(d.weight),
                "applies": True if model is None else d.applies_to(model),
            }
        )

    return {
        "protocol": protocol,
        "model": model,
        "categories": categories,
        "probes": probes,
    }
