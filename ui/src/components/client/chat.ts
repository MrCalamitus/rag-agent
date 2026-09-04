/**
 * La lógica de navegador: envía, lee el stream y pinta.
 *
 * Una máquina de estados sobre los eventos del contrato, que ignora los que no
 * conoce —el spec obliga a ello, y es lo que permite que el servicio añada
 * eventos sin romper esta página—. No importa nada del servidor: el token vive
 * detrás de `/api/chat` y aquí no hay forma de alcanzarlo.
 */

import { renderMarkdown } from "./markdown";
import { readSse } from "../../lib/sse";
import { openDocument } from "./viewer";
import {
  KNOWLEDGE_SEARCH,
  type KnowledgeSearchItem,
  type RetrievalResult,
  type Turn,
} from "../../lib/types";

const CONSULTA_EN_CURSO = "El agente está consultando los documentos.";

interface Elements {
  form: HTMLFormElement;
  input: HTMLTextAreaElement;
  send: HTMLButtonElement;
  sendIcon: HTMLElement;
  thread: HTMLElement;
  empty: HTMLElement;
  live: HTMLElement;
}

export function initChat(root: ParentNode): void {
  const found = collect(root);
  if (!found) return;
  // Si el tema publica alguno de sus documentos. Viene del perfil, vía el
  // servidor: la UI no conoce la política, solo si tiene sentido explicar por
  // qué falta un archivo concreto.
  const exposesDocuments =
    root.querySelector<HTMLElement>("[data-chat]")?.dataset.exposes === "1";
  // Un alias no nulo: el resto son cierres, y ahí el estrechamiento del `if` ya
  // no llega.
  const el: Elements = found;

  const history: Turn[] = [];
  let controller: AbortController | null = null;

  el.form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (controller) {
      // El botón es de parar mientras hay stream. Abortar el fetch cierra el
      // socket, y el servicio cancela la inferencia al ver la desconexión.
      controller.abort();
      return;
    }
    const message = el.input.value.trim();
    if (message !== "") void send(message);
  });

  // Enter envía, Shift+Enter salta línea. Con el IME abierto no se toca nada:
  // el Enter que confirma un candidato no es un envío.
  el.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      el.form.requestSubmit();
    }
  });

  el.input.addEventListener("input", () => autoGrow(el.input));

  for (const button of root.querySelectorAll<HTMLButtonElement>("[data-suggestion]")) {
    button.addEventListener("click", () => {
      el.input.value = button.dataset.suggestion ?? button.textContent ?? "";
      autoGrow(el.input);
      el.input.focus();
    });
  }

  async function send(message: string): Promise<void> {
    el.empty.hidden = true;
    el.input.value = "";
    autoGrow(el.input);

    // La burbuja del usuario aparece ya, sin esperar al servidor: el envío tiene
    // que sentirse instantáneo aunque la primera palabra tarde un segundo.
    appendUserTurn(el.thread, message);
    const turn = appendAgentTurn(el.thread);
    history.push({ role: "user", text: message });

    controller = new AbortController();
    setBusy(el, true);

    let text = "";
    let completed = false;

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, history: history.slice(0, -1) }),
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        turn.fail(await errorMessage(response));
        return;
      }

      turn.pending(true);
      announce(el.live, CONSULTA_EN_CURSO);

      for await (const frame of readSse(response.body)) {
        const data = frame.data as Record<string, any>;

        switch (frame.event) {
          case "response.output_item.added":
            if (data.item?.type === KNOWLEDGE_SEARCH) turn.pending(true);
            break;

          case "response.output_item.done":
            if (data.item?.type === KNOWLEDGE_SEARCH) {
              turn.pending(false);
              turn.showSources(data.item as KnowledgeSearchItem, exposesDocuments);
            }
            break;

          case "response.output_text.delta":
            turn.pending(false);
            text += String(data.delta ?? "");
            turn.setText(text);
            break;

          case "response.output_text.done":
            // El texto completo manda sobre la suma de deltas: si alguno se
            // perdió, esto lo corrige.
            text = String(data.text ?? text);
            turn.setText(text);
            break;

          case "response.completed":
            completed = true;
            break;

          case "error":
            turn.fail(String(data.error?.message ?? "El agente falló."), data.error?.request_id);
            break;

          default:
            break; // Evento desconocido: se ignora, como manda el contrato.
        }
      }

      // `[DONE]` sin `response.completed` significa respuesta incompleta. Es
      // información que el visitante merece: una respuesta cortada a media frase
      // parece una respuesta corta.
      if (!completed && text !== "") {
        turn.warn("La respuesta quedó incompleta: el stream terminó antes de tiempo.");
      }
    } catch (error) {
      if ((error as Error).name === "AbortError") {
        turn.warn("Respuesta detenida.");
      } else {
        turn.fail("Se perdió la conexión con el agente.");
      }
    } finally {
      controller = null;
      setBusy(el, false);
      turn.pending(false);
      turn.finish(text);
      if (text !== "") {
        history.push({ role: "assistant", text });
        announce(el.live, text);
      }
      el.input.focus();
    }
  }
}

/* --- Turnos --------------------------------------------------------------- */

interface AgentTurn {
  setText(value: string): void;
  pending(active: boolean): void;
  showSources(item: KnowledgeSearchItem, exposesDocuments: boolean): void;
  fail(message: string, requestId?: string): void;
  warn(message: string): void;
  finish(text: string): void;
}

function appendUserTurn(thread: HTMLElement, text: string): void {
  const node = clone("tpl-turn-user");
  select(node, "[data-text]").textContent = text;
  thread.append(node);
  scrollToEnd();
}

function appendAgentTurn(thread: HTMLElement): AgentTurn {
  const node = clone("tpl-turn-agent");
  const body = select(node, ".turn__text");
  const status = select(node, "[data-status]");
  const error = select(node, "[data-error]");
  const errorText = select(node, "[data-error-text]");
  const sources = select(node, "[data-sources]");
  const actions = select(node, "[data-actions]");
  const copy = select<HTMLButtonElement>(node, "[data-copy]");
  const copyLabel = select(node, "[data-copy-label]");
  const copyIcon = select(node, ".msym", copy);

  thread.append(node);
  scrollToEnd();

  return {
    setText(value) {
      // Durante el stream se pinta en crudo: el markdown se renderiza una sola
      // vez al final. Reparsear en cada delta haria parpadear la tabla fila a
      // fila mientras llega, y una valla de codigo a medio cerrar se veria como
      // texto suelto hasta que cerrara.
      body.textContent = value;
      scrollToEnd();
    },
    pending(active) {
      status.hidden = !active;
      if (active) scrollToEnd();
    },
    showSources(item, exposesDocuments) {
      const panel = renderSources(item, exposesDocuments);
      if (panel) {
        sources.replaceChildren(panel);
        scrollToEnd();
      }
    },
    fail(message, requestId) {
      error.hidden = false;
      error.classList.add("notice--error");
      errorText.textContent = requestId ? `${message} (petición ${requestId})` : message;
      scrollToEnd();
    },
    warn(message) {
      error.hidden = false;
      error.classList.remove("notice--error");
      errorText.textContent = message;
      scrollToEnd();
    },
    finish(text) {
      if (text === "") return;
      // Ya esta el texto completo: ahora si se puede interpretar el markdown.
      body.replaceChildren(renderMarkdown(text));
      body.classList.add("turn__text--rich");
      actions.hidden = false;
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(text);
          copyLabel.textContent = "Copiado";
          copyIcon.textContent = "check";
          setTimeout(() => {
            copyLabel.textContent = "Copiar";
            copyIcon.textContent = "content_copy";
          }, 1600);
        } catch {
          copyLabel.textContent = "No se pudo copiar";
        }
      });
    },
  };
}

/**
 * Metadatos que no se enseñan: o son del fragmento y no del documento
 * (`fragmento`, `fragmentos_totales`), o ya se muestran en otro sitio
 * (`fuente`), o son maquinaria interna que al usuario no le dice nada
 * (`clase`, `contiene_pii`, `origen_texto`).
 */
const META_OCULTA = new Set([
  "fuente",
  "clase",
  "fragmento",
  "fragmentos_totales",
  "contiene_pii",
  "origen_texto",
]);

interface Fragmento {
  texto: string;
  url: string | null;
}

interface Documento {
  nombre: string;
  ruta: string;
  score: number;
  expuesto: boolean;
  metadata: Record<string, unknown>;
  fragmentos: Fragmento[];
}

/**
 * Del enlace que firma el agente al del proxy de Astro.
 *
 * El agente devuelve `/v1/documents/<nombre>?permiso…`, que el navegador no
 * puede pedir: no hay CORS y no tiene el token. El proxy sí, y solo reenvía el
 * permiso, sin interpretarlo.
 */
function aProxy(documentUrl: string): string | null {
  try {
    const url = new URL(documentUrl, window.location.origin);
    const nombre = decodeURIComponent(url.pathname.split("/").pop() ?? "");
    if (!nombre) return null;
    url.searchParams.set("name", nombre);
    return `/api/document?${url.searchParams}`;
  } catch {
    return null;
  }
}

/**
 * Agrupa los fragmentos por documento de origen.
 *
 * El nombre sale de `metadata.fuente` —el PDF real— y solo cae al
 * `document_id` cuando la ingesta no dejó esa metadata. Un documento se
 * considera consultable si TODOS sus fragmentos lo son: si uno solo no lo es,
 * el archivo no se ofrece.
 */
function agrupar(results: RetrievalResult[]): Documento[] {
  const porDocumento = new Map<string, Documento>();

  for (const result of results) {
    const metadata = result.metadata ?? {};
    const fuente = typeof metadata.fuente === "string" ? metadata.fuente : "";
    const ruta = fuente || result.document_id;
    const previo = porDocumento.get(ruta);

    const fragmento: Fragmento = {
      texto: result.chunk ?? "",
      url: result.document_url ? aProxy(result.document_url) : null,
    };

    if (previo) {
      previo.score = Math.max(previo.score, result.score ?? 0);
      previo.expuesto &&= result.exposed === true;
      previo.fragmentos.push(fragmento);
      continue;
    }

    porDocumento.set(ruta, {
      nombre: basename(ruta),
      ruta,
      score: result.score ?? 0,
      expuesto: result.exposed === true,
      metadata,
      fragmentos: [fragmento],
    });
  }

  return [...porDocumento.values()].sort((a, b) => b.score - a.score);
}

/** El panel de evidencia. `null` si no hubo nada que recuperar. */
function renderSources(item: KnowledgeSearchItem, exposesDocuments: boolean): DocumentFragment | null {
  const results = Array.isArray(item.results) ? item.results : [];
  if (results.length === 0) return null;

  const documentos = agrupar(results);
  const node = clone("tpl-sources");
  const docs = `${documentos.length} ${documentos.length === 1 ? "documento" : "documentos"}`;
  const trozos = `${results.length} ${results.length === 1 ? "fragmento" : "fragmentos"}`;
  const latency = typeof item.latency_ms === "number" ? ` · ${Math.round(item.latency_ms)} ms` : "";
  select(node, "[data-summary]").textContent = `${docs} · ${trozos}${latency}`;

  const list = select(node, "[data-list]");
  for (const documento of documentos) list.append(renderSource(documento, exposesDocuments));
  return node;
}

function renderSource(documento: Documento, exposesDocuments: boolean): DocumentFragment {
  const node = clone("tpl-source");
  const name = select(node, "[data-name]");
  name.textContent = documento.nombre;
  // La ruta completa —que puede ser una URI de S3 larguísima— en el title.
  name.title = documento.ruta;
  select(node, "[data-score]").textContent = documento.score.toFixed(3);

  const meta = select(node, "[data-meta]");
  const pares = Object.entries(documento.metadata)
    .filter(([clave, valor]) => !META_OCULTA.has(clave) && valor !== null && valor !== undefined && valor !== "")
    .map(([clave, valor]) => `${clave}: ${valor}`);
  const total = documento.fragmentos.length;
  pares.push(`${total} ${total === 1 ? "fragmento" : "fragmentos"}`);
  meta.textContent = pares.join(" · ");
  meta.hidden = false;

  // Decir "no divulgable" en un tema que no divulga ninguno sería repetir en
  // cada documento lo que la cabecera ya dice una vez.
  if (exposesDocuments && !documento.expuesto) {
    select(node, "[data-sealed]").hidden = false;
  }

  const fragmentos = select(node, "[data-fragments]");
  for (const fragmento of documento.fragmentos) {
    fragmentos.append(renderFragment(fragmento, documento.nombre));
  }
  return node;
}

/**
 * Un fragmento, con el botón de abrirlo en el documento y el de copiarlo.
 *
 * Copiar la cita es hoy la forma de encontrarla: la metadata guarda el total de
 * páginas del documento, no la página de la que salió este trozo, así que no se
 * puede decir "página 3". Pegar la frase en el buscador del visor sí lleva al
 * sitio exacto.
 */
function renderFragment(fragmento: Fragmento, nombre: string): DocumentFragment {
  const node = clone("tpl-fragment");
  select(node, "[data-chunk]").textContent = fragmento.texto;

  if (fragmento.url) {
    const abrir = select<HTMLButtonElement>(node, "[data-open]");
    abrir.hidden = false;
    abrir.addEventListener("click", () => {
      void openDocument(fragmento.url as string, nombre, fragmento.texto);
    });
  }

  const boton = select<HTMLButtonElement>(node, "[data-quote]");
  const label = select(node, "[data-quote-label]");
  boton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(fragmento.texto);
      label.textContent = "Copiada";
      setTimeout(() => (label.textContent = "Copiar cita"), 1600);
    } catch {
      label.textContent = "No se pudo copiar";
    }
  });
  return node;
}

/* --- Utilidades ----------------------------------------------------------- */

function collect(root: ParentNode): Elements | null {
  const form = root.querySelector<HTMLFormElement>("[data-chat-form]");
  const input = root.querySelector<HTMLTextAreaElement>("[data-input]");
  const send = root.querySelector<HTMLButtonElement>("[data-send]");
  const thread = root.querySelector<HTMLElement>("[data-thread]");
  const empty = root.querySelector<HTMLElement>("[data-empty]");
  const live = root.querySelector<HTMLElement>("[data-live]");
  if (!form || !input || !send || !thread || !empty || !live) return null;
  const sendIcon = send.querySelector<HTMLElement>(".msym");
  if (!sendIcon) return null;
  return { form, input, send, sendIcon, thread, empty, live };
}

function setBusy(el: Elements, busy: boolean): void {
  el.input.readOnly = busy;
  el.sendIcon.textContent = busy ? "stop_circle" : "send";
  el.send.setAttribute("aria-label", busy ? "Detener la respuesta" : "Enviar pregunta");
  el.send.disabled = false; // parar también es una acción: nunca se deshabilita.
}

/**
 * Publica en la región viva. Se hace en un solo golpe y al final, no delta a
 * delta: un `aria-live` que cambia sesenta veces por segundo no se lee, se
 * atropella.
 */
function announce(live: HTMLElement, text: string): void {
  live.textContent = text;
}

function autoGrow(input: HTMLTextAreaElement): void {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
}

function scrollToEnd(): void {
  // Solo si el visitante ya estaba abajo: arrastrarle la vista mientras lee algo
  // más arriba es de las cosas más molestas que puede hacer un chat.
  const distance = document.documentElement.scrollHeight - window.scrollY - window.innerHeight;
  if (distance < 160) {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
  }
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (body?.error?.message) {
      const id = body.error.request_id;
      return id ? `${body.error.message} (petición ${id})` : body.error.message;
    }
  } catch {
    /* cae al genérico */
  }
  return `El agente respondió ${response.status}.`;
}

function basename(id: string): string {
  const clean = id.replace(/\/+$/, "");
  const cut = clean.lastIndexOf("/");
  return cut === -1 ? clean : clean.slice(cut + 1) || clean;
}

function clone(id: string): DocumentFragment {
  const template = document.getElementById(id);
  if (!(template instanceof HTMLTemplateElement)) {
    throw new Error(`Falta la plantilla #${id}.`);
  }
  return template.content.cloneNode(true) as DocumentFragment;
}

function select<T extends HTMLElement = HTMLElement>(
  root: ParentNode,
  selector: string,
  scope?: ParentNode,
): T {
  const found = (scope ?? root).querySelector<T>(selector);
  if (!found) throw new Error(`Falta el nodo ${selector} en la plantilla.`);
  return found;
}
