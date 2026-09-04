"""Registro de bases de conocimiento: qué índice sirve a cada perfil.

Cumple `KnowledgeBaseRegistryPort`. Es la pieza que materializa la decisión de
topología: **una sola infraestructura de cómputo, una Knowledge Base por tema**.
Lo caro del despliegue —VPC, ALB, ECS, endpoints de interfaz— se comparte entre
todos los temas; lo que se duplica es la KB sobre S3 Vectors, que cuesta
centavos. Añadir un tema es crear su índice, no otro balanceador.

Las bases se construyen de forma perezosa y se memorizan: un despliegue con
ocho temas no debe abrir ocho clientes de boto3 al arrancar para atender una
petición de uno solo.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from ...application.ports import KnowledgeBasePort
from ...domain.profile import Profile


class SingleKnowledgeBase:
    """Una misma base para todos los perfiles.

    Es lo correcto en local y en las pruebas —un corpus, un índice léxico— y lo
    que permite sustituir la recuperación entera por un doble sin que el caso
    de uso note la diferencia.
    """

    def __init__(self, knowledge_base: KnowledgeBasePort) -> None:
        self._kb = knowledge_base

    def for_profile(self, profile: Profile) -> KnowledgeBasePort:
        return self._kb

    async def is_available(self) -> bool:
        return await self._kb.is_available()


class PerProfileKnowledgeBases:
    """Una base por perfil, construida bajo demanda."""

    def __init__(
        self,
        factory: Callable[[str], KnowledgeBasePort],
        *,
        slugs: tuple[str, ...],
    ) -> None:
        self._factory = factory
        self._slugs = slugs
        self._cache: dict[str, KnowledgeBasePort] = {}

    def for_profile(self, profile: Profile) -> KnowledgeBasePort:
        kb = self._cache.get(profile.slug)
        if kb is None:
            kb = self._factory(profile.slug)
            self._cache[profile.slug] = kb
        return kb

    async def is_available(self) -> bool:
        """Listo significa listo para todos los temas que se anuncian.

        Un servicio que responde de coches y falla en inversiones devuelve 200
        a la mitad de sus clientes y 500 a la otra mitad; desde fuera eso es un
        servicio roto, no uno listo.
        """
        if not self._slugs:
            return False
        resultados = await asyncio.gather(
            *(self._disponible(slug) for slug in self._slugs), return_exceptions=True
        )
        return all(r is True for r in resultados)

    async def _disponible(self, slug: str) -> bool:
        kb = self._cache.get(slug) or self._factory(slug)
        self._cache[slug] = kb
        return bool(await kb.is_available())


def registry_from_mapping(bases: Mapping[str, KnowledgeBasePort]) -> PerProfileKnowledgeBases:
    """Atajo para pruebas: un mapa ya construido de slug → base."""
    return PerProfileKnowledgeBases(lambda slug: bases[slug], slugs=tuple(bases))
