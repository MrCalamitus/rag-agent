/**
 * El proxy. Es la pieza que justifica que esta UI sea SSR y no un sitio estático.
 *
 * El navegador manda `{message, history}` a este endpoint, del mismo origen; el
 * endpoint añade el `Authorization` y el `X-Rag-Profile` y reenvía el stream tal
 * cual. Tres problemas se resuelven aquí y no en otro sitio: el servicio no monta
 * CORS, el token es un secreto compartido que no puede viajar en un bundle, y el
 * ALB es HTTP puro, así que una página HTTPS que lo llamara de frente daría
 * mixed-content. Servidor contra servidor, nada de eso ocurre.
 */

import type { APIRoute } from "astro";
import { createResponseStream, newRequestId, readError } from "../../lib/rag";
import type { ChatRequest, Turn } from "../../lib/types";

export const prerender = false;

const SSE_HEADERS = {
  "Content-Type": "text/event-stream; charset=utf-8",
  "Cache-Control": "no-cache, no-transform",
  Connection: "keep-alive",
  // Defensivo contra proxies que acumulan la respuesta. Sin esto el stream llega
  // de una vez al final y el streaming deja de existir en la práctica.
  "X-Accel-Buffering": "no",
};

export const POST: APIRoute = async ({ request }) => {
  const requestId = newRequestId();

  let body: ChatRequest;
  try {
    body = (await request.json()) as ChatRequest;
  } catch {
    return problem(400, "El cuerpo no es JSON válido.", requestId);
  }

  const message = typeof body.message === "string" ? body.message.trim() : "";
  if (message === "") {
    return problem(400, "Hace falta una pregunta.", requestId);
  }

  const history = sanitizeHistory(body.history);

  let upstream: Response;
  try {
    upstream = await createResponseStream(message, history, requestId, request.signal);
  } catch (error) {
    // El visitante se fue a mitad de petición: no es un fallo que reportar.
    if (request.signal.aborted) return new Response(null, { status: 499 });
    console.error(`[${requestId}] no se pudo alcanzar el agente:`, error);
    return problem(502, "No se pudo contactar con el agente.", requestId);
  }

  // El servicio consume el primer evento antes de emitir cabeceras, así que un
  // alias inexistente o un token malo siguen siendo un código HTTP y no un error
  // a mitad de stream que el cliente ya no puede distinguir de una respuesta
  // corta. Se aprovecha: el error se devuelve con su propio status.
  if (!upstream.ok || !upstream.body) {
    const detail = await readError(upstream);
    console.error(`[${requestId}] el agente respondió ${upstream.status}: ${detail}`);
    return problem(upstream.status, detail, requestId);
  }

  return new Response(upstream.body, {
    status: 200,
    headers: { ...SSE_HEADERS, "X-Request-Id": requestId },
  });
};

/**
 * El historial llega del navegador, así que se trata como entrada hostil: solo
 * los dos roles que la UI usa, solo texto, y acotado.
 */
function sanitizeHistory(history: unknown): Turn[] {
  if (!Array.isArray(history)) return [];
  const turns: Turn[] = [];
  for (const item of history) {
    if (typeof item !== "object" || item === null) continue;
    const { role, text } = item as Partial<Turn>;
    if (role !== "user" && role !== "assistant") continue;
    if (typeof text !== "string" || text.trim() === "") continue;
    turns.push({ role, text });
  }
  return turns;
}

/** Mismo envoltorio de error que el servicio, para que el cliente lea uno solo. */
function problem(status: number, message: string, requestId: string): Response {
  return new Response(
    JSON.stringify({ error: { message, type: "proxy_error", request_id: requestId } }),
    {
      status,
      headers: { "Content-Type": "application/json", "X-Request-Id": requestId },
    },
  );
}
