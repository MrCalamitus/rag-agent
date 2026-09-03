"""Entrega del documento original.

El agente decide qué documentos puede consultar el usuario mientras responde;
el navegador los pide después, en una petición que no lleva ni pregunta ni
fragmentos. El puente es un permiso firmado, y estas pruebas fijan sus dos
propiedades: que autoriza exactamente lo que el agente autorizó, y ni un
documento más.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_agent.domain.documents import (
    DocumentGrant,
    GrantError,
    is_safe_document_name,
    sign_grant,
    verify_grant,
)
from rag_agent.domain.profile import DocumentPolicy, Profile
from rag_agent.domain.retrieval import Chunk, RetrievalOutcome, aplicar_exposicion
from rag_agent.infrastructure.container import build_container
from rag_agent.infrastructure.outbound.documents import LocalDocumentStore, SignedDocumentLinks
from rag_agent.infrastructure.outbound.knowledge_bases import registry_from_mapping
from rag_agent.infrastructure.profiles import ProfileBinding, StaticProfileRegistry

from ..conftest import build_client
from ..support.fakes import FrozenClock, SequentialIds, StubKnowledgeBase

SECRETO = "token-de-pruebas"

ABIERTO = Profile(
    slug="abierto",
    name="Abierto",
    subject="folletos publicados",
    documents=DocumentPolicy(expone=("publico",), por_defecto="publico"),
)
CERRADO = Profile(
    slug="cerrado",
    name="Cerrado",
    subject="credenciales",
    documents=DocumentPolicy(por_defecto="identidad"),
)


# --- el permiso ---------------------------------------------------------------


def test_el_permiso_solo_vale_para_su_documento():
    """Es lo que impide pedir un archivo que el agente nunca ofreció."""
    concedido = DocumentGrant("abierto", "folleto.pdf", 2000)
    firma = sign_grant(concedido, SECRETO)

    verify_grant(concedido, firma, SECRETO, now=1000)

    otro = DocumentGrant("abierto", "cedula.pdf", 2000)
    with pytest.raises(GrantError):
        verify_grant(otro, firma, SECRETO, now=1000)


def test_el_permiso_no_cruza_de_tema():
    """Dos temas del mismo despliegue no comparten autorizaciones."""
    firma = sign_grant(DocumentGrant("abierto", "folleto.pdf", 2000), SECRETO)

    with pytest.raises(GrantError):
        verify_grant(DocumentGrant("cerrado", "folleto.pdf", 2000), firma, SECRETO, now=1000)


def test_el_permiso_caduca():
    concedido = DocumentGrant("abierto", "folleto.pdf", 2000)
    firma = sign_grant(concedido, SECRETO)

    with pytest.raises(GrantError, match="caducado"):
        verify_grant(concedido, firma, SECRETO, now=2001)


def test_estirar_la_caducidad_invalida_la_firma():
    """La fecha va firmada, así que no se puede reescribir en la URL."""
    firma = sign_grant(DocumentGrant("abierto", "folleto.pdf", 2000), SECRETO)

    with pytest.raises(GrantError, match="firma"):
        verify_grant(DocumentGrant("abierto", "folleto.pdf", 99999), firma, SECRETO, now=1000)


@pytest.mark.parametrize(
    "nombre", ["../secreto.pdf", "a/b.pdf", "..", ".oculto", "", "x" * 256, "nulo\x00.pdf"]
)
def test_nombres_que_no_son_un_archivo_se_rechazan(nombre):
    assert not is_safe_document_name(nombre)


# --- el enlace ----------------------------------------------------------------


def test_solo_se_firman_enlaces_de_lo_expuesto():
    """Un enlace es un permiso: no se conceden los que no se han autorizado."""
    enlaces = SignedDocumentLinks(SECRETO, FrozenClock())
    fragmentos = (
        Chunk("f--001.md", "…", 0.9, {"clase": "publico", "fuente": "folleto.pdf"}),
        Chunk("c--001.md", "…", 0.9, {"clase": "identidad", "fuente": "cedula.pdf"}),
    )

    resultado = aplicar_exposicion(
        RetrievalOutcome(queries=(), chunks=fragmentos, latency_ms=1),
        ABIERTO.documents,
        link=lambda doc: enlaces.link_for(ABIERTO, doc),
    )

    assert resultado.chunks[0].exposed and resultado.chunks[0].document_url
    assert "folleto.pdf" in resultado.chunks[0].document_url
    assert not resultado.chunks[1].exposed
    assert resultado.chunks[1].document_url is None


def test_sin_almacen_no_hay_enlace_pero_sigue_habiendo_evidencia():
    """Autorizar y poder entregar son cosas distintas."""
    resultado = aplicar_exposicion(
        RetrievalOutcome(
            queries=(),
            chunks=(Chunk("f--001.md", "texto", 0.9, {"clase": "publico"}),),
            latency_ms=1,
        ),
        ABIERTO.documents,
        link=None,
    )

    assert resultado.chunks[0].exposed
    assert resultado.chunks[0].document_url is None
    assert resultado.chunks[0].text == "texto"


# --- el almacén ---------------------------------------------------------------


def test_el_almacen_local_encuentra_el_original_en_subcarpetas(tmp_path: Path):
    (tmp_path / "toyota").mkdir()
    (tmp_path / "toyota" / "folleto.pdf").write_bytes(b"%PDF-1.7 contenido")
    almacen = LocalDocumentStore({"abierto": str(tmp_path)})

    import asyncio

    encontrado = asyncio.run(almacen.fetch(ABIERTO, "folleto.pdf"))

    assert encontrado is not None
    assert encontrado.content == b"%PDF-1.7 contenido"
    assert encontrado.media_type == "application/pdf"


def test_el_almacen_local_no_sale_de_su_carpeta(tmp_path: Path):
    (tmp_path / "fuera.pdf").write_bytes(b"secreto")
    raiz = tmp_path / "corpus"
    raiz.mkdir()
    almacen = LocalDocumentStore({"abierto": str(raiz)})

    import asyncio

    assert asyncio.run(almacen.fetch(ABIERTO, "../fuera.pdf")) is None


# --- el endpoint --------------------------------------------------------------


@pytest.fixture
def contenedor_documentos(settings, telemetry, tmp_path: Path):
    (tmp_path / "folleto.pdf").write_bytes(b"%PDF-1.7 el folleto")
    fragmento = Chunk(
        "folleto--001.md",
        "El motor 2.8 diésel entrega 204 CV.",
        0.9,
        {"clase": "publico", "fuente": "folleto.pdf"},
    )
    return build_container(
        settings,
        profiles=StaticProfileRegistry(
            {
                "abierto": ProfileBinding(profile=ABIERTO, source_dir=str(tmp_path)),
                "cerrado": ProfileBinding(profile=CERRADO, source_dir=str(tmp_path)),
            },
            default_slug="abierto",
        ),
        knowledge_bases=registry_from_mapping(
            {
                "abierto": StubKnowledgeBase(chunks=(fragmento,)),
                "cerrado": StubKnowledgeBase(chunks=(fragmento,)),
            }
        ),
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=telemetry,
    )


def _enlace(cliente, auth, tema: str) -> str | None:
    respuesta = cliente.post(
        "/v1/responses",
        json={"model": "agente-rag-sonnet", "input": "¿Qué entrega el motor diésel?"},
        headers={**auth, "X-Rag-Profile": tema},
    )
    recuperacion = [i for i in respuesta.json()["output"] if i["type"] != "message"]
    return recuperacion[0]["results"][0]["document_url"]


def test_el_enlace_que_da_la_respuesta_sirve_el_documento(contenedor_documentos, auth):
    """De punta a punta: lo que el agente ofrece se puede abrir."""
    cliente = build_client(contenedor_documentos)

    respuesta = cliente.get(_enlace(cliente, auth, "abierto"), headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.content == b"%PDF-1.7 el folleto"
    assert respuesta.headers["content-type"].startswith("application/pdf")
    assert "inline" in respuesta.headers["content-disposition"]


def test_un_tema_cerrado_no_ofrece_enlace(contenedor_documentos, auth):
    cliente = build_client(contenedor_documentos)

    assert _enlace(cliente, auth, "cerrado") is None


def test_pedir_el_documento_sin_permiso_se_rechaza(contenedor_documentos, auth):
    """Conocer el nombre del archivo no basta; hay que tener el permiso."""
    respuesta = build_client(contenedor_documentos).get("/v1/documents/folleto.pdf", headers=auth)

    assert respuesta.status_code == 401
    assert respuesta.json()["error"]["code"] == "invalid_document_grant"


def test_el_permiso_de_un_tema_no_abre_el_documento_de_otro(contenedor_documentos, auth):
    """El mismo archivo, dos temas: el permiso lleva dentro cuál lo autorizó."""
    cliente = build_client(contenedor_documentos)
    enlace = _enlace(cliente, auth, "abierto")

    respuesta = cliente.get(enlace.replace("profile=abierto", "profile=cerrado"), headers=auth)

    assert respuesta.status_code == 401


def test_el_documento_exige_autenticacion(contenedor_documentos, auth):
    """El permiso dice qué documento, no quién puede pedirlo."""
    cliente = build_client(contenedor_documentos)

    assert cliente.get(_enlace(cliente, auth, "abierto")).status_code == 401
