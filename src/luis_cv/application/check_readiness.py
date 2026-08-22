"""Caso de uso de readiness: ¿puede el servicio atender de verdad?

`/healthz` responde si el proceso vive. `/readyz` solo responde 200 si el
catálogo de modelos y la base de conocimiento están alcanzables; si no, el ALB
debe dejar de mandar tráfico (contrato §1, caso D2).
"""

from __future__ import annotations

from dataclasses import dataclass

from .ports import KnowledgeBasePort, LanguageModelPort, ModelCatalogPort


@dataclass(frozen=True)
class ReadinessReport:
    models: bool
    knowledge_base: bool
    inference: bool

    @property
    def ready(self) -> bool:
        return self.models and self.knowledge_base and self.inference

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": {
                "model_catalog": self.models,
                "knowledge_base": self.knowledge_base,
                "inference": self.inference,
            },
        }


class CheckReadiness:
    def __init__(
        self,
        *,
        catalog: ModelCatalogPort,
        knowledge_base: KnowledgeBasePort,
        language_model: LanguageModelPort,
    ) -> None:
        self._catalog = catalog
        self._knowledge_base = knowledge_base
        self._language_model = language_model

    async def __call__(self) -> ReadinessReport:
        return ReadinessReport(
            models=await self._catalog.is_available(),
            knowledge_base=await self._knowledge_base.is_available(),
            inference=await self._language_model.is_available(),
        )
