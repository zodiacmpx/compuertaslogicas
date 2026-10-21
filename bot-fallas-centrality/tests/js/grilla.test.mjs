// Tests de extension/grilla.js con un ExtJS falso en globalThis.
// Correr desde la raíz del proyecto: node --test "tests/js/*.test.mjs"
import test from "node:test";
import assert from "node:assert/strict";

import { leerGrillasExt } from "../../extension/grilla.js";

// ---- dobles mínimos de ExtJS (sólo lo que leerGrillasExt usa) ----

function registro(data) {
  return { data, get(k) { return this.data[k]; } };
}

function storeDe(recs, total) {
  return {
    getRange: () => recs,
    getCount: () => recs.length,
    getTotalCount: () => (total == null ? recs.length : total),
  };
}

/** modo: cómo expone las columnas ("getColumns" | "headerCt" | "columns") */
function grilla({ id, titulo = "", columnas = [], recs = [], total, visible = true, modo = "getColumns", extra = {} }) {
  const g = { id, title: titulo, store: storeDe(recs, total), isVisible: (profundo) => (profundo === true ? visible : true), ...extra };
  if (modo === "getColumns") g.getColumns = () => columnas;
  else if (modo === "headerCt") g.headerCt = { getGridColumns: () => columnas };
  else g.columns = columnas;
  return g;
}

function instalar(grillas, { consulta } = {}) {
  globalThis.window = {
    Ext: {
      ComponentQuery: {
        query(sel) {
          assert.equal(sel, "gridpanel");
          if (consulta) return consulta();
          return grillas;
        },
      },
    },
  };
  globalThis.location = { href: "https://centrality.ejemplo.invalid/bandeja" };
  globalThis.document = { title: "Centrality (sintético)" };
}

function desinstalar() {
  delete globalThis.window;
  delete globalThis.location;
  delete globalThis.document;
}

test.afterEach(desinstalar);

const COLS = [
  { dataIndex: "caso", text: "Caso" },
  { dataIndex: "direccion", text: "<span class=\"x-hd\">Dirección</span>" },
  { dataIndex: "nis", header: "NIS" },                       // ExtJS 4: header en vez de text
  { dataIndex: "clientesAfectados", text: "<span data-qtip=\"Clientes afectados\">👥</span>", tooltip: "Clientes afectados" },
  { dataIndex: "fechaCreacion", text: "Fecha" },
  { dataIndex: "", text: "Acciones" },                       // columna de acciones: sin dato
  { text: "#" },                                             // numerador: sin dataIndex
];

function recsSinteticos(n = 3) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push(registro({
      caso: String(40877700 + i),
      direccion: `AV. MATTA ${1000 + i}, OSORNO`,
      nis: 7000000 + i,
      clientesAfectados: 100 + i,
      fechaCreacion: new Date(2026, 9, 21, 13, 10 + i, 0),
      // campos sin columna visible
      sed: `SED ${500 + i}`,
      enInterrupcion: true,
      sinValor: null,
      // no primitivos: se omiten
      brigada: { nombre: "BBOO PRUEBA" },
      etiquetas: ["a", "b"],
      raro: Number.NaN,
      fn() { return 1; },
    }));
  }
  return out;
}

test("sin ExtJS -> error explícito", () => {
  desinstalar();
  assert.deepEqual(leerGrillasExt(10), { error: "La página no expone ExtJS" });
  globalThis.window = {};
  assert.deepEqual(leerGrillasExt(10), { error: "La página no expone ExtJS" });
  globalThis.window = { Ext: { version: "6" } };
  assert.deepEqual(leerGrillasExt(10), { error: "La página no expone ExtJS" });
});

test("columnas: texto sin HTML, dataIndex, tooltip; se saltan las sin dataIndex", () => {
  instalar([grilla({ id: "gridpanel-12", titulo: "Bandeja de reclamos", columnas: COLS, recs: recsSinteticos(1) })]);
  const r = leerGrillasExt(3000);
  assert.equal(r.url, "https://centrality.ejemplo.invalid/bandeja");
  assert.equal(r.titulo, "Centrality (sintético)");
  assert.equal(r.grillas.length, 1);
  const g = r.grillas[0];
  assert.equal(g.id, "gridpanel-12");
  assert.equal(g.titulo, "Bandeja de reclamos");
  assert.deepEqual(g.columnas, [
    { dataIndex: "caso", texto: "Caso" },
    { dataIndex: "direccion", texto: "Dirección" },
    { dataIndex: "nis", texto: "NIS" },
    { dataIndex: "clientesAfectados", texto: "👥", tooltip: "Clientes afectados" },
    { dataIndex: "fechaCreacion", texto: "Fecha" },
  ]);
});

test("filas: clave = texto de columna, Date local, objetos/arrays/NaN omitidos, extras de rec.data", () => {
  instalar([grilla({ id: "g1", columnas: COLS, recs: recsSinteticos(1) })]);
  const fila = leerGrillasExt(3000).grillas[0].filas[0];
  assert.deepEqual(fila, {
    "Caso": "40877700",
    "Dirección": "AV. MATTA 1000, OSORNO",
    "NIS": 7000000,
    "👥": 100,
    "Fecha": "2026-10-21 13:10:00",
    // no cubiertos por columnas, con su nombre crudo
    sed: "SED 500",
    enInterrupcion: true,
    sinValor: null,
  });
  // los campos cubiertos por una columna NO se repiten con el nombre crudo
  assert.equal("caso" in fila, false);
  assert.equal("brigada" in fila || "etiquetas" in fila || "raro" in fila || "fn" in fila, false);
});

test("columna sin texto usa el dataIndex como clave", () => {
  instalar([grilla({ id: "g1", columnas: [{ dataIndex: "caso", text: "  " }], recs: [registro({ caso: "1" })] })]);
  assert.deepEqual(leerGrillasExt(10).grillas[0].filas, [{ caso: "1" }]);
});

test("dos columnas con el mismo título no se pisan", () => {
  const cols = [{ dataIndex: "fechaCreacion", text: "Fecha" }, { dataIndex: "fechaCierre", text: "Fecha" }];
  instalar([grilla({ id: "g1", columnas: cols, recs: [registro({ fechaCreacion: "a", fechaCierre: "b" })] })]);
  assert.deepEqual(leerGrillasExt(10).grillas[0].filas[0], { Fecha: "a", fechaCierre: "b" });
});

test("maxFilas limita filas; total y cargadas informan el universo", () => {
  instalar([grilla({ id: "g1", columnas: COLS, recs: recsSinteticos(5), total: 40 })]);
  const g = leerGrillasExt(2).grillas[0];
  assert.equal(g.filas.length, 2);
  assert.equal(g.cargadas, 5);
  assert.equal(g.total, 40);
  // inválido / ausente -> 3000 por defecto
  assert.equal(leerGrillasExt().grillas[0].filas.length, 5);
  assert.equal(leerGrillasExt(0).grillas[0].filas.length, 5);
  assert.equal(leerGrillasExt("x").grillas[0].filas.length, 5);
});

test("default de 3000 filas", () => {
  const recs = [];
  for (let i = 0; i < 3005; i++) recs.push(registro({ caso: String(i) }));
  instalar([grilla({ id: "g1", columnas: [{ dataIndex: "caso", text: "Caso" }], recs })]);
  assert.equal(leerGrillasExt().grillas[0].filas.length, 3000);
});

test("sólo grillas visibles (isVisible(true)), no destruidas, máximo 15", () => {
  const grillas = [
    grilla({ id: "oculta", columnas: COLS, recs: recsSinteticos(1), visible: false }),
    grilla({ id: "destruida", columnas: COLS, recs: recsSinteticos(1), extra: { destroyed: true } }),
  ];
  for (let i = 0; i < 20; i++) grillas.push(grilla({ id: `v${i}`, columnas: COLS, recs: recsSinteticos(1) }));
  instalar(grillas);
  const r = leerGrillasExt(10);
  assert.equal(r.grillas.length, 15);
  assert.equal(r.grillas[0].id, "v0");
  assert.ok(!r.grillas.some((g) => g.id === "oculta" || g.id === "destruida"));
});

test("columnas desde headerCt.getGridColumns o desde columns", () => {
  instalar([
    grilla({ id: "h", columnas: [{ dataIndex: "caso", text: "Caso" }], recs: [registro({ caso: "1" })], modo: "headerCt" }),
    grilla({ id: "c", columnas: [{ dataIndex: "caso", text: "Caso" }], recs: [registro({ caso: "2" })], modo: "columns" }),
  ]);
  const r = leerGrillasExt(10);
  assert.deepEqual(r.grillas.map((g) => g.filas[0]), [{ Caso: "1" }, { Caso: "2" }]);
  assert.deepEqual(r.grillas.map((g) => g.columnas.length), [1, 1]);
});

test("record sin get() usa rec.data", () => {
  instalar([grilla({ id: "g", columnas: [{ dataIndex: "caso", text: "Caso" }], recs: [{ data: { caso: "9", otra: 1 } }] })]);
  assert.deepEqual(leerGrillasExt(10).grillas[0].filas[0], { Caso: "9", otra: 1 });
});

test("título desde getTitle o desde el panel contenedor", () => {
  instalar([
    grilla({ id: "a", extra: { getTitle: () => "<b>Bandeja</b>" } }),
    grilla({ id: "b", extra: { up: (sel) => (sel === "panel[title]" ? { title: "Pestaña Reclamos" } : null) } }),
  ]);
  const r = leerGrillasExt(10);
  assert.equal(r.grillas[0].titulo, "Bandeja");
  assert.equal(r.grillas[1].titulo, "Pestaña Reclamos");
});

test("una grilla que revienta no tumba a las demás", () => {
  const mala = grilla({ id: "mala", columnas: COLS });
  mala.store = { getRange() { throw new Error("store roto"); } };
  instalar([mala, grilla({ id: "buena", columnas: COLS, recs: recsSinteticos(1) })]);
  const r = leerGrillasExt(10);
  assert.equal(r.grillas.length, 2);
  assert.match(r.grillas[0].error, /store roto/);
  assert.deepEqual(r.grillas[0].filas, []);
  assert.equal(r.grillas[1].filas.length, 1);
});

test("excepción general -> {error}", () => {
  instalar([], { consulta: () => { throw new Error("ExtJS en mal estado"); } });
  const r = leerGrillasExt(10);
  assert.match(r.error, /ExtJS en mal estado/);
});

test("sin grillas visibles -> grillas vacías (no error)", () => {
  instalar([]);
  assert.deepEqual(leerGrillasExt(10).grillas, []);
});

test("es AUTOCONTENIDA: reconstruida desde su texto (como hace Chrome) da lo mismo", () => {
  // Chrome serializa `func` con toString() y la evalúa en la página: si
  // dependiera de algo del módulo, acá saltaría un ReferenceError.
  const reconstruida = new Function(`return (${leerGrillasExt.toString()})`)();
  instalar([grilla({ id: "g1", titulo: "Bandeja", columnas: COLS, recs: recsSinteticos(2), total: 2 })]);
  const original = leerGrillasExt(3000);
  const copia = reconstruida(3000);
  assert.equal(copia.error, undefined);
  assert.deepEqual(copia, original);
  assert.equal(copia.grillas[0].filas.length, 2);
});

test("el resultado es JSON plano (Chrome lo clona para devolverlo)", () => {
  instalar([grilla({ id: "g1", columnas: COLS, recs: recsSinteticos(2) })]);
  const r = leerGrillasExt(10);
  assert.deepEqual(JSON.parse(JSON.stringify(r)), r);
});
