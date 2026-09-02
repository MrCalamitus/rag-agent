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
class CleanupPolicy:
    """Qué se descarta del texto extraído antes de trocearlo.

    Vive en el dominio porque decide qué deja de ser evidencia. Y es
    deliberadamente tímida: las dos heurísticas que gobierna —quitar folios y
    quitar encabezados corridos— son inofensivas sobre prosa y destructivas
    sobre tablas numéricas, donde una línea que solo contiene «125» es un dato y
    no un número de página.

    La versión anterior descartaba cualquier número suelto estuviera donde
    estuviera, y colapsaba líneas idénticas consecutivas. Sobre un estado de
    resultados extraído con etiqueta y valor en líneas separadas eso borraba
    **todas las cifras** y dejaba el documento con todas sus etiquetas: un
    corpus que parece correcto y no puede responder nada.
    """

    # Un número suelto solo es un folio si está en el borde de su página. En
    # cualquier otra posición es contenido. `0` desactiva la regla.
    #
    # El valor es 1 —la primera o la última línea, nada más— porque con 2 ya se
    # come datos: en una tabla que acaba en «… Impuestos / 12 / - 4 -», el 12 cae
    # dentro del borde. Un folio precedido de un pie de página se queda sin
    # detectar, pero ese pie lo caza la regla de líneas corridas, y dejar un
    # número de página suelto es infinitamente menos grave que borrar una cifra.
    folio_lineas_borde: int = 1
    # Un encabezado o pie corrido es una línea que se repite a lo largo del
    # documento, no una que aparece dos veces seguidas.
    repeticion_fraccion: float = 0.6
    # …y además se repite **en el borde** de la página, que es donde viven los
    # encabezados y los pies. Una línea que reaparece en mitad de páginas
    # distintas es contenido que se repite, no decoración.
    repeticion_lineas_borde: int = 3
    # Por debajo de estas páginas no se deduplica nada: en un documento de dos
    # páginas, «repetido en la mayoría» y «aparece dos veces» son lo mismo, y
    # borrar contenido legítimo es peor que dejar un pie duplicado.
    repeticion_min_paginas: int = 3

    def __post_init__(self) -> None:
        if self.folio_lineas_borde < 0:
            raise ValueError("folio_lineas_borde no puede ser negativo")
        if self.repeticion_lineas_borde < 1:
            raise ValueError("repeticion_lineas_borde debe ser al menos 1")
        if not 0.0 < self.repeticion_fraccion <= 1.0:
            raise ValueError("repeticion_fraccion debe estar en (0, 1]")
        if self.repeticion_min_paginas < 2:
            raise ValueError("repeticion_min_paginas debe ser al menos 2")


@dataclass(frozen=True)
class OcrPolicy:
    """Qué hacer con un PDF del que no se puede extraer texto.

    Vive en el dominio porque decide **qué llega a ser evidencia**. Un folleto
    escaneado que se descarta en silencio es una pregunta que el agente
    declinará para siempre sin que nadie sepa por qué; uno transcrito mal es
    peor, porque el agente citará la transcripción como si fuera el original.

    `motor` nombra un motor de extracción, no una biblioteca: la capa exterior
    decide cuál lo cumple.

    - `ninguno`  el documento se reporta como ilegible y no entra al corpus.
    - `tablas`   extracción con estructura de tabla; conserva a qué columna
                 (versión, modelo, plan) pertenece cada valor.
    - `texto`    OCR lineal. Barato y offline, pero **aplana las tablas**: en un
                 comparativo por versiones pierde la columna, y una fila leída
                 sin su columna afirma de todas las versiones lo que solo vale
                 para una.
    """

    motor: str = "ninguno"
    # Por debajo de estos caracteres extraídos se considera que el PDF no tiene
    # capa de texto y se recurre al OCR.
    min_chars: int = 200
    # Resolución de rasterizado. 200 ppp es el punto donde los números de una
    # ficha técnica se leen sin que la página pese de más.
    dpi: int = 200
    # Tope de páginas por documento. Los motores en la nube cobran por página:
    # un tope evita que un manual de 400 páginas se convierta en una factura.
    max_paginas: int = 40
    idioma: str = "spa"
    # Caracteres por página por debajo de los cuales la transcripción se
    # considera fallida. No es un ajuste de calidad: es la diferencia entre
    # evidencia y ruido. Un folleto cuyas páginas son mapas devuelve el mismo
    # pie de página repetido, y ese documento indexado solo puede hacer daño —
    # el agente lo citaría como si respondiera a la pregunta. Medido sobre
    # fichas reales: 7.500-10.000 caracteres por página cuando hay contenido,
    # 60 cuando no. El umbral está en mitad de dos órdenes de magnitud.
    min_chars_por_pagina: int = 200
    # Cómo llamar a las columnas de una tabla comparativa al redactarla: en una
    # ficha de coches son «versiones», en una tabla trimestral «periodos». El
    # módulo de extracción no puede saberlo, y hasta ahora decía «versiones»
    # siempre, que en una tabla financiera es sencillamente falso.
    columnas: str = "columnas"

    MOTORES = ("ninguno", "tablas", "texto")

    def __post_init__(self) -> None:
        if self.motor not in self.MOTORES:
            raise ValueError(
                f"motor de OCR desconocido: '{self.motor}'. Válidos: {list(self.MOTORES)}"
            )
        if self.dpi < 72:
            raise ValueError("dpi por debajo de 72 no deja texto legible")
        if self.max_paginas <= 0:
            raise ValueError("max_paginas debe ser positivo")
        if self.min_chars_por_pagina < 0:
            raise ValueError("min_chars_por_pagina no puede ser negativo")

    @property
    def activo(self) -> bool:
        return self.motor != "ninguno"

    @property
    def conserva_tablas(self) -> bool:
        return self.motor == "tablas"


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
    cleanup: CleanupPolicy = field(default_factory=CleanupPolicy)
    ocr: OcrPolicy = field(default_factory=OcrPolicy)
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
