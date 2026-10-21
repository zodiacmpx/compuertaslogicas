// Tests de extension/plantillas.js (funciones puras).
// Correr desde la raíz del proyecto: node --test "tests/js/*.test.mjs"
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import "../../extension/plantillas.js";

const P = globalThis.Plantillas;
const RUTA = fileURLToPath(new URL("../../extension/plantillas.js", import.meta.url));
// Fecha local fija: los tests no dependen de la zona horaria de la máquina.
const AHORA = new Date(2026, 9, 21, 14, 32, 5);   // 2026-10-21 14:32:05 local
const BASE = "https://centrality.ejemplo.invalid:8443";
const RUTA_REP = "/backend/saesa/reports.php";

test("expone las funciones del contrato", () => {
  for (const f of ["fmtFecha", "expandir", "urlReporte", "ventana", "clasificarRespuesta", "origenDe", "patronHost"]) {
    assert.equal(typeof P[f], "function", f);
  }
});

test("se puede inyectar dos veces en el mismo mundo (content script reinyectado)", () => {
  // un `const` global repetido revienta con "Identifier has already been declared"
  const src = readFileSync(RUTA, "utf8");
  const ctx = vm.createContext({ URL, btoa, atob });
  vm.runInContext(src, ctx);
  vm.runInContext(src, ctx);
  assert.equal(typeof vm.runInContext("globalThis.Plantillas.fmtFecha", ctx), "function");
});

test("fmtFecha: hora local con ceros", () => {
  assert.equal(P.fmtFecha(AHORA), "2026-10-21 14:32:05");
  assert.equal(P.fmtFecha(new Date(2026, 0, 2, 3, 4, 5)), "2026-01-02 03:04:05");
  assert.equal(P.fmtFecha(new Date("no-fecha")), "");
  assert.equal(P.fmtFecha(null), "");
});

test("expandir: variables conocidas", () => {
  const desde = new Date(2026, 9, 18, 14, 32, 5);
  const r = P.expandir("a={desde}&b={hasta}&c={ou}&d={hoy}&e={ahora}",
                       { desde, hasta: AHORA, ou: 195, ahora: AHORA });
  assert.equal(r, "a=2026-10-18 14:32:05&b=2026-10-21 14:32:05&c=195&d=2026-10-21&e=2026-10-21 14:32:05");
});

test("expandir: desconocidas y conocidas sin valor quedan intactas", () => {
  assert.equal(P.expandir("x={foo}&y={desde}", { ahora: AHORA }), "x={foo}&y={desde}");
  assert.equal(P.expandir("{ou}", { ou: "" }), "");
  assert.equal(P.expandir("sin variables", {}), "sin variables");
  assert.equal(P.expandir(null, {}), "");
});

test("expandir: desde/hasta como texto se respetan", () => {
  assert.equal(P.expandir("{desde}", { desde: "2026-10-01 00:00:00" }), "2026-10-01 00:00:00");
});

test("urlReporte: forma completa y codificación", () => {
  const v = P.ventana(72, AHORA);
  const url = P.urlReporte(`${BASE}/`, RUTA_REP, "512", "param_1={desde}&param_2={hasta}&param_3={ou}",
                           { ...v, ou: "195", ahora: AHORA });
  assert.equal(url,
    `${BASE}${RUTA_REP}?id=512&process=1` +
    "&param_1=2026-10-18%2014%3A32%3A05&param_2=2026-10-21%2014%3A32%3A05&param_3=195&format=plaincsv");
  const u = new URL(url);
  assert.equal(u.searchParams.get("param_1"), "2026-10-18 14:32:05");
  assert.equal(u.searchParams.get("format"), "plaincsv");
});

test("urlReporte: sin parámetros no deja && sueltos", () => {
  assert.equal(P.urlReporte(BASE, RUTA_REP, 432, "", {}),
               `${BASE}${RUTA_REP}?id=432&process=1&format=plaincsv`);
  assert.equal(P.urlReporte(BASE, "backend/saesa/reports.php", 432, "  &  ", {}),
               `${BASE}${RUTA_REP}?id=432&process=1&format=plaincsv`);
});

test("urlReporte: ignora id/process/format pegados en la plantilla", () => {
  const url = P.urlReporte(BASE, RUTA_REP, "432", "?id=999&process=0&param_1={ou}&format=html", { ou: "7" });
  const u = new URL(url);
  assert.deepEqual(u.searchParams.getAll("id"), ["432"]);
  assert.deepEqual(u.searchParams.getAll("process"), ["1"]);
  assert.deepEqual(u.searchParams.getAll("format"), ["plaincsv"]);
  assert.equal(u.searchParams.get("param_1"), "7");
});

test("urlReporte: no doble-codifica una plantilla ya codificada", () => {
  const url = P.urlReporte(BASE, RUTA_REP, "1", "param_1=2026-10-01%2000:00:00&param_2=a+b", {});
  const u = new URL(url);
  assert.equal(u.searchParams.get("param_1"), "2026-10-01 00:00:00");
  assert.equal(u.searchParams.get("param_2"), "a b");
});

test("urlReporte: variable sin valor queda visible (codificada) para detectarla", () => {
  const url = P.urlReporte(BASE, RUTA_REP, "1", "param_1={desde}", { ahora: AHORA });
  assert.match(url, /param_1=%7Bdesde%7D/);
});

test("ventana: horas hacia atrás desde ahora", () => {
  const v = P.ventana(72, AHORA);
  assert.equal(P.fmtFecha(v.desde), "2026-10-18 14:32:05");
  assert.equal(P.fmtFecha(v.hasta), "2026-10-21 14:32:05");
  assert.ok(v.hasta instanceof Date && v.desde instanceof Date);
  assert.notEqual(v.hasta, AHORA, "no debe devolver el mismo objeto que recibió");
  assert.deepEqual(P.ventana(0, AHORA), { desde: null, hasta: null });
  assert.deepEqual(P.ventana("x", AHORA), { desde: null, hasta: null });
  assert.equal(P.fmtFecha(P.ventana(1.5, AHORA).desde), "2026-10-21 13:02:05");
});

const CSV = "Caso;Codigo de alimentador;Clientes Interrumpidos\n40808515;A260;120\n";

test("clasificarRespuesta: CSV válido -> null", () => {
  assert.equal(P.clasificarRespuesta(200, "text/csv; charset=utf-8", CSV), null);
  assert.equal(P.clasificarRespuesta(200, "", CSV), null);
  // un CSV con encabezado y cero filas es válido (no hay fallas)
  assert.equal(P.clasificarRespuesta(200, "text/plain", "Caso;Codigo de alimentador;Clientes Interrumpidos\n"), null);
});

test("clasificarRespuesta: login -> SESION_CADUCADA", () => {
  const html = "<!DOCTYPE html><html><head><title>Centrality - Login</title></head><body>...</body></html>";
  assert.match(P.clasificarRespuesta(200, "text/html; charset=UTF-8", html), /^SESION_CADUCADA:/);
  const form = "<html><body><form action=\"login.php\" method=\"post\"></form></body></html>";
  assert.match(P.clasificarRespuesta(200, "text/html", form), /^SESION_CADUCADA:/);
  // redirección seguida por fetch: basta la URL final
  assert.match(P.clasificarRespuesta(200, "text/html", "<html></html>", `${BASE}/login.php?r=1`), /^SESION_CADUCADA:/);
  assert.match(P.clasificarRespuesta(401, "", "x".repeat(60)), /^SESION_CADUCADA:/);
  // login servido con content-type equivocado igual se reconoce
  assert.match(P.clasificarRespuesta(200, "text/plain", html), /^SESION_CADUCADA:/);
});

test("clasificarRespuesta: 5xx / gateway -> REPORTE_PESADO (no sesión)", () => {
  assert.match(P.clasificarRespuesta(504, "text/html", "<html><title>504 Gateway Time-out</title></html>"),
               /^REPORTE_PESADO:/);
  assert.match(P.clasificarRespuesta(502, "", ""), /^REPORTE_PESADO:.*502/);
  assert.match(P.clasificarRespuesta(200, "text/html", "<html><h1>502 Bad Gateway</h1></html>"), /^REPORTE_PESADO:/);
  // un 504 que además menciona login sigue siendo "pesado", no sesión
  assert.match(P.clasificarRespuesta(504, "text/html", "<html>504 Gateway login.php</html>"), /^REPORTE_PESADO:/);
});

test("clasificarRespuesta: otro HTML -> RESPUESTA_HTML con texto corto y sin etiquetas", () => {
  const html = "<html><head><script>var x = 1;</script></head><body><h1>Reporte inexistente</h1>" +
               "<p>" + "relleno ".repeat(50) + "</p></body></html>";
  const r = P.clasificarRespuesta(200, "text/html", html);
  assert.match(r, /^RESPUESTA_HTML:/);
  assert.match(r, /Reporte inexistente/);
  assert.doesNotMatch(r, /<|var x/);
  assert.ok(r.length < 220);
});

test("clasificarRespuesta: 4xx no HTML -> HTTP_ERROR", () => {
  assert.match(P.clasificarRespuesta(404, "text/plain", "not found"), /^HTTP_ERROR:.*404/);
});

test("clasificarRespuesta: casi vacía -> VACIA", () => {
  assert.match(P.clasificarRespuesta(200, "text/csv", ""), /^VACIA:/);
  assert.match(P.clasificarRespuesta(200, "text/csv", "Caso\n"), /^VACIA:/);
  assert.match(P.clasificarRespuesta(200, "text/csv", "   \n\n   "), /^VACIA:/);
});

test("origenDe / patronHost", () => {
  assert.equal(P.origenDe("https://centrality.ejemplo.invalid:8443/backend/saesa/login.php?x=1"),
               "https://centrality.ejemplo.invalid:8443");
  assert.equal(P.origenDe("https://centrality.ejemplo.invalid:443/"), "https://centrality.ejemplo.invalid");
  assert.equal(P.origenDe("http://10.0.0.5:8080"), "http://10.0.0.5:8080");
  assert.equal(P.origenDe("ftp://x.invalid"), "");
  assert.equal(P.origenDe("no es url"), "");
  assert.equal(P.origenDe(""), "");
  assert.equal(P.patronHost(`${BASE}/algo`), `${BASE}/*`);
  assert.equal(P.patronHost(""), "");
});

test("urlBotValida: sólo loopback http", () => {
  assert.equal(P.urlBotValida("http://127.0.0.1:8771/"), "http://127.0.0.1:8771");
  assert.equal(P.urlBotValida("http://localhost:8771"), "http://localhost:8771");
  assert.equal(P.urlBotValida("https://127.0.0.1:8771"), "");
  assert.equal(P.urlBotValida("http://192.168.1.10:8771"), "");
  assert.equal(P.urlBotValida("http://127.0.0.1.ejemplo.invalid:8771"), "");
  assert.equal(P.urlBotValida("http://usuario:clave@127.0.0.1:8771"), "");
});

test("base64: ida y vuelta preserva bytes no UTF-8 (cp1252 sueltos)", () => {
  // "Dirección" con la ó en cp1252 (0xF3): decodificar a texto la perdería
  const bytes = new Uint8Array([0x44, 0x69, 0x72, 0x65, 0x63, 0x63, 0x69, 0xF3, 0x6E, 0x00, 0xFF]);
  const vuelta = P.base64ABytes(P.bytesABase64(bytes));
  assert.deepEqual([...vuelta], [...bytes]);
  // y con más de un trozo de 32 KiB
  const grande = new Uint8Array(100000).map((_, i) => i % 256);
  assert.deepEqual(P.base64ABytes(P.bytesABase64(grande.buffer)), grande);
});

test("extraerReportes: enlaces, onclick, menús ExtJS, select y genérico", () => {
  const html = `
    <html><head><title>Reportes</title></head><body>
    <a href="reports.php?id=432">Tareas abiertas</a>
    <a href="#" onclick="abrir('reports.php?id=512&amp;x=1')"><span>Informe de casos en periodos</span></a>
    <script>
      var menu = [{"id": 397, "text": "Informe de tareas"}, {text: 'Bandeja de reclamos', id: 601}];
      var config = {"items":[{"xtype":"acstriggercombofield","store":[{"id":195,"name":"Grupo SAESA"}]}]};
    </script>
    <select name="id"><option value="700">Reporte por select</option></select>
    <div data-url="reports.php?id=815"><b>Reporte genérico</b></div>
    <a href="reports.php?id=432">Tareas abiertas (duplicado)</a>
    </body></html>`;
  const r = P.extraerReportes(html);
  const porId = Object.fromEntries(r.map((x) => [x.id, x.nombre]));
  assert.equal(porId["432"], "Tareas abiertas");
  assert.equal(porId["512"], "Informe de casos en periodos");
  assert.equal(porId["397"], "Informe de tareas");
  assert.equal(porId["601"], "Bandeja de reclamos");
  assert.equal(porId["700"], "Reporte por select");
  assert.equal(porId["815"], "Reporte genérico");
  // la OU del bloque `var config` no se cuela como reporte
  assert.equal(porId["195"], undefined);
  assert.deepEqual(r.map((x) => x.id), ["397", "432", "512", "601", "700", "815"]);
});

test("extraerReportes: HTML sin reportes -> []", () => {
  assert.deepEqual(P.extraerReportes("<html><body>nada</body></html>"), []);
  assert.deepEqual(P.extraerReportes(""), []);
});

test("extraerOUs: store de acstriggercombofield (arrays u objetos, anidado)", () => {
  const html = `<script>
var config = {
  "title": "Tareas abiertas",
  "items": [
    {"xtype": "panel", "items": [
      {"xtype": "acstriggercombofield", "fieldLabel": "Unidad Organizativa", "name": "param_1",
       "store": [[195, "Grupo SAESA"], [210, "CCD Sur"], [211, "Zona Osorno {x}"]]}
    ]}
  ]
};
</script>`;
  assert.deepEqual(P.extraerOUs(html), [
    { id: "195", nombre: "Grupo SAESA" },
    { id: "210", nombre: "CCD Sur" },
    { id: "211", nombre: "Zona Osorno {x}" },
  ]);
  const html2 = `var config = {"items":[{"xtype":"acstriggercombofield","store":{"data":[{"value":"5","text":"Zona Uno"}]}}]};\n`;
  assert.deepEqual(P.extraerOUs(html2), [{ id: "5", nombre: "Zona Uno" }]);
});

test("extraerOUs: config no-JSON estricto se rescata igual", () => {
  const html = `var config = {items: [{xtype: 'acstriggercombofield', store: [[1, 'Uno'], [2, 'Dos']]}]};\n`;
  assert.deepEqual(P.extraerOUs(html), [{ id: "1", nombre: "Uno" }, { id: "2", nombre: "Dos" }]);
});

test("extraerOUs: sin config -> null", () => {
  assert.equal(P.extraerOUs("<html></html>"), null);
  assert.equal(P.extraerOUs('var config = {"items":[]};\n'), null);
});
