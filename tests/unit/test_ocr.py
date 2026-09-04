"""Extracción de PDFs sin capa de texto.

Lo que se prueba aquí no es «que el OCR funcione» —eso lo decide un servicio
externo— sino las reglas que convierten su salida en evidencia citable. Son las
que, si fallan, producen un corpus que parece bueno y afirma cosas falsas: una
característica de una sola versión escrita como si fuera de todas, o un folleto
de mapas indexado como si fuera una ficha técnica.
"""

from __future__ import annotations

import io
import json

import pytest

from rag_agent.domain.profile import OcrPolicy
from rag_agent.infrastructure.ingest.ocr import PaginaExtraida, ResultadoOcr, build_motor
from rag_agent.infrastructure.ingest.ocr import cache as ocr_cache
from rag_agent.infrastructure.ingest.ocr.textract import Celda, Mancha, _pagina, _umbral_de_marca

PIL = pytest.importorskip("PIL.Image")


# --- utilidades para armar una respuesta de Textract --------------------------

def _caja(izq, der, arriba=0.5, alto=0.02):
    return {"BoundingBox": {"Left": izq, "Top": arriba, "Width": der - izq, "Height": alto}}


class RespuestaFalsa:
    """Constructor de respuestas de Textract con la geometría que importa.

    Las coordenadas no son decorativas: la atribución de un valor a su columna
    se decide comparando distancias entre centros, así que una prueba con
    geometría inventada al azar no probaría nada.
    """

    def __init__(self):
        self.bloques: list[dict] = []
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"b{self._n}"

    def celda(self, fila, columna, izq, der, texto="", texto_izq=None, texto_der=None):
        hijos = []
        if texto:
            palabra = {
                "Id": self._id(), "BlockType": "WORD", "Text": texto,
                "Geometry": _caja(
                    izq + 0.01 if texto_izq is None else texto_izq,
                    der - 0.01 if texto_der is None else texto_der,
                ),
            }
            self.bloques.append(palabra)
            hijos.append(palabra["Id"])
        celda = {
            "Id": self._id(), "BlockType": "CELL", "RowIndex": fila, "ColumnIndex": columna,
            "Geometry": _caja(izq, der, arriba=0.1 * fila, alto=0.03),
        }
        if hijos:
            celda["Relationships"] = [{"Type": "CHILD", "Ids": hijos}]
        self.bloques.append(celda)
        return celda["Id"]

    def tabla(self, ids):
        self.bloques.append({
            "Id": self._id(), "BlockType": "TABLE",
            "Geometry": _caja(0.0, 1.0),
            "Relationships": [{"Type": "CHILD", "Ids": ids}],
        })

    def como_dict(self):
        return {"Blocks": self.bloques}


def _imagen_blanca(ancho=1000, alto=1400) -> bytes:
    buffer = io.BytesIO()
    PIL.new("RGB", (ancho, alto), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _imagen_con_punto(x_rel, y_rel, ancho=1000, alto=1400) -> bytes:
    """Página blanca con una viñeta: lo que hay en una celda marcada."""
    imagen = PIL.new("RGB", (ancho, alto), "white")
    cx, cy = int(x_rel * ancho), int(y_rel * alto)
    for dx in range(-5, 6):
        for dy in range(-5, 6):
            if dx * dx + dy * dy <= 25:
                imagen.putpixel((cx + dx, cy + dy), (0, 0, 0))
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG")
    return buffer.getvalue()


# Cabecera de una ficha comparativa: etiqueta + cuatro versiones.
COLUMNAS = [(0.05, 0.45), (0.45, 0.58), (0.58, 0.71), (0.71, 0.84), (0.84, 0.97)]


def _con_cabecera(r: RespuestaFalsa) -> list[str]:
    return [
        r.celda(1, 1, *COLUMNAS[0]),
        r.celda(1, 2, *COLUMNAS[1], texto="COOL"),
        r.celda(1, 3, *COLUMNAS[2], texto="STYLE"),
        r.celda(1, 4, *COLUMNAS[3], texto="CVT"),
        r.celda(1, 5, *COLUMNAS[4], texto="EXCITE"),
    ]


def _relleno(r: RespuestaFalsa, desde: int = 20, filas: int = 8) -> list[str]:
    """Filas de equipamiento vacías: lo que hace que una tabla *sea* una matriz.

    Sin ellas la tabla tiene casi todas las celdas llenas y el detector la trata
    —correctamente— como una tabla de datos cualquiera.
    """
    ids: list[str] = []
    for fila in range(desde, desde + filas):
        ids.append(r.celda(fila, 1, *COLUMNAS[0], texto=f"Equipamiento {fila}"))
        ids += [r.celda(fila, c, *COLUMNAS[c - 1]) for c in range(2, 6)]
    return ids


# --- atribución de valores a columnas -----------------------------------------


def test_un_valor_centrado_vale_para_todas_las_versiones():
    """«4,113» escrito una sola vez en mitad de la tabla significa que todas las
    versiones miden lo mismo. Atribuirlo a la columna que casualmente lo aloja
    sería una afirmación falsa sobre las otras tres."""
    r = RespuestaFalsa()
    ids = _con_cabecera(r)
    ids += [
        r.celda(2, 1, *COLUMNAS[0], texto="Largo"),
        # El texto está centrado sobre el área de valores (centro ≈ 0.71).
        r.celda(2, 3, *COLUMNAS[2], texto="4,113", texto_izq=0.70, texto_der=0.72),
    ]
    ids += _relleno(r)
    r.tabla(ids)

    texto = _pagina(r.como_dict(), _imagen_blanca(), 1).texto

    assert "Largo: 4,113" in texto
    assert "STYLE" not in texto.split("Largo:")[1]


def test_dos_valores_distintos_se_reparten_entre_sus_columnas():
    """«MT» bajo las manuales y «CVT» bajo las automáticas son dos hechos
    distintos, y fundirlos borra la diferencia que la tabla existe para marcar."""
    r = RespuestaFalsa()
    ids = _con_cabecera(r)
    ids += [
        r.celda(3, 1, *COLUMNAS[0], texto="Transmision"),
        r.celda(3, 2, *COLUMNAS[1], texto="MT", texto_izq=0.50, texto_der=0.53),
        r.celda(3, 5, *COLUMNAS[4], texto="CVT", texto_izq=0.88, texto_der=0.92),
    ]
    ids += _relleno(r)
    r.tabla(ids)

    texto = _pagina(r.como_dict(), _imagen_blanca(), 1).texto

    assert "MT (COOL)" in texto
    assert "CVT (EXCITE)" in texto


def test_un_valor_partido_entre_celdas_se_reagrupa():
    """Textract trocea «108 HP @ 4,500 rpm» en celdas contiguas porque la rejilla
    lo atraviesa. Emitirlo partido produciría dos valores donde hay uno."""
    r = RespuestaFalsa()
    ids = _con_cabecera(r)
    ids += [
        r.celda(4, 1, *COLUMNAS[0], texto="Potencia"),
        r.celda(4, 3, *COLUMNAS[2], texto="108 HP", texto_izq=0.66, texto_der=0.705),
        r.celda(4, 4, *COLUMNAS[3], texto="rpm", texto_izq=0.707, texto_der=0.75),
    ]
    ids += _relleno(r)
    r.tabla(ids)

    texto = _pagina(r.como_dict(), _imagen_blanca(), 1).texto

    assert "Potencia: 108 HP rpm" in texto


# --- viñetas -------------------------------------------------------------------


def test_una_vineta_se_lee_como_disponibilidad_de_esa_version():
    """El «•» de una ficha no es texto ni casilla: Textract devuelve la celda
    vacía. Sin detectarlo, la fila entera desaparece y la característica se
    pierde; leído mal, se atribuye a versiones que no la tienen."""
    r = RespuestaFalsa()
    ids = _con_cabecera(r)
    ids += [
        r.celda(5, 1, *COLUMNAS[0], texto="Quemacocos"),
        r.celda(5, 2, *COLUMNAS[1]),
        r.celda(5, 3, *COLUMNAS[2]),
        r.celda(5, 4, *COLUMNAS[3]),
        r.celda(5, 5, *COLUMNAS[4]),
    ]
    ids += _relleno(r)
    r.tabla(ids)
    # La viñeta se pinta en el centro de la última columna, en la fila 5.
    centro_x = (COLUMNAS[4][0] + COLUMNAS[4][1]) / 2
    imagen = _imagen_con_punto(centro_x, 0.1 * 5 + 0.015)

    texto = _pagina(r.como_dict(), imagen, 1).texto

    assert "Quemacocos: solo en EXCITE" in texto


def test_una_fila_sin_valores_ni_vinetas_se_emite_como_encabezado():
    """Ni valor ni marca: es un título de sección o una fila ilegible. En ambos
    casos se emite como encabezado, porque un encabezado no afirma nada — dejar
    la etiqueta suelta invitaría a dar por presente la característica."""
    r = RespuestaFalsa()
    ids = _con_cabecera(r)
    ids += [r.celda(6, 1, *COLUMNAS[0], texto="CONFORT")] + [
        r.celda(6, c, *COLUMNAS[c - 1]) for c in range(2, 6)
    ]
    ids += _relleno(r)
    r.tabla(ids)

    texto = _pagina(r.como_dict(), _imagen_blanca(), 1).texto

    assert "## CONFORT" in texto
    assert "CONFORT:" not in texto


# --- política -------------------------------------------------------------------


@pytest.mark.parametrize(
    "campos", [{"motor": "magia"}, {"dpi": 40}, {"max_paginas": 0}, {"min_chars_por_pagina": -1}]
)
def test_una_politica_de_ocr_incoherente_no_puede_construirse(campos):
    with pytest.raises(ValueError):
        OcrPolicy(**campos)


def test_sin_motor_no_se_construye_ninguno():
    assert build_motor(OcrPolicy()) is None


def test_el_motor_de_texto_avisa_de_que_aplana_las_tablas():
    """Aplanar una ficha comparativa produce afirmaciones falsas con aspecto de
    ciertas. Si se usa ese motor, tiene que decirse."""
    motor = build_motor(OcrPolicy(motor="texto"))
    disponible, _ = motor.disponible()
    if not disponible:
        pytest.skip("tesseract no está instalado en esta máquina")

    resultado = motor.extraer([], idioma="spa")

    assert any("estructura de tabla" in a for a in resultado.avisos)


# --- caché ----------------------------------------------------------------------


def test_la_clave_del_cache_sigue_al_contenido_y_no_a_la_ruta(tmp_path):
    """Un PDF que solo se movió de carpeta no debe volver a pagarse; uno que
    cambió, sí."""
    a = tmp_path / "a.pdf"
    a.write_bytes(b"contenido")
    movido = tmp_path / "sub" / "a.pdf"
    movido.parent.mkdir()
    movido.write_bytes(b"contenido")
    cambiado = tmp_path / "b.pdf"
    cambiado.write_bytes(b"otro contenido")

    clave = ocr_cache.clave(a, motor="tablas", dpi=200)

    assert ocr_cache.clave(movido, motor="tablas", dpi=200) == clave
    assert ocr_cache.clave(cambiado, motor="tablas", dpi=200) != clave
    assert ocr_cache.clave(a, motor="texto", dpi=200) != clave
    assert ocr_cache.clave(a, motor="tablas", dpi=300) != clave


def test_el_cache_va_y_vuelve_intacto(tmp_path):
    resultado = ResultadoOcr(
        motor="tablas",
        paginas=[PaginaExtraida(numero=1, texto="hola", confianza=97.5, tablas=1)],
        avisos=["algo"],
    )

    ocr_cache.escribir(tmp_path, "k", resultado)
    vuelto = ocr_cache.leer(tmp_path, "k")

    assert vuelto.motor == "tablas"
    assert vuelto.texto == "hola"
    assert vuelto.confianza == 97.5
    assert vuelto.avisos == ["algo"]


def test_un_cache_corrupto_se_ignora_en_vez_de_romper_la_ingesta(tmp_path):
    (tmp_path / "k.json").write_text("{no es json", encoding="utf-8")

    assert ocr_cache.leer(tmp_path, "k") is None


def test_un_cache_de_otra_version_del_formato_no_se_reutiliza(tmp_path):
    (tmp_path / "k.json").write_text(json.dumps({"paginas": []}), encoding="utf-8")

    assert ocr_cache.leer(tmp_path, "k") is None


# --- qué formas de tabla se interpretan y cuáles no ---------------------------


def _tabla_financiera(r: RespuestaFalsa) -> list[str]:
    """Filas y columnas llenas de cifras: una tabla de datos, no una matriz."""
    ids = [
        r.celda(1, 1, 0.05, 0.45, texto="Concepto"),
        r.celda(1, 2, 0.45, 0.71, texto="2024"),
        r.celda(1, 3, 0.71, 0.97, texto="2025"),
    ]
    for fila, (concepto, a, b) in enumerate(
        [("Ingresos", "125", "140"), ("Costos", "87", "92"), ("Margen", "38", "48")], start=2
    ):
        ids += [
            r.celda(fila, 1, 0.05, 0.45, texto=concepto),
            r.celda(fila, 2, 0.45, 0.71, texto=a),
            r.celda(fila, 3, 0.71, 0.97, texto=b),
        ]
    return ids


def test_una_tabla_de_datos_no_se_interpreta_como_ficha_comparativa():
    """Las frases «solo en X» y «vale para todas» afirman algo sobre el
    significado de la rejilla. En un balance ese significado no existe, y
    redactarlo igualmente inventaría un sentido que la tabla no tiene."""
    r = RespuestaFalsa()
    r.tabla(_tabla_financiera(r))

    texto = _pagina(r.como_dict(), _imagen_blanca(), 1).texto

    assert "solo en" not in texto
    assert "en todas las" not in texto


def test_una_tabla_de_datos_conserva_cada_cifra_junto_a_su_columna():
    """Cada fila lleva sus encabezados dentro: un corte de troceado no puede
    dejar una cifra huérfana de la columna a la que pertenece."""
    r = RespuestaFalsa()
    r.tabla(_tabla_financiera(r))

    texto = _pagina(r.como_dict(), _imagen_blanca(), 1).texto

    assert "Concepto: Ingresos | 2024: 125 | 2025: 140" in texto
    assert "Concepto: Margen | 2024: 38 | 2025: 48" in texto


def test_el_nombre_de_las_columnas_lo_pone_el_perfil():
    """«en todas las versiones» es vocabulario de coches. En una tabla
    trimestral la misma frase sería sencillamente falsa."""
    r = RespuestaFalsa()
    ids = _con_cabecera(r) + [
        r.celda(7, 1, *COLUMNAS[0], texto="Cobertura"),
        *[r.celda(7, c, *COLUMNAS[c - 1]) for c in range(2, 6)],
    ]
    ids += _relleno(r)
    r.tabla(ids)
    texto = _pagina(r.como_dict(), _imagen_blanca(), 1, "periodos").texto

    assert "versiones" not in texto


def test_sin_separacion_clara_no_se_emite_ninguna_marca():
    """Si todas las celdas tienen algo de tinta —un sombreado, un icono— no hay
    forma de saber cuál es una viñeta. Antes se aplicaba una banda fija afinada
    sobre un corpus y cualquier adorno de otro documento pasaba por marca."""
    manchas = [Mancha(fraccion=0.02, compacta=True) for _ in range(8)]

    assert _umbral_de_marca(manchas) is None


def test_una_mancha_ancha_no_es_una_vineta():
    """Un sombreado de fondo cubre la celda; una viñeta es pequeña y centrada."""
    sombreada = Mancha(fraccion=0.005, compacta=False)
    vineta = Mancha(fraccion=0.005, compacta=True)

    assert not sombreada.posible_marca
    assert vineta.posible_marca


def test_una_celda_sombreada_no_se_lee_como_marcada():
    celda = Celda(
        fila=2, columna=3, texto="", rejilla_izq=0.4, rejilla_der=0.6,
        mancha=Mancha(fraccion=0.01, compacta=False),
    )

    assert not celda.marcada(umbral=0.002)


def test_una_maquetacion_en_rejilla_no_recibe_encabezados_inventados():
    """Textract marca como TABLE una plana de folleto con bloques de texto en
    fila. Tomar la primera fila como encabezados empareja cosas que no van
    juntas, y ese emparejamiento es una afirmación falsa."""
    r = RespuestaFalsa()
    ids = []
    bloques = [
        ("1030 km Autonomía combinada NEDC", "4.9 s Aceleración de 0 a 100 km/h", "152 km Autonomía eléctrica"),
        ("3 filas de asientos", "Control de amortiguación", "Tracción integral"),
        ("Capacidad para 7 pasajeros", "Suprime balanceo y cabeceo", "AWD inteligente"),
    ]
    for fila, celdas in enumerate(bloques, start=1):
        for columna, texto in enumerate(celdas, start=1):
            ids.append(r.celda(fila, columna, 0.05 + 0.3 * (columna - 1), 0.32 + 0.3 * (columna - 1), texto=texto))
    r.tabla(ids)

    texto = _pagina(r.como_dict(), _imagen_blanca(), 1).texto

    assert "1030 km Autonomía combinada NEDC: 3 filas de asientos" not in texto
    assert "3 filas de asientos | Control de amortiguación | Tracción integral" in texto
