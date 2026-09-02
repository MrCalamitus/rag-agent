"""Casos D del contrato §9 — operación.

D3 (subsegmentos en X-Ray) y D4 (correlación en CloudWatch) se verifican en el
despliegue; aquí se prueba lo que los hace posibles y es verificable en local:
que los tramos de recuperación e inferencia se midan por separado y que todo
error lleve un `request_id` correlacionable.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest

from rag_agent.infrastructure.container import build_container
from rag_agent.infrastructure.inbound.http.app import create_app
from rag_agent.infrastructure.outbound.knowledge_bases import SingleKnowledgeBase
from rag_agent.infrastructure.outbound.local.corpus_knowledge_base import LocalCorpusKnowledgeBase
from rag_agent.infrastructure.outbound.local.grounded_stub_model import GroundedStubLanguageModel
from rag_agent.infrastructure.outbound.telemetry.structured import StructuredTelemetry

from ..conftest import CORPUS, build_client
from ..support.fakes import FrozenClock, SequentialIds, StubKnowledgeBase
from ..support.live import running_app

pytestmark = pytest.mark.operation

RUTA = "/v1/responses"
PREGUNTA = {"model": "agente-rag-sonnet", "input": "¿Cuál es su número de cédula profesional?"}


def test_d1_healthz_sin_autenticacion(client):
    respuesta = client.get("/healthz")

    assert respuesta.status_code == 200
    assert respuesta.json()["status"] == "ok"


def test_d2_readyz_con_recuperacion_inalcanzable_devuelve_503(settings, telemetry):
    contenedor = build_container(
        settings,
        knowledge_base=StubKnowledgeBase(available=False),
        language_model=GroundedStubLanguageModel(),
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=telemetry,
    )
    respuesta = build_client(contenedor).get("/readyz")

    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert cuerpo["status"] == "not_ready"
    assert cuerpo["checks"]["knowledge_base"] is False


def test_d2b_readyz_ok_cuando_todo_responde(client):
    respuesta = client.get("/readyz")

    assert respuesta.status_code == 200
    assert respuesta.json()["checks"] == {
        "model_catalog": True,
        "knowledge_base": True,
        "inference": True,
    }


def test_d3_tramos_de_recuperacion_e_inferencia_se_miden_por_separado(client, auth, telemetry):
    client.post(RUTA, json=PREGUNTA, headers=auth)

    assert telemetry.spans == ["retrieval", "inference"]
    assert telemetry.find("retrieval.completed")["latency_ms"] is not None
    assert "ttft_ms" in telemetry.find("response.completed")


def test_d4_el_error_devuelve_un_request_id_correlacionable(client, auth):
    respuesta = client.post(RUTA, json={"model": "no-existe", "input": "x"}, headers=auth)

    request_id = respuesta.json()["error"]["request_id"]
    assert request_id
    assert respuesta.headers["x-request-id"] == request_id


def test_d4b_el_request_id_del_cliente_se_respeta(client, auth):
    respuesta = client.post(
        RUTA, json=PREGUNTA, headers={**auth, "X-Request-Id": "correlacion-123"}
    )

    assert respuesta.headers["x-request-id"] == "correlacion-123"


def test_d5_ningun_log_contiene_pii_ni_el_texto_del_turno(settings, caplog):
    """Se verifica leyendo los logs reales, no asumiendo (plan E6)."""
    logger = logging.getLogger("rag_agent")
    contenedor = build_container(
        settings,
        knowledge_base=LocalCorpusKnowledgeBase(CORPUS),
        language_model=GroundedStubLanguageModel(),
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=StructuredTelemetry(logger),
    )
    cliente = build_client(contenedor)

    with caplog.at_level(logging.DEBUG, logger="rag_agent"):
        respuesta = cliente.post(
            RUTA,
            json=PREGUNTA,
            headers={"Authorization": "Bearer token-de-prueba", "Content-Type": "application/json"},
        )
    assert respuesta.status_code == 200

    registros = "\n".join(r.getMessage() for r in caplog.records)
    assert registros, "la petición debe dejar rastro estructurado"
    for filtracion in ("12345678", PREGUNTA["input"], "cédula profesional es", "token-de-prueba"):
        assert filtracion not in registros, f"filtración en logs: {filtracion!r}"
    # Lo que sí debe haber: identificadores, contadores y latencias.
    eventos = [json.loads(linea) for linea in registros.splitlines() if linea.startswith("{")]
    completada = next(e for e in eventos if e["event"] == "response.completed")
    assert completada["answer_fingerprint"] and completada["request_id"]
    assert "text" not in completada


def test_d7_limite_de_tasa(client, auth):
    """La petición 21 en un minuto → 429 (rate_limit_per_minute = 20)."""
    codigos = [
        client.post(RUTA, json={"model": "agente-rag-sonnet", "input": "hola"}, headers=auth).status_code
        for _ in range(21)
    ]

    assert codigos[:20] == [200] * 20
    assert codigos[20] == 429


def test_d7b_el_error_de_tasa_esta_bien_formado(client, auth):
    for _ in range(21):
        respuesta = client.post(RUTA, json={"model": "agente-rag-sonnet", "input": "x"}, headers=auth)

    error = respuesta.json()["error"]
    assert error["type"] == "too_many_requests"
    assert error["code"] == "rate_limit_exceeded"


@pytest.mark.slow
def test_d6_diez_streams_simultaneos_sin_degradar_el_ttft(settings, telemetry, auth):
    import asyncio

    contenedor = build_container(
        settings,
        knowledge_base=LocalCorpusKnowledgeBase(CORPUS),
        language_model=GroundedStubLanguageModel(delta_delay_ms=5),
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=telemetry,
    )
    contenedor.settings.rate_limit_per_minute = 100
    app = create_app(contenedor)
    app.state.rate_limiter.limit = 100

    async def un_stream(cliente: httpx.AsyncClient, base: str) -> tuple[float, bool]:
        inicio = time.perf_counter()
        ttft = None
        completado = False
        async with cliente.stream(
            "POST",
            f"{base}{RUTA}",
            json={**PREGUNTA, "stream": True},
            headers=auth,
            timeout=30,
        ) as respuesta:
            async for linea in respuesta.aiter_lines():
                if ttft is None and "response.output_text.delta" in linea:
                    ttft = time.perf_counter() - inicio
                if "response.completed" in linea:
                    completado = True
        return ttft or float("inf"), completado

    async def correr(base: str) -> list[tuple[float, bool]]:
        async with httpx.AsyncClient() as cliente:
            return await asyncio.gather(*(un_stream(cliente, base) for _ in range(10)))

    with running_app(app) as base:
        resultados = asyncio.run(correr(base))

    assert all(completado for _, completado in resultados)
    peor = max(ttft for ttft, _ in resultados)
    assert peor < 2.0, f"el peor TTFT con 10 streams simultáneos fue {peor:.3f}s (§8)"


async def test_la_sonda_de_readiness_esta_acotada(settings, telemetry):
    """Una sonda que tarda más que el balanceador no informa: produce un 504.

    El límite vive en el caso de uso, no en cada adaptador: un adaptador nuevo
    que olvide su timeout no puede dejar la sonda colgada.
    """
    import asyncio
    import time

    from rag_agent.application.check_readiness import CheckReadiness

    class KbQueNuncaResponde:
        async def retrieve(self, queries, *, top_k=6):  # pragma: no cover
            raise NotImplementedError

        async def is_available(self) -> bool:
            await asyncio.sleep(30)
            return True

    caso = CheckReadiness(
        catalog=build_container(
            settings,
            knowledge_base=StubKnowledgeBase(),
            language_model=GroundedStubLanguageModel(),
            clock=FrozenClock(),
            ids=SequentialIds(),
            telemetry=telemetry,
        ).catalog,
        knowledge_bases=SingleKnowledgeBase(KbQueNuncaResponde()),
        language_model=GroundedStubLanguageModel(),
        timeout_s=0.2,
    )

    inicio = time.perf_counter()
    reporte = await caso()
    transcurrido = time.perf_counter() - inicio

    assert transcurrido < 1.0, f"la sonda tardó {transcurrido:.1f}s pese al límite"
    assert reporte.ready is False
    assert reporte.knowledge_base is False, "la dependencia lenta se marca no disponible"
    assert reporte.inference is True, "las demás comprobaciones no se contaminan"


async def test_una_dependencia_que_revienta_no_propaga_la_excepcion():
    from rag_agent.application.check_readiness import CheckReadiness

    class KbRota:
        async def retrieve(self, queries, *, top_k=6):  # pragma: no cover
            raise NotImplementedError

        async def is_available(self) -> bool:
            raise RuntimeError("boom")

    caso = CheckReadiness(
        catalog=StubKnowledgeBase(),  # cualquier objeto con is_available sirve
        knowledge_bases=SingleKnowledgeBase(KbRota()),
        language_model=GroundedStubLanguageModel(),
    )

    reporte = await caso()

    assert reporte.knowledge_base is False
