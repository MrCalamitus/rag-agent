"""Adaptador de recuperación local — el sustituto mientras la ingesta a la
Bedrock Knowledge Base sigue pendiente (plan E2–E3)."""

from __future__ import annotations

import json

import pytest

from rag_agent.infrastructure.outbound.local.corpus_knowledge_base import LocalCorpusKnowledgeBase

from ..conftest import CORPUS


async def test_recupera_el_documento_correcto_con_su_procedencia():
    kb = LocalCorpusKnowledgeBase(CORPUS)

    outcome = await kb.retrieve(["título de ingeniería", "titulo ingenieria"])

    assert outcome.chunks[0].document_id == "titulo-ingenieria-sistemas-2019.md"
    assert outcome.chunks[0].score > 0
    assert "Ingeniería en Sistemas" in outcome.chunks[0].text


async def test_un_documento_es_un_fragmento():
    """Decisión de E2: partir un título o una cédula destroza su sentido."""
    kb = LocalCorpusKnowledgeBase(CORPUS)

    outcome = await kb.retrieve(["cédula profesional"], top_k=10)
    ids = [c.document_id for c in outcome.chunks]

    assert len(ids) == len(set(ids)), "un documento no se parte en varios fragmentos"


async def test_los_metadatos_laterales_se_leen_en_formato_de_bedrock_kb():
    kb = LocalCorpusKnowledgeBase(CORPUS)

    outcome = await kb.retrieve(["título de ingeniería"])
    metadata = outcome.chunks[0].metadata

    assert metadata["tipo"] == "titulo"
    assert metadata["anio"] == 2019


async def test_la_busqueda_ignora_acentos_y_mayusculas():
    kb = LocalCorpusKnowledgeBase(CORPUS)

    con_acentos = await kb.retrieve(["Cédula Profesional"])
    sin_acentos = await kb.retrieve(["cedula profesional"])

    assert [c.document_id for c in con_acentos.chunks] == [
        c.document_id for c in sin_acentos.chunks
    ]


async def test_una_consulta_sin_coincidencias_no_devuelve_nada():
    kb = LocalCorpusKnowledgeBase(CORPUS)

    outcome = await kb.retrieve(["gastronomía molecular"])

    assert outcome.is_empty


async def test_con_el_corpus_vacio_el_agente_se_queda_sin_evidencia(tmp_path):
    """Estado actual del proyecto: sin ingesta, la respuesta correcta es declinar.

    Es la propiedad que hace seguro entregar con el RAG pendiente: la ausencia
    de corpus produce silencio, nunca una credencial inventada.
    """
    kb = LocalCorpusKnowledgeBase(tmp_path)

    outcome = await kb.retrieve(["¿tiene título?"])

    assert outcome.is_empty
    assert await kb.is_available() is True


async def test_un_directorio_inexistente_no_pasa_readiness(tmp_path):
    kb = LocalCorpusKnowledgeBase(tmp_path / "no-existe")

    assert await kb.is_available() is False
    assert (await kb.retrieve(["x"])).is_empty


async def test_metadatos_corruptos_no_tumban_la_recuperacion(tmp_path):
    (tmp_path / "doc.md").write_text("Constancia de curso de nube.", encoding="utf-8")
    (tmp_path / "doc.md.metadata.json").write_text("{roto", encoding="utf-8")
    kb = LocalCorpusKnowledgeBase(tmp_path)

    outcome = await kb.retrieve(["curso de nube"])

    assert outcome.chunks[0].document_id == "doc.md"
    assert outcome.chunks[0].metadata == {}


async def test_los_metadatos_planos_tambien_se_aceptan(tmp_path):
    (tmp_path / "doc.md").write_text("Constancia de curso de nube.", encoding="utf-8")
    (tmp_path / "doc.md.metadata.json").write_text(json.dumps({"tipo": "curso"}), encoding="utf-8")
    kb = LocalCorpusKnowledgeBase(tmp_path)

    outcome = await kb.retrieve(["curso de nube"])

    assert outcome.chunks[0].metadata == {"tipo": "curso"}


@pytest.mark.parametrize("consulta", ["", "   ", "de la y"])
async def test_consultas_sin_contenido_no_recuperan_nada(consulta):
    kb = LocalCorpusKnowledgeBase(CORPUS)

    assert (await kb.retrieve([consulta])).is_empty
