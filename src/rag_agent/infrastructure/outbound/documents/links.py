"""Enlaces firmados a documentos originales."""

from __future__ import annotations

from urllib.parse import quote

from ....domain.documents import GRANT_TTL_S, DocumentGrant, is_safe_document_name, sign_grant
from ....domain.profile import Profile

# Relativa a propósito: el servicio no sabe bajo qué host lo publican —ALB,
# CloudFront, el proxy de una UI— y una URL absoluta armada con la cabecera
# `Host` es una manera conocida de acabar firmando enlaces a otro sitio.
RUTA = "/v1/documents"


class SignedDocumentLinks:
    def __init__(self, secret: str, clock, *, ttl_s: int = GRANT_TTL_S) -> None:
        self._secret = secret
        self._clock = clock
        self._ttl_s = ttl_s

    def link_for(self, profile: Profile, document: str) -> str | None:
        if not document or not is_safe_document_name(document):
            return None
        grant = DocumentGrant(
            profile=profile.slug,
            document=document,
            expires_at=int(self._clock.unix_seconds()) + self._ttl_s,
        )
        firma = sign_grant(grant, self._secret)
        return (
            f"{RUTA}/{quote(document)}"
            f"?profile={quote(profile.slug)}&exp={grant.expires_at}&sig={firma}"
        )
