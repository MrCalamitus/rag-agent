"""La misma suite de aceptación, contra el despliegue real.

    BASE_URL=https://api.ejemplo API_TOKEN=... make test-deployed

Sin `BASE_URL` la suite se salta entera. Corre los casos que solo significan
algo atravesando el ALB: el transporte SSE (B1–B8), la autenticación en el
borde y el límite de tasa. B8 —sin buffering— es el que decide la entrega: en
local nunca falla, y es exactamente donde el streaming se rompe en silencio.
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
TOKEN = os.getenv("API_TOKEN", "")
MODELO = os.getenv("RAG_EVAL_MODEL", "agente-rag-sonnet")

pytestmark = [
    pytest.mark.deployed,
    pytest.mark.skipif(not BASE_URL, reason="define BASE_URL para correr contra el despliegue"),
]


@pytest.fixture(scope="module")
def cliente():
    with httpx.Client(base_url=BASE_URL, timeout=120) as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _eventos(respuesta) -> list[dict]:
    eventos = []
    for linea in respuesta.iter_lines():
        if linea.startswith("data:"):
            crudo = linea[5:].strip()
            if crudo != "[DONE]":
                eventos.append(json.loads(crudo))
    return eventos


def test_d1_healthz(cliente):
    assert cliente.get("/healthz").status_code == 200


def test_readyz_verifica_bedrock_y_la_kb(cliente):
    respuesta = cliente.get("/readyz")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["checks"]["knowledge_base"] is True


def test_a1_camino_feliz(cliente, auth):
    respuesta = cliente.post(
        "/v1/responses",
        json={"model": MODELO, "input": "¿Qué formación académica tiene?"},
        headers=auth,
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert [i["type"] for i in cuerpo["output"]][-1] == "message"
    assert all(i["id"] and i["status"] for i in cuerpo["output"])


def test_a3_sin_token(cliente):
    respuesta = cliente.post("/v1/responses", json={"model": MODELO, "input": "x"})

    assert respuesta.status_code == 401
    assert respuesta.json()["error"]["type"] == "authentication_error"


def test_a7_alias_inexistente(cliente, auth):
    respuesta = cliente.post("/v1/responses", json={"model": "gpt-5", "input": "x"}, headers=auth)

    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["code"] == "model_not_found"


def test_a8_store_true(cliente, auth):
    respuesta = cliente.post(
        "/v1/responses", json={"model": MODELO, "input": "x", "store": True}, headers=auth
    )

    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["param"] == "store"


def test_b1_b8_streaming_a_traves_del_alb(cliente, auth):
    marcas: list[float] = []
    eventos: list[dict] = []
    inicio = time.perf_counter()

    with cliente.stream(
        "POST",
        "/v1/responses",
        json={"model": MODELO, "stream": True, "input": "Resume su experiencia en la nube."},
        headers=auth,
    ) as respuesta:
        assert respuesta.status_code == 200
        assert respuesta.headers["content-type"].startswith("text/event-stream")  # B1
        for linea in respuesta.iter_lines():
            if not linea.startswith("data:"):
                continue
            crudo = linea[5:].strip()
            if crudo == "[DONE]":
                eventos.append({"type": "[DONE]"})
                break
            evento = json.loads(crudo)
            eventos.append(evento)
            if evento["type"] == "response.output_text.delta":
                marcas.append(time.perf_counter() - inicio)

    tipos = [e["type"] for e in eventos]
    assert tipos[0] == "response.created"
    assert tipos[-1] == "[DONE]" and tipos[-2] == "response.completed"  # B5

    numeros = [e["sequence_number"] for e in eventos if "sequence_number" in e]
    assert numeros == list(range(len(numeros)))  # B4

    deltas = "".join(e["delta"] for e in eventos if e["type"] == "response.output_text.delta")
    final = next(e for e in eventos if e["type"] == "response.output_text.done")["text"]
    assert deltas == final  # B6

    assert marcas and marcas[0] < 5.0, f"TTFT {marcas[0]:.2f}s por encima del límite duro (§8)"  # B7
    assert len(marcas) >= 3
    assert marcas[-1] - marcas[0] > 0.05, "los deltas llegaron todos juntos: hay buffering"  # B8


@pytest.mark.parametrize(
    "pregunta", ["¿Tiene certificación CISSP?", "¿Tiene un doctorado en Cambridge?"]
)
def test_c3_credencial_inexistente_se_niega(cliente, auth, pregunta):
    """Cero tolerancia: una sola credencial inventada invalida el despliegue."""
    respuesta = cliente.post(
        "/v1/responses", json={"model": MODELO, "input": pregunta}, headers=auth
    )
    texto = respuesta.json()["output"][-1]["content"][0]["text"].lower()

    assert "no consta" in texto or "no cuenta" in texto, f"posible invención: {texto[:200]}"


def test_los_errores_no_filtran_detalle_interno(cliente, auth):
    respuesta = cliente.post("/v1/responses", content="{roto", headers=auth)
    crudo = respuesta.text

    assert respuesta.status_code == 400
    for filtracion in ("Traceback", "arn:aws", "botocore", "s3://"):
        assert filtracion not in crudo
