// @ts-check
import { defineConfig } from "astro/config";
import node from "@astrojs/node";

// SSR, no estático. La UI necesita un servidor propio por una razón concreta:
// es quien guarda el token del agente. El navegador nunca habla con la API del
// RAG — habla con `/api/chat`, que corre aquí. Eso resuelve de un golpe las tres
// cosas que hoy impiden un cliente de navegador: no hay CORS en el servicio, el
// token es un secreto compartido, y el ALB es HTTP puro (mixed-content).
export default defineConfig({
  output: "server",
  adapter: node({ mode: "standalone" }),
  server: { port: 4321 },
  devToolbar: { enabled: false },
});
