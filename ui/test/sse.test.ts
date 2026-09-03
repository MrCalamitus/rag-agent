/**
 * El parser contra la forma real del stream, incluyendo las dos cosas que se
 * rompen en producción y nunca en una traza limpia: un frame partido entre dos
 * lecturas del socket, y la ruta de fallo `error` → `response.failed` → `[DONE]`.
 */

import { describe, expect, it } from "vitest";
import { readSse, type SseFrame } from "../src/lib/sse";

function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(stream: ReadableStream<Uint8Array>): Promise<SseFrame[]> {
  const frames: SseFrame[] = [];
  for await (const frame of readSse(stream)) frames.push(frame);
  return frames;
}

const frame = (type: string, extra: Record<string, unknown> = {}) =>
  `event: ${type}\ndata: ${JSON.stringify({ type, ...extra })}\n\n`;

describe("readSse", () => {
  it("lee la secuencia canónica y para en [DONE]", async () => {
    const frames = await collect(
      streamOf(
        frame("response.created"),
        frame("response.in_progress"),
        frame("response.output_text.delta", { delta: "Según" }),
        frame("response.output_text.delta", { delta: " el documento" }),
        frame("response.completed"),
        "data: [DONE]\n\n",
      ),
    );

    expect(frames.map((f) => f.event)).toEqual([
      "response.created",
      "response.in_progress",
      "response.output_text.delta",
      "response.output_text.delta",
      "response.completed",
    ]);
    expect((frames[2].data as { delta: string }).delta).toBe("Según");
  });

  it("recompone un frame partido entre dos lecturas", async () => {
    const completo = frame("response.output_text.delta", { delta: "hola" });
    const corte = 12;
    const frames = await collect(
      streamOf(completo.slice(0, corte), completo.slice(corte), "data: [DONE]\n\n"),
    );

    expect(frames).toHaveLength(1);
    expect((frames[0].data as { delta: string }).delta).toBe("hola");
  });

  it("junta varios frames que llegan en un solo chunk", async () => {
    const frames = await collect(
      streamOf(frame("a") + frame("b") + frame("c"), "data: [DONE]\n\n"),
    );
    expect(frames.map((f) => f.event)).toEqual(["a", "b", "c"]);
  });

  it("no emite nada después de [DONE]", async () => {
    const frames = await collect(streamOf("data: [DONE]\n\n", frame("response.created")));
    expect(frames).toEqual([]);
  });

  it("entrega el error y el response.failed antes del terminal", async () => {
    const error = { message: "Bedrock falló.", type: "model_error", request_id: "abc123" };
    const frames = await collect(
      streamOf(
        `event: error\ndata: ${JSON.stringify({ type: "error", error })}\n\n`,
        frame("response.failed"),
        "data: [DONE]\n\n",
      ),
    );

    expect(frames.map((f) => f.event)).toEqual(["error", "response.failed"]);
    expect((frames[0].data as { error: typeof error }).error.request_id).toBe("abc123");
  });

  it("tolera CRLF, comentarios de keep-alive y data ilegible", async () => {
    const frames = await collect(
      streamOf(
        ": keep-alive\n\n",
        "event: response.created\r\ndata: {\"type\":\"response.created\"}\r\n\r\n",
        "event: roto\ndata: {esto no es json}\n\n",
        frame("response.completed"),
        "data: [DONE]\n\n",
      ),
    );

    expect(frames.map((f) => f.event)).toEqual(["response.created", "response.completed"]);
  });

  it("usa 'message' cuando el frame no declara evento", async () => {
    const frames = await collect(streamOf('data: {"ok":true}\n\n', "data: [DONE]\n\n"));
    expect(frames[0].event).toBe("message");
    expect(frames[0].data).toEqual({ ok: true });
  });
});
