"""Perfil: el tema sobre el que el agente puede afirmar algo.

Un perfil es la única pieza que cambia entre un RAG de credenciales
profesionales y uno de fichas técnicas de coches. Todo lo demás —contrato,
orquestación, recuperación, redacción— es idéntico. Por eso vive en el dominio
y es un dato inmutable: es revisable, testeable y comparable sin levantar nada.

Lo que un perfil NO contiene: IDs de Knowledge Base, rutas de corpus ni nada
que dependa de dónde está desplegado. Eso es enlace de despliegue y lo resuelve
la capa exterior al cargar el perfil. Un perfil describe *de qué habla el
agente y con qué reglas*, no *de dónde saca los bytes*.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .redaction import RedactionPolicy

DECLINE_PHRASE_POR_DEFECTO = "Eso no consta en los documentos disponibles."


@dataclass(frozen=True)
class ChunkPolicy:
    """Cómo se trocea un documento largo antes de indexarlo.

    Vive en el dominio porque decide qué es un «fragmento», y un fragmento es
    la unidad de evidencia que el agente puede citar. Un folleto de 40 páginas
    como fragmento único no es una decisión de formato: es un agente que cita
    'el folleto' para cualquier afirmación y un prompt que cuesta 30k tokens.

    `max_chars` en caracteres y no en tokens a propósito: el troceo corre en el
    script de ingesta, sin tokenizador del proveedor delante. Con la relación
    habitual del español (~3.5 caracteres por token) 2000 caracteres son unos
    550 tokens, que es el rango donde Titan v2 rinde bien.
    """

    max_chars: int = 2000
    overlap_chars: int = 200
    # Un documento por debajo de este tamaño no se trocea: partir una constancia
    # de media página rompe la relación entre el dato y quién lo emite.
    min_chars_to_split: int = 2600

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ValueError("max_chars debe ser positivo")
        if not 0 <= self.overlap_chars < self.max_chars:
            raise ValueError("overlap_chars debe estar entre 0 y max_chars")


@dataclass(frozen=True)
class RetrievalPolicy:
    """Cuánta evidencia se recupera y con qué piso de relevancia."""

    top_k: int = 6
    min_score: float = 0.0

    def __post_init__(self) -> None:
        if self.top_k <= 0:
            raise ValueError("top_k debe ser positivo")


@dataclass(frozen=True)
class Profile:
    """Un tema del RAG, con sus reglas duras.

    `subject` y `sources` son las dos frases que sitúan al modelo: sobre qué
    responde y con qué material. Se redactan en el YAML del perfil porque son
    lo primero que hay que ajustar al cambiar de dominio, y hacerlo no debería
    exigir tocar Python.
    """

    slug: str
    name: str
    subject: str
    sources: str = "los documentos disponibles"
    decline_phrase: str = DECLINE_PHRASE_POR_DEFECTO
    # Reglas que solo tienen sentido en este tema. Se numeran después de las
    # innegociables, nunca antes: una regla de perfil no puede relajar el
    # fundamento documental.
    extra_rules: tuple[str, ...] = ()
    redaction: RedactionPolicy = field(default_factory=RedactionPolicy.ninguna)
    retrieval: RetrievalPolicy = field(default_factory=RetrievalPolicy)
    chunking: ChunkPolicy = field(default_factory=ChunkPolicy)
    # Segmentos de la ruta del corpus que se convierten en metadatos. Con
    # `corpus/coches/toyota/hilux.pdf` y `("tema", "marca")` el fragmento sale
    # con `tema=coches, marca=toyota`, que es lo que permite filtrar y lo que
    # el prompt muestra junto a la cita.
    path_metadata: tuple[str, ...] = ()
    # Marcadores de contenido que nunca deben entrar al corpus. En el perfil de
    # CV son documentos de identidad; en otros temas puede ser material
    # confidencial de cliente. Vacío = sin veto.
    banned_markers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.slug:
            raise ValueError("un perfil necesita slug")
        if not self.subject:
            raise ValueError(f"el perfil '{self.slug}' no declara sobre qué responde")

    @property
    def masks_identifiers(self) -> bool:
        return self.redaction.enabled

    def con(self, **cambios: object) -> Profile:
        """Copia con cambios: para pruebas y para sobrescrituras por entorno."""
        return replace(self, **cambios)  # type: ignore[arg-type]


# Perfil de arranque: sirve para levantar el servicio sin ninguna configuración
# —el primer `make run` de alguien que acaba de clonar— y como base de la que
# parte el asistente de inicialización.
GENERIC = Profile(
    slug="generico",
    name="Corpus genérico",
    subject="el contenido de los documentos indexados",
    sources="los documentos que se hayan ingerido en el corpus",
)
