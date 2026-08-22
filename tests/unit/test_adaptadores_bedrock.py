"""Adaptadores de Bedrock con un cliente falso: sin red, sin credenciales.

Este archivo es el que evita que el cambio de recuperación local a Bedrock KB
—el pendiente de la ingesta— se estrene sin haberse ejecutado nunca.
"""

from __future__ import annotations

import pytest

from luis_cv.application.ports import ModelDescriptor, TextChunk, UsageReport
from luis_cv.domain.conversation import Conversation, GenerationSettings, Role, Turn
from luis_cv.domain.errors import AgentError, ErrorType
from luis_cv.infrastructure.outbound.bedrock.knowledge_base import BedrockKnowledgeBase
from luis_cv.infrastructure.outbound.bedrock.language_model import BedrockLanguageModel

MODELO = ModelDescriptor("agente-rag-sonnet", "anthropic.claude-sonnet-5")
CONVERSACION = Conversation(
    (
        Turn(Role.USER, "¿Está titulado?"),
        Turn(Role.ASSISTANT, "Sí."),
        Turn(Role.USER, "¿En qué año?"),
    )
)


class ClienteKbFalso:
    def __init__(self, resultados: dict[str, list[dict]]) -> None:
        self.resultados = resultados
        self.llamadas: list[dict] = []

    def retrieve(self, **kwargs):
        self.llamadas.append(kwargs)
        consulta = kwargs["retrievalQuery"]["text"]
        return {"retrievalResults": self.resultados.get(consulta, [])}


def _resultado(uri: str, texto: str, score: float, metadata: dict | None = None) -> dict:
    return {
        "content": {"text": texto},
        "location": {"type": "S3", "s3Location": {"uri": uri}},
        "score": score,
        "metadata": {"x-amz-bedrock-kb-source-uri": uri, **(metadata or {})},
    }


async def test_la_recuperacion_usa_el_nombre_de_archivo_como_document_id():
    cliente = ClienteKbFalso(
        {"cédula": [_resultado("s3://corpus/cedula-profesional-2020.pdf", "texto", 0.87, {"anio": 2020})]}
    )
    kb = BedrockKnowledgeBase(knowledge_base_id="kb-1", region="us-east-1", client=cliente)

    outcome = await kb.retrieve(["cédula"], top_k=3)

    (chunk,) = outcome.chunks
    assert chunk.document_id == "cedula-profesional-2020.pdf"
    assert chunk.score == 0.87
    assert chunk.metadata == {"anio": 2020}, "los metadatos internos de Bedrock no se propagan"
    assert cliente.llamadas[0]["knowledgeBaseId"] == "kb-1"
    assert cliente.llamadas[0]["retrievalConfiguration"]["vectorSearchConfiguration"][
        "numberOfResults"
    ] == 3


async def test_varias_consultas_se_fusionan_conservando_el_mejor_score():
    cliente = ClienteKbFalso(
        {
            "consulta larga": [_resultado("s3://c/titulo.pdf", "a", 0.55)],
            "titulo": [_resultado("s3://c/titulo.pdf", "a", 0.91), _resultado("s3://c/cv.pdf", "b", 0.4)],
        }
    )
    kb = BedrockKnowledgeBase(knowledge_base_id="kb-1", region="us-east-1", client=cliente)

    outcome = await kb.retrieve(["consulta larga", "titulo"])

    assert [c.document_id for c in outcome.chunks] == ["titulo.pdf", "cv.pdf"]
    assert outcome.chunks[0].score == 0.91
    assert outcome.queries == ("consulta larga", "titulo")
    assert outcome.latency_ms >= 0


async def test_un_fallo_de_la_kb_no_filtra_el_detalle_interno():
    class Explota:
        def retrieve(self, **kwargs):
            raise RuntimeError("arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123")

    kb = BedrockKnowledgeBase(knowledge_base_id="kb-1", region="us-east-1", client=Explota())

    with pytest.raises(AgentError) as exc:
        await kb.retrieve(["x"])

    assert exc.value.type is ErrorType.SERVER_ERROR
    assert "arn:aws" not in exc.value.message
    assert "123456789012" not in exc.value.message


async def test_is_available_es_false_si_la_kb_no_responde():
    class Explota:
        def retrieve(self, **kwargs):
            raise RuntimeError("sin acceso")

    kb = BedrockKnowledgeBase(knowledge_base_id="kb-1", region="us-east-1", client=Explota())

    assert await kb.is_available() is False


class ClienteModeloFalso:
    def __init__(self, eventos: list[dict]) -> None:
        self.eventos = eventos
        self.llamadas: list[dict] = []

    def converse_stream(self, **kwargs):
        self.llamadas.append(kwargs)
        return {"stream": iter(self.eventos)}


def _delta(texto: str) -> dict:
    return {"contentBlockDelta": {"delta": {"text": texto}}}


async def _consumir(modelo: BedrockLanguageModel) -> list:
    return [
        chunk
        async for chunk in modelo.stream(
            model=MODELO,
            system_prompt="reglas",
            conversation=CONVERSACION,
            settings=GenerationSettings(max_output_tokens=256, temperature=0.2),
        )
    ]


async def test_converse_stream_se_traduce_a_deltas_y_uso():
    cliente = ClienteModeloFalso(
        [
            {"messageStart": {"role": "assistant"}},
            _delta("Según "),
            _delta("el documento"),
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 1840, "outputTokens": 96}}},
        ]
    )
    modelo = BedrockLanguageModel(region="us-east-1", client=cliente)

    trozos = await _consumir(modelo)

    assert trozos[:2] == [TextChunk("Según "), TextChunk("el documento")]
    assert trozos[-1] == UsageReport(input_tokens=1840, output_tokens=96)


async def test_la_peticion_lleva_el_historial_el_sistema_y_los_ajustes():
    cliente = ClienteModeloFalso([_delta("ok")])
    modelo = BedrockLanguageModel(region="us-east-1", client=cliente)

    await _consumir(modelo)

    enviado = cliente.llamadas[0]
    assert enviado["modelId"] == "anthropic.claude-sonnet-5"
    assert enviado["system"] == [{"text": "reglas"}]
    assert [m["role"] for m in enviado["messages"]] == ["user", "assistant", "user"]
    assert enviado["inferenceConfig"] == {"maxTokens": 256, "temperature": 0.2}
    assert "guardrailConfig" not in enviado


async def test_no_se_envia_temperature_a_un_modelo_que_la_deprecó():
    """Bedrock responde ValidationException, no una advertencia: el parámetro
    simplemente no puede viajar."""
    cliente = ClienteModeloFalso([_delta("ok")])
    modelo = BedrockLanguageModel(region="us-east-1", client=cliente)
    sin_muestreo = ModelDescriptor("agente-rag-sonnet", "us.anthropic.claude-sonnet-5", supports_sampling=False)

    async for _ in modelo.stream(
        model=sin_muestreo,
        system_prompt="reglas",
        conversation=CONVERSACION,
        settings=GenerationSettings(max_output_tokens=256, temperature=0.2),
    ):
        pass

    config = cliente.llamadas[0]["inferenceConfig"]
    assert config == {"maxTokens": 256}, "temperature no puede llegar al proveedor"


async def test_los_deltas_de_razonamiento_no_llegan_al_cliente():
    """Las familias con pensamiento adaptativo emiten bloques de razonamiento en
    el mismo stream. El contrato §0 los deja fuera: no se expone traza interna.
    """
    cliente = ClienteModeloFalso(
        [
            {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "deliberando..."}}}},
            _delta("Respuesta."),
        ]
    )
    modelo = BedrockLanguageModel(region="us-east-1", client=cliente)

    trozos = await _consumir(modelo)

    assert [c for c in trozos if isinstance(c, TextChunk)] == [TextChunk("Respuesta.")]


async def test_el_guardrail_se_aplica_cuando_esta_configurado():
    cliente = ClienteModeloFalso([_delta("ok")])
    modelo = BedrockLanguageModel(region="us-east-1", client=cliente, guardrail_id="gr-1")

    await _consumir(modelo)

    assert cliente.llamadas[0]["guardrailConfig"]["guardrailIdentifier"] == "gr-1"


async def test_el_throttling_del_proveedor_se_traduce_a_too_many_requests():
    cliente = ClienteModeloFalso([_delta("a"), {"throttlingException": {"message": "slow down"}}])
    modelo = BedrockLanguageModel(region="us-east-1", client=cliente)

    with pytest.raises(AgentError) as exc:
        await _consumir(modelo)

    assert exc.value.type is ErrorType.TOO_MANY_REQUESTS


async def test_un_fallo_del_proveedor_es_model_error_sin_detalle():
    class Explota:
        def converse_stream(self, **kwargs):
            raise RuntimeError("AccessDeniedException: arn:aws:bedrock:...")

    modelo = BedrockLanguageModel(region="us-east-1", client=Explota())

    with pytest.raises(AgentError) as exc:
        await _consumir(modelo)

    assert exc.value.type is ErrorType.MODEL_ERROR
    assert "arn:aws" not in exc.value.message
