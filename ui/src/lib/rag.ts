/**
 * Cliente del agente. Solo corre en el servidor: es el único módulo que toca el
 * token, y por eso nada de lo que hay aquí puede importarse desde el navegador.
 */

import { getConfig } from "../config";
import type { ErrorEnvelope, Profile, ProfilesResponse, Turn } from "./types";

function headers(requestId: string): Record<string, string> {
  const config = getConfig();
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${config.apiToken}`,
    // La extensión que elige el tema. Va en cabecera y no en el cuerpo porque el
    // cuerpo es el de Open Responses e ignora los campos desconocidos: un slug
    // mal escrito ahí se tragaría en silencio y el visitante recibiría respuestas
    // del corpus equivocado sin enterarse.
    "X-Rag-Profile": config.profile,
    "X-Request-Id": requestId,
  };
}

/**
 * El perfil que sirve esta instancia, o `null` si el servicio no lo conoce.
 *
 * Se consulta en el render de la portada para que el título y la descripción
 * salgan del propio perfil en lugar de duplicarse aquí como constantes que se
 * quedan viejas en cuanto alguien edita el YAML.
 */
export async function fetchProfile(signal?: AbortSignal): Promise<Profile | null> {
  const config = getConfig();
  const response = await fetch(`${config.apiBaseUrl}/v1/profiles`, {
    headers: headers(newRequestId()),
    signal,
  });
  if (!response.ok) {
    const detail = await readError(response);
    throw new Error(`GET /v1/profiles devolvió ${response.status}: ${detail}`);
  }
  const body = (await response.json()) as ProfilesResponse;
  return body.data.find((profile) => profile.id === config.profile) ?? null;
}

/**
 * Abre el stream de una respuesta. Devuelve el `Response` crudo: el proxy lo
 * reenvía tal cual, sin reensamblar el SSE, que es lo que hace que el texto
 * llegue token a token en vez de de golpe al final.
 */
export function createResponseStream(
  message: string,
  history: Turn[],
  requestId: string,
  signal?: AbortSignal,
): Promise<Response> {
  const config = getConfig();
  // La API no guarda estado —`store: false`, sin `previous_response_id`— así que
  // el hilo entero viaja en cada turno. Se recorta para no crecer sin límite:
  // `truncation: "disabled"` hace que pasarse de ventana falle la petición en
  // lugar de descartar contexto en silencio, y fallar es lo correcto, pero es
  // mejor no llegar ahí.
  const recent = history.slice(-config.maxHistoryTurns * 2);
  const input = [...recent, { role: "user" as const, text: message }].map((turn) => ({
    type: "message",
    role: turn.role,
    content: [{ type: turn.role === "user" ? "input_text" : "output_text", text: turn.text }],
  }));

  return fetch(`${config.apiBaseUrl}/v1/responses`, {
    method: "POST",
    headers: headers(requestId),
    body: JSON.stringify({
      model: config.model,
      input,
      stream: true,
      store: false,
      // Nunca se piden identificadores en claro. En el perfil `luis-cv` el
      // corpus lleva CURP y cédulas, y una UI pública no es "petición explícita
      // y autenticada" de nadie.
      reveal_identifiers: false,
    }),
    signal,
  });
}

export function newRequestId(): string {
  return crypto.randomUUID().replace(/-/g, "");
}

/** Saca un mensaje legible de una respuesta de error, venga como venga. */
export async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as Partial<ErrorEnvelope>;
    if (body.error?.message) return body.error.message;
    return JSON.stringify(body);
  } catch {
    return response.statusText || `HTTP ${response.status}`;
  }
}
