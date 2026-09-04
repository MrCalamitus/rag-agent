/**
 * El contrato Open Responses, en lo que esta UI consume de él.
 *
 * Solo los campos que se leen. El contrato obliga a los clientes a ignorar lo
 * desconocido sin perder la capacidad de reconstruir la respuesta, así que
 * declarar de menos es correcto; declarar de más sería inventarse un acuerdo.
 * La fuente de verdad es `docs/contrato-open-responses.md`.
 */

/** Tema servido por el despliegue. `GET /v1/profiles`. */
export interface Profile {
  id: string;
  name: string;
  subject: string;
  masks_identifiers: boolean;
  /** Si este tema publica alguno de sus documentos originales. */
  exposes_documents?: boolean;
}

export interface ProfilesResponse {
  default: string;
  data: Profile[];
}

/** Objeto de error del contrato §5. Siempre llega anidado bajo `error`. */
export interface AgentError {
  message: string;
  type: string;
  param?: string | null;
  code?: string | null;
  request_id?: string | null;
}

export interface ErrorEnvelope {
  error: AgentError;
}

/** Un fragmento recuperado. Es la evidencia que sustenta la respuesta. */
export interface RetrievalResult {
  document_id: string;
  chunk: string;
  score: number;
  metadata?: Record<string, unknown>;
  /**
   * Si el perfil deja consultar el documento original de este fragmento. Lo
   * decide el servicio aplicando la política del tema sobre la clase que la
   * ingesta estampó; la UI no conoce ninguna de las dos cosas, solo el veredicto.
   */
  exposed?: boolean;
  /**
   * Dónde abrir el documento original. Lo firma el agente al responder, así que
   * su ausencia significa «no autorizado» o «no hay de dónde servirlo», y en
   * ninguno de los dos casos puede el cliente fabricarlo.
   */
  document_url?: string | null;
}

/** El ítem de la herramienta hospedada. Extensión propia, contrato §4. */
export interface KnowledgeSearchItem {
  type: "agente:knowledge_search";
  id: string;
  status: string;
  queries: string[];
  results: RetrievalResult[];
  latency_ms?: number;
}

export const KNOWLEDGE_SEARCH = "agente:knowledge_search";

/** Rol admitido en `input`. La UI solo usa los dos primeros. */
export type Role = "user" | "assistant";

/** Un turno tal como lo guarda el navegador y lo reenvía al proxy. */
export interface Turn {
  role: Role;
  text: string;
}

/** Cuerpo que el navegador manda a `/api/chat`. */
export interface ChatRequest {
  message: string;
  history: Turn[];
}
