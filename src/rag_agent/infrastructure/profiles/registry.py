"""Registro de perfiles: qué temas sirve este despliegue.

Cumple `ProfileRegistryPort`. Resuelve el slug que llega en la petición, aplica
las sobrescrituras del entorno (los IDs de Knowledge Base los inyecta Terraform
en la task definition, no el YAML) y falla con un error de cliente cuando el
tema pedido no existe.
"""

from __future__ import annotations

from collections.abc import Mapping

from ...domain.errors import profile_not_found
from ...domain.profile import Profile
from .loader import ProfileBinding, ProfileError


class StaticProfileRegistry:
    def __init__(
        self,
        bindings: Mapping[str, ProfileBinding],
        *,
        default_slug: str | None = None,
    ) -> None:
        if not bindings:
            raise ProfileError(
                "un despliegue sin perfiles no puede responder nada. "
                "Ejecuta `make init` o declara al menos un perfil en profiles/."
            )
        self._bindings = dict(bindings)
        if default_slug and default_slug not in self._bindings:
            raise ProfileError(
                f"el perfil por defecto '{default_slug}' no existe. "
                f"Disponibles: {', '.join(sorted(self._bindings))}"
            )
        self._default = default_slug or next(iter(self._bindings))

    # -- puerto ---------------------------------------------------------
    def resolve(self, slug: str | None) -> Profile:
        return self.binding(slug).profile

    def slugs(self) -> tuple[str, ...]:
        return tuple(self._bindings)

    @property
    def default(self) -> Profile:
        return self._bindings[self._default].profile

    # -- uso interno del despliegue --------------------------------------
    def binding(self, slug: str | None) -> ProfileBinding:
        clave = (slug or self._default).strip()
        binding = self._bindings.get(clave)
        if binding is None:
            raise profile_not_found(clave, self.slugs())
        return binding

    def bindings(self) -> tuple[ProfileBinding, ...]:
        return tuple(self._bindings.values())

    def with_knowledge_base_ids(self, ids: Mapping[str, str]) -> StaticProfileRegistry:
        """Los IDs de KB los conoce el despliegue, no el YAML.

        Terraform crea una Knowledge Base por perfil y pasa el mapa
        slug → id por variable de entorno. Mantenerlos fuera del YAML es lo que
        permite que el mismo archivo de perfil sirva en local, en pruebas y en
        producción sin editarse.
        """
        desconocidos = set(ids) - set(self._bindings)
        if desconocidos:
            raise ProfileError(
                f"se declararon Knowledge Bases para perfiles inexistentes: {sorted(desconocidos)}"
            )
        fusionados = {
            slug: (
                binding if slug not in ids else ProfileBinding(
                    profile=binding.profile,
                    knowledge_base_id=ids[slug],
                    source_dir=binding.source_dir,
                    prepared_dir=binding.prepared_dir,
                )
            )
            for slug, binding in self._bindings.items()
        }
        return StaticProfileRegistry(fusionados, default_slug=self._default)
