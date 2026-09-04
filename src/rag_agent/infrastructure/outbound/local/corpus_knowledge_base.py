"""Recuperación local sobre `corpus/` — sustituto de la Bedrock KB.

**Estado: la ingesta a Bedrock Knowledge Base está pendiente (plan E2–E3).**
Este adaptador implementa el mismo puerto que el definitivo, de modo que el
núcleo, el contrato y la suite ya corren de punta a punta hoy. Cuando la KB
exista, se cambia el adaptador en el contenedor de dependencias y nada más.

Estrategia de fragmentación: **un documento = un fragmento**, la decisión de
E2. Títulos, cédulas y constancias son cortos por naturaleza; partirlos en
trozos de 300 tokens rompe la relación entre puesto, institución y fecha.

Puntuación: solapamiento de palabras de contenido normalizadas sin acentos.
No pretende igualar a un vector store; pretende ser honesta y determinista
para que las pruebas de contrato midan el pipeline, no el ranking.

Los conjuntos de palabras se calculan una sola vez al cargar. Con el corpus de
credenciales daba igual —cuatro documentos—, pero uno de folletos son más de mil
fragmentos y retokenizarlos en cada consulta convertía cada pregunta del chat
local en casi un segundo de CPU.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from ....domain.query_planning import condense
from ....domain.retrieval import Chunk, RetrievalOutcome

_TEXT_SUFFIXES = {".md", ".txt", ".markdown"}
_MIN_SCORE = 0.12


def _fold(text: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def _tokens(text: str) -> set[str]:
    return set(_fold(condense(text)).split())


class LocalCorpusKnowledgeBase:
    def __init__(self, corpus_dir: Path | str, *, min_score: float = _MIN_SCORE) -> None:
        self._dir = Path(corpus_dir)
        self._min_score = min_score
        self._cache: tuple[tuple[Chunk, frozenset[str]], ...] | None = None

    # -- puerto ---------------------------------------------------------
    async def retrieve(self, queries: Sequence[str], *, top_k: int = 6) -> RetrievalOutcome:
        documentos = self._documents()
        puntuados: dict[str, tuple[float, Chunk]] = {}
        for query in queries:
            objetivo = _tokens(query)
            if not objetivo:
                continue
            for chunk, palabras in documentos:
                score = self._score(objetivo, palabras)
                if score < self._min_score:
                    continue
                previo = puntuados.get(chunk.document_id)
                if previo is None or score > previo[0]:
                    puntuados[chunk.document_id] = (score, chunk)

        mejores = sorted(puntuados.values(), key=lambda par: par[0], reverse=True)[:top_k]
        chunks = tuple(
            Chunk(
                document_id=chunk.document_id,
                text=chunk.text,
                score=round(score, 4),
                metadata=chunk.metadata,
            )
            for score, chunk in mejores
        )
        return RetrievalOutcome(queries=tuple(queries), chunks=chunks, latency_ms=0)

    async def is_available(self) -> bool:
        return self._dir.is_dir()

    # -- interno --------------------------------------------------------
    def _score(self, objetivo: set[str], palabras: frozenset[str]) -> float:
        if not palabras:
            return 0.0
        return len(objetivo & palabras) / len(objetivo)

    def _documents(self) -> tuple[tuple[Chunk, frozenset[str]], ...]:
        """Fragmentos con su bolsa de palabras ya calculada."""
        if self._cache is not None:
            return self._cache
        indexados: list[tuple[Chunk, frozenset[str]]] = []
        if self._dir.is_dir():
            for ruta in sorted(self._dir.rglob("*")):
                if not ruta.is_file() or ruta.suffix.lower() not in _TEXT_SUFFIXES:
                    continue
                if ruta.name.endswith(".metadata.json"):
                    continue
                texto = ruta.read_text(encoding="utf-8").strip()
                chunk = Chunk(
                    document_id=ruta.name,
                    text=texto,
                    score=0.0,
                    metadata=self._metadata(ruta),
                )
                # El nombre del archivo cuenta como contenido: en un corpus de
                # fichas, «hilux» aparece en el título mucho antes que en el cuerpo.
                nombre = chunk.document_id.replace("-", " ").replace("_", " ")
                indexados.append((chunk, frozenset(_tokens(texto) | _tokens(nombre))))
        self._cache = tuple(indexados)
        return self._cache

    def _metadata(self, ruta: Path) -> dict[str, object]:
        lateral = ruta.with_suffix(ruta.suffix + ".metadata.json")
        if not lateral.is_file():
            return {}
        try:
            crudo = json.loads(lateral.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        # Formato de Bedrock KB o diccionario plano; se acepta cualquiera.
        atributos = crudo.get("metadataAttributes", crudo) if isinstance(crudo, dict) else {}
        return {k: v for k, v in atributos.items() if isinstance(k, str)}

    def invalidate(self) -> None:
        self._cache = None
