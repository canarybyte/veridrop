"""探针元数据规范化测试。

锁定三个协议探测器类上的 name/display_name/weight/category/description,
以及它们与 mode 成员关系的一致性。分类映射在这里作为测试数据单独抄一份,
后续如果有人改了某个探测器的分类,这里会直接失败,避免前端分组悄悄错位。
"""

from __future__ import annotations

import importlib

import pytest

from relay_detector.core.detectors_base import BaseDetector
from relay_detector.core.probes import CATEGORY_ORDER, PROBE_CATEGORIES


PROTOCOLS = ("anthropic", "openai", "gemini")

# 存在于代码中但未被 build_all() 注册的探测器:(module, class_name)。
_EXTRA_DETECTOR_CLASSES: dict[str, tuple[tuple[str, str], ...]] = {
    "openai": (("token_parity", "TokenParityDetector"),),
}

# 分类映射的「唯一定义」快照;与探测器类上的 category 必须逐项一致。
EXPECTED_CATEGORIES: dict[str, dict[str, str]] = {
    "anthropic": {
        "identity": "identity",
        "thinking_signature": "identity",
        "behavioral_signature": "identity",
        "knowledge": "capability",
        "pdf": "capability",
        "structured_output": "capability",
        "long_context": "capability",
        "consistency": "consistency",
        "integrity": "consistency",
        "protocol": "basic",
        "message_id": "basic",
        "token_usage": "billing",
    },
    "openai": {
        "basic_request": "basic",
        "protocol": "basic",
        "function_calling": "capability",
        "structured_output": "capability",
        "long_context": "capability",
        "integrity": "consistency",
        "model_consistency": "consistency",
        "token_parity": "consistency",
        "token_billing": "billing",
    },
    "gemini": {
        "basic_request": "basic",
        "protocol": "basic",
        "model_info": "basic",
        "function_calling": "capability",
        "structured_output": "capability",
        "integrity": "consistency",
        "token_usage": "billing",
    },
}


def _load_protocol(protocol: str):
    """返回 (探测器实例列表, MODE_DETECTORS)。"""
    package = importlib.import_module(f"relay_detector.protocols.{protocol}")
    config = importlib.import_module(f"relay_detector.protocols.{protocol}.config")
    return package.build_detectors(None), config.MODE_DETECTORS


def _detector_classes(protocol: str) -> list[type[BaseDetector]]:
    """返回该协议的全部探测器类(已注册 + 显式列出的未注册类)。

    build_all() 只注册「已启用」的探测器(openai 的 token_parity 目前未注册),
    元数据规范要求覆盖所有探测器类,所以这里额外把未注册类显式带上。
    """
    detectors, _ = _load_protocol(protocol)
    classes: list[type[BaseDetector]] = [type(d) for d in detectors]
    for module_name, class_name in _EXTRA_DETECTOR_CLASSES.get(protocol, ()):
        module = importlib.import_module(
            f"relay_detector.protocols.{protocol}.detectors.{module_name}"
        )
        classes.append(getattr(module, class_name))
    return classes


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_detector_metadata_is_complete(protocol: str) -> None:
    detectors, mode_detectors = _load_protocol(protocol)
    assert detectors, f"{protocol} 没有注册任何探测器"

    names = [d.name for d in detectors]
    # name 非空且协议内唯一。
    assert all(isinstance(n, str) and n.strip() for n in names)
    assert len(names) == len(set(names)), f"{protocol} 探测器 name 重复: {names}"

    # MODE_DETECTORS 里引用的名字必须都有对应探测器。
    known = set(names)
    for mode, members in mode_detectors.items():
        missing = set(members) - known
        assert not missing, f"{protocol}.{mode.value} 引用了不存在的探测器: {missing}"

    # 每个注册的探测器至少出现在一个 mode 中。
    covered = set().union(*mode_detectors.values())
    for name in names:
        assert name in covered, f"{protocol}.{name} 不属于任何 mode"

    for d in detectors:
        assert d.display_name and d.display_name.strip(), f"{d.name} 缺 display_name"
        assert d.description and d.description.strip(), f"{d.name} 缺 description"
        assert d.category in PROBE_CATEGORIES, f"{d.name} 分类非法: {d.category}"
        assert d.weight > 0, f"{d.name} 权重必须为正"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_category_mapping_is_stable(protocol: str) -> None:
    classes = _detector_classes(protocol)
    actual = {cls.name: cls.category for cls in classes}
    expected = EXPECTED_CATEGORIES[protocol]

    # 新增/删除探测器时，映射快照与探测器清单必须同步，否则这里会失败。
    assert set(actual) == set(expected), (
        f"{protocol} 探测器清单与分类快照不一致: "
        f"缺少 {set(expected) - set(actual)}, 多出 {set(actual) - set(expected)}"
    )
    for name, category in expected.items():
        assert actual[name] == category, (
            f"{protocol}.{name} 分类应为 {category}，实际是 {actual[name]}"
        )

    # 所有探测器类(含未注册的)都必须有完整的单句说明与正权重。
    for cls in classes:
        assert cls.display_name and cls.display_name.strip(), (
            f"{protocol}.{cls.name} 缺 display_name"
        )
        assert cls.description and cls.description.strip(), (
            f"{protocol}.{cls.name} 缺 description"
        )
        assert cls.weight > 0, f"{protocol}.{cls.name} 权重必须为正"

    # 分类 id 全部在展示顺序里（防止 category_label 找不到）。
    for category in actual.values():
        assert category in CATEGORY_ORDER


def test_category_order_matches_definition_order() -> None:
    assert CATEGORY_ORDER == list(PROBE_CATEGORIES)
