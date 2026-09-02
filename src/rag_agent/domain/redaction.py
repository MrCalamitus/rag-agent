"""Enmascarado de identificadores (contrato §6.2).

El agente confirma que un dato sensible existe; el identificador íntegro solo
sale ante una petición explícita y autenticada. La regla vive en el dominio
porque es una regla de producto, no un detalle de transporte.

**Qué se enmascara lo decide el perfil, no este módulo.** Un corpus de cédulas
profesionales necesita tapar CURP, RFC y teléfonos; uno de fichas técnicas de
coches no necesita tapar nada — y aplicarle el patrón de cédula convertiría
cualquier número de ocho cifras (una potencia, un precio, un número de parte)
en asteriscos. Por eso `RedactionPolicy` es explícita y su valor por defecto en
un perfil nuevo es «no enmascarar nada».

`StreamingRedactor` existe por un motivo concreto: en streaming un CURP puede
partirse entre dos deltas, y enmascarar delta a delta lo dejaría pasar. El
redactor retiene una cola hasta el siguiente límite de palabra, de modo que
ningún identificador se evalúa a medias.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Catálogo de patrones nombrados. El perfil elige por nombre, que es lo que
# hace legible un YAML: `redaccion: [curp, rfc, telefono]`.
PATRONES: dict[str, re.Pattern[str]] = {
    "curp": re.compile(r"\b[A-ZÑ][AEIOUX][A-ZÑ]{2}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b"),
    "rfc": re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b"),
    # Un CV trae el móvil personal. No es una credencial que haya que
    # confirmar, así que se enmascara igual que un identificador.
    "telefono": re.compile(
        r"(?<![\d\w])(?:\+?52[\s.-]*)?\(?\d{2,3}\)?[\s.-]*\d{3,4}[\s.-]*\d{4}(?![\d\w])"
    ),
    # Deliberadamente laxo: cualquier número de 7-8 cifras aislado. Solo es
    # seguro en un corpus donde esos números son cédulas y nada más.
    "cedula": re.compile(r"(?<![\d\w])\d{7,8}(?![\d\w])"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
}

# Orden de aplicación: los patrones específicos antes que los laxos, para que
# `cedula` no muerda los primeros dígitos de un teléfono ya enmascarado.
_ORDEN = ("curp", "rfc", "email", "telefono", "cedula")

# Cola retenida en streaming: el identificador más largo (CURP, 18) con holgura.
_HOLDBACK = 24
_VISIBLE_TAIL = 4


@dataclass(frozen=True)
class RedactionPolicy:
    """Qué patrones enmascara un perfil. Vacío = no enmascara nada."""

    nombres: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        desconocidos = [n for n in self.nombres if n not in PATRONES]
        if desconocidos:
            raise ValueError(
                f"patrones de redacción desconocidos: {desconocidos}. "
                f"Disponibles: {sorted(PATRONES)}"
            )

    @classmethod
    def ninguna(cls) -> RedactionPolicy:
        return cls(())

    @classmethod
    def mexicana(cls) -> RedactionPolicy:
        """Identificadores personales mexicanos: el conjunto del perfil de CV."""
        return cls(("curp", "rfc", "telefono", "cedula"))

    @property
    def enabled(self) -> bool:
        return bool(self.nombres)

    @property
    def patrones(self) -> tuple[re.Pattern[str], ...]:
        return tuple(PATRONES[n] for n in _ORDEN if n in self.nombres)


# Política por defecto de las funciones sueltas de este módulo. Es la mexicana
# por compatibilidad: quien llama a `mask_identifiers(texto)` a secas espera el
# comportamiento histórico. Los perfiles pasan la suya explícitamente.
POR_DEFECTO = RedactionPolicy.mexicana()


def mask(value: str) -> str:
    """Deja visibles los últimos cuatro caracteres; el resto se enmascara."""
    if len(value) <= _VISIBLE_TAIL:
        return "*" * len(value)
    return "*" * (len(value) - _VISIBLE_TAIL) + value[-_VISIBLE_TAIL:]


def mask_identifiers(
    text: str, *, reveal: bool = False, policy: RedactionPolicy | None = None
) -> str:
    politica = POR_DEFECTO if policy is None else policy
    if reveal or not politica.enabled:
        return text
    masked = text
    for pattern in politica.patrones:
        masked = pattern.sub(lambda m: mask(m.group(0)), masked)
    return masked


def contains_identifier(text: str, *, policy: RedactionPolicy | None = None) -> bool:
    politica = POR_DEFECTO if policy is None else policy
    return any(pattern.search(text) for pattern in politica.patrones)


def fingerprint(text: str) -> str:
    """Huella estable para correlacionar en logs sin escribir el texto (§6.1)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class StreamingRedactor:
    """Enmascara un flujo de deltas sin partir identificadores entre trozos."""

    reveal: bool = False
    policy: RedactionPolicy = field(default_factory=lambda: POR_DEFECTO)
    _buffer: str = ""

    @property
    def _passthrough(self) -> bool:
        """Sin nada que enmascarar el redactor no debe retener cola: hacerlo
        retrasaría cada delta 24 caracteres sin ganar nada, y en un perfil sin
        redacción eso es todo el streaming."""
        return self.reveal or not self.policy.enabled

    def feed(self, delta: str) -> str:
        """Devuelve el trozo ya seguro de emitir (puede ser cadena vacía)."""
        self._buffer += delta
        if self._passthrough:
            emitted, self._buffer = self._buffer, ""
            return emitted
        if len(self._buffer) <= _HOLDBACK:
            return ""
        # No se corta dentro de una palabra: los identificadores no llevan espacios.
        cut = self._buffer.rfind(" ", 0, len(self._buffer) - _HOLDBACK)
        if cut <= 0:
            return ""
        emitted, self._buffer = self._buffer[: cut + 1], self._buffer[cut + 1 :]
        return mask_identifiers(emitted, policy=self.policy)

    def flush(self) -> str:
        emitted, self._buffer = self._buffer, ""
        return mask_identifiers(emitted, reveal=self.reveal, policy=self.policy)
