from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_agent.infrastructure.config import Settings
from rag_agent.infrastructure.container import build_container, build_language_model
from rag_agent.infrastructure.inbound.http.app import create_app
from rag_agent.infrastructure.outbound.local.corpus_knowledge_base import LocalCorpusKnowledgeBase
from rag_agent.infrastructure.outbound.local.grounded_stub_model import GroundedStubLanguageModel

from .support.fakes import FrozenClock, RecordingTelemetry, SequentialIds

TOKEN = "token-de-prueba"
CORPUS = Path(__file__).parent / "fixtures" / "corpus"
# La suite corre contra el perfil real de CV, no contra uno de mentira: sus
# reglas (enmascarado, postura ante contratación, frase de declinación) son
# parte de lo que estos casos verifican.
PERFILES = Path(__file__).resolve().parents[1] / "profiles"


@pytest.fixture
def settings() -> Settings:
    """Adaptadores locales por defecto; con `RAG_INFERENCE_BACKEND=bedrock`
    la misma suite corre contra el modelo real (plan E1, nota de las dos
    familias)."""
    return Settings(
        api_token=TOKEN,
        retrieval_backend="local",
        inference_backend=os.getenv("RAG_INFERENCE_BACKEND", "stub"),
        aws_profile=os.getenv("RAG_AWS_PROFILE"),
        aws_region=os.getenv("RAG_AWS_REGION", "us-east-1"),
        corpus_dir=str(CORPUS),
        profiles_dir=str(PERFILES),
        default_profile="luis-cv",
        rate_limit_per_minute=20,
        _env_file=None,
    )


@pytest.fixture
def telemetry() -> RecordingTelemetry:
    return RecordingTelemetry()


@pytest.fixture
def container(settings: Settings, telemetry: RecordingTelemetry):
    """Contenedor con corpus de prueba: sin AWS, sin red, determinista."""
    modelo = (
        build_language_model(settings)
        if settings.uses_bedrock_inference
        else GroundedStubLanguageModel()
    )
    return build_container(
        settings,
        knowledge_base=LocalCorpusKnowledgeBase(CORPUS),
        language_model=modelo,
        clock=FrozenClock(),
        ids=SequentialIds(),
        telemetry=telemetry,
    )


@pytest.fixture
def app(container):
    return create_app(container)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def build_client(container) -> TestClient:
    return TestClient(create_app(container), raise_server_exceptions=False)


def pytest_collection_modifyitems(config, items):
    """Los casos marcados `requires_bedrock` solo corren contra un modelo real.

    Se saltan de forma explícita —con motivo visible en el reporte— en vez de
    borrarse: forman parte de la suite de aceptación y deben pasar contra el
    despliegue (`RAG_INFERENCE_BACKEND=bedrock make test`).
    """
    import os

    saltos = {
        "requires_bedrock": (
            os.getenv("RAG_INFERENCE_BACKEND") == "bedrock",
            "exige un modelo real; correr con RAG_INFERENCE_BACKEND=bedrock",
        ),
        "requires_kb": (
            os.getenv("RAG_RETRIEVAL_BACKEND") == "bedrock",
            "exige recuperación semántica; correr con RAG_RETRIEVAL_BACKEND=bedrock",
        ),
    }
    for marca, (disponible, motivo) in saltos.items():
        if disponible:
            continue
        salto = pytest.mark.skip(reason=motivo)
        for item in items:
            if marca in item.keywords:
                item.add_marker(salto)
