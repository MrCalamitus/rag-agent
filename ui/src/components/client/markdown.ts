/**
 * Markdown → nodos del DOM.
 *
 * El agente responde en markdown y la burbuja lo pintaba con `textContent`, así
 * que una tabla salía como una reja de pipes y las negritas como asteriscos.
 *
 * **Nunca `innerHTML`.** Todo nodo se crea con `createElement` y todo texto se
 * asigna con `textContent`, así que el HTML que pudiera venir dentro de la
 * respuesta del modelo se muestra como texto y no se ejecuta. Es la razón de no
 * usar una librería de markdown: casi todas emiten una cadena de HTML, y
 * entonces hace falta un sanitizador y confiar en que esté bien configurado.
 * Aquí la seguridad no depende de una lista de permitidos sino de que la ruta
 * que ejecuta HTML no existe.
 *
 * Alcance deliberado: lo que el agente escribe de verdad —párrafos, negritas,
 * cursivas, código, listas, citas, encabezados y tablas GFM—. Sin enlaces a
 * propósito: las citas van como `[2T26.pdf]`, que un parser de enlaces
 * empezaría a interpretar.
 */

type Alineacion = "left" | "right" | "center";

const SEPARADOR_TABLA = /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
const FILA_TABLA = /^\s*\|.*\|\s*$/;
const ENCABEZADO = /^(#{1,6})\s+(.*)$/;
const VINETA = /^\s*[-*+]\s+(.*)$/;
const NUMERADA = /^\s*\d+[.)]\s+(.*)$/;
const CITA = /^\s*>\s?(.*)$/;
const CERCA = /^\s*```/;

export function renderMarkdown(texto: string): DocumentFragment {
  const salida = document.createDocumentFragment();
  const lineas = texto.replace(/\r\n?/g, "\n").split("\n");
  let i = 0;

  while (i < lineas.length) {
    const linea = lineas[i];

    if (linea.trim() === "") {
      i += 1;
      continue;
    }

    if (CERCA.test(linea)) {
      const [nodo, siguiente] = bloqueCodigo(lineas, i);
      salida.append(nodo);
      i = siguiente;
      continue;
    }

    // La tabla se reconoce por su SEGUNDA línea: una fila de guiones. Sin ese
    // ancla, cualquier párrafo con un pipe se convertiría en tabla.
    if (FILA_TABLA.test(linea) && i + 1 < lineas.length && SEPARADOR_TABLA.test(lineas[i + 1])) {
      const [nodo, siguiente] = tabla(lineas, i);
      salida.append(nodo);
      i = siguiente;
      continue;
    }

    const encabezado = ENCABEZADO.exec(linea);
    if (encabezado) {
      const nivel = Math.min(encabezado[1].length + 2, 6); // h1 del markdown → h3 del hilo
      const nodo = document.createElement(`h${nivel}`);
      nodo.append(...enLinea(encabezado[2]));
      salida.append(nodo);
      i += 1;
      continue;
    }

    if (CITA.test(linea)) {
      const [nodo, siguiente] = cita(lineas, i);
      salida.append(nodo);
      i = siguiente;
      continue;
    }

    if (VINETA.test(linea) || NUMERADA.test(linea)) {
      const [nodo, siguiente] = lista(lineas, i);
      salida.append(nodo);
      i = siguiente;
      continue;
    }

    const [nodo, siguiente] = parrafo(lineas, i);
    salida.append(nodo);
    i = siguiente;
  }

  return salida;
}

// --- bloques ----------------------------------------------------------------

function bloqueCodigo(lineas: string[], desde: number): [HTMLElement, number] {
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  const cuerpo: string[] = [];
  let i = desde + 1;
  while (i < lineas.length && !CERCA.test(lineas[i])) {
    cuerpo.push(lineas[i]);
    i += 1;
  }
  code.textContent = cuerpo.join("\n");
  pre.append(code);
  // Si no hay cierre se consume hasta el final: el stream puede haber acabado a
  // media valla y es mejor mostrar el código que tragárselo.
  return [pre, i < lineas.length ? i + 1 : i];
}

function tabla(lineas: string[], desde: number): [HTMLElement, number] {
  const alineaciones = alineacionesDe(lineas[desde + 1]);
  const tabla = document.createElement("table");
  tabla.className = "md-table";

  const thead = document.createElement("thead");
  thead.append(fila(celdasDe(lineas[desde]), "th", alineaciones));
  tabla.append(thead);

  const tbody = document.createElement("tbody");
  let i = desde + 2;
  while (i < lineas.length && FILA_TABLA.test(lineas[i])) {
    tbody.append(fila(celdasDe(lineas[i]), "td", alineaciones));
    i += 1;
  }
  tabla.append(tbody);

  // El envoltorio permite desplazar la tabla en horizontal sin que la burbuja
  // entera se ensanche: en móvil una tabla de seis columnas no cabe.
  const envoltorio = document.createElement("div");
  envoltorio.className = "md-table-wrap";
  envoltorio.append(tabla);
  return [envoltorio, i];
}

function fila(celdas: string[], etiqueta: "th" | "td", alineaciones: Alineacion[]): HTMLElement {
  const tr = document.createElement("tr");
  celdas.forEach((celda, indice) => {
    const nodo = document.createElement(etiqueta);
    const alineacion = alineaciones[indice];
    // Por defecto las columnas numéricas no se alinean solas; si el markdown no
    // lo pide, se respeta el flujo del idioma y no se adivina.
    if (alineacion && alineacion !== "left") nodo.style.textAlign = alineacion;
    nodo.append(...enLinea(celda));
    tr.append(nodo);
  });
  return tr;
}

function celdasDe(linea: string): string[] {
  return linea.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function alineacionesDe(separador: string): Alineacion[] {
  return celdasDe(separador).map((celda) => {
    const izquierda = celda.startsWith(":");
    const derecha = celda.endsWith(":");
    if (izquierda && derecha) return "center";
    if (derecha) return "right";
    return "left";
  });
}

function cita(lineas: string[], desde: number): [HTMLElement, number] {
  const nodo = document.createElement("blockquote");
  const cuerpo: string[] = [];
  let i = desde;
  while (i < lineas.length) {
    const encontrado = CITA.exec(lineas[i]);
    if (!encontrado) break;
    cuerpo.push(encontrado[1]);
    i += 1;
  }
  const p = document.createElement("p");
  p.append(...enLinea(cuerpo.join(" ")));
  nodo.append(p);
  return [nodo, i];
}

function lista(lineas: string[], desde: number): [HTMLElement, number] {
  const numerada = NUMERADA.test(lineas[desde]) && !VINETA.test(lineas[desde]);
  const nodo = document.createElement(numerada ? "ol" : "ul");
  let i = desde;
  while (i < lineas.length) {
    const encontrado = numerada ? NUMERADA.exec(lineas[i]) : VINETA.exec(lineas[i]);
    if (!encontrado) break;
    const li = document.createElement("li");
    li.append(...enLinea(encontrado[1]));
    nodo.append(li);
    i += 1;
  }
  return [nodo, i];
}

function parrafo(lineas: string[], desde: number): [HTMLElement, number] {
  const cuerpo: string[] = [];
  let i = desde;
  while (i < lineas.length && lineas[i].trim() !== "") {
    const linea = lineas[i];
    if (ENCABEZADO.test(linea) || CITA.test(linea) || CERCA.test(linea)) break;
    if (VINETA.test(linea) || NUMERADA.test(linea)) break;
    if (FILA_TABLA.test(linea) && i + 1 < lineas.length && SEPARADOR_TABLA.test(lineas[i + 1])) {
      break;
    }
    cuerpo.push(linea.trim());
    i += 1;
  }
  const p = document.createElement("p");
  p.append(...enLinea(cuerpo.join(" ")));
  return [p, i];
}

// --- en línea ---------------------------------------------------------------

// El código va primero: dentro de `**` literales no hay negrita que valer.
const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\n]+\*)|(_[^_\n]+_)/g;

export function enLinea(texto: string): Node[] {
  const nodos: Node[] = [];
  let ultimo = 0;

  for (const encontrado of texto.matchAll(INLINE)) {
    const inicio = encontrado.index ?? 0;
    if (inicio > ultimo) nodos.push(document.createTextNode(texto.slice(ultimo, inicio)));

    const [entero, codigo, fuerte, fuerteBajo, enfasis, enfasisBajo] = encontrado;
    if (codigo) nodos.push(etiqueta("code", codigo.slice(1, -1)));
    else if (fuerte) nodos.push(etiqueta("strong", fuerte.slice(2, -2)));
    else if (fuerteBajo) nodos.push(etiqueta("strong", fuerteBajo.slice(2, -2)));
    else if (enfasis) nodos.push(etiqueta("em", enfasis.slice(1, -1)));
    else if (enfasisBajo) nodos.push(etiqueta("em", enfasisBajo.slice(1, -1)));

    ultimo = inicio + entero.length;
  }

  if (ultimo < texto.length) nodos.push(document.createTextNode(texto.slice(ultimo)));
  return nodos.length > 0 ? nodos : [document.createTextNode(texto)];
}

function etiqueta(nombre: string, contenido: string): HTMLElement {
  const nodo = document.createElement(nombre);
  nodo.textContent = contenido;
  return nodo;
}
