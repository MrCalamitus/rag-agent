/**
 * Lector de `text/event-stream`.
 *
 * Se escribe a mano en lugar de usar `EventSource` porque `EventSource` no
 * admite POST ni cabeceras, y la petición al proxy necesita ambas cosas. Es una
 * función pura sobre un stream: no sabe de red ni de DOM, y por eso se puede
 * probar con una traza capturada.
 *
 * Reglas del contrato §4 que este parser da por buenas: `event:` coincide con el
 * `type` del cuerpo, no se usa el campo `id:`, y el terminal es la cadena
 * literal `[DONE]`.
 */

export interface SseFrame {
  /** El valor de `event:`. `"message"` si el frame no lo trae, como manda el spec. */
  event: string;
  /** El `data:` ya parseado. */
  data: unknown;
}

/**
 * Emite un frame por cada bloque completo. Se detiene en `[DONE]` sin emitirlo:
 * quien consume sabe que el stream terminó porque el iterador termina.
 *
 * Un frame puede partirse entre dos lecturas del socket, así que el resto sin
 * terminar se guarda en el buffer hasta que llegue el separador.
 */
export async function* readSse(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<SseFrame, void, undefined> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Normalizar CRLF: el separador de bloque es una línea en blanco, y con
      // \r\n el `split("\n\n")` no encontraría ninguna.
      buffer = buffer.replace(/\r\n/g, "\n");

      let cut: number;
      while ((cut = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);

        const frame = parseBlock(block);
        if (frame === DONE) return;
        if (frame) yield frame;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

const DONE = Symbol("done");

/** Convierte un bloque en frame. `null` si no aporta nada (comentarios, vacío). */
function parseBlock(block: string): SseFrame | typeof DONE | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of block.split("\n")) {
    // Un `:` inicial es un comentario del spec, y se usa como keep-alive.
    if (line === "" || line.startsWith(":")) continue;

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    // El spec pide comer UN espacio tras los dos puntos, no todos.
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }

  if (dataLines.length === 0) return null;

  const raw = dataLines.join("\n");
  if (raw === "[DONE]") return DONE;

  try {
    return { event, data: JSON.parse(raw) };
  } catch {
    // Un frame ilegible no debe tumbar el stream: los siguientes pueden traer
    // la respuesta entera. Se ignora, igual que un evento desconocido.
    return null;
  }
}
