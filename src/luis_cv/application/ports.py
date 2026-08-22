"""Puertos: lo que el núcleo necesita del mundo, expresado sin nombrarlo.

Ninguna firma menciona Bedrock, boto3, FastAPI ni HTTP. Cambiar de proveedor de
recuperación o de inferencia es escribir otro adaptador, no tocar el núcleo.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.conversation import Conversation, GenerationSettings
from ..domain.retrieval import RetrievalOutcome


@dataclass(frozen=True)
class ModelDescriptor:
    """Alias público resuelto a un modelo concreto del proveedor.

    `supports_sampling` no es un detalle cosmético: las familias más recientes
    dejaron de aceptar `temperature` y responden con un error de validación si
    se envía. Es una capacidad del modelo, así que viaja con el modelo y no
    dispersa condicionales por el adaptador.
    """

    alias: str
    provider_model_id: str
    family: str = "anthropic"
    supports_sampling: bool = True


@runtime_checkable
class ModelCatalogPort(Protocol):
    def resolve(self, alias: str) -> ModelDescriptor:
        """Devuelve el modelo del alias o lanza `AgentError` model_not_found."""

    def aliases(self) -> tuple[str, ...]: ...

    async def is_available(self) -> bool:
        """Los alias configurados tienen acceso concedido (readiness)."""


@runtime_checkable
class KnowledgeBasePort(Protocol):
    async def retrieve(self, queries: Sequence[str], *, top_k: int = 6) -> RetrievalOutcome: ...

    async def is_available(self) -> bool: ...


@dataclass(frozen=True)
class TextChunk:
    delta: str


@dataclass(frozen=True)
class UsageReport:
    input_tokens: int = 0
    output_tokens: int = 0


LanguageModelChunk = TextChunk | UsageReport


@runtime_checkable
class LanguageModelPort(Protocol):
    def stream(
        self,
        *,
        model: ModelDescriptor,
        system_prompt: str,
        conversation: Conversation,
        settings: GenerationSettings,
    ) -> AsyncIterator[LanguageModelChunk]:
        """Emite deltas de texto y, al final, el consumo de tokens."""

    async def is_available(self) -> bool: ...


@runtime_checkable
class ClockPort(Protocol):
    def unix_seconds(self) -> int: ...

    def monotonic_ms(self) -> float: ...


@runtime_checkable
class IdGeneratorPort(Protocol):
    def response_id(self) -> str: ...

    def item_id(self, prefix: str) -> str: ...


@runtime_checkable
class TelemetryPort(Protocol):
    def event(self, name: str, /, **fields: object) -> None:
        """Registra un evento estructurado. Nunca recibe texto del turno."""

    def warning(self, name: str, /, **fields: object) -> None: ...

    def span(self, name: str, /, **fields: object) -> AbstractAsyncContextManager[None]:
        """Subsegmento de traza: `retrieval` e `inference` van separados."""
