"""Enmascarado de identificadores (contrato §6.2).

El agente confirma que una credencial existe y está vigente; el identificador
íntegro solo sale ante una petición explícita y autenticada. La regla vive en
el dominio porque es una regla de producto, no un detalle de transporte.

`StreamingRedactor` existe por un motivo concreto: en streaming un CURP puede
partirse entre dos deltas, y enmascarar delta a delta lo dejaría pasar. El
redactor retiene una cola hasta el siguiente límite de palabra, de modo que
ningún identificador se evalúa a medias.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Identificadores mexicanos presentes en el corpus.
CURP = re.compile(r"\b[A-ZÑ][AEIOUX][A-ZÑ]{2}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b")
RFC = re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b")
CEDULA = re.compile(r"(?<![\d\w])\d{7,8}(?![\d\w])")
# Teléfono: un CV trae el móvil personal. No es una credencial que haya que
# confirmar, así que se enmascara igual que un identificador.
TELEFONO = re.compile(r"(?<![\d\w])(?:\+?52[\s.-]*)?\(?\d{2,3}\)?[\s.-]*\d{3,4}[\s.-]*\d{4}(?![\d\w])")

_PATTERNS = (CURP, RFC, TELEFONO, CEDULA)

# Cola retenida en streaming: el identificador más largo (CURP, 18) con holgura.
_HOLDBACK = 24
_VISIBLE_TAIL = 4


def mask(value: str) -> str:
    """Deja visibles los últimos cuatro caracteres; el resto se enmascara."""
    if len(value) <= _VISIBLE_TAIL:
        return "*" * len(value)
    return "*" * (len(value) - _VISIBLE_TAIL) + value[-_VISIBLE_TAIL:]


def mask_identifiers(text: str, *, reveal: bool = False) -> str:
    if reveal:
        return text
    masked = text
    for pattern in _PATTERNS:
        masked = pattern.sub(lambda m: mask(m.group(0)), masked)
    return masked


def contains_identifier(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PATTERNS)


def fingerprint(text: str) -> str:
    """Huella estable para correlacionar en logs sin escribir el texto (§6.1)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class StreamingRedactor:
    """Enmascara un flujo de deltas sin partir identificadores entre trozos."""

    reveal: bool = False
    _buffer: str = ""

    def feed(self, delta: str) -> str:
        """Devuelve el trozo ya seguro de emitir (puede ser cadena vacía)."""
        self._buffer += delta
        if self.reveal:
            emitted, self._buffer = self._buffer, ""
            return emitted
        if len(self._buffer) <= _HOLDBACK:
            return ""
        # No se corta dentro de una palabra: los identificadores no llevan espacios.
        cut = self._buffer.rfind(" ", 0, len(self._buffer) - _HOLDBACK)
        if cut <= 0:
            return ""
        emitted, self._buffer = self._buffer[: cut + 1], self._buffer[cut + 1 :]
        return mask_identifiers(emitted)

    def flush(self) -> str:
        emitted, self._buffer = self._buffer, ""
        return mask_identifiers(emitted, reveal=self.reveal)
