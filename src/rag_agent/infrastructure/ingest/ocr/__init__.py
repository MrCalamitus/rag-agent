"""Extracción de PDFs sin capa de texto.

Un PDF de imagen no es un caso raro en un corpus real: fichas técnicas
exportadas como imagen, folletos de diseño con el texto convertido a curvas,
escaneos. Descartarlos en silencio deja preguntas que el agente declinará para
siempre sin que nadie sepa por qué.

El protocolo `MotorOcr` está aquí y no en `application/ports.py` a propósito: el
servicio nunca hace OCR. Es una necesidad de la herramienta de ingesta, y meter
en los puertos del núcleo algo que el núcleo no usa sería ensuciar la frontera
que el resto del proyecto defiende.

Dos motores, y la diferencia entre ellos importa más de lo que parece:

- `tablas` (Textract) conserva la rejilla. En una ficha técnica comparativa
  —filas de características, columnas de versiones— es la única forma de saber
  que el quemacocos es de la versión EXCITE y no de todas.
- `texto` (tesseract) es gratis y offline, pero aplana. Sirve para prosa; sobre
  un comparativo produce afirmaciones falsas con aspecto de ciertas.

**Por qué no un modelo de visión.** Un modelo multimodal leería esta página sin
esfuerzo, incluidas las viñetas. Se descartó: el proyecto entero se sostiene
sobre que cada afirmación sea rastreable hasta un documento, y un modelo que
*transcribe* puede completar una cifra ilegible con una plausible. Un error de
OCR es un error visible y reproducible; una alucinación en la capa de extracción
es indistinguible de un dato real y contamina el corpus de forma permanente.

**La única excepción prevista, y por qué no contradice lo anterior.** Una gráfica
rasterizada no tiene capa de texto ni rejilla: Textract devuelve las etiquetas de
sus ejes —`220 210 200 (8.5%)`— sin relación entre sí, que es ruido con forma de
dato. Ahí la alternativa a un modelo de visión no es un OCR peor: es *nada*. La
excepción se admite con tres condiciones: solo sobre regiones de figura, nunca
sustituyendo texto extraíble, y marcada en `origen_texto` con su confianza para
que una cifra leída de una gráfica no se cite como una publicada.

**Aún no está conectada a esta ingesta.** Se validó en el laboratorio —sobre la
gráfica anual de una acción el modelo leyó 172 y 185 contra los 171,52 y 184,57
que publica la tabla del mismo documento— pero requiere aislar antes las
regiones de figura, y para eso hace falta pedir `LAYOUT` junto a `TABLES`, que
hoy no se pide. Queda anotado como la fase siguiente, no como algo hecho.

`OcrPolicy.paginas` decide sobre qué páginas corre el motor. El supuesto
original —«necesita motor» ⇔ «no tiene capa de texto»— es falso para un informe
financiero nativo: su capa de texto está completa y aun así devuelve las tablas
aplanadas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PaginaExtraida:
    """Texto de una página, con lo que hizo falta para conseguirlo."""

    numero: int
    texto: str
    # Confianza media declarada por el motor, en [0, 100]. `None` si el motor no
    # la reporta. Se propaga a los metadatos: una cita que procede de una
    # transcripción automática no vale lo mismo que una del original.
    confianza: float | None = None
    tablas: int = 0


@dataclass
class ResultadoOcr:
    motor: str
    paginas: list[PaginaExtraida] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def texto(self) -> str:
        return "\n\n".join(p.texto for p in self.paginas if p.texto.strip())

    @property
    def confianza(self) -> float | None:
        valores = [p.confianza for p in self.paginas if p.confianza is not None]
        return round(sum(valores) / len(valores), 1) if valores else None

    @property
    def tablas(self) -> int:
        return sum(p.tablas for p in self.paginas)


@runtime_checkable
class MotorOcr(Protocol):
    nombre: str

    def disponible(self) -> tuple[bool, str]:
        """¿Se puede usar aquí? Devuelve el motivo cuando no."""

    def paginas_por_documento(self, total: int) -> int:
        """Cuántas páginas cobrará/procesará. Para el aviso de costo."""

    def extraer(self, imagenes: list[bytes], *, idioma: str) -> ResultadoOcr:
        """Transcribe páginas ya rasterizadas, en orden."""


class OcrNoDisponible(RuntimeError):
    """El motor pedido no se puede usar en esta máquina."""


def build_motor(policy) -> MotorOcr | None:
    """Motor que cumple la política, o `None` si la política no pide ninguno.

    El perfil y la región de AWS salen de la configuración del proyecto —el
    mismo `.env` que usa el servicio— para que preparar el corpus no exija
    exportar variables a mano que ya están escritas en algún sitio.
    """
    if not policy.activo:
        return None
    if policy.motor == "tablas":
        from ...config import Settings
        from .textract import TextractOcr

        ajustes = Settings()
        return TextractOcr(
            dpi=policy.dpi,
            region=ajustes.aws_region,
            profile=ajustes.aws_profile,
            columnas=policy.columnas,
        )
    from .tesseract import TesseractOcr

    return TesseractOcr()
