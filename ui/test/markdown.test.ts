// @vitest-environment happy-dom
import { beforeAll, describe, expect, it } from "vitest";
import { renderMarkdown } from "../src/components/client/markdown";

function pintar(md: string): HTMLElement {
  const caja = document.createElement("div");
  caja.append(renderMarkdown(md));
  return caja;
}

const TABLA = [
  "| Cartera | 2T25 | 2T26 |",
  "|---|---:|---:|",
  "| Tarjeta de Crédito | 3.2% | 3.5% |",
  "| **Total** | **1.13%** | **1.50%** |",
].join("\n");

describe("renderMarkdown", () => {
  it("convierte una tabla GFM en una tabla real", () => {
    const caja = pintar(TABLA);
    const tabla = caja.querySelector("table");
    expect(tabla).not.toBeNull();
    expect(caja.querySelectorAll("thead th")).toHaveLength(3);
    expect(caja.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(caja.querySelector("thead th")?.textContent).toBe("Cartera");
    // Lo que se veía antes: los pipes en crudo.
    expect(caja.textContent).not.toContain("|---");
  });

  it("respeta la alineación que declara el separador", () => {
    const celdas = pintar(TABLA).querySelectorAll<HTMLElement>("tbody tr:first-child td");
    expect(celdas[0].style.textAlign).toBe("");
    expect(celdas[1].style.textAlign).toBe("right");
  });

  it("interpreta las negritas dentro de una celda", () => {
    const total = pintar(TABLA).querySelector("tbody tr:last-child td");
    expect(total?.querySelector("strong")?.textContent).toBe("Total");
    expect(total?.textContent).not.toContain("**");
  });

  it("envuelve la tabla para que se desplace sin ensanchar la burbuja", () => {
    expect(pintar(TABLA).querySelector(".md-table-wrap > table")).not.toBeNull();
  });

  it("no convierte en tabla un párrafo que solo lleva pipes", () => {
    const caja = pintar("Se usa el operador | para separar campos.");
    expect(caja.querySelector("table")).toBeNull();
    expect(caja.querySelector("p")?.textContent).toContain("|");
  });

  it("deja intactas las citas del agente, que parecen enlaces", () => {
    expect(pintar("Según el reporte [2T26.pdf].").textContent).toContain("[2T26.pdf]");
  });

  it("no ejecuta HTML que venga en la respuesta del modelo", () => {
    const caja = pintar('Antes <img src=x onerror="alert(1)"> y <script>alert(2)</script> después.');
    expect(caja.querySelector("img")).toBeNull();
    expect(caja.querySelector("script")).toBeNull();
    expect(caja.textContent).toContain("<img src=x");
  });

  it("no ejecuta HTML escondido dentro de una celda de tabla", () => {
    const caja = pintar(["| a | b |", "|---|---|", "| <img src=x onerror=alert(1)> | 2 |"].join("\n"));
    expect(caja.querySelector("img")).toBeNull();
    expect(caja.querySelector("td")?.textContent).toContain("<img");
  });

  it("cierra un bloque de código que el stream dejó a medias", () => {
    const caja = pintar("Ejemplo:\n\n```\nmake deploy");
    expect(caja.querySelector("pre code")?.textContent).toBe("make deploy");
  });

  it("arma listas y respeta el tipo", () => {
    expect(pintar("- uno\n- dos").querySelectorAll("ul li")).toHaveLength(2);
    expect(pintar("1. uno\n2. dos").querySelectorAll("ol li")).toHaveLength(2);
  });
});
