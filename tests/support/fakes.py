"""Dobles de prueba: implementan los puertos, no parchean el código.

Que las pruebas de contrato usen estos adaptadores es el argumento práctico de
la arquitectura hexagonal en este proyecto: la suite verifica el emisor de
eventos, el enmascarado y el ciclo de vida de los ítems sin AWS de por medio,
y los mismos casos vuelven a correr contra el despliegue cambiando el
contenedor, no las pruebas.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from rag_agent.application.ports import (
    LanguageModelChunk,
    ModelDescriptor,
    TextChunk,
    UsageReport,
)
from rag_agent.domain.conversation import Conversation, GenerationSettings
from rag_agent.domain.errors import AgentError, model_error
from rag_agent.domain.retrieval import Chunk, RetrievalOutcome


@dataclass
class ScriptedLanguageModel:
    """Emite un guion fijo. Opcionalmente falla a mitad (caso B9)."""

    script: list[str] = field(default_factory=lambda: ["Hola", " mundo", "."])
    fail_after: int | None = None
    delay_s: float = 0.0
    error: AgentError | None = None
    calls: list[str] = field(default_factory=list)

    async def stream(
        self,
        *,
        model: ModelDescriptor,
        system_prompt: str,
        conversation: Conversation,
        settings: GenerationSettings,
    ) -> AsyncIterator[LanguageModelChunk]:
        self.calls.append(system_prompt)
        for indice, pieza in enumerate(self.script):
            if self.fail_after is not None and indice >= self.fail_after:
                raise self.error or model_error()
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield TextChunk(delta=pieza)
        yield UsageReport(input_tokens=11, output_tokens=len(self.script))

    async def is_available(self) -> bool:
        return True


@dataclass
class StubKnowledgeBase:
    """Devuelve fragmentos fijos, o falla, según lo que pida la prueba."""

    chunks: tuple[Chunk, ...] = ()
    latency_ms: int = 7
    available: bool = True
    error: Exception | None = None
    queries_seen: list[tuple[str, ...]] = field(default_factory=list)

    async def retrieve(self, queries: Sequence[str], *, top_k: int = 6) -> RetrievalOutcome:
        self.queries_seen.append(tuple(queries))
        if self.error:
            raise self.error
        return RetrievalOutcome(
            queries=tuple(queries), chunks=self.chunks[:top_k], latency_ms=self.latency_ms
        )

    async def is_available(self) -> bool:
        return self.available


@dataclass
class FrozenClock:
    seconds: int = 1_700_000_000
    ms: float = 0.0
    step_ms: float = 5.0

    def unix_seconds(self) -> int:
        return self.seconds

    def monotonic_ms(self) -> float:
        self.ms += self.step_ms
        return self.ms


@dataclass
class SequentialIds:
    counter: int = 0

    def response_id(self) -> str:
        self.counter += 1
        return f"resp_test_{self.counter:03d}"

    def item_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}_test_{self.counter:03d}"


@dataclass
class RecordingTelemetry:
    events: list[tuple[str, dict]] = field(default_factory=list)
    warnings: list[tuple[str, dict]] = field(default_factory=list)
    spans: list[str] = field(default_factory=list)

    def event(self, name: str, /, **fields: object) -> None:
        self.events.append((name, dict(fields)))

    def warning(self, name: str, /, **fields: object) -> None:
        self.warnings.append((name, dict(fields)))

    @asynccontextmanager
    async def span(self, name: str, /, **fields: object) -> AsyncIterator[None]:
        self.spans.append(name)
        yield

    def names(self) -> list[str]:
        return [nombre for nombre, _ in self.events]

    def find(self, name: str) -> dict | None:
        for nombre, campos in self.events:
            if nombre == name:
                return campos
        return None
