"""Render a detection report as a downloadable JPG.

Layout mirrors the result page: big circular score on the left, per-detector
pass/fail rows on the right, four metric tiles at the bottom. Drawn with
Pillow primitives — no headless browser dependency.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


# Font hunt: Noto CJK first (apt: fonts-noto-cjk) so Chinese characters render.
# DejaVu is the Latin fallback; PIL's built-in bitmap font is the last resort
# so we never crash on a fresh box.
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
]
_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR
    for p in candidates:
        if Path(p).is_file():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# Color palette — green-on-white for "pass", neutral grey for body text,
# warning amber / red for non-pass cases.
_GREEN = (16, 185, 129)
_GREEN_DARK = (5, 150, 105)
_RED = (239, 68, 68)
_AMBER = (245, 158, 11)
_TEXT = (17, 24, 39)
_MUTED = (107, 114, 128)
_LINE = (229, 231, 235)
_BG = (255, 255, 255)
_TILE_BG = (249, 250, 251)


# Detector display labels — Chinese, same order as the result page.
_DETECTOR_LABELS = {
    "anthropic": [
        ("identity", "身份一致性"),
        ("behavioral_signature", "行为签名验证"),
        ("thinking_signature", "思维签名验证"),
        ("consistency", "模型一致性"),
        ("knowledge", "知识准确度"),
        ("pdf", "PDF 文档识别"),
        ("structured_output", "结构化输出"),
        ("protocol", "协议规范性"),
        ("integrity", "响应完整性"),
        ("token_usage", "Token 用量"),
        ("message_id", "消息标识规范"),
    ],
    "openai": [
        ("basic_request", "基础请求"),
        ("model_consistency", "模型一致性"),
        ("function_calling", "函数调用"),
        ("structured_output", "结构化输出"),
        ("protocol", "协议规范性"),
        ("integrity", "流式一致性"),
        ("token_billing", "Token 计费"),
    ],
    "gemini": [
        ("basic_request", "基础请求"),
        ("model_info", "模型响应形状"),
        ("function_calling", "函数调用"),
        ("structured_output", "结构化输出"),
        ("protocol", "协议规范性"),
        ("integrity", "流式一致性"),
        ("token_usage", "Token 用量"),
    ],
}


def _status_label(status: str, score: float) -> tuple[str, tuple[int, int, int]]:
    if status == "pass":
        return "通过", _GREEN
    if status == "skip":
        return "跳过", _MUTED
    if status == "error":
        return "异常", _RED
    if score >= 70:
        return "警告", _AMBER
    return "未通过", _RED


def _verdict_color(score: float, verdict: str) -> tuple[int, int, int]:
    if verdict == "passed" and score >= 85:
        return _GREEN
    if verdict == "passed":
        return _GREEN_DARK
    if verdict == "marginal":
        return _AMBER
    return _RED


def _verdict_caption(score: float, verdict: str, protocol: str = "anthropic") -> str:
    """Mirrors result.html so JPG export and HTML report agree.

    verdict is the source of truth. scorer.effective_verdict() may downgrade
    a high-scoring run to marginal when critical issues are present, so the
    caption needs to follow verdict, not raw score thresholds.
    """
    if verdict == "passed" and score >= 95 and protocol == "anthropic":
        return "完全一致"
    if verdict == "passed" and score >= 85:
        return "协议表现良好" if protocol in {"openai", "gemini"} else "优秀"
    if verdict == "passed":
        return "基本通过" if protocol in {"openai", "gemini"} else "通过"
    if verdict == "marginal":
        return "存在风险" if protocol in {"openai", "gemini"} else "基本合格"
    return "未达标"


def _draw_check(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int,
                color: tuple[int, int, int]) -> None:
    """Filled circle with a white check mark inside."""
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    # check mark — three points: lower-left, bottom-mid, upper-right
    p1 = (cx - r * 0.45, cy + r * 0.05)
    p2 = (cx - r * 0.10, cy + r * 0.40)
    p3 = (cx + r * 0.50, cy - r * 0.30)
    d.line([p1, p2, p3], fill=(255, 255, 255), width=max(2, r // 6))


def _draw_cross(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int,
                color: tuple[int, int, int]) -> None:
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    pad = r * 0.45
    d.line(
        [(cx - pad, cy - pad), (cx + pad, cy + pad)],
        fill=(255, 255, 255), width=max(2, r // 6),
    )
    d.line(
        [(cx - pad, cy + pad), (cx + pad, cy - pad)],
        fill=(255, 255, 255), width=max(2, r // 6),
    )


def _draw_circle_score(d: ImageDraw.ImageDraw, cx: int, cy: int, radius: int,
                       score: float, caption: str,
                       ring_color: tuple[int, int, int]) -> None:
    """Big ring-style score badge — thick colored border, white interior."""
    ring_thickness = 14
    # outer ring
    d.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=ring_color,
    )
    # inner cut-out
    inner_r = radius - ring_thickness
    d.ellipse(
        (cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r),
        fill=_BG,
    )

    # score text — keep share images clean and compact.
    score_text = f"{int(round(score))}%"
    score_font = _load_font(74, bold=True)
    bbox = d.textbbox((0, 0), score_text, font=score_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        (cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1] - 14),
        score_text, fill=_TEXT, font=score_font,
    )

    # caption below the score
    caption_font = _load_font(20)
    cb = d.textbbox((0, 0), caption, font=caption_font)
    cw = cb[2] - cb[0]
    d.text(
        (cx - cw / 2 - cb[0], cy + 30),
        caption, fill=_MUTED, font=caption_font,
    )


def _draw_metric_tile(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                      label: str, value: str,
                      highlight: bool = False) -> None:
    """One of the four bottom metric boxes."""
    border = _RED if highlight else _LINE
    d.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=8,
        fill=_TILE_BG,
        outline=border,
        width=3 if highlight else 1,
    )
    label_font = _load_font(14)
    value_font = _load_font(26, bold=True)

    lb = d.textbbox((0, 0), label, font=label_font)
    lw = lb[2] - lb[0]
    d.text(
        (x + w / 2 - lw / 2 - lb[0], y + 18 - lb[1]),
        label, fill=_MUTED, font=label_font,
    )

    vb = d.textbbox((0, 0), value, font=value_font)
    vw, vh = vb[2] - vb[0], vb[3] - vb[1]
    d.text(
        (x + w / 2 - vw / 2 - vb[0], y + h - 28 - vh - vb[1]),
        value, fill=_TEXT, font=value_font,
    )


def _format_count(n: int | float | None) -> str:
    if n is None:
        return "-"
    if isinstance(n, float):
        return f"{n:,.1f}"
    return f"{n:,}"


def _tokens_per_second(report: dict[str, Any]) -> float | None:
    perf = report.get("performance") or {}
    out = (perf.get("usage") or {}).get("output_tokens")
    latency_ms = perf.get("total_latency_ms")
    if not out or not latency_ms:
        return None
    return round(out * 1000.0 / latency_ms, 1)


def _openai_jpg_note(report: dict[str, Any]) -> str:
    if str(report.get("protocol") or "") != "openai":
        return ""
    results = {
        r.get("name"): r for r in report.get("results", [])
        if isinstance(r, dict)
    }
    structured = results.get("structured_output") or {}
    if structured.get("status") != "pass":
        details = structured.get("details") or {}
        if isinstance(details, dict) and details.get("markdown_json_seen"):
            return (
                "结构化输出未生效: 已发送 json_schema strict=true,但返回了 Markdown 文本。"
            )
        return "结构化输出未通过: 返回内容不能按 JSON schema 解析。"

    token_billing = results.get("token_billing") or {}
    if token_billing.get("status") == "skip":
        return "Token 计费无法判断: 接口没有返回足够完整的 Token 用量信息。"
    if token_billing.get("status") == "fail":
        return "Token 计费存在风险: 返回的 Token 统计有明显偏差。"

    protocol = results.get("protocol") or {}
    details = protocol.get("details") or {}
    if isinstance(details, dict):
        for issue in details.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            if issue.get("code") in {
                "usage_contains_claude_fields",
                "usage_source_anthropic",
                "usage_mixed_token_fields",
            }:
                return "协议提示: usage 字段带有非 OpenAI 适配层痕迹。"
    return ""


def _anthropic_jpg_note(report: dict[str, Any]) -> str:
    if str(report.get("protocol") or "anthropic") != "anthropic":
        return ""
    results = {
        r.get("name"): r for r in report.get("results", [])
        if isinstance(r, dict)
    }
    token_usage = results.get("token_usage") or {}
    if token_usage.get("status") == "skip":
        return "Token 用量无法判断: 接口没有返回完整 usage 字段。"
    if token_usage.get("status") == "fail":
        return "Token 用量存在风险: usage 字段缺失或统计偏差明显。"
    return ""


def _gemini_jpg_note(report: dict[str, Any]) -> str:
    if str(report.get("protocol") or "") != "gemini":
        return ""
    results = {
        r.get("name"): r for r in report.get("results", [])
        if isinstance(r, dict)
    }
    structured = results.get("structured_output") or {}
    if structured.get("status") != "pass":
        return "结构化输出未生效: 已要求 JSON,但返回内容无法按 schema 解析。"
    token_usage = results.get("token_usage") or {}
    if token_usage.get("status") == "fail":
        return "Token 用量存在风险: usage 字段不完整或统计不自洽。"
    integrity = results.get("integrity") or {}
    if integrity.get("status") == "fail":
        return "流式响应存在偏差: stream 与 non-stream 没有完全对齐。"
    return ""


def render_report_jpg(report: dict[str, Any]) -> bytes:
    """Render the report into a JPG and return the bytes."""
    W, H = 1400, 1000
    img = Image.new("RGB", (W, H), _BG)
    d = ImageDraw.Draw(img)

    # ------- header -------
    title_font = _load_font(34, bold=True)
    protocol = str(report.get("protocol") or "anthropic")
    if protocol == "openai":
        title = "OpenAI 中转站检测报告"
    elif protocol == "gemini":
        title = "Gemini 中转站检测报告"
    else:
        title = "中转站检测报告"
    d.text((60, 50), title, fill=_TEXT, font=title_font)

    # share/brand pill in top-right
    pill_font = _load_font(18)
    pill_text = "veridrop.org"
    pb = d.textbbox((0, 0), pill_text, font=pill_font)
    pw = pb[2] - pb[0]
    pill_x = W - 60 - pw - 32
    pill_y = 56
    d.rounded_rectangle(
        (pill_x, pill_y - 6, pill_x + pw + 32, pill_y + 28),
        radius=18,
        fill=_TILE_BG,
        outline=_LINE,
        width=1,
    )
    d.text(
        (pill_x + 16 - pb[0], pill_y - pb[1] + 2),
        pill_text, fill=_MUTED, font=pill_font,
    )

    # divider under header
    d.line([(60, 110), (W - 60, 110)], fill=_LINE, width=1)

    # ------- left: score circle -------
    score = float(report.get("total_score", 0.0))
    verdict = report.get("verdict", "failed")
    ring = _verdict_color(score, verdict)
    caption = _verdict_caption(score, verdict, protocol)

    circle_cx, circle_cy = 280, 420
    circle_r = 175
    _draw_circle_score(d, circle_cx, circle_cy, circle_r, score, caption, ring)

    # model line under the circle
    sub_font = _load_font(16)
    model_label = report.get("target_model", "")
    sub_text = model_label
    sb = d.textbbox((0, 0), sub_text, font=sub_font)
    sw = sb[2] - sb[0]
    d.text(
        (circle_cx - sw / 2 - sb[0], circle_cy + circle_r + 30),
        sub_text, fill=_MUTED, font=sub_font,
    )

    # base_url (truncated) below model line
    base_url = report.get("base_url", "")
    if len(base_url) > 38:
        base_url_disp = base_url[:35] + "..."
    else:
        base_url_disp = base_url
    bb = d.textbbox((0, 0), base_url_disp, font=sub_font)
    bw = bb[2] - bb[0]
    d.text(
        (circle_cx - bw / 2 - bb[0], circle_cy + circle_r + 56),
        base_url_disp, fill=_TEXT, font=sub_font,
    )

    # 报告来源
    attr_font = _load_font(14)
    attr_text = "由 veridrop.org 生成"
    ab = d.textbbox((0, 0), attr_text, font=attr_font)
    aw = ab[2] - ab[0]
    d.text(
        (circle_cx - aw / 2 - ab[0], circle_cy + circle_r + 100),
        attr_text, fill=_MUTED, font=attr_font,
    )

    # ------- right: detector rows -------
    rows_x = 620
    rows_top = 160
    row_h = 56
    rows_w = W - rows_x - 60
    label_font = _load_font(20)
    status_font = _load_font(20, bold=True)

    # build name -> result dict for fast lookup
    by_name: dict[str, dict[str, Any]] = {
        r.get("name"): r for r in report.get("results", [])
        if isinstance(r, dict)
    }

    labels = _DETECTOR_LABELS.get(protocol, _DETECTOR_LABELS["anthropic"])
    for i, (name, label) in enumerate(labels):
        ry = rows_top + i * row_h
        result = by_name.get(name) or {}
        status = str(result.get("status") or "skip")
        score_v = float(result.get("score", 0.0))
        status_text, status_color = _status_label(status, score_v)
        if name in {"token_billing", "token_usage"} and status == "skip":
            status_text = "无法判断"

        # icon
        icon_cx = rows_x + 22
        icon_cy = ry + row_h // 2
        if status == "pass":
            _draw_check(d, icon_cx, icon_cy, 14, status_color)
        elif status == "skip":
            d.ellipse(
                (icon_cx - 14, icon_cy - 14, icon_cx + 14, icon_cy + 14),
                outline=_MUTED, width=2,
            )
        else:
            _draw_cross(d, icon_cx, icon_cy, 14, status_color)

        # label
        d.text(
            (rows_x + 56, ry + row_h // 2 - 14),
            label, fill=_TEXT, font=label_font,
        )

        # right-aligned status
        sb2 = d.textbbox((0, 0), status_text, font=status_font)
        sw2 = sb2[2] - sb2[0]
        d.text(
            (rows_x + rows_w - sw2 - sb2[0], ry + row_h // 2 - 14),
            status_text, fill=status_color, font=status_font,
        )

        # row divider
        if i < len(labels) - 1:
            d.line(
                [(rows_x, ry + row_h), (rows_x + rows_w, ry + row_h)],
                fill=_LINE, width=1,
            )

    # Plain-language note for share images. Keep it short so the JPG remains
    # readable on mobile and social previews.
    if protocol == "gemini":
        note = _gemini_jpg_note(report)
    elif protocol == "openai":
        note = _openai_jpg_note(report)
    else:
        note = _anthropic_jpg_note(report)
    if note:
        note_font = _load_font(17)
        note_y = rows_top + len(labels) * row_h + 36
        d.rounded_rectangle(
            (rows_x, note_y, rows_x + rows_w, note_y + 54),
            radius=8,
            fill=(255, 251, 235),
            outline=(253, 230, 138),
            width=1,
        )
        d.text(
            (rows_x + 18, note_y + 16),
            note[:64],
            fill=(146, 64, 14),
            font=note_font,
        )

    # ------- bottom: metric tiles -------
    perf = report.get("performance") or {}
    usage = perf.get("usage") or {}
    ttft = perf.get("ttft_ms")
    metrics = [
        ("首 TOKEN", f"{_format_count(ttft)}ms" if ttft is not None else "—"),
        ("总耗时", f"{_format_count(perf.get('total_latency_ms'))}ms"),
        ("吞吐 T/S", _format_count(_tokens_per_second(report))),
        ("输入 TOKENS", _format_count(usage.get("input_tokens"))),
        ("输出 TOKENS", _format_count(usage.get("output_tokens"))),
    ]
    tile_y = H - 150
    tile_h = 90
    tile_gap = 14
    tile_total_w = W - 120
    tile_w = (tile_total_w - tile_gap * (len(metrics) - 1)) // len(metrics)
    for i, (label, value) in enumerate(metrics):
        tx = 60 + i * (tile_w + tile_gap)
        # Highlight pathological values: TTFT >2s or total latency >30s
        # suggests a slow / overloaded relay. Otherwise tile renders plain.
        highlight = False
        if label == "首 TOKEN" and isinstance(ttft, int) and ttft > 2000:
            highlight = True
        elif label == "总耗时" and isinstance(perf.get("total_latency_ms"), int) \
                and perf["total_latency_ms"] > 30000:
            highlight = True
        _draw_metric_tile(d, tx, tile_y, tile_w, tile_h, label, value, highlight)

    # ------- footer note -------
    foot_font = _load_font(13)
    foot = report.get("timestamp", "")
    if isinstance(foot, str) and foot:
        d.text(
            (60, H - 32),
            f"生成于 {foot[:19].replace('T', ' ')} UTC", fill=_MUTED,
            font=foot_font,
        )
    masked = report.get("api_key_masked", "")
    if masked:
        rt = f"密钥 {masked}"
        rb = d.textbbox((0, 0), rt, font=foot_font)
        rw = rb[2] - rb[0]
        d.text(
            (W - 60 - rw - rb[0], H - 32),
            rt, fill=_MUTED, font=foot_font,
        )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()
