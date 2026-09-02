"""Resolución de alias públicos a modelos del proveedor (contrato §3).

El cliente nunca envía un ID de Bedrock. El mapa se carga de configuración,
nunca del código, y se valida al arranque: un alias configurado sin acceso
concedido hace que el servicio no pase `readyz`.
"""

from __future__ import annotations

from collections.abc import Mapping

from ...application.ports import ModelDescriptor
from ...domain.errors import model_not_found


class StaticModelCatalog:
    def __init__(self, mapping: Mapping[str, ModelDescriptor]) -> None:
        if not mapping:
            raise ValueError("El mapa de alias de modelo no puede estar vacío.")
        self._mapping = dict(mapping)

    def resolve(self, alias: str) -> ModelDescriptor:
        try:
            return self._mapping[alias]
        except KeyError:
            raise model_not_found(alias) from None

    def aliases(self) -> tuple[str, ...]:
        return tuple(self._mapping)

    async def is_available(self) -> bool:
        return bool(self._mapping)


class BedrockModelCatalog(StaticModelCatalog):
    """Comprueba el acceso real a los modelos, una sola vez y sin bloquear.

    La verificación usa el **plano de control** de Bedrock (`ListFoundationModels`),
    que es un servicio distinto de `bedrock-runtime` y necesita su propio camino
    de red. En una VPC sin salida a internet y sin endpoint para él, la llamada
    no tiene por dónde salir.

    Por eso el resultado se cachea y un fallo de verificación **no** tumba
    `readyz`: que el catálogo no sea auditable desde dentro de la red no
    significa que el servicio no pueda responder. Lo que sí tumba `readyz` es
    que la recuperación o la inferencia no respondan, que es lo que de verdad
    impide servir.
    """

    def __init__(
        self,
        mapping: Mapping[str, ModelDescriptor],
        *,
        region: str,
        profile: str | None = None,
        telemetry: object | None = None,
    ) -> None:
        super().__init__(mapping)
        self._region = region
        self._profile = profile
        self._telemetry = telemetry
        self._verificado: bool | None = None

    async def is_available(self) -> bool:
        """Readiness solo mira lo que impide servir.

        Que los alias tengan acceso concedido es un hecho de despliegue: no
        cambia mientras el servicio corre, y comprobarlo exige el plano de
        control de Bedrock, que es un servicio distinto del runtime y necesita
        su propio endpoint de VPC. Pagar ese endpoint y su latencia en cada
        sonda para reverificar algo inmutable es desperdicio; peor, reportaría
        `not_ready` un servicio que atiende peticiones sin problema.

        La verificación vive en `verify_access()`, que `deploy.sh` ejecuta
        desde fuera de la VPC antes de publicar la imagen.
        """
        return bool(self._mapping)

    def verify_access(self) -> list[str]:
        """Alias configurados sin acceso concedido. Vacío = todo en orden.

        Pensado para el despliegue, no para la ruta de la petición.
        """
        return self._check_aliases()

    def _check_aliases(self) -> list[str]:
        from botocore.exceptions import BotoCoreError, ClientError

        from .bedrock.clients import CONTROL_PLANE, build_client

        try:
            client = build_client(
                "bedrock", region=self._region, profile=self._profile, config=CONTROL_PLANE
            )
            disponibles = {
                m["modelId"] for m in client.list_foundation_models().get("modelSummaries", [])
            }
        except (ClientError, BotoCoreError, OSError) as exc:
            self._avisar("model_catalog.unverifiable", motivo=type(exc).__name__)
            return []  # no verificable ≠ sin acceso

        # Los perfiles de inferencia (`us.anthropic…`) no aparecen en la lista;
        # se comprueba el ID base contenido en el identificador configurado.
        faltantes = [
            alias
            for alias, d in self._mapping.items()
            if not any(base in d.provider_model_id for base in disponibles)
        ]
        if faltantes:
            self._avisar("model_catalog.sin_acceso", aliases=faltantes)
        return faltantes

    def _avisar(self, evento: str, **campos: object) -> None:
        if self._telemetry is not None and hasattr(self._telemetry, "warning"):
            self._telemetry.warning(evento, **campos)
