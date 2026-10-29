"""Azure 式 model 回显探针(行为层)。

Azure 与多数上游会回显带日期戳的真实快照名(如 gpt-5.5-2026-04-24),
中转自造响应壳时通常只原样回显请求名。判据:回显带日期戳且与请求名不同 → PASS;
原样回显 → INFO(官方直连也会这样,无鉴别力)。
"""

from __future__ import annotations

import re

from .base import ActiveDetector

PROMPT = "Reply with exactly: pong"
DATE_STAMP_RE = re.compile(r"-\d{4}-\d{2}-\d{2}")


class AzureModelEchoDetector(ActiveDetector):
    name = "azure_model_echo"
    display_name = "Azure 式 model 回显"
    weight = 10.0
    category = "azure"
    description = "响应 model 是否由服务端回显日期戳快照名(行为层)"

    async def run(self, client, model: str):
        try:
            _req, resp, _h, _lat = await client.chat_completions_create(
                model=model,
                max_completion_tokens=16,
                messages=[{"role": "user", "content": PROMPT}],
            )
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        echoed = str(resp.get("model") or "")
        details = {"request_model": model, "response_model": echoed}
        if echoed and echoed != model and DATE_STAMP_RE.search(echoed):
            details["evaluation_zh"] = "响应 model 为服务端决定的日期戳快照名,与请求名不同。"
            return self._result("pass", 100.0, details)
        details["evaluation_zh"] = "响应 model 原样回显或无日期戳,无鉴别力,仅作 INFO。"
        return self._result("skip", 0.0, {**details, "skip_reason": "echo-not-discriminative"})
