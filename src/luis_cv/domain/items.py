"""Ítems de salida y respuesta final.

Todo ítem lleva `id`, `type` y `status` (contrato §4). El ítem de recuperación
es una extensión con el prefijo del slug del implementador: es el recibo que
hace auditable la respuesta sin abrir un log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .retrieval import RetrievalOutcome

IMPLEMENTOR_SLUG = "agente"
KNOWLEDGE_SEARCH_TYPE = f"{IMPLEMENTOR_SLUG}:knowledge_search"


class ItemStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


class ResponseStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class KnowledgeSearchItem:
    id: str
    outcome: RetrievalOutcome
    status: ItemStatus = ItemStatus.COMPLETED

    type: str = field(default=KNOWLEDGE_SEARCH_TYPE, init=False)


@dataclass(frozen=True)
class MessageItem:
    id: str
    text: str
    status: ItemStatus = ItemStatus.COMPLETED
    role: str = "assistant"

    type: str = field(default="message", init=False)


OutputItem = KnowledgeSearchItem | MessageItem


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class AgentResponse:
    """Respuesta completa. No se persiste: `store: false` es postura, no ajuste."""

    id: str
    model: str
    created_at: int
    output: tuple[OutputItem, ...]
    usage: Usage
    status: ResponseStatus = ResponseStatus.COMPLETED
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def output_text(self) -> str:
        return "".join(item.text for item in self.output if isinstance(item, MessageItem))
