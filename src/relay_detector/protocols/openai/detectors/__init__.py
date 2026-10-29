"""OpenAI Chat Completions detector registry."""

from .base import ActiveDetector, BaseDetector, PassiveDetector
from .azure_api_version import AzureApiVersionDetector
from .azure_content_filter import AzureContentFilterDetector
from .azure_model_echo import AzureModelEchoDetector
from .basic_request import BasicRequestDetector
from .function_calling import FunctionCallingDetector
from .integrity import IntegrityDetector
from .long_context import LongContextDetector, LongContextExtremeDetector
from .model_consistency import ModelConsistencyDetector
from .protocol import ProtocolDetector
from .structured_output import StructuredOutputDetector
from .token_billing import TokenBillingDetector


def build_all() -> list[BaseDetector]:
    return [
        BasicRequestDetector(),
        ModelConsistencyDetector(),
        FunctionCallingDetector(),
        StructuredOutputDetector(),
        ProtocolDetector(),
        IntegrityDetector(),
        TokenBillingDetector(),
        LongContextDetector(),
        LongContextExtremeDetector(),
        AzureApiVersionDetector(),
        AzureContentFilterDetector(),
        AzureModelEchoDetector(),
    ]


__all__ = [
    "BaseDetector",
    "ActiveDetector",
    "PassiveDetector",
    "BasicRequestDetector",
    "ModelConsistencyDetector",
    "FunctionCallingDetector",
    "StructuredOutputDetector",
    "ProtocolDetector",
    "IntegrityDetector",
    "TokenBillingDetector",
    "LongContextDetector",
    "LongContextExtremeDetector",
    "AzureApiVersionDetector",
    "AzureContentFilterDetector",
    "AzureModelEchoDetector",
    "build_all",
]
