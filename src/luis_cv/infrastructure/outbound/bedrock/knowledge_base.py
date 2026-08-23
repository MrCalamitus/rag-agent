"""Recuperación sobre Bedrock Knowledge Base (plan E3).

Se usa `retrieve`, no `retrieve_and_generate`: la segunda se queda con el
control del prompt y del formato de eventos, que es justamente lo que el
núcleo ya construyó. El resultado se devuelve como evidencia con procedencia,
que es lo que el ítem `agente:knowledge_search` publica al cliente.

La ingesta del corpus a esta KB está pendiente (E2–E3); el adaptador ya está
escrito para que activarlo sea cambiar configuración, no código.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from ....domain.errors import AgentError, ErrorType
from ....domain.retrieval import Chunk, RetrievalOutcome

_SOURCE_KEYS = ("x-amz-bedrock-kb-source-uri", "source", "document_id")


class BedrockKnowledgeBase:
    def __init__(
        self,
        *,
        knowledge_base_id: str,
        region: str,
        profile: str | None = None,
        min_score: float = 0.0,
        client: Any | None = None,
    ) -> None:
        self._kb_id = knowledge_base_id
        self._region = region
        self._profile = profile
        self._min_score = min_score
        self._client = client

    async def retrieve(self, queries: Sequence[str], *, top_k: int = 6) -> RetrievalOutcome:
        import anyio

        inicio = time.perf_counter()
        try:
            respuestas = [
                await anyio.to_thread.run_sync(self._retrieve_one, query, top_k) for query in queries
            ]
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - frontera con boto3
            raise AgentError(
                message="La base de conocimiento no pudo atender la consulta.",
                type=ErrorType.SERVER_ERROR,
                code="retrieval_failure",
            ) from exc

        # `retrieve` siempre devuelve `numberOfResults` fragmentos, ordenados
        # por score pero sin filtrar: un saludo recupera seis documentos igual
        # que una pregunta legítima. Sin piso de relevancia, cada turno paga el
        # prompt completo y el modelo recibe evidencia que no viene al caso.
        mejores: dict[str, Chunk] = {}
        for resultados in respuestas:
            for chunk in resultados:
                if chunk.score < self._min_score:
                    continue
                previo = mejores.get(chunk.document_id)
                if previo is None or chunk.score > previo.score:
                    mejores[chunk.document_id] = chunk
        ordenados = tuple(sorted(mejores.values(), key=lambda c: c.score, reverse=True)[:top_k])
        return RetrievalOutcome(
            queries=tuple(queries),
            chunks=ordenados,
            latency_ms=int((time.perf_counter() - inicio) * 1000),
        )

    async def is_available(self) -> bool:
        import anyio

        try:
            await anyio.to_thread.run_sync(self._retrieve_one, "ping", 1)
        except Exception:  # noqa: BLE001 - readiness no propaga detalle
            return False
        return True

    # -- interno --------------------------------------------------------
    def _boto(self) -> Any:
        if self._client is None:
            from .clients import RETRIEVAL, build_client

            self._client = build_client(
                "bedrock-agent-runtime", region=self._region, profile=self._profile, config=RETRIEVAL
            )
        return self._client

    def _retrieve_one(self, query: str, top_k: int) -> list[Chunk]:
        respuesta = self._boto().retrieve(
            knowledgeBaseId=self._kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k}},
        )
        return [_to_chunk(item) for item in respuesta.get("retrievalResults", [])]


def _to_chunk(item: dict[str, Any]) -> Chunk:
    metadata = dict(item.get("metadata") or {})
    return Chunk(
        document_id=_document_id(item, metadata),
        text=(item.get("content") or {}).get("text", ""),
        score=float(item.get("score") or 0.0),
        metadata={k: v for k, v in metadata.items() if not k.startswith("x-amz-")},
    )


def _document_id(item: dict[str, Any], metadata: dict[str, Any]) -> str:
    location = item.get("location") or {}
    uri = (location.get("s3Location") or {}).get("uri")
    if not uri:
        for key in _SOURCE_KEYS:
            if metadata.get(key):
                uri = str(metadata[key])
                break
    if not uri:
        return "desconocido"
    # El nombre del archivo es lo que el agente cita; que sea legible importa.
    return uri.rstrip("/").rsplit("/", 1)[-1] or uri
