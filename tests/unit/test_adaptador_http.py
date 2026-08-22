"""Adaptador de entrada: normalización del esquema, emisor SSE y seguridad."""

from __future__ import annotations

import json

import pytest

from luis_cv.domain import events as ev
from luis_cv.domain.conversation import Role, ToolChoice
from luis_cv.domain.errors import AgentError, ErrorType
from luis_cv.domain.items import (
    AgentResponse,
    ItemStatus,
    KnowledgeSearchItem,
    MessageItem,
    ResponseStatus,
    Usage,
)
from luis_cv.domain.retrieval import Chunk, RetrievalOutcome
from luis_cv.infrastructure.inbound.http.schemas import CreateResponseRequest, unknown_fields
from luis_cv.infrastructure.inbound.http.security import TokenBucketRateLimiter, authenticate
from luis_cv.infrastructure.inbound.http.sse import OpenResponsesTranslator, format_sse


# -- esquema ------------------------------------------------------------
def test_input_como_cadena_se_normaliza_a_un_turno_de_usuario():
    peticion = CreateResponseRequest(model="agente-rag-sonnet", input="hola")

    comando = peticion.to_command(request_id="r")

    assert comando.conversation.turns == (comando.conversation.turns[0],)
    assert comando.conversation.turns[0].role is Role.USER
    assert comando.conversation.last_user_text == "hola"


def test_los_turnos_de_sistema_no_cuentan_como_dialogo():
    peticion = CreateResponseRequest(
        model="agente-rag-sonnet",
        input=[
            {"type": "message", "role": "developer", "content": "sé formal"},
            {"type": "message", "role": "user", "content": "¿está titulado?"},
        ],
    )

    conversacion = peticion.to_command(request_id="r").conversation

    assert len(conversacion.system_turns) == 1
    assert len(conversacion.dialogue) == 1
    assert conversacion.last_user_text == "¿está titulado?"


def test_store_true_es_un_error_de_dominio_con_param_exacto():
    peticion = CreateResponseRequest(model="agente-rag-sonnet", input="hola", store=True)

    with pytest.raises(AgentError) as exc:
        peticion.to_command(request_id="r")

    assert exc.value.param == "store"
    assert exc.value.type is ErrorType.INVALID_REQUEST


def test_input_vacio_se_rechaza():
    with pytest.raises(AgentError) as exc:
        CreateResponseRequest(model="agente-rag-sonnet", input="   ").to_command(request_id="r")

    assert exc.value.param == "input"


def test_rol_no_soportado_se_rechaza_con_su_ruta():
    peticion = CreateResponseRequest(
        model="agente-rag-sonnet",
        input=[{"type": "message", "role": "tool", "content": "x"}],
    )

    with pytest.raises(AgentError) as exc:
        peticion.to_command(request_id="r")

    assert exc.value.code == "unsupported_role"
    assert exc.value.param == "input[0].role"


def test_tool_choice_y_ajustes_llegan_al_comando():
    peticion = CreateResponseRequest(
        model="agente-rag-sonnet",
        input="hola",
        tool_choice="required",
        temperature=0.2,
        max_output_tokens=512,
        metadata={"caso": "demo-01"},
    )

    ajustes = peticion.to_command(request_id="r").settings

    assert ajustes.tool_choice is ToolChoice.REQUIRED
    assert (ajustes.temperature, ajustes.max_output_tokens) == (0.2, 512)
    assert ajustes.metadata == {"caso": "demo-01"}


def test_los_campos_desconocidos_se_detectan_para_registrarlos():
    assert unknown_fields({"model": "x", "input": "y", "raro": 1}) == ["raro"]


# -- emisor SSE ---------------------------------------------------------
def _eventos_de_dominio() -> list:
    outcome = RetrievalOutcome(
        queries=("cédula",), chunks=(Chunk("cedula.md", "texto", 0.8, {}),), latency_ms=12
    )
    ks = KnowledgeSearchItem(id="ks_1", outcome=outcome)
    mensaje = MessageItem(id="msg_1", text="Hola mundo.")
    return [
        ev.ResponseStarted(response_id="resp_1", model="agente-rag-sonnet", created_at=1),
        ev.RetrievalStarted(
            item=KnowledgeSearchItem(
                id="ks_1",
                outcome=RetrievalOutcome(queries=("cédula",), chunks=(), latency_ms=0),
                status=ItemStatus.IN_PROGRESS,
            )
        ),
        ev.RetrievalCompleted(item=ks),
        ev.MessageStarted(item_id="msg_1"),
        ev.TextDelta(item_id="msg_1", delta="Hola "),
        ev.TextDelta(item_id="msg_1", delta="mundo."),
        ev.MessageCompleted(item=mensaje),
        ev.ResponseCompleted(
            response=AgentResponse(
                id="resp_1",
                model="agente-rag-sonnet",
                created_at=1,
                output=(ks, mensaje),
                usage=Usage(10, 2),
            )
        ),
    ]


def _traducir(eventos) -> list[dict]:
    translator = OpenResponsesTranslator()
    return [payload for evento in eventos for payload in translator.translate(evento)]


def test_la_numeracion_es_monotonica_y_arranca_en_cero():
    salida = _traducir(_eventos_de_dominio())

    assert [p["sequence_number"] for p in salida] == list(range(len(salida)))


def test_los_indices_de_salida_siguen_el_orden_de_los_items():
    salida = _traducir(_eventos_de_dominio())

    ks = [p for p in salida if p.get("item", {}).get("type", "").endswith("knowledge_search")]
    mensajes = [p for p in salida if p.get("item", {}).get("type") == "message"]

    assert {p["output_index"] for p in ks} == {0}
    assert {p["output_index"] for p in mensajes} == {1}


def test_el_evento_de_error_no_choca_con_el_tipo_del_evento():
    """El objeto de error va anidado; si se aplanara rompería `event:` == `type`."""
    translator = OpenResponsesTranslator()
    translator.translate(_eventos_de_dominio()[0])

    salida = translator.translate(
        ev.ResponseFailed(error=AgentError("falló", type=ErrorType.MODEL_ERROR, code="x"))
    )

    assert [p["type"] for p in salida] == ["error", "response.failed"]
    assert salida[0]["error"]["type"] == "model_error"
    assert salida[1]["response"]["status"] == ResponseStatus.FAILED.value


def test_el_formato_sse_no_emite_campo_id():
    linea = format_sse("response.created", {"type": "response.created", "sequence_number": 0})

    assert linea.startswith("event: response.created\ndata: {")
    assert linea.endswith("\n\n")
    assert "id:" not in linea
    assert json.loads(linea.split("data: ", 1)[1])["type"] == "response.created"


def test_el_acento_no_se_escapa_en_la_carga_util():
    linea = format_sse("x", {"type": "x", "delta": "formación"})

    assert "formación" in linea


# -- seguridad ----------------------------------------------------------
@pytest.mark.parametrize(
    "cabecera,codigo",
    [
        (None, "missing_authorization"),
        ("", "missing_authorization"),
        ("Basic abc", "invalid_authorization_scheme"),
        ("Bearer otro", "invalid_token"),
    ],
)
def test_autenticacion_rechaza_lo_que_debe(cabecera, codigo):
    with pytest.raises(AgentError) as exc:
        authenticate(cabecera, "secreto")

    assert exc.value.code == codigo
    assert exc.value.type is ErrorType.AUTHENTICATION_ERROR


def test_autenticacion_acepta_el_token_correcto():
    authenticate("Bearer secreto", "secreto")
    authenticate("bearer secreto", "secreto")


def test_la_cubeta_permite_el_limite_y_frena_el_siguiente():
    cubeta = TokenBucketRateLimiter(limit=3)

    for _ in range(3):
        cubeta.check("cliente", now=100.0)
    with pytest.raises(AgentError) as exc:
        cubeta.check("cliente", now=100.0)

    assert exc.value.type is ErrorType.TOO_MANY_REQUESTS


def test_la_cubeta_se_recarga_con_el_tiempo():
    cubeta = TokenBucketRateLimiter(limit=60, window_s=60.0)
    for _ in range(60):
        cubeta.check("cliente", now=0.0)

    cubeta.check("cliente", now=1.01)  # un token recargado tras un segundo


def test_la_cubeta_separa_clientes():
    cubeta = TokenBucketRateLimiter(limit=1)
    cubeta.check("a", now=0.0)
    cubeta.check("b", now=0.0)
