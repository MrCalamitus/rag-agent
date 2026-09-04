"""Caso de uso: crear una respuesta fundamentada.

Una sola orquestación alimenta los dos modos del contrato. `stream()` emite
eventos de dominio; `execute()` los consume y arma la respuesta completa. Si
los dos modos divergieran, el modo no streaming dejaría de ser una vista del
mismo comportamiento y las pruebas de contrato dejarían de decir algo.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..domain import events as ev
from ..domain.conversation import ToolChoice
from ..domain.errors import AgentError, ErrorType, invalid_request, model_error
from ..domain.items import (
    AgentResponse,
    ItemStatus,
    KnowledgeSearchItem,
    MessageItem,
    OutputItem,
    ResponseStatus,
    Usage,
)
from ..domain.profile import Profile
from ..domain.prompts import build_system_prompt
from ..domain.query_planning import plan_queries
from ..domain.redaction import StreamingRedactor, fingerprint
from ..domain.retrieval import RetrievalOutcome, aplicar_exposicion
from .commands import CreateResponseCommand
from .ports import (
    ClockPort,
    DocumentLinkPort,
    IdGeneratorPort,
    KnowledgeBasePort,
    KnowledgeBaseRegistryPort,
    LanguageModelPort,
    ModelCatalogPort,
    ProfileRegistryPort,
    TelemetryPort,
    TextChunk,
    UsageReport,
)


class CreateResponse:
    def __init__(
        self,
        *,
        catalog: ModelCatalogPort,
        profiles: ProfileRegistryPort,
        knowledge_bases: KnowledgeBaseRegistryPort,
        language_model: LanguageModelPort,
        clock: ClockPort,
        ids: IdGeneratorPort,
        telemetry: TelemetryPort,
        document_links: DocumentLinkPort | None = None,
    ) -> None:
        self._catalog = catalog
        self._profiles = profiles
        self._knowledge_bases = knowledge_bases
        self._language_model = language_model
        self._clock = clock
        self._ids = ids
        self._telemetry = telemetry
        self._document_links = document_links

    async def stream(self, command: CreateResponseCommand) -> AsyncIterator[ev.DomainEvent]:
        # Resolver el alias antes de emitir nada: un alias inválido debe poder
        # convertirse en un 400 con cabeceras, no en un error a medio stream.
        model = self._catalog.resolve(command.model_alias)
        # El perfil decide las reglas, cuánta evidencia se recupera y qué se
        # enmascara. Resolverlo antes de emitir nada hace que un slug inválido
        # sea un 400 con cabeceras, igual que un alias inválido.
        profile = self._profiles.resolve(command.profile_slug)
        if command.settings.temperature is not None and not model.supports_sampling:
            # Ignorarlo en silencio sería mentir sobre lo que el servidor hizo
            # con la petición; el contrato prefiere fallar de forma ruidosa.
            raise invalid_request(
                f"El modelo '{command.model_alias}' no acepta 'temperature'.",
                param="temperature",
                code="temperature_not_supported",
            )

        response_id = self._ids.response_id()
        created_at = self._clock.unix_seconds()
        yield ev.ResponseStarted(response_id=response_id, model=command.model_alias, created_at=created_at)

        output: list[OutputItem] = []
        usage = Usage()
        try:
            outcome = RetrievalOutcome(queries=(), chunks=(), latency_ms=0)
            if command.settings.tool_choice is not ToolChoice.NONE:
                item_id = self._ids.item_id("ks")
                queries = plan_queries(command.conversation.last_user_text)
                yield ev.RetrievalStarted(
                    item=KnowledgeSearchItem(
                        id=item_id,
                        outcome=RetrievalOutcome(queries=queries, chunks=(), latency_ms=0),
                        status=ItemStatus.IN_PROGRESS,
                    )
                )
                outcome = await self._retrieve(profile, queries)
                completed = KnowledgeSearchItem(id=item_id, outcome=outcome, status=ItemStatus.COMPLETED)
                output.append(completed)
                yield ev.RetrievalCompleted(item=completed)

            message_id = self._ids.item_id("msg")
            yield ev.MessageStarted(item_id=message_id)

            system_prompt = build_system_prompt(
                command.conversation,
                outcome.chunks,
                profile=profile,
                instructions=command.instructions,
                reveal_identifiers=command.settings.reveal_identifiers,
            )
            redactor = StreamingRedactor(
                reveal=command.settings.reveal_identifiers, policy=profile.redaction
            )
            texto: list[str] = []
            started = self._clock.monotonic_ms()
            ttft: float | None = None

            async with self._telemetry.span("inference", model=model.provider_model_id):
                async for chunk in self._language_model.stream(
                    model=model,
                    system_prompt=system_prompt,
                    conversation=command.conversation,
                    settings=command.settings,
                ):
                    if isinstance(chunk, UsageReport):
                        usage = Usage(input_tokens=chunk.input_tokens, output_tokens=chunk.output_tokens)
                        continue
                    if not isinstance(chunk, TextChunk) or not chunk.delta:
                        continue
                    if ttft is None:
                        ttft = self._clock.monotonic_ms() - started
                    seguro = redactor.feed(chunk.delta)
                    if seguro:
                        texto.append(seguro)
                        yield ev.TextDelta(item_id=message_id, delta=seguro)

            cola = redactor.flush()
            if cola:
                texto.append(cola)
                yield ev.TextDelta(item_id=message_id, delta=cola)

            final = "".join(texto)
            message = MessageItem(id=message_id, text=final, status=ItemStatus.COMPLETED)
            output.append(message)
            yield ev.MessageCompleted(item=message)

            self._telemetry.event(
                "response.completed",
                request_id=command.request_id,
                response_id=response_id,
                model=command.model_alias,
                profile=profile.slug,
                chunks_retrieved=len(outcome.chunks),
                ttft_ms=round(ttft or 0.0, 1),
                total_ms=round(self._clock.monotonic_ms() - started, 1),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                answer_fingerprint=fingerprint(final),
                grounded=_is_grounded(final, outcome, profile),
            )
            if not _is_grounded(final, outcome, profile):
                self._telemetry.warning(
                    "grounding.failure",
                    request_id=command.request_id,
                    response_id=response_id,
                    profile=profile.slug,
                    chunks_retrieved=len(outcome.chunks),
                )

            yield ev.ResponseCompleted(
                response=AgentResponse(
                    id=response_id,
                    model=command.model_alias,
                    created_at=created_at,
                    output=tuple(output),
                    usage=usage,
                    status=ResponseStatus.COMPLETED,
                    metadata=dict(command.settings.metadata),
                )
            )
        except AgentError as exc:
            self._telemetry.event(
                "response.failed",
                request_id=command.request_id,
                response_id=response_id,
                error_type=exc.type.value,
                code=exc.code,
            )
            yield ev.ResponseFailed(error=exc)
        except Exception as exc:  # noqa: BLE001 - frontera: nada interno sale al cliente
            self._telemetry.event(
                "response.failed",
                request_id=command.request_id,
                response_id=response_id,
                error_type=ErrorType.SERVER_ERROR.value,
                exception=type(exc).__name__,
            )
            yield ev.ResponseFailed(
                error=AgentError(
                    message="Ocurrió un fallo interno al generar la respuesta.",
                    type=ErrorType.SERVER_ERROR,
                    code="internal_error",
                )
            )

    async def execute(self, command: CreateResponseCommand) -> AgentResponse:
        """Modo no streaming: misma orquestación, agregada."""
        async for event in self.stream(command):
            if isinstance(event, ev.ResponseCompleted):
                return event.response
            if isinstance(event, ev.ResponseFailed):
                raise event.error
        raise model_error("El flujo terminó sin producir una respuesta.")

    async def _retrieve(self, profile: Profile, queries: tuple[str, ...]) -> RetrievalOutcome:
        if not queries:
            return RetrievalOutcome(queries=(), chunks=(), latency_ms=0)
        knowledge_base: KnowledgeBasePort = self._knowledge_bases.for_profile(profile)
        started = self._clock.monotonic_ms()
        async with self._telemetry.span("retrieval", profile=profile.slug, queries=len(queries)):
            outcome = await knowledge_base.retrieve(queries, top_k=profile.retrieval.top_k)
        elapsed = int(self._clock.monotonic_ms() - started)
        self._telemetry.event(
            "retrieval.completed",
            profile=profile.slug,
            queries=len(outcome.queries),
            chunks=len(outcome.chunks),
            documents=len(outcome.documents()),
            latency_ms=outcome.latency_ms or elapsed,
        )
        # La política del tema decide qué documentos puede consultar el usuario.
        # Se aplica aquí, sobre la clase que la ingesta estampó, y no en el
        # adaptador HTTP: si viviera allí, cada transporte nuevo tendría que
        # reimplementarla y alguno se olvidaría.
        outcome = aplicar_exposicion(
            outcome,
            profile.documents,
            link=(lambda doc: self._document_links.link_for(profile, doc))
            if self._document_links
            else None,
        )
        if outcome.latency_ms:
            return outcome
        return RetrievalOutcome(queries=outcome.queries, chunks=outcome.chunks, latency_ms=elapsed)


def _is_grounded(answer: str, outcome: RetrievalOutcome, profile: Profile) -> bool:
    """Una respuesta está fundamentada si cita un documento o declina.

    No prueba veracidad —eso lo hace la evaluación con preguntas de oro— pero
    detecta el modo de falla barato: afirmar sin citar teniendo evidencia.
    """
    if profile.decline_phrase.lower().rstrip(".") in answer.lower():
        return True
    if outcome.is_empty:
        return False
    # Sobre `citations()` y no sobre `documents()`: es lo que el prompt pide
    # citar. Comprobarlo contra los nombres internos daría por no fundamentada
    # una respuesta que cita el PDF correctamente.
    return any(f"[{doc}]" in answer for doc in outcome.citations())
