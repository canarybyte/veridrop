"""探针目录:把某协议的探测器元数据整理成前端可直接渲染的结构。

按协议懒加载对应包,读取探测器类上的 name/display_name/description/category
以及各 mode 的成员关系,输出分类、探针清单与各 mode 的默认勾选集合。本模块
只做元数据整理,不发起任何上游请求。
"""

from __future__ import annotations

import importlib

from relay_detector.core.detectors_base import BaseDetector, PassiveDetector
from relay_detector.core.models import Mode
from relay_detector.core.probes import (
    CATEGORY_ORDER,
    PROBE_CATEGORIES,
    category_order_key,
)


_SUPPORTED_PROTOCOLS = ("anthropic", "openai", "gemini")


def _load_protocol(protocol: str) -> tuple[list[BaseDetector], dict[Mode, set[str]]]:
    """懒加载协议包,返回 (探测器实例, mode->名字 映射)。"""
    if protocol not in _SUPPORTED_PROTOCOLS:
        raise ValueError(f"unsupported protocol: {protocol}")

    package = importlib.import_module(f"relay_detector.protocols.{protocol}")
    config = importlib.import_module(f"relay_detector.protocols.{protocol}.config")
    # build_detectors(None):拿到全部探测器实例,mode 过滤留给上层。
    detectors = package.build_detectors(None)
    return detectors, config.MODE_DETECTORS


def _mode_values(
    detector: BaseDetector, mode_detectors: dict[Mode, set[str]]
) -> list[str]:
    """探测器所属的 mode 集合。

    真相来源是协议 config 的 MODE_DETECTORS(构建探测器时用它过滤),
    不是类属性 `modes`——绝大多数探测器没有单独声明 modes,直接读类属性
    会把它们误报成 quick/standard/full 全支持。
    """
    return sorted(
        mode.value
        for mode, names in mode_detectors.items()
        if detector.name in names
    )


def build_catalog(protocol: str, model: str | None) -> dict:
    """构建某协议的探针目录。

    ``model`` 为空表示「未指定模型」,此时所有探针的 applies 一律为 True;
    指定模型时按探测器的 applies_to() 判定该模型下是否可用。
    """
    detectors, mode_detectors = _load_protocol(protocol)
    known_names = {d.name for d in detectors}

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
                "modes": _mode_values(d, mode_detectors),
                "applies": True if model is None else d.applies_to(model),
            }
        )

    applies_by_name = {p["name"]: p["applies"] for p in probes}
    defaults = {
        mode.value: sorted(
            name
            for name in mode_detectors[mode]
            if name in known_names and applies_by_name.get(name, False)
        )
        for mode in (Mode.QUICK, Mode.STANDARD, Mode.FULL)
    }

    return {
        "protocol": protocol,
        "model": model,
        "categories": categories,
        "probes": probes,
        "defaults": defaults,
    }
