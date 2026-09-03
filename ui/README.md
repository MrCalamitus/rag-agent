# UI del agente RAG

Una página de chat sobre `POST /v1/responses`. Vive aparte del servicio a
propósito: tiene su propio `package.json`, su propio despliegue y su propio ciclo
de vida. Borrar esta carpeta no deja rastro en `src/`, `infra/` ni en la suite de
pruebas del backend.

## Una instancia, un tema

El despliegue del agente sirve **todos** los perfiles a la vez y deja que cada
petición elija con la cabecera `X-Rag-Profile`. Esta UI hace lo contrario: fija
el tema en `RAG_PROFILE` y no ofrece selector. No es una consola de operador, es
la portada de un corpus concreto. Cambiar de base de conocimiento es cambiar una
variable y volver a arrancar.

El título, la descripción y las preguntas de ejemplo salen del propio perfil
—`GET /v1/profiles`— en lugar de estar escritos aquí. Si el YAML del servicio
cambia, esta página cambia con él. Si el slug configurado no existe en el
despliegue, la página lo dice al cargar en vez de dejar que cada pregunta muera
con un `400 profile_not_found`.

## Por qué SSR y no un sitio estático

Tres cosas impiden que un navegador llame directamente al agente:

1. El servicio no monta CORS.
2. El token es un secreto compartido: en un bundle de JS estaría publicado.
3. El ALB es HTTP puro, así que una página HTTPS daría *mixed-content*.

Las tres se resuelven con la misma pieza. El navegador habla solo con
`/api/chat`, del mismo origen; ese endpoint corre en el servidor de Astro, añade
el `Authorization` y el `X-Rag-Profile`, y reenvía el `text/event-stream` tal
cual, sin reensamblarlo —por eso el texto aparece token a token—.

```
navegador ──POST /api/chat──> Astro (node SSR) ──POST /v1/responses──> agente
   SSE passthrough               Authorization: Bearer …
                                 X-Rag-Profile: <slug>
```

El token nunca cruza ese límite. Está verificado en el paso 5 de la lista de
abajo, y conviene volver a comprobarlo cada vez que se toque `config.ts`.

## Arrancar en local

Con el agente corriendo sin AWS, desde la raíz del repositorio:

```bash
RAG_INFERENCE_BACKEND=stub RAG_RETRIEVAL_BACKEND=local RAG_CORPUS_DIR= \
  make run
```

`RAG_CORPUS_DIR=` vacío es importante: si el `.env` de la raíz lo tiene puesto,
fuerza ese corpus para **todos** los perfiles y da igual el tema que pidas.

Y aquí:

```bash
cp .env.example .env     # ajusta RAG_PROFILE al tema que quieras servir
npm install
npm run dev              # http://localhost:4321
```

| Comando | Qué hace |
|---|---|
| `npm run dev` | Servidor de desarrollo con recarga |
| `npm run build` | Compila a `dist/` |
| `npm run preview` | Sirve `dist/` con `.env` cargado |
| `npm run check` | Tipos de TypeScript y de los `.astro` |
| `npm test` | El parser de SSE |

## Configuración

Todas en `.env.example`. Ninguna lleva prefijo `PUBLIC_` porque ninguna debe
llegar al navegador. Se leen de `process.env` **en tiempo de ejecución**, nunca
de `import.meta.env`: Vite sustituye ese objeto durante el build y hornearía el
token dentro de `dist/`, que es exactamente lo que no debe pasar en una imagen de
contenedor. En producción las variables las pone el entorno; en local las carga
node con `--env-file-if-exists`.

## Desplegar

`npm run build` produce un servidor de node autónomo:

```bash
npm run build
RAG_API_BASE_URL=… RAG_API_TOKEN=… RAG_PROFILE=… PORT=8080 \
  node ./dist/server/entry.mjs
```

No hay `Dockerfile` todavía ni nada en `infra/`: el despliegue está deliberadamente
sin decidir, y hasta que se decida esta carpeta no obliga a nada.

Dos cosas a tener presentes antes de abrirla al público:

- **El límite de tasa es compartido.** El agente lo calcula sobre la cabecera
  `Authorization`, así que todos los visitantes gastan del mismo cupo (20 por
  minuto por defecto). Para una demo va bien; para tráfico real hay que subirlo
  en el servicio o limitar por IP en `api/chat.ts`.
- **La UI no tiene autenticación propia.** Quien alcance el host puede preguntar.
  El sitio donde poner la puerta es `api/chat.ts`.

## Cómo está organizado

```
src/
  config.ts             La configuración base: qué agente, qué token, QUÉ TEMA
  lib/types.ts          El contrato, en lo que esta UI consume de él
  lib/sse.ts            Parser de text/event-stream (puro, con pruebas)
  lib/rag.ts            Cliente del agente — solo servidor, es quien toca el token
  pages/index.astro     La portada; resuelve el perfil y su copy
  pages/api/chat.ts     El proxy con passthrough del stream
  components/           Icon, Message, Sources, Chat + la lógica de navegador
  styles/               tokens.css (la marca) y global.css
```

## El panel de fuentes

Agrupa por documento y nombra el PDF real (`metadata.fuente`), no el
`document_id` —que es el nombre interno del trozo de markdown que produjo la
ingesta y no existe para nadie fuera del corpus—. Cada fragmento trae un botón de
copiar la cita: la metadata guarda el total de páginas del documento, no la
página del fragmento, así que pegar la frase en el buscador del visor es hoy la
forma de llegar al sitio exacto.

Cada resultado llega con `exposed`, que dice si el tema deja consultar el
documento original. Un documento no expuesto no se oculta: se ve su nombre, su
score y su texto, y solo se añade una nota de que el archivo no se entrega —y
únicamente en temas que sí publican algunos, porque en uno que no publica ninguno
sería ruido repetido en cada tarjeta. La política vive en el `documentos:` del
perfil, en el servicio; aquí solo se lee el veredicto.

## El visor del documento

Cuando el agente autoriza un documento, cada fragmento trae un botón que lo abre
en un visor de pdf.js con la cita resaltada. La página no se sabe —la metadata
guarda el total del documento, no la del fragmento— así que se busca en la capa
de texto del PDF; buscar da además las coordenadas de la marca, que es lo que se
quería. Si la cita no se localiza, el visor abre igual y lo dice.

El PDF llega por `/api/document`, que reenvía al agente con el `Authorization`.
El permiso viaja firmado en la query y este proxy no lo interpreta: la
autorización se decidió al responder.

## Iconos

Los iconos son **Material Symbols Rounded** servidos desde el propio origen —el
paquete npm, no el `<link>` a Google— para no depender de un tercero y funcionar
sin red. Van con `aria-hidden` y `translate="no"`: el glifo se dibuja desde un
ligature, o sea que el nodo contiene la palabra "send" como texto, y sin eso un
lector de pantalla la leería y un traductor la rompería.
