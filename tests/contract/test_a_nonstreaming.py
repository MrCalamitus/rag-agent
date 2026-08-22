"""Casos A del contrato §9 — superficie HTTP y esquema, sin streaming."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.contract

RUTA = "/v1/responses"


def _peticion(**extra) -> dict:
    base = {"model": "agente-rag-sonnet", "input": "¿Tiene título de ingeniería?"}
    base.update(extra)
    return base


def test_a1_peticion_minima_valida(client, auth):
    respuesta = client.post(RUTA, json=_peticion(), headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("application/json")

    cuerpo = respuesta.json()
    assert cuerpo["status"] == "completed"
    assert cuerpo["store"] is False
    assert [item["type"] for item in cuerpo["output"]][-1] == "message"
    for item in cuerpo["output"]:
        assert item["id"] and item["type"] and item["status"], "todo ítem lleva id, type y status"
    mensaje = cuerpo["output"][-1]
    assert mensaje["role"] == "assistant"
    assert mensaje["content"][0]["type"] == "output_text"
    assert cuerpo["usage"]["total_tokens"] == (
        cuerpo["usage"]["input_tokens"] + cuerpo["usage"]["output_tokens"]
    )


def test_a2_input_como_cadena_equivale_al_arreglo(client, auth):
    pregunta = "¿Tiene título de ingeniería?"
    como_cadena = client.post(RUTA, json=_peticion(input=pregunta), headers=auth).json()
    como_arreglo = client.post(
        RUTA,
        json=_peticion(
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": pregunta}],
                }
            ]
        ),
        headers=auth,
    ).json()

    def forma(cuerpo: dict) -> list:
        return [
            (item["type"], item["status"], item.get("content", [{}])[0].get("text"))
            for item in cuerpo["output"]
        ]

    assert forma(como_cadena) == forma(como_arreglo)


def test_a3_falta_authorization(client):
    respuesta = client.post(RUTA, json=_peticion(), headers={"Content-Type": "application/json"})

    assert respuesta.status_code == 401
    error = respuesta.json()["error"]
    assert error["type"] == "authentication_error"
    assert error["code"] == "missing_authorization"
    assert error["message"]


def test_a3b_token_invalido(client):
    respuesta = client.post(
        RUTA,
        json=_peticion(),
        headers={"Authorization": "Bearer equivocado", "Content-Type": "application/json"},
    )

    assert respuesta.status_code == 401
    assert respuesta.json()["error"]["code"] == "invalid_token"


def test_a4_content_type_no_json(client, auth):
    respuesta = client.post(
        RUTA,
        content=json.dumps(_peticion()),
        headers={"Authorization": auth["Authorization"], "Content-Type": "text/plain"},
    )

    assert respuesta.status_code == 400
    error = respuesta.json()["error"]
    assert error["type"] == "invalid_request"
    assert error["param"] == "Content-Type"


def test_a5_json_malformado_sin_traza(client, auth):
    respuesta = client.post(RUTA, content='{"model": "agente-rag-sonnet", ', headers=auth)

    assert respuesta.status_code == 400
    error = respuesta.json()["error"]
    assert error["type"] == "invalid_request"
    assert error["code"] == "invalid_json"
    # Ni traza, ni posición del parser, ni detalle interno.
    for filtracion in ("Traceback", "line 1", "char", "Expecting"):
        assert filtracion not in json.dumps(respuesta.json())


def test_a6_falta_model(client, auth):
    respuesta = client.post(RUTA, json={"input": "hola"}, headers=auth)

    assert respuesta.status_code == 400
    error = respuesta.json()["error"]
    assert error["type"] == "invalid_request"
    assert error["param"] == "model"


def test_a7_alias_inexistente(client, auth):
    respuesta = client.post(RUTA, json=_peticion(model="gpt-5"), headers=auth)

    assert respuesta.status_code == 400
    error = respuesta.json()["error"]
    assert error["code"] == "model_not_found"
    assert error["param"] == "model"


def test_a8_store_true_se_rechaza(client, auth):
    respuesta = client.post(RUTA, json=_peticion(store=True), headers=auth)

    assert respuesta.status_code == 400
    error = respuesta.json()["error"]
    assert error["type"] == "invalid_request"
    assert error["param"] == "store"


def test_a9_campo_desconocido_se_ignora_y_se_registra(client, auth, telemetry):
    respuesta = client.post(RUTA, json=_peticion(campo_inventado="x"), headers=auth)

    assert respuesta.status_code == 200
    avisos = [campos for nombre, campos in telemetry.warnings if nombre == "request.unknown_fields"]
    assert avisos and "campo_inventado" in avisos[0]["fields"]


def test_a10_ruta_inexistente(client, auth):
    respuesta = client.get("/v1/no-existe", headers=auth)

    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["type"] == "not_found"


def test_entrada_multimodal_se_rechaza_explicitamente(client, auth):
    respuesta = client.post(
        RUTA,
        json=_peticion(
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "https://ejemplo/x.png"}],
                }
            ]
        ),
        headers=auth,
    )

    assert respuesta.status_code == 400
    assert respuesta.json()["error"]["code"] == "unsupported_content_type"


def test_historial_multiturno_se_acepta_sin_estado(client, auth):
    respuesta = client.post(
        RUTA,
        json=_peticion(
            input=[
                {"type": "message", "role": "user", "content": "¿Tiene experiencia en AWS?"},
                {"type": "message", "role": "assistant", "content": "Sí."},
                {"type": "message", "role": "user", "content": "¿Tiene título de ingeniería?"},
            ]
        ),
        headers=auth,
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["status"] == "completed"


def test_previous_response_id_no_se_soporta(client, auth):
    """Fuera de alcance por retención cero (contrato §7): se ignora, no se finge."""
    respuesta = client.post(RUTA, json=_peticion(previous_response_id="resp_x"), headers=auth)

    assert respuesta.status_code == 200
    assert "previous_response_id" not in json.dumps(respuesta.json())


def test_metadata_se_propaga_a_la_respuesta(client, auth):
    respuesta = client.post(RUTA, json=_peticion(metadata={"caso": "demo-01"}), headers=auth)

    assert respuesta.json()["metadata"] == {"caso": "demo-01"}
