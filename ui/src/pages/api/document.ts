/**
 * Proxy del documento original.
 *
 * Mismo papel que `api/chat.ts` y por las mismas razones: el navegador no puede
 * hablar con el agente —no hay CORS y el token no puede salir del servidor—, así
 * que pide el archivo aquí y este lo reenvía con el `Authorization`.
 *
 * El permiso viaja en la query, firmado por el agente cuando emitió la
 * respuesta. Este proxy no lo interpreta: lo pasa tal cual, porque la
 * autorización se decidió al responder y aquí no hay contexto para revisarla.
 */

import type { APIRoute } from "astro";
import { getConfig } from "../../config";

export const prerender = false;

/** Solo lo que el enlace del agente lleva. Nada más se reenvía. */
const PERMITIDOS = ["profile", "exp", "sig"] as const;

export const GET: APIRoute = async ({ url, request }) => {
  const config = getConfig();
  const nombre = url.searchParams.get("name") ?? "";

  // El nombre viaja como parámetro y no como tramo de ruta para que Astro no
  // tenga que decidir qué hacer con los espacios y acentos de los folletos.
  if (!nombre || nombre.includes("/") || nombre.includes("\\")) {
    return new Response("Documento no válido.", { status: 400 });
  }

  const consulta = new URLSearchParams();
  for (const clave of PERMITIDOS) {
    const valor = url.searchParams.get(clave);
    if (valor !== null) consulta.set(clave, valor);
  }

  const destino = `${config.apiBaseUrl}/v1/documents/${encodeURIComponent(nombre)}?${consulta}`;

  let upstream: Response;
  try {
    upstream = await fetch(destino, {
      headers: { Authorization: `Bearer ${config.apiToken}` },
      signal: request.signal,
    });
  } catch {
    if (request.signal.aborted) return new Response(null, { status: 499 });
    return new Response("No se pudo contactar con el agente.", { status: 502 });
  }

  if (!upstream.ok || !upstream.body) {
    return new Response("El documento no está disponible.", { status: upstream.status });
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": upstream.headers.get("content-type") ?? "application/pdf",
      "Content-Disposition": upstream.headers.get("content-disposition") ?? "inline",
      "Cache-Control": "private, max-age=300",
    },
  });
};
