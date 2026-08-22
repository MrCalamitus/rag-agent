"""Construcción de clientes de boto3 con límites explícitos.

Los defaults de botocore son generosos —60 s de conexión y varios reintentos—
porque están pensados para scripts, no para una sonda detrás de un balanceador.
Aquí cada cliente declara cuánto está dispuesto a esperar:

* **Plano de control** (listar modelos): presupuesto corto y sin reintentos.
  Se usa en `readyz`, y una sonda que tarda más que el `idle_timeout` del ALB
  no informa de nada: solo produce un 504.
* **Inferencia con streaming**: lectura larga a propósito. Una respuesta
  fundamentada puede tardar; lo que no puede es quedarse colgada para siempre.
"""

from __future__ import annotations

from typing import Any

from botocore.config import Config

CONTROL_PLANE = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={"max_attempts": 1, "mode": "standard"},
)

RETRIEVAL = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)

INFERENCE = Config(
    connect_timeout=5,
    read_timeout=120,  # por debajo del idle_timeout del ALB
    retries={"max_attempts": 1, "mode": "standard"},
)


def build_client(service: str, *, region: str, profile: str | None, config: Config) -> Any:
    import boto3

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client(service, region_name=region, config=config)
