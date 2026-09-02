"""La petición conversacional, ya normalizada y libre de transporte."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"


class ToolChoice(str, Enum):
    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"


@dataclass(frozen=True)
class Turn:
    role: Role
    text: str


@dataclass(frozen=True)
class Conversation:
    """Historial completo del cliente (el servidor es sin estado, contrato §7)."""

    turns: tuple[Turn, ...]

    @property
    def system_turns(self) -> tuple[Turn, ...]:
        return tuple(t for t in self.turns if t.role in (Role.SYSTEM, Role.DEVELOPER))

    @property
    def dialogue(self) -> tuple[Turn, ...]:
        """Turnos de usuario/asistente, en orden."""
        return tuple(t for t in self.turns if t.role in (Role.USER, Role.ASSISTANT))

    @property
    def last_user_text(self) -> str:
        for turn in reversed(self.dialogue):
            if turn.role is Role.USER:
                return turn.text
        return ""


@dataclass(frozen=True)
class GenerationSettings:
    max_output_tokens: int | None = None
    temperature: float | None = None
    tool_choice: ToolChoice = ToolChoice.AUTO
    reveal_identifiers: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
