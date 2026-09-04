"""Plan de consultas para la recuperación.

Una sola consulta literal recupera mal cuando la pregunta trae relleno
conversacional. Se añade una variante condensada en palabras de contenido, que
es la que suele acertar en un corpus de documentos cortos.
"""

from __future__ import annotations

import re
import unicodedata

_STOPWORDS = frozenset(
    """
    a al algo alguna algunas alguno algunos ante antes como con contra cual cuales cuando de del desde donde
    dos el ella ellas ellos en entre era eran es esa esas ese eso esos esta estan estas este esto estos ha
    hace hacen hasta hay la las le les lo los mas me mi mis mucho muy no nos o os otra otras otro otros para
    pero poco por porque que quien quienes se sea sean ser si sin sobre su sus tambien tanto te tiene tienen
    tu tus un una uno unos y ya
    about all and any are as at be but by can did do does for from has have how in is it its of on or that
    the their there they this to was were what when where which who why with you your
    """.split()
)

_WORD = re.compile(r"[\wÁÉÍÓÚÜÑáéíóúüñ]+", re.UNICODE)


def _fold(palabra: str) -> str:
    """Sin acentos: 'cuál' y 'cual' son la misma palabra vacía de contenido."""
    descompuesto = unicodedata.normalize("NFKD", palabra.lower())
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def condense(text: str) -> str:
    palabras = [
        w for w in _WORD.findall(text.lower()) if _fold(w) not in _STOPWORDS and len(w) > 2
    ]
    return " ".join(palabras)


def plan_queries(question: str, *, max_queries: int = 2) -> tuple[str, ...]:
    pregunta = question.strip()
    if not pregunta:
        return ()
    queries = [pregunta]
    condensada = condense(pregunta)
    if condensada and condensada.lower() != pregunta.lower():
        queries.append(condensada)
    return tuple(queries[:max_queries])
