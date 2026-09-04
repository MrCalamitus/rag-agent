"""Ingesta del corpus: las reglas que impiden un error irreversible.

Dos de ellas no se arreglan con un `rm` si fallan: vetar material que el perfil
prohíbe indexar, y no escribir datos sensibles dentro del repositorio. Las
demás —metadatos desde la ruta, troceado de documentos largos— son las que
hacen que el mismo pipeline sirva para credenciales y para folletos.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_agent.domain.profile import ChunkPolicy, Profile
from rag_agent.domain.redaction import RedactionPolicy
from rag_agent.infrastructure.ingest import (
    DestinoInvalido,
    escribir,
    marcador_vetado,
    metadata_de_ruta,
    preparar,
    slug,
    validar_destino,
)
from rag_agent.infrastructure.ingest.documents import clasificar
from rag_agent.infrastructure.profiles import load_profiles

RAIZ = Path(__file__).resolve().parents[2]
FIXTURES = RAIZ / "tests" / "fixtures" / "corpus"

VETADOS = ("CREDENCIAL PARA VOTAR", "CLAVE DE ELECTOR", "PASAPORTE", "LICENCIA DE CONDUCIR")

SENSIBLE = Profile(
    slug="sensible",
    name="Sensible",
    subject="credenciales",
    redaction=RedactionPolicy.mexicana(),
    banned_markers=VETADOS,
)
PUBLICO = Profile(slug="publico", name="Público", subject="folletos")


@pytest.mark.parametrize(
    "texto",
    [
        "INSTITUTO NACIONAL ELECTORAL — CREDENCIAL PARA VOTAR",
        "CLAVE DE ELECTOR AAAABB000000H000",
        "pasaporte mexicano número X",
        "Licencia de conducir tipo A",
    ],
)
def test_los_marcadores_vetados_del_perfil_se_detectan(texto):
    assert marcador_vetado(texto, VETADOS) is not None


def test_un_cliente_llamado_instituto_nacional_electoral_no_activa_el_veto():
    """El veto mira marcadores concretos, no menciones sueltas del documento."""
    assert marcador_vetado("Proyecto para el Instituto Nacional Electoral", VETADOS) is None


def test_un_perfil_sin_marcadores_no_veta_nada():
    """Un corpus público no debe heredar las prohibiciones de otro tema."""
    assert marcador_vetado("CREDENCIAL PARA VOTAR", ()) is None


@pytest.mark.parametrize(
    "stem,esperado",
    [
        ("CV-2026", "cv"),
        ("constancia-curso-2023", "certificado"),
        ("titulo-ingenieria", "titulo"),
        ("ficha-tecnica-hilux", "ficha_tecnica"),
        ("folleto-corolla-2024", "folleto"),
        ("cualquier-cosa", "documento"),
    ],
)
def test_el_tipo_se_infiere_del_nombre(stem, esperado):
    assert clasificar(stem) == esperado


def test_el_slug_produce_un_document_id_legible_y_sin_acentos():
    assert slug("Cédula Profesional — Ingeniería (2020)") == "cedula-profesional-ingenieria-2020"


def test_la_ruta_se_convierte_en_metadatos_segun_el_perfil(tmp_path):
    """Es lo que hace que organizar los PDFs por carpeta ya sea etiquetarlos."""
    archivo = tmp_path / "toyota" / "SUV" / "hilux-2024.pdf"
    archivo.parent.mkdir(parents=True)
    archivo.touch()

    assert metadata_de_ruta(archivo, tmp_path, ("marca",)) == {"marca": "toyota"}
    assert metadata_de_ruta(archivo, tmp_path, ("marca", "segmento")) == {
        "marca": "toyota",
        "segmento": "suv",
    }
    assert metadata_de_ruta(archivo, tmp_path, ()) == {}


def test_un_perfil_sensible_no_puede_escribir_dentro_del_repositorio(tmp_path):
    with pytest.raises(DestinoInvalido):
        validar_destino(RAIZ / "corpus-de-prueba", SENSIBLE)
    validar_destino(tmp_path / "fuera", SENSIBLE)  # fuera del repo: permitido


def test_un_perfil_publico_si_puede_preparar_dentro_del_repositorio():
    """Obligar a un folleto de coches a salir del árbol sería fricción sin motivo."""
    validar_destino(RAIZ / ".corpus-preparado" / "coches", PUBLICO)


def test_un_documento_largo_se_trocea_y_los_ids_lo_reflejan(tmp_path):
    largo = "# Folleto\n\n" + "\n\n".join(f"Párrafo {i} con datos técnicos." for i in range(200))
    (tmp_path / "marca").mkdir()
    (tmp_path / "marca" / "folleto-2024.md").write_text(largo, encoding="utf-8")
    perfil = Profile(
        slug="p", name="P", subject="s",
        chunking=ChunkPolicy(max_chars=600, overlap_chars=80, min_chars_to_split=800),
        path_metadata=("marca",),
    )

    reporte = preparar(tmp_path, perfil)

    assert reporte.documentos == 1
    assert reporte.total_fragmentos > 1
    assert reporte.fragmentos[0].document_id == "folleto-2024--001"
    assert all(f.metadata["marca"] == "marca" for f in reporte.fragmentos)
    assert all(f.metadata["fragmentos_totales"] == reporte.total_fragmentos for f in reporte.fragmentos)


def test_un_documento_corto_conserva_su_id_sin_sufijo():
    perfil = load_profiles(RAIZ / "profiles")["luis-cv"].profile

    reporte = preparar(FIXTURES, perfil)

    assert reporte.documentos == 4
    assert reporte.total_fragmentos == 4
    assert "cedula-profesional-2020" in {f.document_id for f in reporte.fragmentos}


def test_un_archivo_ilegible_no_tumba_la_preparacion_del_lote(tmp_path):
    """Con decenas de PDFs de origen desconocido alguno estará roto. Que eso
    aborte los otros 130 es el peor comportamiento posible."""
    (tmp_path / "bueno.md").write_text("# Bueno\n\nContenido.", encoding="utf-8")
    (tmp_path / "roto.pdf").write_bytes(b"esto no es un PDF")

    reporte = preparar(tmp_path, PUBLICO)

    assert reporte.documentos == 1
    assert [nombre for nombre, _ in reporte.errores] == ["roto.pdf"]


def test_el_motivo_distingue_un_escaneo_de_un_formato_sin_extractor(tmp_path):
    """Decir «requiere OCR» de un JSON manda al usuario a perder una tarde."""
    (tmp_path / "catalogo.json").write_text('[{"modelo": "hilux"}]', encoding="utf-8")

    reporte = preparar(tmp_path, PUBLICO)

    assert reporte.sin_texto == [("catalogo.json", "ningún extractor reconoce este json → conviértelo o usa --skip")]


def test_escribir_deja_el_corpus_listo_para_la_recuperacion_local(tmp_path):
    """El corpus preparado alimenta por igual al índice local y a la KB."""
    (tmp_path / "origen").mkdir()
    (tmp_path / "origen" / "nota.md").write_text("# Nota\n\nContenido verificable.", encoding="utf-8")
    destino = tmp_path / "preparado"

    reporte = preparar(tmp_path / "origen", PUBLICO)
    escribir(reporte, destino, PUBLICO)

    assert (destino / "nota.md").is_file()
    assert (destino / "nota.md.metadata.json").is_file()
    assert (destino / "manifiesto.csv").is_file()


# --- transcripción de PDFs sin capa de texto ----------------------------------


class MotorFalso:
    """Motor de OCR programable: estas pruebas miden las reglas del pipeline,
    no la calidad de un servicio externo."""

    nombre = "tablas"

    def __init__(self, por_pagina: list[str], fallar_paginas: int = 0):
        self._por_pagina = por_pagina
        self._fallar = fallar_paginas
        self.llamadas = 0

    def disponible(self):
        return True, ""

    def paginas_por_documento(self, total):
        return total

    def extraer(self, imagenes, *, idioma="spa"):
        from rag_agent.infrastructure.ingest.ocr import PaginaExtraida, ResultadoOcr

        self.llamadas += 1
        resultado = ResultadoOcr(motor=self.nombre)
        for numero, _ in enumerate(imagenes, start=1):
            if numero <= self._fallar:
                resultado.avisos.append(f"página {numero}: ConnectionClosedError")
                continue
            resultado.paginas.append(
                PaginaExtraida(numero=numero, texto=self._por_pagina[numero - 1], confianza=95.0)
            )
        return resultado


def _transcriptor(motor, tmp_path, policy, reporte=None):
    from rag_agent.infrastructure.ingest.pipeline import Reporte, Transcriptor

    return Transcriptor(motor, policy, tmp_path / "cache", reporte=reporte or Reporte())


def _pdf_de_una_pagina(destino):
    """Un PDF mínimo válido, sin capa de texto."""
    pypdfium2 = pytest.importorskip("pypdfium2")
    doc = pypdfium2.PdfDocument.new()
    doc.new_page(200, 200)
    doc.save(str(destino))
    return destino


def test_una_transcripcion_pobre_se_descarta_en_vez_de_indexarse(tmp_path, monkeypatch):
    """Un PDF de catorce páginas del que salen 900 caracteres no se transcribió
    mal: no contiene lo que su nombre promete. Indexarlo es peor que
    descartarlo, porque el agente lo citaría para responder algo que no dice."""
    from rag_agent.domain.profile import OcrPolicy
    from rag_agent.infrastructure.ingest import pipeline
    from rag_agent.infrastructure.ingest.pipeline import Reporte

    ruta = _pdf_de_una_pagina(tmp_path / "folleto.pdf")
    monkeypatch.setattr(pipeline, "rasterizar", lambda *a, **k: [b"x"])
    monkeypatch.setattr(pipeline, "contar_paginas", lambda *a, **k: 1)
    reporte = Reporte()
    motor = MotorFalso(["texto muy corto"])

    resultado = _transcriptor(motor, tmp_path, OcrPolicy(motor="tablas"), reporte)(ruta)

    assert resultado is None
    assert any("descartada" in a for a in reporte.avisos)
    assert reporte.transcritos == []


def test_una_transcripcion_densa_se_acepta(tmp_path, monkeypatch):
    from rag_agent.domain.profile import OcrPolicy
    from rag_agent.infrastructure.ingest import pipeline
    from rag_agent.infrastructure.ingest.pipeline import Reporte

    ruta = _pdf_de_una_pagina(tmp_path / "ficha.pdf")
    monkeypatch.setattr(pipeline, "rasterizar", lambda *a, **k: [b"x"])
    monkeypatch.setattr(pipeline, "contar_paginas", lambda *a, **k: 1)
    reporte = Reporte()

    resultado = _transcriptor(MotorFalso(["dato " * 400]), tmp_path, OcrPolicy(motor="tablas"), reporte)(ruta)

    assert resultado is not None
    assert reporte.transcritos == [("ficha.pdf", "tablas", 95.0)]


def test_una_transcripcion_incompleta_no_se_cachea(tmp_path, monkeypatch):
    """Guardar un resultado parcial convierte un fallo de red pasajero en la
    versión definitiva del documento, y nadie volvería a mirarlo."""
    from rag_agent.domain.profile import OcrPolicy
    from rag_agent.infrastructure.ingest import pipeline
    from rag_agent.infrastructure.ingest.pipeline import Reporte

    ruta = _pdf_de_una_pagina(tmp_path / "ficha.pdf")
    monkeypatch.setattr(pipeline, "rasterizar", lambda *a, **k: [b"x", b"y"])
    monkeypatch.setattr(pipeline, "contar_paginas", lambda *a, **k: 2)
    reporte = Reporte()
    motor = MotorFalso(["", "dato " * 400], fallar_paginas=1)
    transcribir = _transcriptor(motor, tmp_path, OcrPolicy(motor="tablas"), reporte)

    transcribir(ruta)
    transcribir(ruta)

    assert motor.llamadas == 2, "un resultado incompleto debe reintentarse"
    assert any("incompleta" in a for a in reporte.avisos)


def test_una_transcripcion_completa_se_cachea_y_no_se_vuelve_a_pagar(tmp_path, monkeypatch):
    from rag_agent.domain.profile import OcrPolicy
    from rag_agent.infrastructure.ingest import pipeline
    from rag_agent.infrastructure.ingest.pipeline import Reporte

    ruta = _pdf_de_una_pagina(tmp_path / "ficha.pdf")
    monkeypatch.setattr(pipeline, "rasterizar", lambda *a, **k: [b"x"])
    monkeypatch.setattr(pipeline, "contar_paginas", lambda *a, **k: 1)
    motor = MotorFalso(["dato " * 400])
    transcribir = _transcriptor(motor, tmp_path, OcrPolicy(motor="tablas"), Reporte())

    transcribir(ruta)
    transcribir(ruta)

    assert motor.llamadas == 1


def test_sin_ocr_un_pdf_ilegible_dice_como_activarlo(tmp_path, monkeypatch):
    """El reporte tiene que decir qué hacer, no solo que algo falló."""
    from rag_agent.infrastructure.ingest import pipeline

    ruta = _pdf_de_una_pagina(tmp_path / "escaneado.pdf")
    assert ruta.exists()

    reporte = preparar(tmp_path, PUBLICO)

    assert any("ocr.motor" in motivo for _, motivo in reporte.sin_texto)


# --- limpieza del texto extraído ----------------------------------------------


def test_una_tabla_numerica_conserva_todas_sus_cifras():
    """El fallo que motivó esta fase: `limpiar` descartaba cualquier número
    suelto por parecerse a un folio. En un PDF cuya capa de texto pone etiqueta
    y valor en líneas separadas —lo normal en tablas financieras— eso borraba
    todos los valores y dejaba el documento con todas sus etiquetas: un corpus
    que parece correcto y no puede responder nada."""
    from rag_agent.infrastructure.ingest.extractors import limpiar

    pagina = "\n".join(
        ["Estado de resultados", "Ingresos", "125", "Costos", "87",
         "Margen operativo", "38", "Provisiones", "0", "0", "Impuestos", "12"]
    )

    salida = limpiar([pagina]).splitlines()

    for cifra in ("125", "87", "38", "12"):
        assert cifra in salida
    assert salida.count("0") == 2, "un cero repetido en dos filas son dos datos"


def test_los_folios_se_quitan_solo_si_forman_una_serie_creciente():
    """La posición sola no basta: un balance puede acabar una página en «0». Un
    folio de verdad crece de página en página."""
    from rag_agent.infrastructure.ingest.extractors import limpiar

    con_folios = limpiar([f"contenido {i}\n{i + 1}" for i in range(6)]).splitlines()
    con_datos = limpiar([f"contenido {i}\n0" for i in range(6)]).splitlines()

    assert con_folios == [f"contenido {i}" for i in range(6)]
    assert con_datos.count("0") == 6


def test_un_folio_escrito_con_letras_se_quita_este_donde_este():
    from rag_agent.infrastructure.ingest.extractors import limpiar

    assert limpiar(["Ingresos\nPágina 3 de 12\n125"]).splitlines() == ["Ingresos", "125"]


def test_de_una_linea_corrida_se_conserva_la_primera_aparicion():
    """Suele ser el modelo o el título. Borrarla de todas partes deja los
    fragmentos del medio del documento sin decir de qué hablan."""
    from rag_agent.infrastructure.ingest.extractors import limpiar

    paginas = [f"MAZDA2 SEDÁN 2026\ndato {i}\nAviso legal" for i in range(8)]

    salida = limpiar(paginas).splitlines()

    assert salida.count("MAZDA2 SEDÁN 2026") == 1
    assert salida.count("Aviso legal") == 1
    assert salida[0] == "MAZDA2 SEDÁN 2026", "el título se conserva al principio"


def test_un_documento_corto_no_deduplica_nada():
    """Con dos páginas, «repetido en la mayoría» y «aparece dos veces» son lo
    mismo, y borrar contenido legítimo es peor que dejar un pie duplicado."""
    from rag_agent.infrastructure.ingest.extractors import limpiar

    salida = limpiar(["Total\n0", "Total\n0"]).splitlines()

    assert salida == ["Total", "0", "Total", "0"]


def test_la_politica_de_limpieza_se_puede_desactivar():
    from rag_agent.domain.profile import CleanupPolicy
    from rag_agent.infrastructure.ingest.extractors import limpiar

    paginas = [f"c{i}\n{i + 1}" for i in range(6)]

    sin_folios = limpiar(paginas, CleanupPolicy(folio_lineas_borde=0)).splitlines()

    assert sin_folios == ["c0", "1", "c1", "2", "c2", "3", "c3", "4", "c4", "5", "c5", "6"]
