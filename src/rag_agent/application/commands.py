"""Comandos de entrada al núcleo, ya normalizados por el adaptador."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.conversation import Conversation, GenerationSettings


@dataclass(frozen=True)
class CreateResponseCommand:
    model_alias: str
    conversation: Conversation
    settings: GenerationSettings = field(default_factory=GenerationSettings)
    instructions: str | None = None
    request_id: str = ""
    # Tema sobre el que responder. `None` = el perfil por defecto del
    # despliegue. Un mismo servicio atiende varios temas, así que esto no es
    # configuración: es parte de la petición.
    profile_slug: str | None = None
