"""Evidencia documental: el material con el que el agente puede afirmar algo."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Chunk:
    """Fragmento recuperado, con su procedencia."""

    document_id: str
    text: str
    score: float
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalOutcome:
    """Recibo de una recuperación: qué se preguntó, qué volvió y cuánto tardó."""

    queries: tuple[str, ...]
    chunks: tuple[Chunk, ...]
    latency_ms: int

    @property
    def is_empty(self) -> bool:
        return len(self.chunks) == 0

    def documents(self) -> tuple[str, ...]:
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk.document_id not in seen:
                seen.append(chunk.document_id)
        return tuple(seen)
