"""Autenticación por token y límite de tasa (contrato §1 y caso D7).

El token llega por entorno desde Secrets Manager; nunca se registra ni se
compara con `==`, que filtra información por tiempo de respuesta.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ....domain.errors import AgentError, ErrorType

_BEARER = "bearer "


def authenticate(authorization: str | None, expected_token: str) -> None:
    if not authorization:
        raise AgentError(
            message="Falta la cabecera Authorization.",
            type=ErrorType.AUTHENTICATION_ERROR,
            code="missing_authorization",
        )
    if not authorization.lower().startswith(_BEARER):
        raise AgentError(
            message="La cabecera Authorization debe usar el esquema Bearer.",
            type=ErrorType.AUTHENTICATION_ERROR,
            code="invalid_authorization_scheme",
        )
    provisto = authorization[len(_BEARER) :].strip()
    if not hmac.compare_digest(provisto, expected_token):
        raise AgentError(
            message="Token de API inválido.",
            type=ErrorType.AUTHENTICATION_ERROR,
            code="invalid_token",
        )


@dataclass
class TokenBucketRateLimiter:
    """Cubeta por cliente: `limit` peticiones por minuto, recarga continua.

    Vive en el proceso: con varias tareas de ECS el límite es por tarea. Es
    suficiente como freno de cortesía; el límite duro global corresponde al
    WAF o al ALB, no a la aplicación.
    """

    limit: int = 20
    window_s: float = 60.0
    _buckets: dict[str, tuple[float, float]] = field(default_factory=lambda: defaultdict(tuple))

    def check(self, key: str, *, now: float | None = None) -> None:
        if self.limit <= 0:
            return
        ahora = time.monotonic() if now is None else now
        tokens, ultimo = self._buckets.get(key, (float(self.limit), ahora))
        tokens = min(float(self.limit), tokens + (ahora - ultimo) * (self.limit / self.window_s))
        if tokens < 1.0:
            self._buckets[key] = (tokens, ahora)
            raise AgentError(
                message="Se excedió el límite de peticiones por minuto.",
                type=ErrorType.TOO_MANY_REQUESTS,
                code="rate_limit_exceeded",
            )
        self._buckets[key] = (tokens - 1.0, ahora)

    def reset(self) -> None:
        self._buckets.clear()
