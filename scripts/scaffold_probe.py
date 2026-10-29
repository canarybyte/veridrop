"""探针脚手架：生成符合项目规范的 detector 骨架，并自动注册到 build_all()。

用法示例：

    .\\venv\\Scripts\\python.exe scripts\\scaffold_probe.py --protocol anthropic ^
        --name cache_probe --display-name "缓存命中验证" --category billing ^
        --weight 10 --kind active --passive false

脚本只依赖标准库；生成文件后仍需要手动把探针名加入对应协议 config.py 的
MODE_DETECTORS / DETECTOR_WEIGHTS（脚本会在末尾打印具体步骤）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROTOCOLS = ("anthropic", "openai", "gemini")
CATEGORIES = ("basic", "identity", "capability", "consistency", "billing")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# 协议 -> 示例请求代码片段。active 骨架按协议使用对应的客户端方法。
_ACTIVE_REQUEST = {
    "anthropic": (
        "        body = {\n"
        '            "model": model,\n'
        '            "max_tokens": MAX_TOKENS,\n'
        '            "temperature": 0,\n'
        '            "messages": [{"role": "user", "content": PROBE_PROMPT}],\n'
        "        }\n"
        "        try:\n"
        "            _req, resp, _headers, latency_ms = await client.messages_create(**body)\n"
        '        except Exception as e:  # noqa: BLE001 — 探针异常不能拖垮整次检测\n'
        '            return self._result("error", 0.0, {"stage": "request"}, error=str(e))\n'
    ),
    "openai": (
        "        try:\n"
        "            _req, resp, _headers, latency_ms = await client.chat_completions_create(\n"
        "                model=model,\n"
        "                max_completion_tokens=MAX_TOKENS,\n"
        "                temperature=0,\n"
        '                messages=[{"role": "user", "content": PROBE_PROMPT}],\n'
        "            )\n"
        '        except Exception as e:  # noqa: BLE001 — 探针异常不能拖垮整次检测\n'
        '            return self._result("error", 0.0, {"stage": "request"}, error=str(e))\n'
    ),
}
_ACTIVE_REQUEST["gemini"] = _ACTIVE_REQUEST["openai"]


def _class_name(name: str) -> str:
    """snake_case -> PascalCase + Detector，例如 cache_probe -> CacheProbeDetector。"""
    parts = name.split("_")
    if parts and parts[-1] == "detector":
        # 避免 FooDetectorDetector。
        parts = parts[:-1]
    return "".join(part[:1].upper() + part[1:] for part in parts) + "Detector"


def _one_line(text: str) -> str:
    """压成一行，避免 docstring / 注释被换行或三引号截断。"""
    return " ".join(text.split()).replace('"""', '"')


def _quote(text: str) -> str:
    """生成安全的 Python 字符串字面量（保留中文）。"""
    return json.dumps(text, ensure_ascii=False)


def _module_docstring(display_name: str, name: str, category: str) -> str:
    return (
        f'"""「{_one_line(display_name)}」探针（name={name}，分类={category}）。\n'
        "\n"
        "目的：TODO 说明该探针要验证的中转站行为，以及判定依据。\n"
        "成本：TODO 说明请求次数与大致 token 开销（按 Haiku 基准估算）。\n"
        "适用模型：TODO 说明限制（例如仅特定模型、需要 thinking / PDF 等能力）。\n"
        '"""\n'
    )


def _active_template(
    protocol: str,
    name: str,
    display_name: str,
    category: str,
    description: str,
    weight: float,
) -> str:
    cls = _class_name(name)
    return (
        _module_docstring(display_name, name, category)
        + "\n"
        "from __future__ import annotations\n"
        "\n"
        "from ....core.detectors_base import ActiveDetector\n"
        "from ....core.models import DetectorResult\n"
        "\n"
        'PROBE_PROMPT = "Reply with exactly: ok"\n'
        "MAX_TOKENS = 16\n"
        "\n"
        "\n"
        f"class {cls}(ActiveDetector):\n"
        f"    name = {_quote(name)}\n"
        f"    display_name = {_quote(display_name)}\n"
        f"    category = {_quote(category)}\n"
        f"    description = {_quote(description)}\n"
        f"    weight = {weight}\n"
        "\n"
        "    async def run(self, client, model: str) -> DetectorResult:\n"
        "        # TODO: 按探针目的调整 prompt、请求参数与判定阈值。\n"
        + _ACTIVE_REQUEST[protocol]
        + "\n"
        "        # TODO: 根据 resp 计算分数；当前骨架先把请求跑通。\n"
        '        return self._result(\n'
        '            "pass",\n'
        "            100.0,\n"
        "            {\n"
        '                "latency_ms": latency_ms,\n'
        '                "response_id": resp.get("id"),\n'
        '                "usage": resp.get("usage"),\n'
        "            },\n"
        "        )\n"
    )


def _passive_template(
    name: str,
    display_name: str,
    category: str,
    description: str,
    weight: float,
) -> str:
    cls = _class_name(name)
    return (
        _module_docstring(display_name, name, category)
        + "\n"
        "from __future__ import annotations\n"
        "\n"
        "from typing import Any\n"
        "\n"
        "import httpx\n"
        "\n"
        "from ....core.detectors_base import PassiveDetector\n"
        "from ....core.models import DetectorResult\n"
        "\n"
        "\n"
        f"class {cls}(PassiveDetector):\n"
        f"    name = {_quote(name)}\n"
        f"    display_name = {_quote(display_name)}\n"
        f"    category = {_quote(category)}\n"
        f"    description = {_quote(description)}\n"
        f"    weight = {weight}\n"
        "\n"
        "    def __init__(self) -> None:\n"
        "        self._observations = 0\n"
        "        self._issues: list[dict[str, Any]] = []\n"
        "\n"
        "    def observe(\n"
        "        self,\n"
        "        request: dict[str, Any],\n"
        "        response: dict[str, Any],\n"
        "        headers: httpx.Headers,\n"
        "        latency_ms: int,\n"
        "    ) -> None:\n"
        "        # 被动探针不发请求：只检查其他 active 探针留下的 request/response。\n"
        "        # TODO: 按探针目的记录 issue，例如 self._issues.append({...})。\n"
        "        self._observations += 1\n"
        "\n"
        "    def finalize(self) -> DetectorResult:\n"
        "        if self._observations == 0:\n"
        '            return self.skip("no observations")\n'
        "\n"
        "        # TODO: 按采集到的 issue 扣分；issues 里 severity=critical 会触发硬否决。\n"
        '        return self._result(\n'
        '            "pass",\n'
        "            100.0,\n"
        "            {\n"
        '                "observation_count": self._observations,\n'
        '                "issues": self._issues,\n'
        "            },\n"
        "        )\n"
    )


def _insert_import(lines: list[str], module: str, class_name: str) -> bool:
    """在本地 import 块末尾插入一行；已存在则返回 False。"""
    target = f"from .{module} import {class_name}"
    if any(line.strip() == target for line in lines):
        return False
    import_re = re.compile(r"^from \.[A-Za-z_][A-Za-z0-9_]* import ")
    indexes = [i for i, line in enumerate(lines) if import_re.match(line)]
    if not indexes:
        raise SystemExit("无法自动注册：detectors/__init__.py 里没有本地 import 块")
    lines.insert(indexes[-1] + 1, target + "\n")
    return True


def _insert_entry(lines: list[str], class_name: str) -> bool:
    """把 ClassName() 追加到 build_all() 的 return 列表末尾。"""
    entry = f"{class_name}()"
    if any(line.strip() == entry + "," for line in lines):
        return False

    start = next(
        (i for i, line in enumerate(lines) if line.startswith("def build_all")),
        None,
    )
    if start is None:
        raise SystemExit("无法自动注册：detectors/__init__.py 里找不到 build_all()")

    return_line = next(
        (i for i in range(start, len(lines)) if "return [" in lines[i]),
        None,
    )
    if return_line is None or lines[return_line].rstrip().endswith("]"):
        raise SystemExit(
            "无法自动注册：build_all() 不是多行 return 列表，请手动在列表末尾追加实例"
        )

    close = next(
        (
            i
            for i in range(return_line + 1, len(lines))
            if re.match(r"^\s*\]\s*$", lines[i])
        ),
        None,
    )
    if close is None:
        raise SystemExit("无法自动注册：build_all() 的 return 列表缺少闭合括号")

    entry_indent = " " * (len(lines[close]) - len(lines[close].lstrip()) + 4)
    lines.insert(close, f"{entry_indent}{entry},\n")
    return True


def _insert_all_export(lines: list[str], class_name: str) -> bool:
    """把类名追加到 __all__；文件没有 __all__ 时静默跳过。"""
    quoted = f'"{class_name}"'
    if any(line.strip() == quoted + "," for line in lines):
        return False
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("__all__ = [")),
        None,
    )
    if start is None:
        return False
    close = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^\s*\]\s*$", lines[i])),
        None,
    )
    if close is None:
        return False
    # 类名排在 "build_all" 之前，保持 __all__ 里“类在前、函数在后”的观感。
    build_all_line = next(
        (
            i
            for i in range(start + 1, close)
            if lines[i].strip() == '"build_all",'
        ),
        close,
    )
    lines.insert(build_all_line, f"    {quoted},\n")
    return True


def _register(init_path: Path, module: str, class_name: str) -> list[str]:
    """更新 detectors/__init__.py，返回实际发生的改动说明。"""
    lines = init_path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed: list[str] = []
    if _insert_import(lines, module, class_name):
        changed.append("import")
    if _insert_entry(lines, class_name):
        changed.append(f"build_all() 追加 {class_name}()")
    if _insert_all_export(lines, class_name):
        changed.append("__all__")
    if changed:
        init_path.write_text("".join(lines), encoding="utf-8", newline="\n")
    return changed


def _config_hints(protocol: str, name: str, weight: float) -> list[str]:
    """根据对应 config.py 的现状，生成准确的下一步提示。"""
    cfg = ROOT / "src" / "relay_detector" / "protocols" / protocol / "config.py"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    rel = cfg.relative_to(ROOT).as_posix()

    hints: list[str] = []
    if "DETECTOR_WEIGHTS" in text:
        hints.append(
            f'在 {rel} 的 DETECTOR_WEIGHTS 中登记权重："{name}": {weight}'
        )
    full_auto = bool(
        re.search(r"Mode\.FULL:\s*set\(DETECTOR_WEIGHTS\.keys\(\)\)", text)
    )
    if full_auto:
        hints.append(
            "FULL 模式由 set(DETECTOR_WEIGHTS.keys()) 自动包含该探针；"
            f'如需在 standard / quick 运行，把 "{name}" 加进对应 Mode 的集合。'
        )
    else:
        hints.append(f'在 {rel} 的 MODE_DETECTORS 中把 "{name}" 加入 Mode.FULL（至少 full）。')
    return hints


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成 Veridrop 探测器骨架并自动注册到 detectors/__init__.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  .\\venv\\Scripts\\python.exe scripts\\scaffold_probe.py --protocol anthropic "
            '--name cache_probe --display-name "缓存命中验证" --category billing '
            "--weight 10 --kind active --passive false\n"
        ),
    )
    parser.add_argument(
        "--protocol",
        required=True,
        choices=PROTOCOLS,
        help="协议包（决定生成目录与示例请求写法）",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="探针内部名，snake_case（正则 ^[a-z][a-z0-9_]*$）",
    )
    parser.add_argument(
        "--display-name",
        required=True,
        help="前端展示名（中文或任意文本）",
    )
    parser.add_argument(
        "--category",
        required=True,
        choices=CATEGORIES,
        help="探针分类：basic / identity / capability / consistency / billing",
    )
    parser.add_argument(
        "--weight",
        type=float,
        default=10.0,
        help="评分权重，默认 10",
    )
    parser.add_argument(
        "--kind",
        choices=("active", "passive"),
        default="active",
        help="探针类型，默认 active",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="一句话说明；默认自动生成 TODO 占位说明",
    )
    parser.add_argument(
        "--passive",
        nargs="?",
        const=True,
        default=None,
        metavar="{true,false}",
        help="兼容写法：--passive / --passive true|false，优先于 --kind",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的探针文件",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not NAME_PATTERN.fullmatch(args.name):
        print(
            f"错误：--name {args.name!r} 不合法，必须匹配 ^[a-z][a-z0-9_]*$"
            "（小写字母开头，仅含小写字母、数字、下划线）",
            file=sys.stderr,
        )
        return 1

    display_name = args.display_name.strip()
    if not display_name:
        print("错误：--display-name 不能为空", file=sys.stderr)
        return 1

    if args.weight < 0:
        print("错误：--weight 必须 >= 0", file=sys.stderr)
        return 1

    kind = args.kind
    if args.passive is not None:
        value = args.passive
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered not in ("true", "false"):
                print(
                    f"错误：--passive 只接受 true/false，收到 {value!r}",
                    file=sys.stderr,
                )
                return 1
            value = lowered == "true"
        kind = "passive" if value else "active"

    description = (args.description or "").strip() or (
        f"TODO: 说明「{display_name}」探针要验证的行为与判定标准。"
    )

    detectors_dir = (
        ROOT / "src" / "relay_detector" / "protocols" / args.protocol / "detectors"
    )
    init_path = detectors_dir / "__init__.py"
    if not detectors_dir.is_dir() or not init_path.is_file():
        print(
            f"错误：找不到协议探测器目录或注册文件：{detectors_dir}",
            file=sys.stderr,
        )
        return 1

    target = detectors_dir / f"{args.name}.py"
    if target.exists() and not args.force:
        print(
            f"错误：{target.relative_to(ROOT)} 已存在；如需覆盖请加 --force",
            file=sys.stderr,
        )
        return 1

    cls = _class_name(args.name)
    if kind == "active":
        content = _active_template(
            args.protocol,
            args.name,
            display_name,
            args.category,
            description,
            args.weight,
        )
    else:
        content = _passive_template(
            args.name, display_name, args.category, description, args.weight
        )

    target.write_text(content, encoding="utf-8", newline="\n")

    try:
        changes = _register(init_path, args.name, cls)
    except SystemExit as exc:
        # 文件已经生成，但注册失败：给出准确的收尾指引。
        print(f"错误：{exc}", file=sys.stderr)
        print(
            f"探针文件已生成：{target.relative_to(ROOT)}；"
            f"请手动把它加入 {init_path.relative_to(ROOT)} 的 build_all() 与 __all__。",
            file=sys.stderr,
        )
        return 1

    print(f"已生成：{target.relative_to(ROOT)}")
    print(f"类名：{cls}（{kind}）")
    if changes:
        print(f"已注册：{init_path.relative_to(ROOT)}（{', '.join(changes)}）")
    else:
        print(f"注册已存在，未重复写入：{init_path.relative_to(ROOT)}")

    print("\n下一步：")
    for i, hint in enumerate(_config_hints(args.protocol, args.name, args.weight), 1):
        print(f"  {i}. {hint}")
    steps = [
        "实现 run()（active）或 observe() + finalize()（passive），替换所有 TODO；"
        "被动探针不要自己发请求，trace.record_exchange 由客户端自动记录。",
        "补充 applies_to()（如仅支持部分模型）与失败分支，成本高或可选探针需自我 skip。",
        f"新建 tests/test_{args.name}.py：用 respx / httpx.MockTransport 覆盖成功与失败分支。",
        "运行 .\\venv\\Scripts\\python.exe -m pytest -q，确认全绿。",
        "前端无需改动：探针按 category 自动分组；可用 GET /app/api/probes 查看 "
        "name / category / modes 是否出现。",
    ]
    for i, step in enumerate(steps, len(_config_hints(args.protocol, args.name, args.weight)) + 1):
        print(f"  {i}. {step}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
