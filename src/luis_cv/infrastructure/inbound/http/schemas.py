"""Esquema de petición (contrato §2) y su traducción al dominio.

Pydantic valida la forma; las reglas de producto —`store: true` se rechaza,
la entrada multimodal se rechaza— se expresan como errores de dominio para que
lleven `param` y `code` exactos, no un 422 genérico.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ....application.commands import CreateResponseCommand
from ....domain.conversation import Conversation, GenerationSettings, Role, ToolChoice, Turn
from ....domain.errors import AgentError, invalid_request, store_not_supported

_TEXT_PARTS = {"input_text", "output_text", "text"}


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    text: str | None = None


class InputItem(BaseModel):
    """Un ítem de la transcripción reenviada por el cliente.

    El servidor es sin estado, así que en cada turno el cliente reproduce todo
    lo anterior — **incluidos los ítems que este servidor emitió**, como
    `agente:knowledge_search`. El spec lo exige: un ítem de herramienta
    hospedada debe permitir «lossless rehydration on followup request».

    Por eso `type` es libre y `content` opcional: solo los `message` se
    convierten en turnos; el resto se ignora al reconstruir la conversación,
    porque la recuperación se vuelve a ejecutar con la pregunta nueva. Rechazar
    esos ítems rompía el segundo turno de cualquier cliente conforme.
    """

    model_config = ConfigDict(extra="ignore")

    type: str = "message"
    role: str | None = None
    content: str | list[ContentPart] | None = None

    @property
    def es_mensaje(self) -> bool:
        return self.type == "message" and self.content is not None


class CreateResponseRequest(BaseModel):
    # Tolerancia hacia adelante: los campos desconocidos se ignoran y se
    # registran en WARN (contrato §2, regla 5).
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    model: str
    # Cadena, un ítem suelto o una lista. Los clientes reales mandan las tres
    # formas; aceptarlas no relaja ninguna regla y evita un 400 inútil.
    input: str | InputItem | list[InputItem]
    instructions: str | None = None
    stream: bool = False
    store: bool = False
    max_output_tokens: Annotated[int, Field(gt=0)] | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    tool_choice: Literal["auto", "none", "required"] = "auto"
    truncation: Literal["auto", "disabled"] = "disabled"
    metadata: dict[str, str] = Field(default_factory=dict)
    # Extensión: petición explícita de identificadores completos (contrato §6.2).
    # Solo tiene efecto sobre una petición ya autenticada.
    reveal_identifiers: bool = False

    def to_command(self, *, request_id: str) -> CreateResponseCommand:
        if self.store:
            raise store_not_supported()
        return CreateResponseCommand(
            model_alias=self.model,
            conversation=self._conversation(),
            instructions=self.instructions,
            settings=GenerationSettings(
                max_output_tokens=self.max_output_tokens,
                temperature=self.temperature,
                tool_choice=ToolChoice(self.tool_choice),
                reveal_identifiers=self.reveal_identifiers,
                metadata=dict(self.metadata),
            ),
            request_id=request_id,
        )

    def _conversation(self) -> Conversation:
        if isinstance(self.input, InputItem):
            return self._from_items([self.input])
        if isinstance(self.input, str):
            texto = self.input.strip()
            if not texto:
                raise invalid_request("El campo 'input' no puede estar vacío.", param="input")
            return Conversation(turns=(Turn(role=Role.USER, text=texto),))

        if not self.input:
            raise invalid_request("El campo 'input' no puede estar vacío.", param="input")
        return self._from_items(list(self.input))

    def _from_items(self, items: list[InputItem]) -> Conversation:
        turnos: list[Turn] = []
        for indice, item in enumerate(items):
            if not item.es_mensaje:
                # Ítem rehidratado (recuperación previa, razonamiento, etc.):
                # forma parte de la transcripción, no del diálogo.
                continue
            turnos.append(Turn(role=_role(item.role or "user", indice), text=_text(item, indice)))
        if not any(t.role in (Role.USER, Role.ASSISTANT) for t in turnos):
            raise invalid_request(
                "'input' debe incluir al menos un turno de usuario.", param="input"
            )
        return Conversation(turns=tuple(turnos))


def _role(valor: str, indice: int) -> Role:
    try:
        return Role(valor)
    except ValueError:
        raise invalid_request(
            f"Rol no soportado: '{valor}'.",
            param=f"input[{indice}].role",
            code="unsupported_role",
        ) from None


def _text(mensaje: InputItem, indice: int) -> str:
    if isinstance(mensaje.content, str):
        return mensaje.content
    if mensaje.content is None:  # pragma: no cover - filtrado por `es_mensaje`
        return ""
    piezas: list[str] = []
    for j, parte in enumerate(mensaje.content):
        if parte.type not in _TEXT_PARTS:
            # El corpus es texto; la entrada multimodal se rechaza de forma
            # explícita en vez de ignorarse en silencio (contrato §0).
            raise invalid_request(
                f"Tipo de contenido no soportado: '{parte.type}'. El agente solo acepta texto.",
                param=f"input[{indice}].content[{j}].type",
                code="unsupported_content_type",
            )
        piezas.append(parte.text or "")
    return "".join(piezas)


def unknown_fields(raw: dict[str, Any]) -> list[str]:
    conocidos = set(CreateResponseRequest.model_fields)
    return sorted(k for k in raw if k not in conocidos)


# Ramas de la unión de `input`. Pydantic reporta un fallo por rama, y quedarse
# con el primero produce mensajes inútiles del tipo "input.str: debe ser una
# cadena" cuando el cliente mandó una lista bien intencionada pero mal formada.
_RAMAS_DE_UNION = ("str", "list[InputItem]", "InputItem", "function-after")


def param_from_validation_error(error: Any) -> tuple[str | None, str]:
    """Extrae `param` y un mensaje accionable del fallo de Pydantic.

    Ante una unión se descarta el ruido de las ramas que ni siquiera coincidían
    en tipo y se prefiere el error más profundo: es el que apunta al campo que
    de verdad está mal.
    """
    detalles = error.errors()
    if not detalles:
        return None, "La petición no cumple el esquema."

    utiles = [d for d in detalles if not _es_ruido_de_union(d)] or detalles
    elegido = max(utiles, key=lambda d: len(d.get("loc", ())))
    loc = ".".join(str(p) for p in elegido.get("loc", ()) if str(p) not in _RAMAS_DE_UNION)
    mensaje = elegido.get("msg", "valor inválido")
    return (loc or None), (f"Campo '{loc}': {mensaje}." if loc else f"{mensaje}.")


def _es_ruido_de_union(detalle: dict) -> bool:
    """Un fallo de tipo en la raíz de una rama de unión no dice nada del error real."""
    loc = detalle.get("loc", ())
    return len(loc) == 2 and str(loc[1]) in _RAMAS_DE_UNION and detalle.get("type", "").endswith("_type")


def as_agent_error(error: Any) -> AgentError:
    param, mensaje = param_from_validation_error(error)
    return invalid_request(mensaje, param=param, code="schema_violation")
