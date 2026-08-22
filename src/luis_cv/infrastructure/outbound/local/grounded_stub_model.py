"""Modelo de inferencia local, determinista y estrictamente fundamentado.

**Sustituye a Bedrock mientras la ingesta del RAG está pendiente.** No genera
lenguaje: compone la respuesta a partir de los fragmentos recuperados y declina
cuando la pregunta menciona algo que no aparece en ellos. Es deliberadamente
tonto, y por eso sirve: hace que las pruebas de contrato midan el emisor de
eventos, el enmascarado y la orquestación, y no la suerte de un modelo.

Lo que este adaptador **no** valida es veracidad de un modelo real: eso lo
prueba la evaluación con preguntas de oro contra Bedrock (plan E7).
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import AsyncIterator

from ....application.ports import (
    LanguageModelChunk,
    ModelDescriptor,
    TextChunk,
    UsageReport,
)
from ....domain.conversation import Conversation, GenerationSettings
from ....domain.prompts import DECLINE_PHRASE
from ....domain.query_planning import condense

_FRAGMENTOS = re.compile(r"^\[(?P<doc>[^\]]+)\](?P<meta>[^\n]*)\n(?P<texto>.*?)(?=\n---\n|\Z)", re.S | re.M)
_ORACION = re.compile(r"[^\n.;]+[.;]?")
_DISTINTIVO = 4


_PALABRA = re.compile(r"[\w]+", re.UNICODE)


def _fold(text: str) -> str:
    d = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _tokens(text: str) -> set[str]:
    """Palabras sin puntuación: 'profesional:' y 'profesional' son la misma."""
    return set(_PALABRA.findall(_fold(text)))


class GroundedStubLanguageModel:
    def __init__(self, *, delta_delay_ms: float = 0.0) -> None:
        self._delay = delta_delay_ms / 1000.0

    async def stream(
        self,
        *,
        model: ModelDescriptor,
        system_prompt: str,
        conversation: Conversation,
        settings: GenerationSettings,
    ) -> AsyncIterator[LanguageModelChunk]:
        pregunta = conversation.last_user_text
        fragmentos = _parse_fragments(system_prompt)
        texto = _compose(pregunta, fragmentos)

        emitidos = 0
        for pieza in _split_deltas(texto):
            if self._delay:
                await asyncio.sleep(self._delay)
            emitidos += 1
            yield TextChunk(delta=pieza)
        yield UsageReport(
            input_tokens=len(system_prompt.split()) + len(pregunta.split()),
            output_tokens=emitidos,
        )

    async def is_available(self) -> bool:
        return True


def _parse_fragments(system_prompt: str) -> list[tuple[str, str]]:
    bloque = system_prompt.split("FRAGMENTOS:", 1)
    if len(bloque) < 2 or "(ninguno)" in bloque[1][:20]:
        return []
    return [(m.group("doc"), m.group("texto").strip()) for m in _FRAGMENTOS.finditer(bloque[1])]


def _compose(pregunta: str, fragmentos: list[tuple[str, str]]) -> str:
    if not fragmentos:
        return DECLINE_PHRASE

    consulta = _tokens(condense(pregunta))
    cuerpo = _fold(" ".join(texto for _, texto in fragmentos))
    faltantes = {t for t in consulta if len(t) >= _DISTINTIVO and t not in cuerpo}
    if faltantes:
        # La pregunta menciona algo que no aparece en la evidencia: se niega.
        return DECLINE_PHRASE

    partes: list[str] = []
    for doc, texto in fragmentos[:2]:
        oracion = _mejor_oracion(consulta, texto)
        if oracion:
            partes.append(f"{oracion.rstrip('.')} [{doc}].")
    if not partes:
        return DECLINE_PHRASE
    return "Según los documentos disponibles: " + " ".join(partes)


def _mejor_oracion(consulta: set[str], texto: str) -> str:
    oraciones = [o.strip() for o in _ORACION.findall(texto) if o.strip()]
    if not oraciones:
        return ""
    def puntaje(oracion: str) -> int:
        return len(consulta & _tokens(oracion))
    mejor = max(oraciones, key=puntaje)
    return mejor if puntaje(mejor) else oraciones[0]


def _split_deltas(texto: str) -> list[str]:
    """Trocea como lo haría un modelo: palabras sueltas, espacios incluidos."""
    piezas = re.findall(r"\S+\s*", texto)
    return piezas or ([texto] if texto else [])
