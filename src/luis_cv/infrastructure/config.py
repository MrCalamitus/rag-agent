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
# Los modelos de Anthropic en Bedrock se invocan por perfil de inferencia
# (`us.` / `global.`), no por el ID base: el ID a secas responde
# "on-demand throughput isn't supported".
DEFAULT_MODEL_ALIASES: dict[str, str] = {
    "agente-rag-sonnet": "us.anthropic.claude-sonnet-5",
    "agente-rag-haiku": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "agente-rag-gpt": "openai.gpt-oss-120b-1:0",
}

# Qué alias acepta `temperature`. Sonnet 5 la dejó deprecada y responde con un
# error de validación; Haiku 4.5 y la familia GPT siguen aceptándola.
DEFAULT_MODEL_SAMPLING: dict[str, bool] = {
    "agente-rag-sonnet": False,
    "agente-rag-haiku": True,
    "agente-rag-gpt": True,
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
    model_sampling: dict[str, bool] = Field(default_factory=lambda: dict(DEFAULT_MODEL_SAMPLING))

    retrieval_top_k: int = 6
    # Piso de relevancia. **Desactivado a propósito (0.0).**
    #
    # La idea era ahorrar tokens descartando los fragmentos que la KB devuelve
    # ante un saludo. Medido sobre este corpus con Titan v2, las bandas se
    # solapan: "Gracias" puntúa 0.586 y "¿Cuál es su número de teléfono?" 0.564.
    # Cualquier piso que filtre el ruido tumba también preguntas legítimas, y un
    # falso negativo —declinar teniendo la evidencia delante— es mucho peor que
    # pagar unos tokens de más en un saludo.
    #
    # El filtrado de lo irrelevante lo hace el prompt, que ya declina
    # correctamente aunque reciba fragmentos que no vienen al caso. Se conserva
    # el parámetro para corpus mayores, donde los scores sí se separan.
    retrieval_min_score: float = 0.0
    rate_limit_per_minute: int = 20
    request_timeout_s: float = 110.0  # por debajo del idle_timeout del ALB (120 s)
    # La sonda de readiness debe contestar mucho antes que el health check del
    # target group (15 s de intervalo, 5 s de timeout).
    readiness_timeout_s: float = 4.0

    @property
    def uses_bedrock_retrieval(self) -> bool:
        return self.retrieval_backend == "bedrock"

    @property
    def uses_bedrock_inference(self) -> bool:
        return self.inference_backend == "bedrock"
