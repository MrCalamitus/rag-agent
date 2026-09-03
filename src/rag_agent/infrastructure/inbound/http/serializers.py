"""Traducción del dominio a la carga útil de Open Responses."""

from __future__ import annotations

from typing import Any

from ....domain.errors import AgentError
from ....domain.items import (
    AgentResponse,
    KnowledgeSearchItem,
    MessageItem,
    OutputItem,
    ResponseStatus,
    Usage,
)


def item_to_dict(item: OutputItem) -> dict[str, Any]:
    if isinstance(item, KnowledgeSearchItem):
        return {
            "type": item.type,
            "id": item.id,
            "status": item.status.value,
            "queries": list(item.outcome.queries),
            "results": [
                {
                    "document_id": chunk.document_id,
                    "chunk": chunk.text,
                    "score": chunk.score,
                    "metadata": chunk.metadata,
                    # Extensión del ítem: si el perfil deja consultar el
                    # documento original de este fragmento. El cliente no tiene
                    # que conocer la política para saberlo.
                    "exposed": chunk.exposed,
                }
                for chunk in item.outcome.chunks
            ],
            "latency_ms": item.outcome.latency_ms,
        }
    if isinstance(item, MessageItem):
        return {
            "type": item.type,
            "id": item.id,
            "status": item.status.value,
            "role": item.role,
            "content": [{"type": "output_text", "text": item.text, "annotations": []}],
        }
    raise TypeError(f"Ítem de salida no serializable: {type(item)!r}")


def usage_to_dict(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


def response_to_dict(response: AgentResponse) -> dict[str, Any]:
    return {
        "id": response.id,
        "object": "response",
        "created_at": response.created_at,
        "status": response.status.value,
        "model": response.model,
        "store": False,
        "output": [item_to_dict(item) for item in response.output],
        "usage": usage_to_dict(response.usage),
        "metadata": response.metadata,
    }


def response_skeleton(
    response_id: str,
    model: str,
    created_at: int,
    *,
    status: ResponseStatus = ResponseStatus.IN_PROGRESS,
    output: list[dict[str, Any]] | None = None,
    error: AgentError | None = None,
) -> dict[str, Any]:
    """Objeto Response para los eventos de ciclo de vida del stream."""
    payload: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status.value,
        "model": model,
        "store": False,
        "output": output or [],
        "usage": usage_to_dict(Usage()),
        "metadata": {},
    }
    if error is not None:
        payload["error"] = error_to_dict(error)["error"]
    return payload


def error_to_dict(error: AgentError, *, request_id: str | None = None) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "message": error.message,
        "type": error.type.value,
        "param": error.param,
        "code": error.code,
    }
    if request_id:
        cuerpo["request_id"] = request_id
    return {"error": cuerpo}
