"""Errores de dominio.

El dominio nombra el *tipo* de fallo (contrato §5); la traducción a códigos
HTTP y a cuerpos JSON vive en el adaptador de entrada. Ningún detalle interno
—ARN, id de cuenta, traza de boto3— viaja dentro de estos objetos: se registra
con el `request_id` y el cliente recibe solo ese identificador.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorType(str, Enum):
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION_ERROR = "authentication_error"
    NOT_FOUND = "not_found"
    TOO_MANY_REQUESTS = "too_many_requests"
    SERVER_ERROR = "server_error"
    MODEL_ERROR = "model_error"


@dataclass
class AgentError(Exception):
    """Fallo expresable al cliente, ya despojado de detalle interno.

    No es `frozen`: al propagarse por un generador asíncrono, Python asigna
    `__traceback__` sobre la excepción. Congelarla convierte cada error de
    modelo en un `server_error` genérico, y el cliente pierde la distinción
    entre "falló el proveedor" y "falló el servicio".
    """

    message: str
    type: ErrorType = ErrorType.SERVER_ERROR
    param: str | None = None
    code: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def invalid_request(message: str, *, param: str | None = None, code: str | None = None) -> AgentError:
    return AgentError(message=message, type=ErrorType.INVALID_REQUEST, param=param, code=code)


def model_not_found(alias: str) -> AgentError:
    return AgentError(
        message=f"El modelo solicitado '{alias}' no existe.",
        type=ErrorType.INVALID_REQUEST,
        param="model",
        code="model_not_found",
    )


def store_not_supported() -> AgentError:
    return AgentError(
        message="Este endpoint no persiste respuestas: 'store' solo admite false.",
        type=ErrorType.INVALID_REQUEST,
        param="store",
        code="store_not_supported",
    )


def model_error(message: str = "El proveedor de inferencia falló al atender la petición.") -> AgentError:
    return AgentError(message=message, type=ErrorType.MODEL_ERROR, code="upstream_failure")
