"""Emisor SSE: eventos de dominio → secuencia canónica del contrato §4.

Invariantes que la suite verifica (casos B2–B6):

* `event:` es idéntico al `type` del cuerpo.
* No se usa el campo `id:` de SSE.
* `sequence_number` arranca en 0, es monotónico y no deja huecos.
* El evento terminal es la cadena literal `[DONE]`.
* Concatenar los `delta` reproduce exactamente el `text` de `output_text.done`.

El contador vive aquí y en ningún otro sitio: si la numeración se repartiera
entre varias capas, el hueco aparecería justo el día del despliegue.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ....domain import events as ev
from ....domain.items import ResponseStatus
from .serializers import error_to_dict, item_to_dict, response_skeleton, response_to_dict

DONE = "data: [DONE]\n\n"


def format_sse(event_type: str, payload: dict[str, Any]) -> str:
    cuerpo = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_type}\ndata: {cuerpo}\n\n"


class OpenResponsesTranslator:
    """Convierte eventos de dominio en eventos del protocolo, numerados."""

    def __init__(self) -> None:
        self._sequence = 0
        self._output_index = -1
        self._response_id = ""
        self._model = ""
        self._created_at = 0
        self._items: list[dict[str, Any]] = []

    def _emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        cuerpo = {"type": event_type, "sequence_number": self._sequence, **payload}
        self._sequence += 1
        return cuerpo

    def translate(self, event: ev.DomainEvent) -> list[dict[str, Any]]:
        if isinstance(event, ev.ResponseStarted):
            self._response_id = event.response_id
            self._model = event.model
            self._created_at = event.created_at
            esqueleto = response_skeleton(self._response_id, self._model, self._created_at)
            return [
                self._emit("response.created", {"response": esqueleto}),
                self._emit("response.in_progress", {"response": esqueleto}),
            ]

        if isinstance(event, ev.RetrievalStarted):
            self._output_index += 1
            return [
                self._emit(
                    "response.output_item.added",
                    {"output_index": self._output_index, "item": item_to_dict(event.item)},
                )
            ]

        if isinstance(event, ev.RetrievalCompleted):
            item = item_to_dict(event.item)
            self._items.append(item)
            return [
                self._emit(
                    "response.output_item.done",
                    {"output_index": self._output_index, "item": item},
                )
            ]

        if isinstance(event, ev.MessageStarted):
            self._output_index += 1
            item = {
                "type": "message",
                "id": event.item_id,
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            return [
                self._emit(
                    "response.output_item.added",
                    {"output_index": self._output_index, "item": item},
                ),
                self._emit(
                    "response.content_part.added",
                    {
                        "item_id": event.item_id,
                        "output_index": self._output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                ),
            ]

        if isinstance(event, ev.TextDelta):
            return [
                self._emit(
                    "response.output_text.delta",
                    {
                        "item_id": event.item_id,
                        "output_index": self._output_index,
                        "content_index": 0,
                        "delta": event.delta,
                    },
                )
            ]

        if isinstance(event, ev.MessageCompleted):
            item = item_to_dict(event.item)
            self._items.append(item)
            return [
                self._emit(
                    "response.output_text.done",
                    {
                        "item_id": event.item.id,
                        "output_index": self._output_index,
                        "content_index": 0,
                        "text": event.item.text,
                    },
                ),
                self._emit(
                    "response.content_part.done",
                    {
                        "item_id": event.item.id,
                        "output_index": self._output_index,
                        "content_index": 0,
                        "part": {
                            "type": "output_text",
                            "text": event.item.text,
                            "annotations": [],
                        },
                    },
                ),
                self._emit(
                    "response.output_item.done",
                    {"output_index": self._output_index, "item": item},
                ),
            ]

        if isinstance(event, ev.ResponseCompleted):
            return [self._emit("response.completed", {"response": response_to_dict(event.response)})]

        if isinstance(event, ev.ResponseFailed):
            # Ya se enviaron 200 y cabeceras: el error viaja como evento y
            # SIEMPRE va seguido de response.failed (contrato §5).
            fallida = response_skeleton(
                self._response_id,
                self._model,
                self._created_at,
                status=ResponseStatus.FAILED,
                output=self._items,
                error=event.error,
            )
            # El objeto de error va anidado: si se aplanara, su campo `type`
            # (model_error, …) chocaría con el `type` del evento y rompería la
            # invariante `event:` == `type` que verifica el caso B3.
            return [
                self._emit("error", error_to_dict(event.error)),
                self._emit("response.failed", {"response": fallida}),
            ]

        raise TypeError(f"Evento de dominio no traducible: {type(event)!r}")


async def encode(events: AsyncIterator[ev.DomainEvent]) -> AsyncIterator[str]:
    """Serializa el flujo completo, terminal `[DONE]` incluido."""
    translator = OpenResponsesTranslator()
    async for event in events:
        for payload in translator.translate(event):
            yield format_sse(payload["type"], payload)
    yield DONE
