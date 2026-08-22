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
    """Igual que el estático, pero comprueba el acceso real a los modelos."""

    def __init__(self, mapping: Mapping[str, ModelDescriptor], *, region: str, profile: str | None = None) -> None:
        super().__init__(mapping)
        self._region = region
        self._profile = profile

    async def is_available(self) -> bool:
        import anyio

        return await anyio.to_thread.run_sync(self._check)

    def _check(self) -> bool:
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError:  # pragma: no cover - boto3 es dependencia de runtime
            return False
        try:
            session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
            client = session.client("bedrock", region_name=self._region)
            disponibles = {
                m["modelId"] for m in client.list_foundation_models().get("modelSummaries", [])
            }
        except (ClientError, BotoCoreError):
            return False
        # Los perfiles de inferencia (`us.anthropic...`) no aparecen en la lista;
        # se comprueba el ID base contenido en el identificador configurado.
        for descriptor in self._mapping.values():
            if not any(base in descriptor.provider_model_id for base in disponibles):
                return False
        return True
