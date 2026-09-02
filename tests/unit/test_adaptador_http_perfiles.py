"""Selección de tema por HTTP.

Un despliegue sirve varios temas y el cliente elige el suyo con una cabecera.
Va en cabecera y no en el cuerpo porque el cuerpo es el de Open Responses: un
cliente estándar debe seguir funcionando sin saber que esto existe.
"""

from __future__ import annotations

import pytest

from rag_agent.domain.profile import Profile
from rag_agent.infrastructure.container import build_container
from rag_agent.infrastructure.outbound.knowledge_bases import registry_from_mapping
from rag_agent.infrastructure.profiles import ProfileBinding, StaticProfileRegistry

from ..conftest import build_client
from ..support.fakes import FrozenClock, SequentialIds, StubKnowledgeBase
from rag_agent.domain.retrieval import Chunk

COCHES = Chunk("hilux-2024--001.md", "La Hilux 2024 monta un motor 2.8 diésel.", 0.9, {"marca": "toyota"})
INVERSIONES = Chunk("informe-2025.md", "El fondo cerró 2025 con un 7,4% de rentabilidad.", 0.9, {})


@pytest.fixture
def contenedor_multitema(settings, telemetry):
    """Dos temas con bases distintas: es la topología real del despliegue."""
    perfiles = StaticProfileRegistry(
        {
            "coches": ProfileBinding(
                profile=Profile(slug="coches", name="Coches", subject="las fichas técnicas")
            ),
            "inversiones": ProfileBinding(
                profile=Profile(
                    slug="inversiones",
                    name="Inversiones",
                    subject="los informes del fondo",
                    decline_phrase="No aparece en los informes.",
                )
            ),
        },
        default_slug="coches",
    )
    return build_container(
        settings,
        profiles=perfiles,
        knowledge_bases=registry_from_mapping(
            {
                "coches": StubKnowledgeBase(chunks=(COCHES,)),
                "inversiones": StubKnowledgeBase(chunks=(INVERSIONES,)),
            }
        ),
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=telemetry,
    )


def _preguntar(client, auth, pregunta: str, *, tema: str | None = None):
    cabeceras = dict(auth)
    if tema is not None:
        cabeceras["X-Rag-Profile"] = tema
    return client.post(
        "/v1/responses",
        headers=cabeceras,
        json={"model": "agente-rag-sonnet", "input": pregunta},
    )


def _texto(cuerpo: dict) -> str:
    return "".join(
        parte.get("text", "")
        for item in cuerpo["output"]
        if item["type"] == "message"
        for parte in item.get("content", [])
    )


def _documentos(cuerpo: dict) -> list[str]:
    return [
        resultado["document_id"]
        for item in cuerpo["output"]
        if item["type"].endswith("knowledge_search")
        for resultado in item.get("results", [])
    ]


def test_la_cabecera_elige_de_que_tema_se_recupera(contenedor_multitema, auth):
    client = build_client(contenedor_multitema)

    coches = _preguntar(client, auth, "¿Qué motor monta?", tema="coches")
    inversiones = _preguntar(client, auth, "¿Qué rentabilidad tuvo?", tema="inversiones")

    assert coches.status_code == 200 and inversiones.status_code == 200
    assert _documentos(coches.json()) == ["hilux-2024--001.md"]
    assert _documentos(inversiones.json()) == ["informe-2025.md"]


def test_sin_cabecera_se_usa_el_tema_por_defecto(contenedor_multitema, auth):
    """Un cliente de Open Responses que no sabe de temas sigue funcionando."""
    respuesta = _preguntar(build_client(contenedor_multitema), auth, "¿Qué motor monta?")

    assert respuesta.status_code == 200
    assert _documentos(respuesta.json()) == ["hilux-2024--001.md"]


def test_un_tema_inexistente_es_un_400_con_los_temas_validos(contenedor_multitema, auth):
    respuesta = _preguntar(build_client(contenedor_multitema), auth, "¿Y?", tema="motos")

    assert respuesta.status_code == 400
    error = respuesta.json()["error"]
    assert error["code"] == "profile_not_found"
    assert "coches" in error["message"] and "inversiones" in error["message"]


def test_el_tema_invalido_falla_antes_de_abrir_el_stream(contenedor_multitema, auth):
    """Como con un alias inválido: 400 con cabeceras, no un error a medio SSE."""
    respuesta = build_client(contenedor_multitema).post(
        "/v1/responses",
        headers={**auth, "X-Rag-Profile": "motos"},
        json={"model": "agente-rag-sonnet", "input": "¿Y?", "stream": True},
    )

    assert respuesta.status_code == 400
    assert "text/event-stream" not in respuesta.headers.get("content-type", "")


def test_el_endpoint_de_temas_los_enumera(contenedor_multitema, auth):
    respuesta = build_client(contenedor_multitema).get("/v1/profiles", headers=auth)

    cuerpo = respuesta.json()
    assert respuesta.status_code == 200
    assert cuerpo["default"] == "coches"
    assert [t["id"] for t in cuerpo["data"]] == ["coches", "inversiones"]
    assert cuerpo["data"][0]["subject"] == "las fichas técnicas"


def test_el_endpoint_de_temas_exige_autenticacion(contenedor_multitema):
    """La lista de temas dice qué documentación hay indexada: ya es información."""
    respuesta = build_client(contenedor_multitema).get("/v1/profiles")

    assert respuesta.status_code == 401


def test_la_telemetria_registra_el_tema_de_cada_peticion(contenedor_multitema, auth, telemetry):
    """Sin esto, un despliegue multitema no se puede depurar: no se sabe cuál
    de los temas está fallando."""
    _preguntar(build_client(contenedor_multitema), auth, "¿Qué motor monta?", tema="inversiones")

    aceptadas = [e for e in telemetry.events if e[0] == "request.accepted"]
    assert aceptadas and aceptadas[-1][1]["profile"] == "inversiones"
