"""Guardas de la arquitectura hexagonal.

Estas pruebas no comprueban comportamiento: comprueban que las dependencias
sigan apuntando hacia adentro. Es lo que hace que cambiar la recuperación
local por Bedrock —cuando la ingesta esté hecha— sea cambiar un adaptador y no
un refactor.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from rag_agent.application.ports import (
    ClockPort,
    IdGeneratorPort,
    KnowledgeBasePort,
    LanguageModelPort,
    ModelCatalogPort,
    ModelDescriptor,
    TelemetryPort,
)
from rag_agent.infrastructure.outbound.bedrock.knowledge_base import BedrockKnowledgeBase
from rag_agent.infrastructure.outbound.bedrock.language_model import BedrockLanguageModel
from rag_agent.infrastructure.outbound.local.clock import SystemClock
from rag_agent.infrastructure.outbound.local.corpus_knowledge_base import LocalCorpusKnowledgeBase
from rag_agent.infrastructure.outbound.local.grounded_stub_model import GroundedStubLanguageModel
from rag_agent.infrastructure.outbound.local.ids import UuidGenerator
from rag_agent.infrastructure.outbound.model_catalog import BedrockModelCatalog, StaticModelCatalog
from rag_agent.infrastructure.outbound.telemetry.structured import StructuredTelemetry

from ..support.fakes import (
    FrozenClock,
    RecordingTelemetry,
    ScriptedLanguageModel,
    SequentialIds,
    StubKnowledgeBase,
)

RAIZ = Path(__file__).resolve().parents[2] / "src" / "rag_agent"
PROHIBIDAS = {"fastapi", "starlette", "boto3", "botocore", "uvicorn", "pydantic", "pydantic_settings", "httpx"}


def _imports(modulo: Path) -> set[str]:
    arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres.update(alias.name.split(".")[0] for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0 and nodo.module:
            nombres.add(nodo.module.split(".")[0])
    return nombres


def _modulos(capa: str) -> list[Path]:
    return sorted((RAIZ / capa).rglob("*.py"))


@pytest.mark.parametrize("capa", ["domain", "application"])
def test_el_nucleo_no_conoce_frameworks_ni_sdks(capa):
    for modulo in _modulos(capa):
        prohibidas = _imports(modulo) & PROHIBIDAS
        assert not prohibidas, f"{modulo.relative_to(RAIZ)} importa {prohibidas}"


def test_el_dominio_no_depende_de_la_aplicacion_ni_de_la_infraestructura():
    for modulo in _modulos("domain"):
        texto = modulo.read_text(encoding="utf-8")
        assert "application" not in texto.split("\n\n")[0]
        assert "infrastructure" not in texto


def test_la_aplicacion_no_depende_de_la_infraestructura():
    for modulo in _modulos("application"):
        assert "infrastructure" not in modulo.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "adaptador",
    [
        LocalCorpusKnowledgeBase("corpus"),
        BedrockKnowledgeBase(knowledge_base_id="kb-x", region="us-east-1"),
        StubKnowledgeBase(),
    ],
)
def test_todo_adaptador_de_recuperacion_cumple_el_puerto(adaptador):
    """Guarda del cambio pendiente: local hoy, Bedrock KB cuando haya ingesta."""
    assert isinstance(adaptador, KnowledgeBasePort)


@pytest.mark.parametrize(
    "adaptador",
    [GroundedStubLanguageModel(), BedrockLanguageModel(region="us-east-1"), ScriptedLanguageModel()],
)
def test_todo_adaptador_de_inferencia_cumple_el_puerto(adaptador):
    assert isinstance(adaptador, LanguageModelPort)


def test_los_catalogos_cumplen_el_puerto():
    mapa = {"a": ModelDescriptor("a", "anthropic.claude-sonnet-5")}

    assert isinstance(StaticModelCatalog(mapa), ModelCatalogPort)
    assert isinstance(BedrockModelCatalog(mapa, region="us-east-1"), ModelCatalogPort)


def test_los_adaptadores_de_soporte_cumplen_sus_puertos():
    assert isinstance(SystemClock(), ClockPort)
    assert isinstance(FrozenClock(), ClockPort)
    assert isinstance(UuidGenerator(), IdGeneratorPort)
    assert isinstance(SequentialIds(), IdGeneratorPort)
    assert isinstance(StructuredTelemetry(), TelemetryPort)
    assert isinstance(RecordingTelemetry(), TelemetryPort)


def test_un_catalogo_vacio_no_puede_construirse():
    """Un mapa de alias vacío es un fallo de configuración, no un caso válido."""
    with pytest.raises(ValueError):
        StaticModelCatalog({})


def test_la_imagen_incluye_los_perfiles():
    """Guarda de un fallo que ya ocurrió: sin `COPY profiles`, el contenedor
    arrancaba, devolvía 200 y respondía con un perfil genérico que nadie
    escribió, sobre un corpus que no era el suyo. Un despliegue que funciona a
    medias es peor que uno que no arranca."""
    repo = RAIZ.parents[1]
    dockerfile = (repo / "Dockerfile").read_text(encoding="utf-8")
    ignorados = (repo / ".dockerignore").read_text(encoding="utf-8").split()

    assert "COPY profiles" in dockerfile
    assert "profiles" not in ignorados


def test_todo_perfil_declarado_es_cargable():
    """Terraform crea una Knowledge Base por archivo de `profiles/`. Un YAML
    inválido rompería el despliegue, no solo el arranque local."""
    from rag_agent.infrastructure.profiles import load_profiles

    perfiles = load_profiles(RAIZ.parents[1] / "profiles")

    assert perfiles, "el repositorio debe traer al menos un tema de ejemplo"
    for slug, binding in perfiles.items():
        assert binding.profile.slug == slug
        assert binding.profile.subject
