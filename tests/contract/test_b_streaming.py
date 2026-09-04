"""Casos B del contrato §9 — transporte SSE y secuencia canónica del §4."""

from __future__ import annotations

import time

import httpx
import pytest

from rag_agent.domain.errors import model_error
from rag_agent.infrastructure.container import build_container
from rag_agent.infrastructure.inbound.http.app import create_app
from rag_agent.infrastructure.outbound.local.corpus_knowledge_base import LocalCorpusKnowledgeBase
from rag_agent.infrastructure.outbound.local.grounded_stub_model import GroundedStubLanguageModel

from ..conftest import CORPUS, build_client
from ..support.fakes import FrozenClock, RecordingTelemetry, ScriptedLanguageModel, SequentialIds
from ..support.live import running_app
from ..support.sse import names, of_type, parse_sse

pytestmark = pytest.mark.contract

RUTA = "/v1/responses"
PETICION = {
    "model": "agente-rag-sonnet",
    "stream": True,
    "input": "¿Tiene título de ingeniería?",
}

SECUENCIA_CANONICA = [
    "response.created",
    "response.in_progress",
    "response.output_item.added",  # agente:knowledge_search
    "response.output_item.done",  # fragmentos recuperados
    "response.output_item.added",  # message
    "response.content_part.added",
    # response.output_text.delta × N
    "response.output_text.done",
    "response.content_part.done",
    "response.output_item.done",
    "response.completed",
]


def _stream(client, auth, peticion=None) -> str:
    with client.stream("POST", RUTA, json=peticion or PETICION, headers=auth) as respuesta:
        assert respuesta.status_code == 200
        assert respuesta.headers["content-type"].startswith("text/event-stream")
        return "".join(respuesta.iter_text())


def test_b1_content_type_es_event_stream(client, auth):
    with client.stream("POST", RUTA, json=PETICION, headers=auth) as respuesta:
        assert respuesta.headers["content-type"].startswith("text/event-stream")
        assert respuesta.headers["cache-control"] == "no-cache"
        # Defensa contra proxies que acumulan la respuesta.
        assert respuesta.headers["x-accel-buffering"] == "no"
        respuesta.read()


def test_b2_orden_de_eventos_coincide_con_la_secuencia_canonica(client, auth):
    eventos = parse_sse(_stream(client, auth))
    tipos = [e.data["type"] for e in eventos if not e.is_done]
    sin_deltas = [t for t in tipos if t != "response.output_text.delta"]

    assert sin_deltas == SECUENCIA_CANONICA
    assert tipos.count("response.output_text.delta") >= 1

    ks = of_type(eventos, "response.output_item.added")[0].data["item"]
    assert ks["type"] == "agente:knowledge_search"
    assert ks["status"] == "in_progress"
    completado = of_type(eventos, "response.output_item.done")[0].data["item"]
    assert completado["status"] == "completed"
    assert completado["queries"]
    assert "latency_ms" in completado


def test_b3_campo_event_coincide_con_el_type_del_cuerpo(client, auth):
    eventos = parse_sse(_stream(client, auth))
    for evento in eventos:
        if evento.is_done:
            assert evento.name is None, "el terminal [DONE] no lleva campo event:"
            continue
        assert evento.name == evento.data["type"]


def test_b3b_no_se_usa_el_campo_id_de_sse(client, auth):
    crudo = _stream(client, auth)
    assert not any(linea.startswith("id:") for linea in crudo.split("\n"))


def test_b4_sequence_number_monotonico_sin_huecos_desde_cero(client, auth):
    eventos = parse_sse(_stream(client, auth))
    numeros = [e.data["sequence_number"] for e in eventos if not e.is_done]

    assert numeros == list(range(len(numeros)))


def test_b5_evento_terminal_es_done_literal(client, auth):
    crudo = _stream(client, auth)
    assert crudo.rstrip().endswith("data: [DONE]")
    assert parse_sse(crudo)[-1].is_done


def test_b6_concatenar_deltas_reproduce_el_texto_final(client, auth):
    eventos = parse_sse(_stream(client, auth))
    concatenado = "".join(e.data["delta"] for e in of_type(eventos, "response.output_text.delta"))
    final = of_type(eventos, "response.output_text.done")[0].data["text"]
    item = of_type(eventos, "response.output_item.done")[-1].data["item"]

    assert concatenado == final
    assert item["content"][0]["text"] == final
    completada = of_type(eventos, "response.completed")[0].data["response"]
    assert completada["output"][-1]["content"][0]["text"] == final


def test_b7_ttft_por_debajo_de_dos_segundos(client, auth):
    inicio = time.perf_counter()
    primer_delta = None
    with client.stream("POST", RUTA, json=PETICION, headers=auth) as respuesta:
        for linea in respuesta.iter_lines():
            if "response.output_text.delta" in linea and linea.startswith("data:"):
                primer_delta = time.perf_counter() - inicio
                break

    assert primer_delta is not None
    assert primer_delta < 2.0, f"TTFT local {primer_delta:.3f}s excede el presupuesto (§8)"


def test_b8_los_deltas_llegan_espaciados_no_todos_al_final(auth, settings, telemetry):
    """Sin buffering: la prueba que salva el despliegue.

    Corre contra un servidor real, no contra el cliente de pruebas: medir el
    espaciado sobre un transporte simulado no prueba nada. En local demuestra
    que la aplicación no acumula; la verificación que decide la entrega es la
    misma prueba contra el ALB (`make test-deployed`), que es donde el
    streaming se rompe en silencio.
    """
    contenedor = build_container(
        settings,
        knowledge_base=LocalCorpusKnowledgeBase(CORPUS),
        language_model=GroundedStubLanguageModel(delta_delay_ms=25),
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=telemetry,
    )

    marcas: list[float] = []
    with running_app(create_app(contenedor)) as base:
        inicio = time.perf_counter()
        with httpx.stream("POST", f"{base}{RUTA}", json=PETICION, headers=auth, timeout=30) as r:
            assert r.status_code == 200
            for linea in r.iter_lines():
                if linea.startswith("data:") and "response.output_text.delta" in linea:
                    marcas.append(time.perf_counter() - inicio)

    assert len(marcas) >= 3
    separacion = marcas[-1] - marcas[0]
    assert separacion > 0.05, f"todos los deltas llegaron juntos ({separacion*1000:.1f} ms)"
    assert marcas[0] < separacion, "el primer delta debe salir antes de que termine la respuesta"


def test_b9_fallo_a_media_respuesta_emite_error_y_response_failed(auth, settings, telemetry):
    contenedor = build_container(
        settings,
        knowledge_base=LocalCorpusKnowledgeBase(CORPUS),
        language_model=ScriptedLanguageModel(
            script=["Según", " el", " documento"], fail_after=2, error=model_error()
        ),
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=telemetry,
    )
    cliente = build_client(contenedor)

    with cliente.stream("POST", RUTA, json=PETICION, headers=auth) as respuesta:
        # Ya se enviaron 200 y cabeceras: el fallo no puede cambiar el estado.
        assert respuesta.status_code == 200
        eventos = parse_sse("".join(respuesta.iter_text()))

    tipos = names(eventos)
    assert "response.completed" not in tipos
    assert tipos[-2:] == ["error", "response.failed"]
    assert eventos[-1].is_done

    error = of_type(eventos, "error")[0].data["error"]
    assert error["type"] == "model_error"
    assert "boto" not in error["message"].lower()
    fallida = of_type(eventos, "response.failed")[0].data["response"]
    assert fallida["status"] == "failed"
    assert fallida["error"]["type"] == "model_error"


def test_b10_corte_del_cliente_cierra_el_flujo(auth, settings):
    """El servidor cancela la inferencia; no queda una tarea colgada."""
    telemetria = RecordingTelemetry()
    contenedor = build_container(
        settings,
        knowledge_base=LocalCorpusKnowledgeBase(CORPUS),
        language_model=GroundedStubLanguageModel(delta_delay_ms=10),
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=telemetria,
    )
    cliente = build_client(contenedor)

    with cliente.stream("POST", RUTA, json=PETICION, headers=auth) as respuesta:
        for linea in respuesta.iter_lines():
            if "response.output_text.delta" in linea:
                break  # el cliente se va a mitad del stream

    assert "stream.closed" in telemetria.names()


def test_alias_inexistente_en_streaming_devuelve_400_no_un_error_a_medio_stream(client, auth):
    respuesta = client.post(RUTA, json={**PETICION, "model": "no-existe"}, headers=auth)

    assert respuesta.status_code == 400
    assert respuesta.headers["content-type"].startswith("application/json")
    assert respuesta.json()["error"]["code"] == "model_not_found"


def test_tool_choice_none_no_emite_item_de_recuperacion(client, auth):
    eventos = parse_sse(_stream(client, auth, {**PETICION, "tool_choice": "none"}))
    tipos = [e.data["type"] for e in eventos if not e.is_done]
    items = [e.data["item"]["type"] for e in of_type(eventos, "response.output_item.added")]

    assert "agente:knowledge_search" not in items
    assert tipos[:3] == ["response.created", "response.in_progress", "response.output_item.added"]
