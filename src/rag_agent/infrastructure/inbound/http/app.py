"""Adaptador de entrada HTTP (FastAPI).

Traduce HTTP al comando del núcleo y el resultado al protocolo. Ninguna regla
de negocio vive aquí: si algo de este archivo decidiera *qué* responde el
agente, estaría en la capa equivocada.

Detalle deliberado del modo streaming: el primer evento se consume **antes** de
devolver la respuesta. Así un alias inexistente sigue siendo un 400 con
cabeceras, y no un error a medio stream que el cliente ya no puede distinguir
de una respuesta corta.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ....domain.errors import AgentError, ErrorType, invalid_request
from ....domain.items import ResponseStatus
from ...config import Settings
from ...container import Container, build_container
from .schemas import CreateResponseRequest, as_agent_error, unknown_fields
from .security import TokenBucketRateLimiter, authenticate
from .serializers import error_to_dict, response_skeleton
from .sse import DONE, OpenResponsesTranslator, format_sse

STATUS_BY_ERROR = {
    ErrorType.INVALID_REQUEST: 400,
    ErrorType.AUTHENTICATION_ERROR: 401,
    ErrorType.NOT_FOUND: 404,
    ErrorType.TOO_MANY_REQUESTS: 429,
    ErrorType.SERVER_ERROR: 500,
    ErrorType.MODEL_ERROR: 500,
}

# Extensión al contrato, no una ruptura: un mismo despliegue sirve varios temas
# y el cliente elige el suyo por cabecera. Va en cabecera y no en el cuerpo a
# propósito — el cuerpo es el de Open Responses y rechaza campos desconocidos,
# así que un cliente estándar sigue funcionando y recibe el tema por defecto.
PROFILE_HEADER = "x-rag-profile"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Defensivo contra proxies que acumulan la respuesta: sin esto el stream
    # llega de golpe al final y deja de existir en la práctica (contrato §1).
    "X-Accel-Buffering": "no",
}


def create_app(container: Container | None = None, settings: Settings | None = None) -> FastAPI:
    container = container or build_container(settings)
    app = FastAPI(
        title="rag-agent",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.container = container
    app.state.rate_limiter = TokenBucketRateLimiter(limit=container.settings.rate_limit_per_minute)
    app.include_router(_router())
    _install_handlers(app)
    return app


def _router() -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> JSONResponse:  # liveness, sin auth (caso D1)
        return JSONResponse({"status": "ok"})

    @router.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        container: Container = request.app.state.container
        report = await container.check_readiness()
        return JSONResponse(report.as_dict(), status_code=200 if report.ready else 503)

    @router.get("/v1/profiles")
    async def profiles(request: Request) -> JSONResponse:
        """Temas que sirve este despliegue.

        Existe para que un cliente —el menú interactivo, sin ir más lejos— pueda
        descubrir qué puede preguntar sin que nadie le pase una lista a mano.
        Va autenticado: la lista de temas describe qué documentación hay
        indexada, y eso ya es información.
        """
        container: Container = request.app.state.container
        authenticate(request.headers.get("authorization"), container.settings.api_token)
        registro = container.profiles
        return JSONResponse(
            {
                "default": registro.default.slug,
                "data": [
                    {
                        "id": binding.profile.slug,
                        "name": binding.profile.name,
                        "subject": binding.profile.subject,
                        "masks_identifiers": binding.profile.masks_identifiers,
                    }
                    for binding in registro.bindings()
                ],
            }
        )

    @router.post("/v1/responses")
    async def create_response(request: Request) -> Any:
        container: Container = request.app.state.container
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex

        authenticate(request.headers.get("authorization"), container.settings.api_token)
        request.app.state.rate_limiter.check(_client_key(request))

        payload = _parse_body(await request.body(), request.headers.get("content-type"))
        try:
            peticion = CreateResponseRequest.model_validate(payload)
        except ValidationError as exc:
            raise as_agent_error(exc) from None

        desconocidos = unknown_fields(payload)
        if desconocidos:
            container.telemetry.warning(
                "request.unknown_fields", request_id=request_id, fields=desconocidos
            )

        comando = peticion.to_command(
            request_id=request_id, profile_slug=request.headers.get(PROFILE_HEADER)
        )
        container.telemetry.event(
            "request.accepted",
            request_id=request_id,
            model=comando.model_alias,
            profile=comando.profile_slug or container.profiles.default.slug,
            stream=peticion.stream,
            turns=len(comando.conversation.turns),
            metadata=comando.settings.metadata or None,
        )

        if not peticion.stream:
            respuesta = await container.create_response.execute(comando)
            from .serializers import response_to_dict

            return JSONResponse(
                response_to_dict(respuesta),
                headers={"X-Request-Id": request_id},
                media_type="application/json",
            )

        eventos = container.create_response.stream(comando)
        # Se consume el primer evento aquí: los fallos previos al stream deben
        # poder devolver un código de estado HTTP.
        primero = await eventos.__anext__()
        return StreamingResponse(
            _sse(primero, eventos, container, request_id),
            media_type="text/event-stream",
            headers={**SSE_HEADERS, "X-Request-Id": request_id},
        )

    return router


async def _sse(primero, eventos, container: Container, request_id: str) -> AsyncIterator[str]:
    translator = OpenResponsesTranslator()
    try:
        for payload in translator.translate(primero):
            yield format_sse(payload["type"], payload)
        async for evento in eventos:
            for payload in translator.translate(evento):
                yield format_sse(payload["type"], payload)
        yield DONE
    finally:
        # El cliente cortó (caso B10): cerrar el generador cancela la
        # inferencia río arriba en lugar de dejar una tarea colgada.
        await eventos.aclose()
        container.telemetry.event("stream.closed", request_id=request_id)


def _client_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization:
        # Nunca se guarda el token: solo un identificador estable derivado.
        import hashlib

        return hashlib.sha256(authorization.encode("utf-8")).hexdigest()[:16]
    return request.client.host if request.client else "anonimo"


def _parse_body(raw: bytes, content_type: str | None) -> dict[str, Any]:
    if not content_type or not content_type.split(";")[0].strip().lower() == "application/json":
        raise invalid_request(
            "El cuerpo debe enviarse como application/json.",
            param="Content-Type",
            code="unsupported_media_type",
        )
    try:
        payload = json.loads(raw or b"")
    except json.JSONDecodeError:
        # Nunca se propaga la traza ni la posición exacta del error (§5).
        raise invalid_request(
            "El cuerpo no es JSON válido.", code="invalid_json"
        ) from None
    if not isinstance(payload, dict):
        raise invalid_request("El cuerpo debe ser un objeto JSON.", code="invalid_json")
    return payload


def _install_handlers(app: FastAPI) -> None:
    @app.exception_handler(AgentError)
    async def _agent_error(request: Request, exc: AgentError) -> JSONResponse:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        container: Container = request.app.state.container
        container.telemetry.warning(
            "request.rejected",
            request_id=request_id,
            error_type=exc.type.value,
            code=exc.code,
            param=exc.param,
        )
        return JSONResponse(
            error_to_dict(exc, request_id=request_id),
            status_code=STATUS_BY_ERROR[exc.type],
            headers={"X-Request-Id": request_id},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        tipo = {
            401: ErrorType.AUTHENTICATION_ERROR,
            404: ErrorType.NOT_FOUND,
            405: ErrorType.NOT_FOUND,
            429: ErrorType.TOO_MANY_REQUESTS,
        }.get(exc.status_code, ErrorType.INVALID_REQUEST if exc.status_code < 500 else ErrorType.SERVER_ERROR)
        error = AgentError(message=_http_message(exc), type=tipo)
        return JSONResponse(error_to_dict(error), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        container: Container = request.app.state.container
        container.telemetry.event(
            "request.failed", request_id=request_id, exception=type(exc).__name__
        )
        error = AgentError(
            message="Ocurrió un fallo interno.",
            type=ErrorType.SERVER_ERROR,
            code="internal_error",
        )
        return JSONResponse(
            error_to_dict(error, request_id=request_id),
            status_code=500,
            headers={"X-Request-Id": request_id},
        )


def _http_message(exc: StarletteHTTPException) -> str:
    if exc.status_code == 404:
        return "La ruta solicitada no existe."
    if exc.status_code == 405:
        return "El método no está permitido en esta ruta."
    return str(exc.detail) if exc.detail else "La petición no pudo atenderse."


__all__ = ["create_app", "response_skeleton", "ResponseStatus"]
