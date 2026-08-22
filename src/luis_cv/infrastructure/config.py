"""Configuración del servicio.

Los IDs de modelo, el ID de la KB y el token nunca viven en el código: se
inyectan por entorno (contrato §3). Los defaults sirven para correr en local
con los adaptadores locales; en AWS los sobrescribe la task definition y el
secreto de Secrets Manager.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Alias público → ID del proveedor. Sobrescribible con LUISCV_MODEL_ALIASES
# (JSON). En Bedrock el ID puede ser también un perfil de inferencia regional.
DEFAULT_MODEL_ALIASES: dict[str, str] = {
    "agente-rag-sonnet": "anthropic.claude-sonnet-5",
    "agente-rag-haiku": "anthropic.claude-haiku-4-5",
    "agente-rag-gpt": "openai.gpt-oss-120b-1:0",
}

DEFAULT_MODEL_FAMILIES: dict[str, str] = {
    "agente-rag-sonnet": "anthropic",
    "agente-rag-haiku": "anthropic",
    "agente-rag-gpt": "openai",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LUISCV_", env_file=".env", extra="ignore")

    environment: str = "local"
    log_level: str = "INFO"

    # Autenticación (contrato §1). En AWS llega desde Secrets Manager.
    api_token: str = "local-dev-token"
    default_model: str = "agente-rag-sonnet"

    # Backends. `local`/`stub` mantienen el servicio en pie mientras la ingesta
    # a la Knowledge Base sigue pendiente (plan E2–E3).
    retrieval_backend: str = "local"
    inference_backend: str = "stub"

    corpus_dir: str = "corpus"
    stub_delta_delay_ms: float = 0.0

    aws_region: str = "us-east-1"
    aws_profile: str | None = None
    knowledge_base_id: str | None = None
    guardrail_id: str | None = None
    guardrail_version: str = "DRAFT"

    model_aliases: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_MODEL_ALIASES))
    model_families: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_MODEL_FAMILIES))

    retrieval_top_k: int = 6
    rate_limit_per_minute: int = 20
    request_timeout_s: float = 110.0  # por debajo del idle_timeout del ALB (120 s)

    @property
    def uses_bedrock_retrieval(self) -> bool:
        return self.retrieval_backend == "bedrock"

    @property
    def uses_bedrock_inference(self) -> bool:
        return self.inference_backend == "bedrock"
