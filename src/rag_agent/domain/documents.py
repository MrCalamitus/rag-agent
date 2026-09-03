"""Permiso para abrir un documento original.

El agente ya decide, al recuperar, qué documentos puede consultar el usuario:
es la política del perfil aplicada sobre la clase que estampó la ingesta. El
problema es que esa decisión se toma al responder y el navegador pide el archivo
*después*, en otra petición que no lleva ni pregunta ni fragmentos.

Se resuelve con un permiso firmado. Al emitir la respuesta, cada fragmento
expuesto se acompaña de un enlace que lleva dentro qué documento, de qué tema y
hasta cuándo, firmado con el secreto del servicio. El endpoint que sirve el
archivo no vuelve a consultar la política: verifica la firma. Así la decisión se
toma una sola vez, en el sitio donde hay contexto para tomarla, y es imposible
pedir un documento que el agente no autorizó — ni siquiera conociendo su nombre.

La alternativa —resolver la política otra vez en el endpoint— exigiría averiguar
la clase de un documento a partir de su nombre, que es justo lo que la ingesta
existe para no tener que hacer.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from hashlib import sha256

# Ventana del permiso. Corta porque el enlace viaja en una respuesta que puede
# quedar en un historial, y larga porque alguien puede abrir el panel de fuentes
# un rato después de leer la respuesta.
GRANT_TTL_S = 30 * 60


class GrantError(ValueError):
    """El permiso no es válido: mal firmado, caducado o mal formado."""


@dataclass(frozen=True)
class DocumentGrant:
    """Qué documento, de qué tema y hasta cuándo."""

    profile: str
    document: str
    expires_at: int

    def payload(self) -> str:
        return f"{self.profile}\n{self.document}\n{self.expires_at}"


def sign_grant(grant: DocumentGrant, secret: str) -> str:
    """Firma del permiso. Hexadecimal, para que viaje en una URL sin escapes."""
    return hmac.new(secret.encode(), grant.payload().encode(), sha256).hexdigest()


def verify_grant(grant: DocumentGrant, signature: str, secret: str, *, now: int) -> None:
    """Lanza `GrantError` si el permiso no autoriza a abrir ese documento.

    El orden importa: primero la firma y después la caducidad. Comprobar la
    caducidad antes diría, ante un permiso inventado, si la fecha era plausible.
    """
    esperada = sign_grant(grant, secret)
    if not hmac.compare_digest(esperada, signature):
        raise GrantError("firma inválida")
    if grant.expires_at < now:
        raise GrantError("permiso caducado")


def is_safe_document_name(nombre: str) -> bool:
    """Un nombre de archivo, no una ruta.

    La firma ya impide pedir un documento arbitrario, pero esto es defensa en
    profundidad y cuesta tres líneas: si algún día el secreto se filtra, que el
    daño no incluya leer `../../etc/passwd`.
    """
    if not nombre or len(nombre) > 255:
        return False
    if "/" in nombre or "\\" in nombre or "\x00" in nombre:
        return False
    return nombre not in (".", "..") and not nombre.startswith(".")
