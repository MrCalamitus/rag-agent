"""Composición de dependencias: el único lugar que conoce a todos.

El núcleo recibe puertos; aquí se decide qué adaptador los cumple. Pasar de la
recuperación local a Bedrock Knowledge Base —cuando la ingesta esté hecha— es
cambiar `LUISCV_RETRIEVAL_BACKEND=bedrock`, sin tocar dominio ni aplicación.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..application.check_readiness import CheckReadiness
from ..application.create_response import CreateResponse
from ..application.ports import (
    ClockPort,
    IdGeneratorPort,
    KnowledgeBasePort,
    LanguageModelPort,
    ModelCatalogPort,
    ModelDescriptor,
    TelemetryPort,
)
from .config import Settings
from .outbound.local.clock import SystemClock
from .outbound.local.corpus_knowledge_base import LocalCorpusKnowledgeBase
from .outbound.local.grounded_stub_model import GroundedStubLanguageModel
from .outbound.local.ids import UuidGenerator
from .outbound.model_catalog import BedrockModelCatalog, StaticModelCatalog
from .outbound.telemetry.structured import StructuredTelemetry, configure_logging


@dataclass
class Container:
    settings: Settings
    catalog: ModelCatalogPort
    knowledge_base: KnowledgeBasePort
    language_model: LanguageModelPort
    clock: ClockPort
    ids: IdGeneratorPort
    telemetry: TelemetryPort
    create_response: CreateResponse
    check_readiness: CheckReadiness


def build_catalog(settings: Settings, telemetry: TelemetryPort | None = None) -> ModelCatalogPort:
    mapping = {
        alias: ModelDescriptor(
            alias=alias,
            provider_model_id=model_id,
            family=settings.model_families.get(alias, "anthropic"),
            supports_sampling=settings.model_sampling.get(alias, True),
        )
        for alias, model_id in settings.model_aliases.items()
    }
    if settings.uses_bedrock_inference:
        return BedrockModelCatalog(
            mapping,
            region=settings.aws_region,
            profile=settings.aws_profile,
            telemetry=telemetry,
        )
    return StaticModelCatalog(mapping)


def build_knowledge_base(settings: Settings) -> KnowledgeBasePort:
    if settings.uses_bedrock_retrieval:
        if not settings.knowledge_base_id:
            raise ValueError("LUISCV_KNOWLEDGE_BASE_ID es obligatorio con retrieval_backend=bedrock")
        from .outbound.bedrock.knowledge_base import BedrockKnowledgeBase

        return BedrockKnowledgeBase(
            knowledge_base_id=settings.knowledge_base_id,
            region=settings.aws_region,
            profile=settings.aws_profile,
        )
    return LocalCorpusKnowledgeBase(Path(settings.corpus_dir))


def build_language_model(settings: Settings) -> LanguageModelPort:
    if settings.uses_bedrock_inference:
        from .outbound.bedrock.language_model import BedrockLanguageModel

        return BedrockLanguageModel(
            region=settings.aws_region,
            profile=settings.aws_profile,
            guardrail_id=settings.guardrail_id,
            guardrail_version=settings.guardrail_version,
        )
    return GroundedStubLanguageModel(delta_delay_ms=settings.stub_delta_delay_ms)


def build_container(
    settings: Settings | None = None,
    *,
    knowledge_base: KnowledgeBasePort | None = None,
    language_model: LanguageModelPort | None = None,
    catalog: ModelCatalogPort | None = None,
    clock: ClockPort | None = None,
    ids: IdGeneratorPort | None = None,
    telemetry: TelemetryPort | None = None,
) -> Container:
    """Los parámetros opcionales existen para las pruebas: sustituir un puerto
    no debe requerir variables de entorno ni monkeypatching."""
    settings = settings or Settings()
    configure_logging(settings.log_level)

    telemetry = telemetry or StructuredTelemetry()
    catalog = catalog or build_catalog(settings, telemetry)
    knowledge_base = knowledge_base or build_knowledge_base(settings)
    language_model = language_model or build_language_model(settings)
    clock = clock or SystemClock()
    ids = ids or UuidGenerator()

    return Container(
        settings=settings,
        catalog=catalog,
        knowledge_base=knowledge_base,
        language_model=language_model,
        clock=clock,
        ids=ids,
        telemetry=telemetry,
        create_response=CreateResponse(
            catalog=catalog,
            knowledge_base=knowledge_base,
            language_model=language_model,
            clock=clock,
            ids=ids,
            telemetry=telemetry,
            top_k=settings.retrieval_top_k,
        ),
        check_readiness=CheckReadiness(
            catalog=catalog,
            knowledge_base=knowledge_base,
            language_model=language_model,
            timeout_s=settings.readiness_timeout_s,
        ),
    )
