"""Puertos: lo que el núcleo necesita del mundo, expresado sin nombrarlo.

Ninguna firma menciona Bedrock, boto3, FastAPI ni HTTP. Cambiar de proveedor de
recuperación o de inferencia es escribir otro adaptador, no tocar el núcleo.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.conversation import Conversation, GenerationSettings
from ..domain.profile import Profile
from ..domain.retrieval import RetrievalOutcome


@dataclass(frozen=True)
class ModelDescriptor:
    """Alias público resuelto a un modelo concreto del proveedor.

    `supports_sampling` no es un detalle cosmético: las familias más recientes
    dejaron de aceptar `temperature` y responden con un error de validación si
    se envía. Es una capacidad del modelo, así que viaja con el modelo y no
    dispersa condicionales por el adaptador.
    """

    alias: str
    provider_model_id: str
    family: str = "anthropic"
    supports_sampling: bool = True


@runtime_checkable
class ModelCatalogPort(Protocol):
    def resolve(self, alias: str) -> ModelDescriptor:
        """Devuelve el modelo del alias o lanza `AgentError` model_not_found."""

    def aliases(self) -> tuple[str, ...]: ...

    async def is_available(self) -> bool:
        """Los alias configurados tienen acceso concedido (readiness)."""


@runtime_checkable
class KnowledgeBasePort(Protocol):
    async def retrieve(self, queries: Sequence[str], *, top_k: int = 6) -> RetrievalOutcome: ...

    async def is_available(self) -> bool: ...


@runtime_checkable
class DocumentLinkPort(Protocol):
    """De dónde sale el enlace a un documento original.

    Devuelve `None` cuando este despliegue no puede entregar el archivo — sin
    almacén configurado, por ejemplo—. Que un perfil autorice a consultarlo y
    que exista de dónde servirlo son dos cosas distintas, y la segunda es de
    infraestructura.
    """

    def link_for(self, profile: Profile, document: str) -> str | None: ...


@dataclass(frozen=True)
class StoredDocument:
    """Un documento original listo para enviar al navegador."""

    content: bytes
    media_type: str
    name: str


@runtime_checkable
class DocumentStorePort(Protocol):
    """Dónde viven los documentos originales.

    En local son los archivos que el usuario tiene en disco; en el despliegue,
    un prefijo de S3 aparte del corpus indexado. Separados a propósito: lo que
    entra al índice es lo que el agente puede recitar, y no tiene por qué
    coincidir con lo que un lector puede abrir.
    """

    async def fetch(self, profile: Profile, document: str) -> StoredDocument | None: ...


@runtime_checkable
class ProfileRegistryPort(Protocol):
    """Los temas que este despliegue sabe responder.

    Un servicio atiende varios perfiles a la vez —esa es la razón de que la
    infraestructura cara (ALB, ECS, endpoints) se comparta y solo se duplique
    la Knowledge Base, que es la parte barata—, así que resolver el perfil es
    una operación por petición y no un ajuste de arranque.
    """

    def resolve(self, slug: str | None) -> Profile:
        """Devuelve el perfil del slug, el por defecto si es `None`, o lanza
        `AgentError` profile_not_found."""

    def slugs(self) -> tuple[str, ...]: ...

    @property
    def default(self) -> Profile: ...


@runtime_checkable
class KnowledgeBaseRegistryPort(Protocol):
    """Qué base de conocimiento sirve a cada perfil.

    Separado de `ProfileRegistryPort` a propósito: el perfil es una regla de
    producto y el enlace a una KB concreta es un hecho del despliegue. Mezclarlos
    obligaría a redesplegar para cambiar una frase del prompt.
    """

    def for_profile(self, profile: Profile) -> KnowledgeBasePort: ...

    async def is_available(self) -> bool:
        """Todas las bases enlazadas responden (readiness)."""


@dataclass(frozen=True)
class TextChunk:
    delta: str


@dataclass(frozen=True)
class UsageReport:
    input_tokens: int = 0
    output_tokens: int = 0


LanguageModelChunk = TextChunk | UsageReport


@runtime_checkable
class LanguageModelPort(Protocol):
    def stream(
        self,
        *,
        model: ModelDescriptor,
        system_prompt: str,
        conversation: Conversation,
        settings: GenerationSettings,
    ) -> AsyncIterator[LanguageModelChunk]:
        """Emite deltas de texto y, al final, el consumo de tokens."""

    async def is_available(self) -> bool: ...


@runtime_checkable
class ClockPort(Protocol):
    def unix_seconds(self) -> int: ...

    def monotonic_ms(self) -> float: ...


@runtime_checkable
class IdGeneratorPort(Protocol):
    def response_id(self) -> str: ...

    def item_id(self, prefix: str) -> str: ...


@runtime_checkable
class TelemetryPort(Protocol):
    def event(self, name: str, /, **fields: object) -> None:
        """Registra un evento estructurado. Nunca recibe texto del turno."""

    def warning(self, name: str, /, **fields: object) -> None: ...

    def span(self, name: str, /, **fields: object) -> AbstractAsyncContextManager[None]:
        """Subsegmento de traza: `retrieval` e `inference` van separados."""
