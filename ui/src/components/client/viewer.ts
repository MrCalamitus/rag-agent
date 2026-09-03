/**
 * Abre un PDF y resalta el fragmento que el agente citó.
 *
 * La metadata guarda el total de páginas del documento, no la página de la que
 * salió cada trozo, así que la página no se sabe: se busca. Es mejor de lo que
 * suena, porque buscar el texto da además las coordenadas para dibujar la
 * marca, que es lo que de verdad se quería.
 */

import * as pdfjs from "pdfjs-dist";
import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).href;

/**
 * Longitud mínima de una línea para buscarla.
 *
 * Por debajo, las coincidencias son casualidad: en una ficha técnica abundan las
 * celdas con dos palabras que aparecen en veinte sitios distintos.
 */
const MIN_LINEA = 14;

interface Marca {
  page: number;
  items: number[];
  /** Cuántas líneas del fragmento aparecen en esta página. Ordena los saltos. */
  aciertos: number;
}

interface Estado {
  doc: PDFDocumentProxy | null;
  marcas: Marca[];
  actual: number;
  render: number;
  /**
   * El rasterizado en curso.
   *
   * pdf.js no admite dos `render` simultáneos sobre el mismo canvas: el segundo
   * se queda esperando para siempre y la página nunca aparece. Y hay dos
   * disparadores que se pisan con facilidad —abrir el documento y redimensionar
   * la ventana— así que se guarda la tarea para cancelarla antes de empezar otra.
   */
  tarea: { cancel: () => void; promise: Promise<void> } | null;
}

let dialogo: HTMLDialogElement | null = null;
let elementos: ReturnType<typeof recoger> | null = null;
const estado: Estado = { doc: null, marcas: [], actual: 0, render: 0, tarea: null };

function recoger(raiz: HTMLDialogElement) {
  const q = <T extends HTMLElement>(sel: string): T => {
    const nodo = raiz.querySelector<T>(sel);
    if (!nodo) throw new Error(`Falta ${sel} en el visor.`);
    return nodo;
  };
  return {
    titulo: q("[data-viewer-title]"),
    estado: q("[data-viewer-state]"),
    pagina: q("[data-viewer-page]"),
    canvas: q<HTMLCanvasElement>("[data-viewer-canvas]"),
    marcas: q("[data-viewer-marks]"),
    nav: q("[data-viewer-nav]"),
    cuenta: q("[data-viewer-count]"),
    previo: q<HTMLButtonElement>("[data-viewer-prev]"),
    siguiente: q<HTMLButtonElement>("[data-viewer-next]"),
    cerrar: q<HTMLButtonElement>("[data-viewer-close]"),
  };
}

export function initViewer(raiz: ParentNode): void {
  const nodo = raiz.querySelector<HTMLDialogElement>("[data-viewer]");
  if (!nodo) return;
  dialogo = nodo;
  elementos = recoger(nodo);

  elementos.cerrar.addEventListener("click", () => nodo.close());
  elementos.previo.addEventListener("click", () => saltar(-1));
  elementos.siguiente.addEventListener("click", () => saltar(1));
  // Clic fuera del contenido cierra, que es lo que espera cualquiera.
  nodo.addEventListener("click", (evento) => {
    if (evento.target === nodo) nodo.close();
  });
  nodo.addEventListener("close", descartar);
  // Con debounce: redimensionar dispara decenas de eventos y cada uno cancela
  // el rasterizado del anterior, de modo que sin esto la página no llega a
  // dibujarse mientras se arrastra el borde de la ventana.
  let pendiente: number | undefined;
  window.addEventListener("resize", () => {
    window.clearTimeout(pendiente);
    pendiente = window.setTimeout(() => {
      if (nodo.open) void pintar();
    }, 150);
  });
}

/**
 * Abre el documento y salta al fragmento.
 *
 * `url` es la del proxy de Astro, no la del agente: el navegador nunca habla
 * con el servicio directamente.
 */
export async function openDocument(url: string, nombre: string, fragmento: string): Promise<void> {
  if (!dialogo || !elementos) return;
  const ui = elementos;

  descartar();
  ui.titulo.textContent = nombre;
  ui.estado.textContent = "Abriendo el documento…";
  ui.estado.hidden = false;
  ui.pagina.hidden = true;
  ui.nav.hidden = true;
  if (!dialogo.open) dialogo.showModal();

  const propio = ++estado.render;
  try {
    const doc = await pdfjs.getDocument({ url }).promise;
    if (propio !== estado.render) {
      void doc.destroy();
      return;
    }
    estado.doc = doc;
    // Un manual de cientos de páginas tarda en recorrerse, y un visor en blanco
    // sin explicación parece colgado.
    estado.marcas = await buscar(doc, fragmento, (pagina) => {
      ui.estado.textContent = `Buscando la cita… página ${pagina} de ${doc.numPages}`;
    });
    estado.actual = 0;

    if (estado.marcas.length === 0) {
      // Honesto: se abre igual, pero diciendo que no se pudo situar la cita.
      // El fragmento pudo cruzar un salto de página, o el PDF puede tener el
      // texto en una capa que no coincide con lo que extrajo la ingesta.
      estado.marcas = [{ page: 1, items: [], aciertos: 0 }];
      ui.estado.textContent = "No se pudo localizar la cita; se abre la primera página.";
    }
    await pintar();
  } catch {
    if (propio !== estado.render) return;
    ui.estado.textContent = "No se pudo abrir el documento.";
    ui.estado.hidden = false;
    ui.pagina.hidden = true;
  }
}

function descartar(): void {
  estado.render += 1;
  void estado.doc?.destroy();
  estado.tarea?.cancel();
  estado.tarea = null;
  estado.doc = null;
  estado.marcas = [];
  estado.actual = 0;
  elementos?.marcas.replaceChildren();
}

function saltar(paso: number): void {
  if (estado.marcas.length === 0) return;
  const total = estado.marcas.length;
  estado.actual = (estado.actual + paso + total) % total;
  void pintar();
}

/* --- Búsqueda -------------------------------------------------------------- */

/** Sin acentos, sin mayúsculas y con los espacios colapsados. */
function normalizar(texto: string): string {
  return texto
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * En qué páginas está el fragmento y sobre qué trozos de texto cae.
 *
 * La capa de texto de un PDF viene partida en pedazos arbitrarios —a veces por
 * palabra—, así que se concatena todo y se guarda dónde empieza cada pedazo:
 * con eso, una coincidencia en la cadena se traduce a los pedazos que hay que
 * marcar.
 */
/**
 * Dónde está el fragmento y qué hay que marcar.
 *
 * Un fragmento es una **región** del documento, no una frase, así que se marca
 * entera: se buscan todas sus líneas y se resaltan las que aparezcan en la
 * página. La versión anterior buscaba una sola aguja y, cuando el arranque del
 * fragmento no casaba, caía en la línea más larga — que en una tabla de
 * especificaciones es lotería: ante una pregunta por la potencia subrayaba la
 * fila del sistema de tracción, que era la más larga y no tenía nada que ver.
 *
 * Y se elige la página que contiene MÁS líneas del fragmento, no la primera que
 * contiene alguna: una línea suelta de texto legal se repite en varias páginas y
 * arrastraba el visor a la equivocada.
 */
async function buscar(
  doc: PDFDocumentProxy,
  fragmento: string,
  avisar: (pagina: number) => void,
): Promise<Marca[]> {
  const lineas = lineasDeCita(fragmento);
  if (lineas.length === 0) return [];

  const encontradas: Marca[] = [];
  for (let numero = 1; numero <= doc.numPages; numero += 1) {
    avisar(numero);
    const pagina = await doc.getPage(numero);
    const contenido = await pagina.getTextContent();

    // Una sola extracción por página, con todas las líneas probadas contra
    // ella. Recorrer el documento una vez por línea multiplicaría el coste por
    // cuarenta, y sobre un manual de 440 páginas eso es un visor congelado.
    let plano = "";
    const inicios: number[] = [];
    for (const item of contenido.items) {
      inicios.push(plano.length);
      plano += `${normalizar("str" in item ? item.str : "")} `;
    }

    const items = new Set<number>();
    let aciertos = 0;
    for (const linea of lineas) {
      const tocados = itemsQueContienen(plano, inicios, linea);
      if (tocados.length === 0) continue;
      aciertos += 1;
      for (const indice of tocados) items.add(indice);
    }

    if (aciertos > 0) {
      encontradas.push({ page: numero, items: [...items].sort((a, b) => a - b), aciertos });
    }
    // Una página que contiene el fragmento entero no la va a mejorar ninguna.
    if (aciertos === lineas.length) break;
  }

  return encontradas.sort((a, b) => b.aciertos - a.aciertos || a.page - b.page);
}

/**
 * Las líneas del fragmento que vale la pena buscar.
 *
 * Se descartan las cortas y las que no tienen palabras: en una ficha técnica
 * abundan las celdas con un `•` o un número suelto, y buscar «180» acierta en
 * media docena de sitios que no son el bueno.
 */
function lineasDeCita(fragmento: string): string[] {
  const lineas = fragmento
    .split("\n")
    .map((linea) => normalizar(linea))
    .filter((linea) => linea.length >= MIN_LINEA && /[a-z]{4}/.test(linea));

  // Tope por coste: el fragmento más largo del corpus ronda las 60 líneas y
  // cada una se prueba contra cada página.
  return [...new Set(lineas)].slice(0, 60);
}

/** Índices de los trozos de la capa de texto que cubre una línea. */
function itemsQueContienen(plano: string, inicios: number[], linea: string): number[] {
  const desde = plano.indexOf(linea);
  if (desde === -1) return [];
  const hasta = desde + linea.length;

  const items: number[] = [];
  for (let i = 0; i < inicios.length; i += 1) {
    const fin = i + 1 < inicios.length ? inicios[i + 1] : plano.length;
    if (inicios[i] < hasta && fin > desde) items.push(i);
  }
  return items;
}

/* --- Pintado --------------------------------------------------------------- */

/**
 * Los pintados se encadenan, nunca se solapan.
 *
 * pdf.js rechaza dos `render()` sobre el mismo canvas —"Cannot use the same
 * canvas during multiple render() operations"— y `cancel()` no basta: cancelar
 * pide el fin, no lo espera. Abrir un documento y redimensionar la ventana son
 * dos disparadores fáciles de solapar, así que cada pintado espera al anterior.
 */
let cola: Promise<void> = Promise.resolve();

function pintar(): Promise<void> {
  cola = cola.catch(() => {}).then(pintarSeguro);
  return cola;
}

async function pintarSeguro(): Promise<void> {
  try {
    await pintarPagina();
  } catch (error) {
    if (!elementos) return;
    // Sin esto, un fallo al rasterizar deja el visor en blanco y sin explicación,
    // que es el peor estado posible: parece que el documento no tiene páginas.
    elementos.estado.textContent = "No se pudo dibujar la página del documento.";
    elementos.estado.hidden = false;
    elementos.pagina.hidden = true;
    console.error("[visor]", error);
  }
}

async function pintarPagina(): Promise<void> {
  if (!estado.doc || !elementos || estado.marcas.length === 0) return;
  const ui = elementos;
  const marca = estado.marcas[estado.actual];
  const propio = estado.render;

  const pagina = await estado.doc.getPage(marca.page);
  if (propio !== estado.render) return;

  const ancho = Math.min(ui.pagina.parentElement?.clientWidth ?? 900, 900) - 8;
  const base = pagina.getViewport({ scale: 1 });
  // Nítido en pantallas de alta densidad sin repintar en cada scroll.
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const escala = ancho / base.width;
  const viewport = pagina.getViewport({ scale: escala });

  ui.canvas.width = Math.floor(viewport.width * dpr);
  ui.canvas.height = Math.floor(viewport.height * dpr);
  ui.canvas.style.width = `${Math.floor(viewport.width)}px`;
  ui.canvas.style.height = `${Math.floor(viewport.height)}px`;
  ui.pagina.style.width = `${Math.floor(viewport.width)}px`;

  const contexto = ui.canvas.getContext("2d");
  if (!contexto) return;
  contexto.setTransform(dpr, 0, 0, dpr, 0, 0);

  const tarea = pagina.render({ canvasContext: contexto, viewport });
  estado.tarea = tarea;
  try {
    await tarea.promise;
  } catch (error) {
    // Cancelar es lo normal aquí —otro pintado tomó el relevo—, no un fallo.
    if ((error as { name?: string })?.name === "RenderingCancelledException") return;
    throw error;
  } finally {
    if (estado.tarea === tarea) estado.tarea = null;
  }
  if (propio !== estado.render) return;

  await dibujarMarcas(pagina, viewport, marca);

  ui.pagina.hidden = false;
  const conCita = marca.items.length > 0;
  ui.estado.hidden = conCita;
  ui.nav.hidden = false;
  const varias = estado.marcas.length > 1;
  ui.previo.hidden = !varias;
  ui.siguiente.hidden = !varias;
  ui.cuenta.textContent = conCita
    ? `Página ${marca.page}` +
      (estado.marcas.length > 1 ? ` · coincidencia ${estado.actual + 1} de ${estado.marcas.length}` : "")
    : `Página ${marca.page}`;
}

async function dibujarMarcas(
  pagina: PDFPageProxy,
  viewport: { transform: number[] },
  marca: Marca,
): Promise<void> {
  if (!elementos) return;
  elementos.marcas.replaceChildren();
  if (marca.items.length === 0) return;

  const contenido = await pagina.getTextContent();
  const fragmento = document.createDocumentFragment();
  for (const indice of marca.items) {
    const item = contenido.items[indice];
    if (!item || !("transform" in item)) continue;

    // La matriz del item está en coordenadas del PDF (origen abajo). Componerla
    // con la del viewport la lleva a píxeles de pantalla (origen arriba).
    const m = pdfjs.Util.transform(viewport.transform, item.transform);
    const alto = Math.hypot(m[2], m[3]);
    const nodo = document.createElement("span");
    nodo.className = "viewer__mark";
    nodo.style.left = `${m[4]}px`;
    nodo.style.top = `${m[5] - alto}px`;
    nodo.style.width = `${item.width * (viewport as any).scale}px`;
    nodo.style.height = `${alto}px`;
    fragmento.append(nodo);
  }
  elementos.marcas.append(fragmento);

  // La marca puede caer a media página: se lleva a la vista.
  const primera = elementos.marcas.firstElementChild;
  primera?.scrollIntoView({ block: "center", behavior: "smooth" });
}
