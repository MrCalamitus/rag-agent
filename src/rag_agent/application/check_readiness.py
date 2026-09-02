"""Caso de uso de readiness: ¿puede el servicio atender de verdad?

`/healthz` responde si el proceso vive. `/readyz` solo responde 200 si el
catálogo, la recuperación y la inferencia están alcanzables (contrato §1,
caso D2). Con varios temas servidos por el mismo despliegue, la recuperación se
comprueba sobre el registro completo: un servicio que responde bien de coches y
falla en inversiones no está listo.

Dos propiedades que la sonda debe cumplir por encima de todo:

1. **Acotada.** Una sonda que tarda más que el `idle_timeout` del balanceador
   no informa de nada: produce un 504 y deja al operador sin saber qué falló.
   El límite vive aquí, no en cada adaptador: aunque un adaptador nuevo olvide
   configurar su timeout, la sonda sigue respondiendo a tiempo.
2. **Concurrente.** Tres comprobaciones en serie suman tres presupuestos. En
   paralelo, el peor caso es el de la más lenta.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass

from .ports import KnowledgeBaseRegistryPort, LanguageModelPort, ModelCatalogPort

TIMEOUT_POR_DEFECTO_S = 4.0


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
        knowledge_bases: KnowledgeBaseRegistryPort,
        language_model: LanguageModelPort,
        timeout_s: float = TIMEOUT_POR_DEFECTO_S,
    ) -> None:
        self._catalog = catalog
        self._knowledge_bases = knowledge_bases
        self._language_model = language_model
        self._timeout_s = timeout_s

    async def __call__(self) -> ReadinessReport:
        models, knowledge_base, inference = await asyncio.gather(
            self._acotar(self._catalog.is_available()),
            self._acotar(self._knowledge_bases.is_available()),
            self._acotar(self._language_model.is_available()),
        )
        return ReadinessReport(
            models=models, knowledge_base=knowledge_base, inference=inference
        )

    async def _acotar(self, comprobacion: Awaitable[bool]) -> bool:
        """Agotar el tiempo o reventar cuentan como no disponible.

        Una dependencia que no contesta a tiempo es indistinguible, desde
        fuera, de una que no contesta.
        """
        try:
            return bool(await asyncio.wait_for(comprobacion, timeout=self._timeout_s))
        except (TimeoutError, asyncio.TimeoutError):
            return False
        except Exception:  # noqa: BLE001 - frontera: readiness nunca propaga
            return False
