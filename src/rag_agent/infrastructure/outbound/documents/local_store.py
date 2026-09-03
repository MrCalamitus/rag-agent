"""Documentos originales desde el disco. Es el almacén de desarrollo."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from ....application.ports import StoredDocument
from ....domain.documents import is_safe_document_name
from ....domain.profile import Profile

# Tope por archivo. Un folleto de coches ronda los 5 MB; un manual escaneado
# puede pasar de 100 y no tiene sentido empujarlo entero por el proxy.
MAX_BYTES = 40 * 1024 * 1024


class LocalDocumentStore:
    """Busca el documento en la carpeta de originales que declara cada perfil.

    La metadata guarda `fuente` como nombre de archivo, no como ruta, así que la
    búsqueda es recursiva. Si dos carpetas tienen un archivo con el mismo nombre
    gana el primero por orden alfabético — poco probable en un corpus real, y
    preferible a exigir que el nombre sea único en todo el árbol.
    """

    def __init__(self, source_dirs: dict[str, str]) -> None:
        self._dirs = source_dirs

    async def fetch(self, profile: Profile, document: str) -> StoredDocument | None:
        return await asyncio.to_thread(self._fetch, profile, document)

    def _fetch(self, profile: Profile, document: str) -> StoredDocument | None:
        if not is_safe_document_name(document):
            return None
        crudo = self._dirs.get(profile.slug)
        if not crudo:
            return None
        raiz = Path(crudo).expanduser()
        if not raiz.is_dir():
            return None

        for ruta in sorted(raiz.rglob(document)):
            if not ruta.is_file():
                continue
            # `rglob` no sigue enlaces fuera del árbol, pero un enlace dentro sí
            # puede apuntar afuera: se comprueba el destino real.
            if raiz.resolve() not in ruta.resolve().parents:
                continue
            if ruta.stat().st_size > MAX_BYTES:
                return None
            return StoredDocument(
                content=ruta.read_bytes(),
                media_type=mimetypes.guess_type(ruta.name)[0] or "application/octet-stream",
                name=ruta.name,
            )
        return None
