"""Caso de uso `CreateResponse` con puertos dobles: sin HTTP y sin AWS."""

from __future__ import annotations

import pytest

from luis_cv.application.commands import CreateResponseCommand
from luis_cv.application.create_response import CreateResponse
from luis_cv.application.ports import ModelDescriptor
from luis_cv.domain import events as ev
from luis_cv.domain.conversation import Conversation, GenerationSettings, Role, ToolChoice, Turn
from luis_cv.domain.errors import AgentError, ErrorType
from luis_cv.domain.items import ItemStatus, KnowledgeSearchItem, MessageItem
from luis_cv.domain.retrieval import Chunk
from luis_cv.infrastructure.outbound.model_catalog import StaticModelCatalog

from ..support.fakes import (
    FrozenClock,
    RecordingTelemetry,
    ScriptedLanguageModel,
    SequentialIds,
    StubKnowledgeBase,
)

CHUNKS = (Chunk("titulo-2019.md", "Título de Ingeniería expedido en 2019.", 0.91, {"anio": 2019}),)


def construir(**overrides) -> CreateResponse:
    base = {
        "catalog": StaticModelCatalog(
            {"agente-rag-sonnet": ModelDescriptor("agente-rag-sonnet", "anthropic.claude-sonnet-5")}
        ),
        "knowledge_base": StubKnowledgeBase(chunks=CHUNKS),
        "language_model": ScriptedLanguageModel(script=["Título ", "de ", "Ingeniería."]),
        "clock": FrozenClock(),
        "ids": SequentialIds(),
        "telemetry": RecordingTelemetry(),
    }
    base.update(overrides)
    return CreateResponse(**base)


def comando(pregunta: str = "¿Está titulado?", **settings) -> CreateResponseCommand:
    return CreateResponseCommand(
        model_alias="agente-rag-sonnet",
        conversation=Conversation((Turn(Role.USER, pregunta),)),
        settings=GenerationSettings(**settings),
        request_id="req-1",
    )


async def recolectar(caso: CreateResponse, cmd: CreateResponseCommand) -> list:
    return [evento async for evento in caso.stream(cmd)]


async def test_el_flujo_emite_los_eventos_de_dominio_en_orden():
    eventos = await recolectar(construir(), comando())

    tipos = [type(e).__name__ for e in eventos]
    assert tipos[0] == "ResponseStarted"
    assert tipos[1:4] == ["RetrievalStarted", "RetrievalCompleted", "MessageStarted"]
    assert tipos[-1] == "ResponseCompleted"
    assert tipos.count("TextDelta") >= 1


async def test_el_item_de_recuperacion_pasa_de_in_progress_a_completed():
    eventos = await recolectar(construir(), comando())

    iniciado = next(e for e in eventos if isinstance(e, ev.RetrievalStarted)).item
    completado = next(e for e in eventos if isinstance(e, ev.RetrievalCompleted)).item

    assert iniciado.id == completado.id
    assert iniciado.status is ItemStatus.IN_PROGRESS
    assert completado.status is ItemStatus.COMPLETED
    assert completado.outcome.chunks == CHUNKS
    assert iniciado.outcome.queries == completado.outcome.queries


async def test_el_modo_no_streaming_es_la_misma_ejecucion_agregada():
    caso = construir()
    eventos = await recolectar(caso, comando())
    respuesta = await construir().execute(comando())

    texto_streaming = "".join(e.delta for e in eventos if isinstance(e, ev.TextDelta))
    assert respuesta.output_text == texto_streaming
    assert [type(i) for i in respuesta.output] == [KnowledgeSearchItem, MessageItem]
    assert respuesta.usage.output_tokens == 3


async def test_el_prompt_recibe_los_fragmentos_recuperados():
    modelo = ScriptedLanguageModel(script=["ok"])
    await recolectar(construir(language_model=modelo), comando())

    assert "titulo-2019.md" in modelo.calls[0]
    assert "Título de Ingeniería expedido en 2019." in modelo.calls[0]


async def test_tool_choice_none_omite_la_recuperacion():
    kb = StubKnowledgeBase(chunks=CHUNKS)
    eventos = await recolectar(
        construir(knowledge_base=kb), comando(tool_choice=ToolChoice.NONE)
    )

    assert kb.queries_seen == []
    assert not any(isinstance(e, ev.RetrievalStarted) for e in eventos)


async def test_un_alias_desconocido_falla_antes_de_emitir_nada():
    caso = construir()
    cmd = CreateResponseCommand(
        model_alias="inexistente", conversation=Conversation((Turn(Role.USER, "x"),))
    )

    with pytest.raises(AgentError) as exc:
        await recolectar(caso, cmd)

    assert exc.value.code == "model_not_found"


async def test_un_fallo_de_recuperacion_termina_en_response_failed():
    caso = construir(knowledge_base=StubKnowledgeBase(error=RuntimeError("boom")))

    eventos = await recolectar(caso, comando())

    fallo = eventos[-1]
    assert isinstance(fallo, ev.ResponseFailed)
    assert fallo.error.type is ErrorType.SERVER_ERROR
    assert "boom" not in fallo.error.message, "el detalle interno no llega al cliente"


async def test_un_fallo_del_modelo_conserva_su_tipo():
    caso = construir(language_model=ScriptedLanguageModel(script=["a", "b"], fail_after=1))

    eventos = await recolectar(caso, comando())

    assert isinstance(eventos[-1], ev.ResponseFailed)
    assert eventos[-1].error.type is ErrorType.MODEL_ERROR


async def test_execute_propaga_el_error_para_que_el_adaptador_lo_mapee():
    caso = construir(language_model=ScriptedLanguageModel(script=["a"], fail_after=0))

    with pytest.raises(AgentError) as exc:
        await caso.execute(comando())

    assert exc.value.type is ErrorType.MODEL_ERROR


async def test_los_identificadores_se_enmascaran_en_los_deltas():
    caso = construir(
        language_model=ScriptedLanguageModel(script=["La cédula es ", "12345", "678 y está vigente."])
    )

    eventos = await recolectar(caso, comando())
    texto = "".join(e.delta for e in eventos if isinstance(e, ev.TextDelta))

    assert "12345678" not in texto
    assert "****5678" in texto


async def test_la_telemetria_mide_ambos_tramos_y_no_registra_el_turno():
    telemetria = RecordingTelemetry()

    await recolectar(construir(telemetry=telemetria), comando("¿Cuál es su cédula?"))

    assert telemetria.spans == ["retrieval", "inference"]
    completada = telemetria.find("response.completed")
    assert completada["chunks_retrieved"] == 1
    assert "¿Cuál es su cédula?" not in str(telemetria.events)


async def test_afirmar_sin_citar_se_registra_como_fallo_de_fundamentacion():
    telemetria = RecordingTelemetry()
    caso = construir(
        language_model=ScriptedLanguageModel(script=["Sí, está titulado."]), telemetry=telemetria
    )

    await recolectar(caso, comando())

    assert any(nombre == "grounding.failure" for nombre, _ in telemetria.warnings)
    assert telemetria.find("response.completed")["grounded"] is False
