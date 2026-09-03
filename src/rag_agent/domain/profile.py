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
from fnmatch import fnmatch

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
    # Qué páginas pasan por el motor. El supuesto original —«necesita motor» ⇔
    # «no tiene capa de texto»— es falso para un informe financiero nativo: su
    # capa de texto existe y es completa, pero aplana las tablas y deja una
    # columna de cifras sin fila ni encabezado, que es peor que no tenerlas.
    #
    # - `sin-texto`   solo cuando el PDF no da texto. El comportamiento histórico.
    # - `todas`       toda página, aunque haya capa de texto. Para corpus donde
    #                 casi cada página lleva tabla: evita decidir y no puede
    #                 perder ninguna.
    # - `con-tablas`  solo las páginas donde se detectan tablas. Ahorra en
    #                 documentos mayoritariamente narrativos, a cambio de que un
    #                 fallo del detector pierda una tabla en silencio. Requiere
    #                 la extra `deteccion`.
    paginas: str = "sin-texto"

    MOTORES = ("ninguno", "tablas", "texto")
    PAGINAS = ("sin-texto", "todas", "con-tablas")

    def __post_init__(self) -> None:
        if self.motor not in self.MOTORES:
            raise ValueError(
                f"motor de OCR desconocido: '{self.motor}'. Válidos: {list(self.MOTORES)}"
            )
        if self.paginas not in self.PAGINAS:
            raise ValueError(
                f"selección de páginas desconocida: '{self.paginas}'. "
                f"Válidas: {list(self.PAGINAS)}"
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

    @property
    def sobre_capa_de_texto(self) -> bool:
        """¿El motor corre también en PDFs que sí tienen texto extraíble?"""
        return self.activo and self.paginas in ("todas", "con-tablas")


@dataclass(frozen=True)
class DocumentClass:
    """Una clase de documento y las señales por las que se reconoce.

    Las tres señales no son intercambiables y por eso se aplican en un orden
    fijo —marcador, ruta, tipo—: el contenido de un documento no se puede
    cambiar renombrándolo, la carpeta es una decisión explícita de quien
    organizó el corpus, y el tipo se infiere del nombre del archivo, que es la
    señal más barata y la más fácil de equivocar.
    """

    nombre: str
    # Patrones glob contra la ruta relativa al origen: `toyota/**`, `*.pdf`.
    rutas: tuple[str, ...] = ()
    # Valores de `tipo`, el que infiere `documents.clasificar` del nombre.
    tipos: tuple[str, ...] = ()
    # Texto que, si aparece en el documento, lo mete en esta clase pase lo que
    # pase. Es la red de seguridad: un título escaneado que alguien dejó en la
    # carpeta equivocada sigue siendo un título.
    marcadores: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.nombre:
            raise ValueError("una clase de documento necesita nombre")


@dataclass(frozen=True)
class DocumentPolicy:
    """Qué documentos del corpus pueden llegar al usuario y cuáles no.

    Está partida en dos a propósito, porque las dos mitades tienen costes muy
    distintos de cambiar. **Clasificar** es una propiedad del documento y se
    estampa en la ingesta: reclasificar exige volver a preparar el corpus.
    **Exponer** es política del tema y se evalúa al responder: cambiar de
    opinión es gratis y no toca un solo byte del corpus.

    Si la exposición se hubiera resuelto entera en la ingesta, cualquier cambio
    de criterio obligaría a reingestar; si se hubiera resuelto entera al
    servir, habría que releer los documentos en cada respuesta para saber qué
    son. El reparto evita las dos cosas.

    Por defecto **no expone nada**. Es lo contrario a `RedactionPolicy`, que por
    defecto no enmascara, y la asimetría está justificada: allí equivocarse tapa
    un dato, aquí publica un archivo. Un documento que nadie clasificó, o un
    perfil que nadie configuró, no deben filtrar nada por omisión.
    """

    # Clases que este tema deja llegar al usuario. Vacío = ninguna.
    expone: tuple[str, ...] = ()
    # Clase que se asigna cuando ninguna regla acierta.
    por_defecto: str = "interno"
    clases: tuple[DocumentClass, ...] = ()

    def __post_init__(self) -> None:
        if not self.por_defecto:
            raise ValueError("'por_defecto' no puede estar vacío")
        nombres = [c.nombre for c in self.clases]
        repetidas = {n for n in nombres if nombres.count(n) > 1}
        if repetidas:
            raise ValueError(f"clases de documento repetidas: {sorted(repetidas)}")
        # Un nombre mal escrito en `expone` no expondría nada y no avisaría de
        # nada. Peor aún: si el error fuera al revés —escribir bien la clase que
        # NO se quería exponer— el silencio publicaría archivos.
        conocidas = set(nombres) | {self.por_defecto}
        desconocidas = [c for c in self.expone if c not in conocidas]
        if desconocidas:
            raise ValueError(
                f"'expone' nombra clases que no existen: {sorted(desconocidas)}. "
                f"Declaradas: {sorted(conocidas)}"
            )

    def clasificar(self, *, ruta: str = "", tipo: str = "", texto: str = "") -> str:
        """La clase de un documento, según sus señales. Nunca devuelve vacío."""
        plano = texto.upper()
        for clase in self.clases:
            if any(m.upper() in plano for m in clase.marcadores):
                return clase.nombre
        for clase in self.clases:
            if any(fnmatch(ruta, patron) for patron in clase.rutas):
                return clase.nombre
        for clase in self.clases:
            if tipo and tipo in clase.tipos:
                return clase.nombre
        return self.por_defecto

    def expuesta(self, clase: object) -> bool:
        """¿Puede el usuario consultar el documento original de esta clase?"""
        return isinstance(clase, str) and clase in self.expone

    @property
    def expone_algo(self) -> bool:
        return bool(self.expone)


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
    # Qué documentos de este corpus puede llegar a consultar el usuario. Por
    # defecto ninguno: exponer un archivo es una decisión que alguien tiene que
    # escribir, no algo que se herede por olvido.
    documents: DocumentPolicy = field(default_factory=DocumentPolicy)

    def __post_init__(self) -> None:
        if not self.slug:
            raise ValueError("un perfil necesita slug")
        if not self.subject:
            raise ValueError(f"el perfil '{self.slug}' no declara sobre qué responde")

    @property
    def masks_identifiers(self) -> bool:
        return self.redaction.enabled

    @property
    def exposes_documents(self) -> bool:
        return self.documents.expone_algo

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
