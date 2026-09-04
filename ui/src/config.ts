/**
 * La configuración base: qué agente, con qué credencial y —lo importante— sobre
 * QUÉ TEMA.
 *
 * El despliegue del servicio sirve todos los perfiles a la vez y deja que cada
 * petición elija con `X-Rag-Profile`. Esta UI hace lo contrario a propósito: una
 * instancia = un tema. No hay selector porque no es una consola de operador, es
 * la portada de un corpus concreto; cambiar de base de conocimiento es cambiar
 * `RAG_PROFILE` y volver a desplegar.
 *
 * Se valida al importar. Un despliegue mal configurado debe caerse al arrancar,
 * no responder 500 a la primera pregunta de un visitante.
 */

export interface Config {
  apiBaseUrl: string;
  apiToken: string;
  profile: string;
  model: string;
  title: string | null;
  intro: string | null;
  maxHistoryTurns: number;
}

/**
 * Solo `process.env`, y solo en tiempo de ejecución.
 *
 * `import.meta.env` NO se usa a propósito: Vite lo sustituye por un objeto
 * literal durante el build, así que leer el token por ahí lo hornea dentro del
 * artefacto —se comprobó: el `dist/` quedaba con el token en claro—. Una imagen
 * de contenedor no debe llevar secretos dentro; los recibe del entorno al
 * arrancar, que es como se los pasa la task de ECS desde Secrets Manager.
 *
 * En desarrollo, `npm run dev` y `npm run preview` cargan `ui/.env` con la
 * bandera `--env-file` de node.
 */
function read(name: string): string | undefined {
  const value = process.env[name];
  return value === undefined || value === "" ? undefined : value;
}

function required(name: string): string {
  const value = read(name);
  if (value === undefined) {
    throw new Error(
      `Falta la variable de entorno ${name}. Copia ui/.env.example a ui/.env y complétala.`,
    );
  }
  return value;
}

function positiveInt(name: string, fallback: number): number {
  const raw = read(name);
  if (raw === undefined) return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value) || value < 1) {
    throw new Error(`${name} debe ser un entero positivo; llegó ${JSON.stringify(raw)}.`);
  }
  return value;
}

let cached: Config | null = null;

/**
 * Perezosa a propósito: el build no tiene —ni debe tener— el token ni la URL del
 * agente, y una constante de módulo se evaluaría durante `astro build` y lo
 * rompería en cualquier CI. Se resuelve en la primera petición y se memoiza.
 */
export function getConfig(): Config {
  if (cached) return cached;
  cached = {
    // Sin barra final: todas las rutas se concatenan con `/v1/...`.
    apiBaseUrl: required("RAG_API_BASE_URL").replace(/\/+$/, ""),
    apiToken: required("RAG_API_TOKEN"),
    profile: required("RAG_PROFILE"),
    model: read("RAG_MODEL") ?? "agente-rag-sonnet",
    title: read("UI_TITLE") ?? null,
    intro: read("UI_INTRO") ?? null,
    maxHistoryTurns: positiveInt("UI_MAX_HISTORY_TURNS", 8),
  };
  return cached;
}
