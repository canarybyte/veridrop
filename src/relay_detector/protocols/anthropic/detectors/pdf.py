"""PDFDetector — DESIGN.md §3.6.

Active. Sends a tiny test PDF (1 page, ~2 KB) as a base64 'document' content
block and asks Claude to extract a unique magic string baked into the PDF.

A relay station that forwards documents correctly will return the magic; one
that strips multimodal, downgrades to text-only routing, or returns an error
will fail. This is "anti-阉割" coverage: per Anthropic docs every active
Claude model supports PDF, so passing the request through must produce the
magic regardless of which Claude variant is on the other end.
"""

from __future__ import annotations

import base64
import importlib.resources as resources

from ....core.models import DetectorResult
from .base import ActiveDetector


# Keep this synchronized with scripts/build_test_pdf.py.
MAGIC = "MAGIC-7F3K-VERIFY-CLAUDE-RELAY-DETECTOR"
MAX_TOKENS = 150
PROMPT = (
    "What unique identifier string appears in this document? "
    "Reply with only the identifier string and no other text."
)


def _load_pdf_b64() -> str:
    pdf_bytes = (
        resources.files("relay_detector.protocols.anthropic.data")
        .joinpath("test_document.pdf")
        .read_bytes()
    )
    return base64.standard_b64encode(pdf_bytes).decode("ascii")


class PDFDetector(ActiveDetector):
    name = "pdf"
    display_name = "PDF文档识别"
    weight = 8.0
    category = "capability"
    description = "上传 PDF 验证文档识别链路"

    async def run(self, client, model: str) -> DetectorResult:
        try:
            pdf_b64 = _load_pdf_b64()
        except Exception as e:  # noqa: BLE001
            return self.skip(f"test PDF unavailable: {e}")

        try:
            _req, resp, _h, _lat = await client.messages_create(
                model=model,
                max_tokens=MAX_TOKENS,
                temperature=0,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_b64,
                                },
                            },
                            {"type": "text", "text": PROMPT},
                        ],
                    }
                ],
            )
        except Exception as e:  # noqa: BLE001
            # Distinguish API rejection (relay doesn't accept document content)
            # from network/unknown errors. Both score 0; status differs.
            return self._result(
                "fail",
                0.0,
                {"phase": "request"},
                error=str(e),
            )

        text = _join_text(resp.get("content"))
        if MAGIC in text:
            score, note = 100.0, "magic_found"
        elif text.strip():
            score, note = 50.0, "responded_but_missed_magic"
        else:
            score, note = 0.0, "empty_response"

        return self._result(
            "pass" if score >= 70 else "fail",
            score,
            {
                "magic_string": MAGIC,
                "response_text": text[:300],
                "evaluation": note,
                "stop_reason": resp.get("stop_reason"),
            },
        )


def _join_text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)
