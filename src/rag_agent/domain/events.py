"""Eventos de dominio del stream.

Son semánticos, no de transporte: no llevan `sequence_number` ni formato SSE.
El adaptador de entrada los traduce a la secuencia canónica del contrato §4.
Esto permite que la misma ejecución alimente el modo streaming y el no
streaming sin duplicar la orquestación.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import AgentError
from .items import AgentResponse, KnowledgeSearchItem, MessageItem


@dataclass(frozen=True)
class ResponseStarted:
    response_id: str
    model: str
    created_at: int


@dataclass(frozen=True)
class RetrievalStarted:
    item: KnowledgeSearchItem


@dataclass(frozen=True)
class RetrievalCompleted:
    item: KnowledgeSearchItem


@dataclass(frozen=True)
class MessageStarted:
    item_id: str


@dataclass(frozen=True)
class TextDelta:
    item_id: str
    delta: str


@dataclass(frozen=True)
class MessageCompleted:
    item: MessageItem


@dataclass(frozen=True)
class ResponseCompleted:
    response: AgentResponse


@dataclass(frozen=True)
class ResponseFailed:
    error: AgentError


DomainEvent = (
    ResponseStarted
    | RetrievalStarted
    | RetrievalCompleted
    | MessageStarted
    | TextDelta
    | MessageCompleted
    | ResponseCompleted
    | ResponseFailed
)
