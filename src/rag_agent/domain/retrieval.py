"""Evidencia documental: el material con el que el agente puede afirmar algo."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .profile import DocumentPolicy


@dataclass(frozen=True)
class Chunk:
    """Fragmento recuperado, con su procedencia."""

    document_id: str
    text: str
    score: float
    metadata: dict[str, object] = field(default_factory=dict)
    # ¿Puede el usuario consultar el documento original de donde salió?
    # Se resuelve al responder, aplicando la política del perfil sobre la clase
    # que la ingesta estampó. Va como campo propio y no como un metadato más
    # porque es una decisión ya tomada: un cliente no debería tener que conocer
    # la política del tema para saber si puede ofrecer el documento.
    exposed: bool = False

    @property
    def citation(self) -> str:
        """Con qué nombre se cita este fragmento en la respuesta.

        El del documento original —`ficha-tecnica-hilux.pdf`— y no el
        `document_id`, que es el nombre del trozo de markdown que produjo la
        ingesta: `ficha-tecnica-hilux--003.md` no existe para nadie fuera del
        corpus, y una cita que el lector no puede buscar no es una cita.

        Dos fragmentos del mismo PDF citan igual, y está bien: al lector le
        importa qué archivo abrir, no qué trozo del índice acertó.
        """
        fuente = self.metadata.get("fuente")
        return fuente if isinstance(fuente, str) and fuente else self.document_id


@dataclass(frozen=True)
class RetrievalOutcome:
    """Recibo de una recuperación: qué se preguntó, qué volvió y cuánto tardó."""

    queries: tuple[str, ...]
    chunks: tuple[Chunk, ...]
    latency_ms: int

    @property
    def is_empty(self) -> bool:
        return len(self.chunks) == 0

    def documents(self) -> tuple[str, ...]:
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk.document_id not in seen:
                seen.append(chunk.document_id)
        return tuple(seen)

    def citations(self) -> tuple[str, ...]:
        """Nombres con los que la respuesta puede citar esta evidencia.

        Distinto de `documents()`: ahí cada trozo cuenta por separado, que es lo
        que mide la telemetría; aquí ocho fragmentos de dos PDF son dos nombres,
        que es lo que puede aparecer entre corchetes.
        """
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk.citation not in seen:
                seen.append(chunk.citation)
        return tuple(seen)


def aplicar_exposicion(outcome: RetrievalOutcome, policy: DocumentPolicy) -> RetrievalOutcome:
    """Marca cada fragmento según lo que el perfil deje consultar.

    Un fragmento no expuesto **no se oculta**: sigue apareciendo con su nombre,
    su score y su texto. Lo único que falta es el original. Esconder la
    evidencia entera sería peor que no dar el archivo, porque el recibo de qué
    sustentó la respuesta es justo lo que hace auditable al agente.
    """
    if not outcome.chunks:
        return outcome
    return replace(
        outcome,
        chunks=tuple(
            replace(chunk, exposed=policy.expuesta(chunk.metadata.get("clase")))
            for chunk in outcome.chunks
        ),
    )
