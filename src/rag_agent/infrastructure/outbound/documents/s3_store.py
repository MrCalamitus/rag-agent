"""Documentos originales desde S3. Es el almacén del despliegue.

Viven bajo un prefijo distinto del corpus indexado y ese prefijo **no** es
origen de datos de ninguna Knowledge Base. La separación es la que sostiene la
postura del proyecto: lo que entra al índice es lo que el agente puede recitar,
y no tiene por qué coincidir con lo que un lector puede abrir.

Los bytes pasan por el servicio en lugar de entregarse con una URL prefirmada.
Cuesta un salto más, y a cambio: el navegador nunca habla con S3 —así que no
hace falta abrir CORS en el bucket—, el enlace caduca cuando decide el servicio
y no cuando lo decidió una firma de AWS, y no hay ninguna URL de S3 circulando
por un historial.
"""

from __future__ import annotations

import asyncio
import mimetypes

from ....application.ports import StoredDocument
from ....domain.documents import is_safe_document_name
from ....domain.profile import Profile

PREFIJO = "originales"
MAX_BYTES = 40 * 1024 * 1024


class S3DocumentStore:
    def __init__(self, bucket: str, client_factory) -> None:
        self._bucket = bucket
        self._client_factory = client_factory
        self._client = None

    def _cliente(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    async def fetch(self, profile: Profile, document: str) -> StoredDocument | None:
        if not is_safe_document_name(document) or not self._bucket:
            return None
        return await asyncio.to_thread(self._fetch, profile, document)

    def _fetch(self, profile: Profile, document: str) -> StoredDocument | None:
        clave = f"{PREFIJO}/{profile.slug}/{document}"
        try:
            objeto = self._cliente().get_object(Bucket=self._bucket, Key=clave)
        except Exception:  # noqa: BLE001 - un original ausente no es un fallo del servicio
            return None
        if objeto.get("ContentLength", 0) > MAX_BYTES:
            return None
        return StoredDocument(
            content=objeto["Body"].read(),
            media_type=objeto.get("ContentType")
            or mimetypes.guess_type(document)[0]
            or "application/octet-stream",
            name=document,
        )
