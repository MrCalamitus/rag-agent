# UI web (Astro) para consumir el RAG

## Contexto

El agente RAG solo se puede usar hoy por HTTP (`POST /v1/responses`) o por el menú de
terminal (`src/rag_agent/infrastructure/inbound/cli/menu.py`). No existe ninguna pieza
web en el repositorio: cero HTML, cero tooling de node. `docs/PLAN.md` lo dice
explícitamente — *"Decisión cerrada #1: Sin frontend. Solo endpoint"*. Este plan revierte
esa decisión **sin tocar el backend**: añade una UI de chat en Astro que vive en `ui/`,
se despliega aparte y puede borrarse entera sin dejar rastro en `src/`, `infra/` ni en
la suite de pruebas.

El despliegue del backend sirve **todos** los temas a la vez y el cliente elige con la
cabecera `X-Rag-Profile`. La UI hace lo contrario a propósito: **una instancia de la UI
= un solo tema**. El slug se fija en configuración al arrancar y la UI no expone
selector; cambiar de KB es cambiar una variable de entorno y volver a desplegar.

Tres hechos del backend condicionan el diseño y no son negociables:

1. **No hay CORS.** `create_app` (`src/rag_agent/infrastructure/inbound/http/app.py:58`)
   no monta `CORSMiddleware`. Un navegador en otro origen queda bloqueado.
2. **El token es un secreto compartido** (`RAG_API_TOKEN`, Secrets Manager). Meterlo en
   un bundle de JS lo filtra.
3. **El ALB es HTTP puro** (sin `certificate_arn`): una página servida por HTTPS que
   llamara al ALB directamente daría mixed-content.

Los tres se resuelven con la misma pieza: **Astro en modo SSR con un proxy propio**. El
navegador habla solo con Astro (mismo origen, sin CORS, sin token); Astro habla con
FastAPI desde el servidor (sin navegador de por medio, sin mixed-content).

```
navegador ──POST /api/chat──> Astro (node SSR) ──POST /v1/responses──> FastAPI/ALB
   (SSE passthrough)             Authorization: Bearer $RAG_API_TOKEN
                                 X-Rag-Profile: <slug fijo>
```

## Contrato que consume la UI (verificado en el código)

- `POST /v1/responses` — cuerpo validado por `CreateResponseRequest`
  (`.../http/schemas.py:53`). La UI envía `{model, input[], stream:true, store:false}`.
- `input` acepta ítems `{"type":"message","role":"user"|"assistant","content":[{"type":"input_text","text":"…"}]}`.
  **No existe `previous_response_id` ni `store:true`**, así que la UI mantiene el
  historial en el cliente y lo reenvía completo en cada turno.
- SSE (`.../http/sse.py:49`): `event:` == `type` del cuerpo, `sequence_number`
  monótono desde 0, terminal literal `data: [DONE]`. Orden normativo:
  `response.created` → `response.in_progress` → `output_item.added/done`
  (`agente:knowledge_search`) → `output_item.added` + `content_part.added` (message) →
  `response.output_text.delta`* → `output_text.done` → `content_part.done` →
  `output_item.done` → `response.completed` → `[DONE]`.
- Fallo a mitad de stream: evento `error` (con `{"error":{…}}` anidado) **seguido
  siempre** de `response.failed` y `[DONE]`.
- `GET /v1/profiles` (autenticado) devuelve `{default, data:[{id,name,subject,masks_identifiers}]}`.
- Errores HTTP: envoltorio `{"error":{message,type,param,code,request_id}}`.

`EventSource` no sirve (no admite POST ni cabeceras): hay que leer con
`fetch` + `ReadableStream`.

## Estructura a crear

Todo bajo `ui/`, con su propio `package.json`. Nada fuera de esa carpeta salvo lo
indicado en "Toques mínimos al repo existente".

```
ui/
  package.json            # astro, @astrojs/node, @material-symbols/font-400, typescript, vitest
  astro.config.mjs        # output: 'server', adapter node en modo 'standalone'
  tsconfig.json
  .env.example            # documenta todas las variables
  README.md               # cómo correrla contra `make run` y contra el ALB
  src/
    config.ts             # ← la "configuración base": una sola KB
    lib/
      types.ts            # tipos del contrato Open Responses
      sse.ts              # parser de text/event-stream (puro, testeable)
      rag.ts              # cliente server-side: fetch a /v1/responses y /v1/profiles
    pages/
      index.astro         # la página del chat
      api/
        chat.ts           # POST proxy → passthrough del SSE
    components/
      Chat.astro          # marcado + isla de cliente
      Message.astro
      Sources.astro       # panel plegable de evidencia
      Icon.astro          # Material Symbols, decorativo por defecto
      client/chat.ts      # lógica de navegador: fetch stream, render incremental
    styles/
      tokens.css          # custom properties: color, espaciado, tipografía, radios
      global.css          # reset, @font-face de Material Symbols, layout
  test/
    sse.test.ts           # parser contra una traza real de eventos
```

### `ui/src/config.ts` — la configuración base (una sola KB)

Módulo único que lee el entorno, valida y falla ruidosamente si falta algo. Es el punto
donde se fija el tema.

| Variable | Obligatoria | Uso |
|---|---|---|
| `RAG_API_BASE_URL` | sí | `http://localhost:8080` en local; el ALB en despliegue |
| `RAG_API_TOKEN` | sí | va en `Authorization: Bearer`, **solo server-side** |
| `RAG_PROFILE` | sí | slug del único tema servido → cabecera `X-Rag-Profile` |
| `RAG_MODEL` | no | alias, default `agente-rag-sonnet` |
| `UI_TITLE` / `UI_INTRO` | no | overrides de copy |
| `UI_MAX_HISTORY_TURNS` | no | recorte del historial reenviado, default 8 |

Ninguna se prefija `PUBLIC_`: **ninguna debe llegar al navegador**. Se leen de
`process.env` en tiempo de ejecución y **nunca** de `import.meta.env`: durante la
implementación se comprobó que Vite sustituye ese objeto por un literal en el build y
deja el token en claro dentro de `dist/server/`, que es justo lo que no puede llevar
una imagen de contenedor. Por la misma razón la configuración se resuelve de forma
perezosa (`getConfig()`, memoizada) y no como constante de módulo: una constante se
evaluaría durante `astro build` y rompería cualquier CI que no tenga —ni deba tener— el
token. En local, `npm run dev` y `npm run preview` cargan `ui/.env` con la bandera
`--env-file-if-exists` de node. La página obtiene su
título y su descripción del propio perfil, no de constantes duplicadas: en el render del
servidor `index.astro` llama a `GET /v1/profiles`, busca el `id === RAG_PROFILE` y usa su
`name` y `subject` como encabezado y subtítulo (con `UI_TITLE`/`UI_INTRO` como override y
un fallback estático si la llamada falla). Si el slug configurado no está en la lista, la
página muestra un error de configuración claro en vez de dejar que cada pregunta falle
con un 400 `profile_not_found`.

### `ui/src/pages/api/chat.ts` — el proxy

`POST` que recibe `{message: string, history: {role, text}[]}` desde el navegador y:

1. Construye el cuerpo Open Responses: `input` = historial recortado a
   `UI_MAX_HISTORY_TURNS` turnos + el mensaje nuevo, `model` del config, `stream: true`,
   `store: false`.
2. Llama a `${RAG_API_BASE_URL}/v1/responses` con `Authorization`, `Content-Type` y
   `X-Rag-Profile`. Genera y propaga `X-Request-Id`.
3. Si la respuesta **no** es 2xx (alias inválido, token malo, 429), devuelve el
   `{"error":{…}}` tal cual con el mismo status. Esto funciona porque el backend consume
   el primer evento antes de emitir cabeceras (`app.py:157`), así que los fallos previos
   al stream siguen siendo códigos HTTP.
4. Si es 2xx, devuelve `new Response(upstream.body, …)` — **passthrough del stream sin
   reensamblar**, con `Content-Type: text/event-stream`, `Cache-Control: no-cache` y
   `X-Accel-Buffering: no`.
5. Aborta el fetch upstream cuando el cliente se desconecta (`request.signal`), que es lo
   que el backend ya sabe manejar (`app.py:170`).

El token nunca cruza este límite. `export const prerender = false`.

### `ui/src/lib/sse.ts` — parser

Función pura sobre un `ReadableStream<Uint8Array>` que emite `{event, data}`, con
buffer entre chunks (un frame puede partirse en dos lecturas) y corte en `[DONE]`. Se
usa en el navegador y se prueba con vitest sin red.

### `ui/src/components/client/chat.ts` — render incremental

Máquina de estados sobre los eventos, ignorando los desconocidos (el contrato lo exige):

- `response.output_item.added` con `item.type === "agente:knowledge_search"` → estado
  "consultando documentos".
- `response.output_item.done` del mismo tipo → guarda `item.results`
  (`{document_id, chunk, score, metadata}`) y `item.latency_ms` para el panel de fuentes.
- `response.output_text.delta` → concatena `delta` al burbuja del asistente.
- `response.output_text.done` → fija el texto final (fuente de verdad frente a los deltas).
- `error` → pinta `error.message` y `error.request_id`.
- `[DONE]` sin `response.completed` → marca la respuesta como incompleta, tal como manda
  el contrato §5.

### Panel de fuentes (`Sources.astro`)

Bajo cada respuesta, un `<details>` — *"N fragmentos consultados · 340 ms"* — que al
abrirse lista cada resultado con el `document_id` (basename, con la ruta completa en
`title`), el `score` formateado, los metadatos del perfil (p. ej. `marca` en `autos`) y
el `chunk` en un bloque con scroll propio. Es el argumento de auditabilidad del contrato
§4 hecho visible; el `<details>` nativo evita JS y da accesibilidad gratis.

### Iconografía: Material Symbols

Se usa **Material Symbols Rounded**, autohospedado con el paquete npm
`@material-symbols/font-400` en lugar del `<link>` a
`fonts.googleapis.com`: sin petición a terceros, sin FOUT y funciona con el backend
local sin red. Se declara una `@font-face` en `global.css` con `font-display: block` y
la clase `.msym` con los `font-variation-settings` de la fuente. El paquete trae el peso
fijado en 400, así que el eje `wght` no varía; los que sí responden son `FILL`, `GRAD` y
`opsz`.

Un componente `Icon.astro` (`<Icon name="send" />`) envuelve el patrón para no repetir
markup, y **todo icono es decorativo por defecto**: `aria-hidden="true"` más
`translate="no"`, porque el ligature de Material Symbols es texto literal que un lector
de pantalla leería y un traductor automático rompería. Cuando el icono es el único
contenido de un control, la etiqueta va en el `aria-label` del botón, nunca en el icono.

Inventario (nombres exactos de la fuente): `send` (enviar), `stop_circle` (cancelar el
stream en curso), `person` y `smart_toy` (avatares de turno), `description` (documento en
el panel de fuentes), `expand_more` (marcador del `<details>`, rotado por CSS al abrir),
`content_copy` / `check` (copiar respuesta, con intercambio al confirmar), `error` (fallo)
y `search` (estado "consultando documentos"). Tamaño base 20 px alineado con la altura de
línea del texto.

### UX/UI: criterios que se aplican

Sin librería de componentes — CSS propio (~250 líneas) sobre tokens, que es lo que
mantiene la UI desacoplada. Las decisiones concretas:

- **Tokens, no valores sueltos.** Custom properties para color, espaciado (escala de
  4 px), radio y tipografía en `:root`, redefinidas bajo `@media (prefers-color-scheme:
  dark)`. Un solo sitio donde cambiar la marca.
- **Los cinco estados, dibujados.** Vacío (con 3–4 preguntas de ejemplo derivadas del
  `subject` del perfil, clicables), enviando, en streaming, error y vacío-tras-error. La
  respuesta que declina ("Eso no consta…") **no** es un error: se pinta como respuesta
  normal, porque en este agente es un acierto.
- **Feedback inmediato.** La burbuja del usuario aparece al pulsar enviar, sin esperar al
  servidor; el indicador de recuperación (`search` + texto) aparece con el
  `output_item.added` del `knowledge_search`; el primer `delta` lo sustituye. Botón de
  parar (`stop_circle`) mientras hay stream, que aborta el `fetch` — el backend ya cancela
  la inferencia al desconectarse el cliente.
- **Contraste y foco (WCAG 2.2 AA).** Texto ≥ 4.5:1, bordes y estados de UI ≥ 3:1,
  `:focus-visible` con anillo de 2 px y `outline-offset` visible en ambos temas, objetivos
  táctiles ≥ 24×24 px reales (2.5.8), y `prefers-reduced-motion` respetado en todas las
  transiciones.
- **Teclado y lectores.** Enter envía / Shift+Enter salta línea; el hilo es una región
  `aria-live="polite"` que anuncia la respuesta **al terminar** (anunciar cada delta
  inunda el lector: los deltas se escriben en un nodo `aria-hidden` y el texto final se
  publica en el nodo vivo); foco de vuelta al textarea al completar; jerarquía real de
  encabezados y `<form>` de verdad, para que funcione sin JS hasta donde se pueda.
- **Legibilidad del hilo.** Ancho de medida ~68ch, jerarquía por peso y color antes que
  por tamaño, `overflow-wrap: anywhere` y bloques de código/tablas con scroll propio para
  que la página nunca desplace en horizontal.
- **Móvil primero.** Un solo diseño fluido, textarea que crece hasta un máximo, compositor
  fijo abajo con `env(safe-area-inset-bottom)`, y `100dvh` (no `100vh`) para que la barra
  de URL de iOS no tape el input.
- **Carga cognitiva.** Una sola acción primaria por pantalla; el panel de fuentes empieza
  cerrado y resume ("6 fragmentos · 340 ms") para que la evidencia esté disponible sin
  competir con la respuesta.

Tras la implementación, pasada de revisión con el agente `ux-ui-expert` sobre los
componentes ya construidos, que es su orden de uso previsto.

## Toques mínimos al repo existente

Solo tres, todos aditivos y reversibles:

1. `.gitignore` — `ui/node_modules/`, `ui/dist/`, `ui/.astro/`, `ui/.env`.
2. `Makefile` — targets opcionales `ui-install`, `ui-dev`, `ui-build` que delegan en npm
   dentro de `ui/`. No se enganchan a `test` ni a `deploy`.
3. `docs/` — la documentación del plan (ver abajo).

**No** se toca `src/`, **no** se añade CORS, **no** se toca `infra/`.

## Dónde se documenta

`docs/PLAN.md` ya existe como archivo, así que en un macOS con APFS insensible a
mayúsculas **no se puede crear un directorio `docs/plan/`**: colisionaría. El plan va
por tanto en **`docs/plan-ui-astro.md`** (este documento, adaptado), y en `docs/PLAN.md`
se añade una nota corta donde vive la *"Decisión cerrada #1: Sin frontend"* señalando que
se revierte, con enlace al nuevo archivo y la razón (la UI no vive en el servicio, vive
aparte).

## Verificación de punta a punta

1. **Backend local sin AWS**: `make run` con `.env` en `RAG_RETRIEVAL_BACKEND=local` y
   `RAG_INFERENCE_BACKEND=stub`. Comprobar `curl localhost:8080/healthz`.
2. **UI**: `cd ui && npm install && npm run dev` con `ui/.env` apuntando a
   `RAG_API_BASE_URL=http://localhost:8080`, `RAG_API_TOKEN=local-dev-token`,
   `RAG_PROFILE=autos`.
3. **Camino feliz**: preguntar algo del corpus de `autos` y confirmar en el navegador que
   (a) el texto aparece token a token, no de golpe; (b) el panel de fuentes lista
   fragmentos con `document_id` y `score`; (c) una segunda pregunta que dependa de la
   primera demuestra que el historial se reenvía.
4. **Camino de fallo**: con `RAG_PROFILE=noexiste` la página debe avisar de configuración
   inválida; con `RAG_API_TOKEN` erróneo, la UI debe mostrar el 401 y no colgarse; matar
   `uvicorn` a mitad de stream debe dejar la respuesta marcada como incompleta.
5. **Fuga de token**: `curl -s localhost:4321/ | grep -i "$RAG_API_TOKEN"` no debe
   devolver nada, y lo mismo sobre los `.js` de `ui/dist/client/` tras `npm run build`.
6. **Accesibilidad**: recorrer el chat completo solo con teclado (Tab → textarea → enviar
   → abrir fuentes → copiar) comprobando que el foco es siempre visible; ejecutar Lighthouse
   accesibilidad ≥ 95; verificar con `prefers-reduced-motion: reduce` activo que no queda
   ninguna animación; y confirmar que la fuente de iconos se sirve desde el propio origen
   (pestaña Network sin peticiones a `fonts.gstatic.com`).
7. **Unitarias**: `cd ui && npm test` — el parser de `sse.ts` sobre una traza capturada
   con `curl -N` contra el backend local, incluyendo frames partidos y la ruta
   `error` → `response.failed` → `[DONE]`.
8. **Producción**: `npm run build && node ./dist/server/entry.mjs` con
   `RAG_API_BASE_URL` apuntando al ALB y `RAG_PROFILE=luis-cv`; repetir el paso 3.
9. **No regresión del backend**: `make test` debe seguir verde y su salida idéntica —
   ningún archivo de `src/` ni de `tests/` cambia.

## Riesgos conocidos (a dejar escritos, no a resolver ahora)

- **Rate limit compartido.** El bucket se calcula con el sha256 de la cabecera
  `Authorization` (`.../http/security.py:41`), así que *todos* los visitantes de la UI
  comparten un solo cupo de 20 req/min. Aceptable para una demo; si la UI se abre al
  público hay que subir `rate_limit_per_minute` o pasar a limitar por IP en el proxy de
  Astro.
- **La UI queda sin autenticación propia.** Quien alcance el host de Astro puede
  preguntar. Si eso importa, el sitio para poner la puerta es `api/chat.ts`.
- **El perfil `luis-cv` enmascara identificadores**; la UI nunca envía
  `reveal_identifiers: true`.

---

## Lo que quedó construido

Implementado y verificado el 2026-09-02. Todo bajo `ui/`; el backend no cambió.

- `ui/src/config.ts` — la configuración base, perezosa y solo desde `process.env`.
- `ui/src/lib/{types,sse,rag}.ts` — el contrato, el parser de SSE y el cliente del agente.
- `ui/src/pages/api/chat.ts` — el proxy con passthrough del stream.
- `ui/src/pages/index.astro` — la portada; resuelve el perfil y saca de él el copy.
- `ui/src/components/` — `Icon`, `Message`, `Sources`, `Chat` y `client/chat.ts`.
- `ui/src/styles/{tokens,global}.css` — la marca en un archivo, el resto en otro.
- `ui/test/sse.test.ts` — 7 casos, incluidos el frame partido y la ruta de fallo.
- `ui/README.md` — cómo arrancarla, cómo desplegarla y qué mirar antes de abrirla.

Fuera de `ui/`: cuatro objetivos `ui-*` en el `Makefile`, cuatro líneas en
`.gitignore`, la nota en `docs/PLAN.md` y este documento.

**Dos hallazgos durante la implementación**, ambos ya resueltos arriba: el token se
horneaba en `dist/` al leerlo por `import.meta.env`, y `RAG_CORPUS_DIR` en el `.env` de
la raíz fuerza un corpus para *todos* los perfiles, de modo que la UI parecía ignorar el
tema cuando en realidad lo ignoraba el servicio. Lo segundo no es un fallo —la variable
existe justo para eso— pero conviene saberlo antes de depurar en falso.

**Un arreglo ajeno.** `tests/unit/test_perfiles.py` seguía afirmando sobre el slug
`coches` después del renombrado a `autos` que ya estaba en el índice, y dejaba la suite en
rojo. Se corrigieron esas dos líneas; el resto de apariciones de `coches` en ese archivo
son fixtures inventados y se quedan como están.

### Lo que no se hizo

- **Sin `Dockerfile` ni nada en `infra/`.** El despliegue sigue sin decidir, y hasta que
  se decida esta carpeta no obliga a nada.
- **Sin autenticación propia ni límite de tasa por visitante.** Ambos riesgos siguen
  escritos abajo tal cual; la UI es hoy apta para una demo, no para tráfico abierto.


---

## Exposición de documentos por perfil

Añadido después de la primera entrega, para responder a *«¿dónde puede el usuario
buscar lo que el agente está contestando?»*.

### El obstáculo

Los PDF originales no están en ningún sitio alcanzable, y no por descuido:
`scripts/sync-kb.sh:8` sube **solo** el corpus preparado, *"nunca la de documentos
originales: lo que entra a S3 es lo que el agente puede llegar a recitar"*. Lo
que sí viaja con cada fragmento es `fuente` —el nombre del PDF—, `paginas` —el
total del documento, no la página del fragmento— y `fragmento X de N`.

### El reparto: clasificar en la ingesta, decidir al servir

Las dos mitades cuestan muy distinto de cambiar, así que viven en sitios
distintos:

- **La ingesta estampa una clase**, no un permiso. `pipeline.preparar` la calcula
  con el documento entero delante y la escribe en el sidecar como `clase`. Es una
  propiedad del documento; reclasificar exige reingesta.
- **El perfil declara la política**: `documentos.expone`. Se evalúa al responder,
  en `create_response._retrieve` vía `retrieval.aplicar_exposicion`. Cambiar de
  opinión no toca un solo byte del corpus.

Si todo se hubiera resuelto en la ingesta, cada cambio de criterio obligaría a
reingestar; si todo se hubiera resuelto al servir, habría que releer los
documentos en cada respuesta para saber qué son.

### La clase se deduce de tres señales, en orden

`marcadores` → `rutas` → `tipos` → `por_defecto`. El orden no es arbitrario: el
contenido de un documento no se puede cambiar renombrándolo, la carpeta es una
decisión explícita de quien organizó el corpus, y el tipo se infiere del nombre
del archivo, que es la señal más barata y la más fácil de equivocar. Un CV que
lleve dentro una CURP cae en `identidad` aunque su nombre y su carpeta digan otra
cosa, que es la respuesta correcta.

### Cerrado por defecto

Un perfil sin bloque `documentos:` no expone nada, y un fragmento sin `clase`
—un corpus preparado antes de que esto existiera— tampoco. Es lo contrario al
criterio de `redaction`, y la asimetría es intencional: equivocarse allí tapa un
dato, equivocarse aquí publica un archivo. Por la misma razón, `expone` con una
clase que no existe es un error duro y no un silencio.

### Los dos perfiles del repositorio

| Perfil | `por_defecto` | `expone` | Por qué |
|---|---|---|---|
| `autos` | `publico` | `[publico]` | Folletos y fichas que las marcas publican para que se lean. Un PDF nuevo entra expuesto sin que nadie lo clasifique |
| `luis-cv` | `identidad` | `[]` | Credenciales. Entregar el archivo anularía el enmascarado: daría íntegro, en un PDF, el número que la respuesta tapa con asteriscos |

El YAML de `luis-cv` deja escrito, comentado, qué habría que añadir para abrir el
CV —el único documento de ese corpus que su dueño reparte él mismo—. Es una
decisión suya, no una que herede de un defecto.

### Lo que ve el usuario

El panel de fuentes agrupa por documento y nombra el PDF real (`metadata.fuente`)
en lugar del `document_id`, que es el nombre interno del trozo de markdown
—`ficha-tecnica-hilux--001.md` no existe para nadie fuera del corpus—. Cada
fragmento lleva un botón de **copiar cita**: como la metadata guarda el total de
páginas y no la página del fragmento, pegar la frase en el buscador del visor es
hoy la forma de llegar al sitio exacto.

Un documento no expuesto **no se oculta**: se ve su nombre, su score y su texto,
y solo se añade una nota diciendo que el archivo no se entrega. Esa nota aparece
únicamente en temas que sí publican algunos documentos; en uno que no publica
ninguno sería repetir en cada tarjeta lo que la cabecera ya dice una vez.

### La respuesta también cita el PDF

El panel dejaba de hablar de `.md` pero el modelo seguía escribiendo
`[ficha-tecnica-hilux--003]` dentro del texto: dos nombres para lo mismo, y el
que el lector veía primero era el que no podía buscar.

El modelo solo cita lo que el prompt le pone entre corchetes, así que el cambio
es el encabezado de cada fragmento en `prompts.render_context`, que ahora usa
`Chunk.citation` —`metadata.fuente`, con `document_id` como respaldo— y omite
`fuente` de la línea de metadatos para no invitar a citar `fuente=…`.

Con eso, `_is_grounded` tenía que mirar el mismo nombre: comprobarlo contra
`documents()` daría por no fundamentada una respuesta que cita el PDF
correctamente. Ahora usa `RetrievalOutcome.citations()`, que es la lista de lo
que el prompt pidió citar. `documents()` se queda como está porque es lo que
mide la telemetría: ahí cada trozo cuenta por separado.

Dos fragmentos del mismo PDF citan igual, y es lo que se busca: al lector le
importa qué archivo abrir, no qué trozo del índice acertó.

### Lo que queda pendiente

- **La página de cada fragmento.** `extractors.limpiar` recibe las páginas por
  separado y las funde antes de trocear, así que el límite se conoce y se tira.
  Propagarlo daría *"página 3 de 12"* y habilitaría el enlace `#page=N`. Exige
  reingesta.
- **El archivo en sí.** `exposed: true` dice que se *puede* entregar, pero
  todavía no hay de dónde: haría falta subir los originales a un prefijo aparte
  de S3 y firmarlos desde el proxy de Astro. Eso matiza la postura escrita en
  `sync-kb.sh` y merece decidirse aparte, no colarse aquí.
