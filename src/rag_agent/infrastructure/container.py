"""Composición de dependencias: el único lugar que conoce a todos.

El núcleo recibe puertos; aquí se decide qué adaptador los cumple. Pasar de la
recuperación local a Bedrock Knowledge Base es cambiar
`RAG_RETRIEVAL_BACKEND=bedrock`, sin tocar dominio ni aplicación.

Aquí también se resuelve la topología multi-tema: los perfiles se cargan de
`profiles/`, los IDs de sus Knowledge Bases llegan por entorno desde Terraform,
y el registro entrega a cada perfil la suya. El resto del servicio —contrato,
orquestación, redacción— no sabe que existe más de un tema.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..application.check_readiness import CheckReadiness
from ..application.create_response import CreateResponse
from ..application.ports import (
    ClockPort,
    DocumentLinkPort,
    DocumentStorePort,
    IdGeneratorPort,
    KnowledgeBasePort,
    KnowledgeBaseRegistryPort,
    LanguageModelPort,
    ModelCatalogPort,
    ModelDescriptor,
    TelemetryPort,
)
from ..domain.profile import GENERIC, RetrievalPolicy
from .config import Settings
from .outbound.documents import LocalDocumentStore, S3DocumentStore, SignedDocumentLinks
from .outbound.knowledge_bases import PerProfileKnowledgeBases, SingleKnowledgeBase
from .outbound.local.clock import SystemClock
from .outbound.local.corpus_knowledge_base import LocalCorpusKnowledgeBase
from .outbound.local.grounded_stub_model import GroundedStubLanguageModel
from .outbound.local.ids import UuidGenerator
from .outbound.model_catalog import BedrockModelCatalog, StaticModelCatalog
from .outbound.telemetry.structured import StructuredTelemetry, configure_logging
from .profiles import ProfileBinding, ProfileError, StaticProfileRegistry, load_profiles


@dataclass
class Container:
    settings: Settings
    catalog: ModelCatalogPort
    profiles: StaticProfileRegistry
    knowledge_bases: KnowledgeBaseRegistryPort
    language_model: LanguageModelPort
    clock: ClockPort
    ids: IdGeneratorPort
    telemetry: TelemetryPort
    document_links: DocumentLinkPort | None
    document_store: DocumentStorePort | None
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


def build_profiles(settings: Settings) -> StaticProfileRegistry:
    """Los temas declarados en `profiles/`, o uno genérico si no hay ninguno.

    El respaldo importa: alguien que acaba de clonar el repositorio debe poder
    hacer `make run` y obtener un servicio en pie sobre `corpus/`, sin haber
    ejecutado todavía el asistente de inicialización.
    """
    bindings = load_profiles(settings.profiles_dir)
    if not bindings and settings.environment != "local":
        # En un despliegue, quedarse sin perfiles significa que la imagen no los
        # copió o que la ruta cambió. Caer al genérico ahí es el peor desenlace:
        # el servicio arranca, contesta 200 y responde con reglas que nadie
        # escribió, sobre un corpus que no es el suyo.
        raise ProfileError(
            f"no se encontró ningún perfil en '{settings.profiles_dir}' y el entorno es "
            f"'{settings.environment}'. La imagen debe incluir la carpeta de perfiles."
        )
    if not bindings:
        generico = GENERIC.con(
            retrieval=RetrievalPolicy(
                top_k=settings.retrieval_top_k, min_score=settings.retrieval_min_score
            )
        )
        bindings = {
            generico.slug: ProfileBinding(
                profile=generico,
                knowledge_base_id=settings.knowledge_base_id,
                prepared_dir=settings.corpus_dir or "corpus",
            )
        }
    registry = StaticProfileRegistry(bindings, default_slug=settings.default_profile or None)
    if settings.profile_knowledge_bases:
        registry = registry.with_knowledge_base_ids(settings.profile_knowledge_bases)
    return registry


def build_knowledge_bases(
    settings: Settings, profiles: StaticProfileRegistry
) -> KnowledgeBaseRegistryPort:
    """Una base de conocimiento por perfil, del backend que toque."""
    if settings.uses_bedrock_retrieval:
        from .outbound.bedrock.knowledge_base import BedrockKnowledgeBase

        def crear(slug: str) -> KnowledgeBasePort:
            binding = profiles.binding(slug)
            kb_id = binding.knowledge_base_id or settings.knowledge_base_id
            if not kb_id:
                raise ValueError(
                    f"el perfil '{slug}' no tiene Knowledge Base asignada. "
                    "Declara RAG_PROFILE_KNOWLEDGE_BASES o despliega su índice."
                )
            return BedrockKnowledgeBase(
                knowledge_base_id=kb_id,
                region=settings.aws_region,
                profile=settings.aws_profile,
                min_score=binding.profile.retrieval.min_score,
            )
    else:

        def crear(slug: str) -> KnowledgeBasePort:
            binding = profiles.binding(slug)
            # Un `RAG_CORPUS_DIR` explícito gana al perfil: quien lo define
            # está apuntando el servicio a un corpus concreto a propósito.
            ruta = settings.corpus_dir or binding.prepared_dir or "corpus"
            # `~` no lo expande `Path`: sin esto un perfil con `~/corpus-x`
            # busca una carpeta llamada literalmente «~» y no recupera nada.
            return LocalCorpusKnowledgeBase(Path(ruta).expanduser())

    return PerProfileKnowledgeBases(crear, slugs=profiles.slugs())


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


def build_document_store(
    settings: Settings, profiles: StaticProfileRegistry
) -> DocumentStorePort | None:
    """De dónde salen los originales, si es que hay de dónde.

    `None` es una respuesta legítima y frecuente: un despliegue puede autorizar
    a consultar documentos y no tener todavía dónde guardarlos. En ese caso los
    fragmentos siguen llegando con `exposed`, pero sin enlace.
    """
    if not any(b.profile.exposes_documents for b in profiles.bindings()):
        return None

    if settings.documents_bucket:
        from .outbound.bedrock.clients import RETRIEVAL, build_client

        return S3DocumentStore(
            settings.documents_bucket,
            lambda: build_client(
                "s3",
                region=settings.aws_region,
                profile=settings.aws_profile,
                config=RETRIEVAL,
            ),
        )

    carpetas = {
        b.slug: b.source_dir
        for b in profiles.bindings()
        if b.profile.exposes_documents and b.source_dir
    }
    return LocalDocumentStore(carpetas) if carpetas else None


def build_container(
    settings: Settings | None = None,
    *,
    knowledge_base: KnowledgeBasePort | None = None,
    knowledge_bases: KnowledgeBaseRegistryPort | None = None,
    profiles: StaticProfileRegistry | None = None,
    language_model: LanguageModelPort | None = None,
    catalog: ModelCatalogPort | None = None,
    clock: ClockPort | None = None,
    ids: IdGeneratorPort | None = None,
    telemetry: TelemetryPort | None = None,
    document_store: DocumentStorePort | None = None,
    document_links: DocumentLinkPort | None = None,
) -> Container:
    """Los parámetros opcionales existen para las pruebas: sustituir un puerto
    no debe requerir variables de entorno ni monkeypatching."""
    settings = settings or Settings()
    configure_logging(settings.log_level)

    telemetry = telemetry or StructuredTelemetry()
    catalog = catalog or build_catalog(settings, telemetry)
    profiles = profiles or build_profiles(settings)
    # `knowledge_base=` (singular) sigue existiendo por comodidad: sustituir la
    # recuperación entera por un doble no debería obligar a construir un
    # registro. Significa «esta base para todos los perfiles».
    if knowledge_bases is None:
        knowledge_bases = (
            SingleKnowledgeBase(knowledge_base)
            if knowledge_base is not None
            else build_knowledge_bases(settings, profiles)
        )
    language_model = language_model or build_language_model(settings)
    clock = clock or SystemClock()
    ids = ids or UuidGenerator()
    document_store = (
        document_store if document_store is not None else build_document_store(settings, profiles)
    )
    # Solo se firman enlaces si hay de dónde servirlos: un enlace que siempre
    # devuelve 404 es peor que no ofrecerlo.
    document_links = document_links or (
        SignedDocumentLinks(settings.api_token, clock) if document_store else None
    )

    return Container(
        settings=settings,
        catalog=catalog,
        profiles=profiles,
        knowledge_bases=knowledge_bases,
        language_model=language_model,
        clock=clock,
        ids=ids,
        telemetry=telemetry,
        document_links=document_links,
        document_store=document_store,
        create_response=CreateResponse(
            catalog=catalog,
            profiles=profiles,
            knowledge_bases=knowledge_bases,
            language_model=language_model,
            clock=clock,
            ids=ids,
            telemetry=telemetry,
            document_links=document_links,
        ),
        check_readiness=CheckReadiness(
            catalog=catalog,
            knowledge_bases=knowledge_bases,
            language_model=language_model,
            timeout_s=settings.readiness_timeout_s,
        ),
    )
