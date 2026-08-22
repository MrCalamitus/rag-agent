"""Inferencia con `bedrock-runtime:ConverseStream` (plan E1/E5).

`Converse` normaliza las diferencias entre familias de modelo, que es lo que
permite que el alias GPT y el alias Anthropic compartan este adaptador. El
guardrail se aplica aquí, en el borde con el proveedor, no en el núcleo.

El SDK entrega un iterador bloqueante; se consume evento a evento en un hilo
para no bloquear el bucle y para que el primer delta salga en cuanto exista —
si se acumulara, el streaming dejaría de existir en la práctica.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ....application.ports import (
    LanguageModelChunk,
    ModelDescriptor,
    TextChunk,
    UsageReport,
)
from ....domain.conversation import Conversation, Role
from ....domain.errors import AgentError, model_error


class BedrockLanguageModel:
    def __init__(
        self,
        *,
        region: str,
        profile: str | None = None,
        guardrail_id: str | None = None,
        guardrail_version: str = "DRAFT",
        client: Any | None = None,
    ) -> None:
        self._region = region
        self._profile = profile
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version
        self._client = client

    async def stream(
        self,
        *,
        model: ModelDescriptor,
        system_prompt: str,
        conversation: Conversation,
        settings,
    ) -> AsyncIterator[LanguageModelChunk]:
        import anyio

        kwargs = self._build_request(model, system_prompt, conversation, settings)
        try:
            respuesta = await anyio.to_thread.run_sync(lambda: self._boto().converse_stream(**kwargs))
            iterador = iter(respuesta["stream"])
            while True:
                evento = await anyio.to_thread.run_sync(lambda: next(iterador, None))
                if evento is None:
                    break
                for chunk in _translate(evento):
                    yield chunk
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - frontera con boto3
            raise model_error() from exc

    async def is_available(self) -> bool:
        import anyio

        def _probe() -> bool:
            try:
                self._boto().meta.service_model  # noqa: B018 - fuerza la creación del cliente
            except Exception:  # noqa: BLE001
                return False
            return True

        return await anyio.to_thread.run_sync(_probe)

    # -- interno --------------------------------------------------------
    def _boto(self) -> Any:
        if self._client is None:
            import boto3

            session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
            self._client = session.client("bedrock-runtime", region_name=self._region)
        return self._client

    def _build_request(self, model, system_prompt, conversation, settings) -> dict[str, Any]:
        mensajes = [
            {
                "role": "user" if turn.role is Role.USER else "assistant",
                "content": [{"text": turn.text}],
            }
            for turn in conversation.dialogue
            if turn.text.strip()
        ]
        config: dict[str, Any] = {}
        if settings.max_output_tokens:
            config["maxTokens"] = settings.max_output_tokens
        if settings.temperature is not None and model.supports_sampling:
            config["temperature"] = settings.temperature

        kwargs: dict[str, Any] = {
            "modelId": model.provider_model_id,
            "messages": mensajes,
            "system": [{"text": system_prompt}],
        }
        if config:
            kwargs["inferenceConfig"] = config
        if self._guardrail_id:
            kwargs["guardrailConfig"] = {
                "guardrailIdentifier": self._guardrail_id,
                "guardrailVersion": self._guardrail_version,
                "trace": "enabled",
            }
        return kwargs


def _translate(evento: dict[str, Any]) -> list[LanguageModelChunk]:
    if "contentBlockDelta" in evento:
        texto = (evento["contentBlockDelta"].get("delta") or {}).get("text")
        return [TextChunk(delta=texto)] if texto else []
    if "metadata" in evento:
        uso = evento["metadata"].get("usage") or {}
        return [
            UsageReport(
                input_tokens=int(uso.get("inputTokens") or 0),
                output_tokens=int(uso.get("outputTokens") or 0),
            )
        ]
    if "internalServerException" in evento or "modelStreamErrorException" in evento:
        raise model_error()
    if "throttlingException" in evento:
        from ....domain.errors import AgentError, ErrorType

        raise AgentError(
            message="El proveedor de inferencia está limitando la tasa de peticiones.",
            type=ErrorType.TOO_MANY_REQUESTS,
            code="upstream_throttled",
        )
    return []
