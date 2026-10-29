"""Azure OpenAI api-version 敏感性探针(行为层)。

经典 Azure 端点 `.../deployments/<部署名>/chat/completions?api-version=<版本>`
中 api-version 参与路由:非法值会导致路由失败。标准 OpenAI 与多数中转会忽略该参数。

判据(收紧版):
  - 合法值不是 200 → NA,本轮作废(渠道掉线/限流会伪装成分化)
  - 合法 200 且非法值报错、错误文本含 api-version 等 Azure 措辞 → PASS
  - 合法 200 且非法值与合法值状态不同但无 Azure 措辞 → INFO(不作正面证据)
  - 三者一致 → INFO(新版 /openai/v1/ 端点或网关忽略参数,不能判假)
"""

from __future__ import annotations

from ..client import OpenAIAPIError
from .base import ActiveDetector

LEGAL_VERSION = "2024-06-01"
ILLEGAL_VERSION = "1900-01-01"
AZURE_MARKERS = ("api-version", "deployment", "azure")
PROMPT = "Reply with exactly: pong"


class AzureApiVersionDetector(ActiveDetector):
    name = "azure_api_version"
    display_name = "Azure api-version 敏感性"
    weight = 15.0
    category = "azure"
    description = "非法 api-version 是否引发 Azure 式路由失败(行为层)"

    async def run(self, client, model: str):
        try:
            legal_status, legal_body = await _probe(client, model, LEGAL_VERSION)
            illegal_status, illegal_body = await _probe(client, model, ILLEGAL_VERSION)
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        details = {
            "legal_version": LEGAL_VERSION,
            "legal_status": legal_status,
            "illegal_version": ILLEGAL_VERSION,
            "illegal_status": illegal_status,
            "illegal_body": illegal_body[:300],
        }

        if legal_status != 200:
            details["evaluation_zh"] = "合法 api-version 请求未返回 200,本轮结果作废(NA)。"
            return self._result("skip", 0.0, {**details, "skip_reason": "legal-not-200"})

        if illegal_status == 200:
            details["evaluation_zh"] = "非法 api-version 被忽略,无分化;不能据此判假。"
            return self._result("skip", 0.0, {**details, "skip_reason": "no-divergence"})

        body_lower = illegal_body.lower()
        if any(marker in body_lower for marker in AZURE_MARKERS):
            details["evaluation_zh"] = "非法 api-version 导致路由失败且错误含 Azure 措辞,参数参与下游路由。"
            return self._result("pass", 100.0, details)

        details["evaluation_zh"] = "非法 api-version 状态有分化但无 Azure 措辞,可能来自负载或超时,仅作 INFO。"
        return self._result("skip", 0.0, {**details, "skip_reason": "divergence-without-azure-wording"})


async def _probe(client, model: str, version: str) -> tuple[int, str]:
    try:
        await client.chat_completions_create(
            model=model,
            max_completion_tokens=16,
            messages=[{"role": "user", "content": PROMPT}],
            query_params={"api-version": version},
        )
    except OpenAIAPIError as e:
        return e.status, e.body or ""
    return 200, ""
