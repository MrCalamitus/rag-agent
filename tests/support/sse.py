"""Utilidades para leer un stream SSE en las pruebas de contrato."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class SseEvent:
    name: str | None
    raw: str

    @property
    def data(self) -> dict:
        return json.loads(self.raw)

    @property
    def is_done(self) -> bool:
        return self.raw.strip() == "[DONE]"


def parse_sse(texto: str) -> list[SseEvent]:
    eventos: list[SseEvent] = []
    nombre: str | None = None
    datos: list[str] = []
    for linea in texto.split("\n"):
        if linea.startswith("event:"):
            nombre = linea[len("event:") :].strip()
        elif linea.startswith("data:"):
            datos.append(linea[len("data:") :].strip())
        elif linea == "":
            if datos:
                eventos.append(SseEvent(name=nombre, raw="\n".join(datos)))
            nombre, datos = None, []
    if datos:
        eventos.append(SseEvent(name=nombre, raw="\n".join(datos)))
    return eventos


def names(eventos: list[SseEvent]) -> list[str]:
    return [e.name for e in eventos if not e.is_done]


def of_type(eventos: list[SseEvent], tipo: str) -> list[SseEvent]:
    return [e for e in eventos if not e.is_done and e.data.get("type") == tipo]
