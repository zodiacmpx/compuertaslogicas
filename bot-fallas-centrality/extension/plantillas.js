/**
 * Funciones PURAS compartidas por el service worker, el content script y las
 * páginas de la extensión (popup / opciones).
 *
 * Por qué es un script clásico que cuelga de `globalThis` y no un módulo ES
 * -------------------------------------------------------------------------
 * El mismo archivo se usa en tres contextos que cargan código distinto:
 *   - background.js (module)  -> `import "./plantillas.js"`
 *   - content.js (clásico)    -> se inyecta ANTES con executeScript({files})
 *   - options/popup (<script>)
 * Un content script no puede usar `import`, así que lo único que sirve en los
 * tres es asignar `globalThis.Plantillas`.
 *
 * Y va envuelto en una IIFE sin `const`/`let` al nivel superior porque el
 * content script se REINYECTA (al recargar la extensión, o en cada ping
 * fallido): un `const` global declarado dos veces en el mismo mundo aislado
 * revienta con "Identifier has already been declared" y la pestaña queda muda.
 */
(function () {
  "use strict";

  function p2(n) {
    return String(n).padStart(2, "0");
  }

  function aFecha(v) {
    if (v instanceof Date) return v;
    if (v == null || v === "") return null;
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  /** "YYYY-MM-DD HH:MM:SS" en hora LOCAL: es lo que espera Centrality en
   *  param_1/param_2 y lo que el bot parsea como datetime ingenuo. */
  function fmtFecha(d) {
    const x = aFecha(d);
    if (!x || Number.isNaN(x.getTime())) return "";
    return `${x.getFullYear()}-${p2(x.getMonth() + 1)}-${p2(x.getDate())} ` +
           `${p2(x.getHours())}:${p2(x.getMinutes())}:${p2(x.getSeconds())}`;
  }

  const VARIABLES = ["desde", "hasta", "ou", "hoy", "ahora"];

  function valorVariable(nombre, vars) {
    const v = vars || {};
    // `ahora` se recibe por parámetro para que sea testeable; si no viene,
    // se usa el reloj (es la única impureza y está acotada a {hoy}/{ahora}).
    const ahora = aFecha(v.ahora) || new Date();
    if (nombre === "hoy") return fmtFecha(ahora).slice(0, 10);
    if (nombre === "ahora") return fmtFecha(ahora);
    const x = v[nombre];
    if (x == null) return null;
    if (x instanceof Date) return fmtFecha(x);
    return String(x);
  }

  /**
   * Reemplaza {desde} {hasta} {ou} {hoy} {ahora}.
   * Lo desconocido ({param}, {foo}) y lo conocido SIN valor se deja intacto:
   * así el llamador puede detectar una plantilla que no se resolvió en vez de
   * mandarle a Centrality un parámetro vacío que devuelve "todo".
   */
  function expandir(tpl, vars) {
    return String(tpl == null ? "" : tpl).replace(/\{(\w+)\}/g, (m, k) => {
      if (!VARIABLES.includes(k)) return m;
      const val = valorVariable(k, vars);
      return val == null ? m : val;
    });
  }

  function decodificarSeguro(s) {
    try { return decodeURIComponent(String(s).replace(/\+/g, " ")); } catch (_) { return String(s); }
  }

  /**
   * `${base}${ruta}?id=${id}&process=1&<params>&format=plaincsv`.
   *
   * Los parámetros se expanden y se codifican uno por uno. Se decodifica antes
   * por si el usuario pegó una URL ya codificada (%20): sin eso saldría %2520.
   * `id`, `process` y `format` de la plantilla se ignoran porque los pone esta
   * función: duplicados, PHP se queda con el último y el `format=plaincsv`
   * podría perderse (y llegaría HTML).
   */
  function urlReporte(base, ruta, id, paramsTpl, vars) {
    const b = String(base || "").trim().replace(/\/+$/, "");
    let r = String(ruta || "").trim();
    if (r && !r.startsWith("/")) r = "/" + r;
    const partes = [`id=${encodeURIComponent(String(id == null ? "" : id).trim())}`, "process=1"];
    const crudo = String(paramsTpl || "").trim().replace(/^[?&]+/, "");
    for (const pieza of crudo.split("&")) {
      const p = pieza.trim();
      if (!p) continue;
      const i = p.indexOf("=");
      const k = decodificarSeguro(i >= 0 ? p.slice(0, i) : p).trim();
      if (!k || ["id", "process", "format"].includes(k.toLowerCase())) continue;
      if (i < 0) { partes.push(encodeURIComponent(k)); continue; }
      const v = expandir(decodificarSeguro(p.slice(i + 1)), vars);
      partes.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
    }
    partes.push("format=plaincsv");
    return `${b}${r}?${partes.join("&")}`;
  }

  /** Ventana [ahora − horas, ahora]. horas ≤ 0 o inválidas -> sin ventana
   *  ({desde: null, hasta: null}): el reporte no es por periodo. */
  function ventana(horas, ahora) {
    const h = Number(horas);
    const a = aFecha(ahora) || new Date();
    if (!Number.isFinite(h) || h <= 0) return { desde: null, hasta: null };
    const hasta = new Date(a.getTime());
    return { desde: new Date(hasta.getTime() - h * 3600000), hasta };
  }

  function textoPlano(html) {
    return decodificarEntidades(String(html || "")
      .replace(/<script\b[\s\S]*?<\/script>/gi, " ")
      .replace(/<style\b[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]*>/g, " "))
      .replace(/\s+/g, " ")
      .trim();
  }

  function decodificarEntidades(s) {
    const nombres = { amp: "&", lt: "<", gt: ">", quot: "\"", apos: "'", nbsp: " " };
    return String(s).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
      const el = e.toLowerCase();
      if (el[0] === "#") {
        const n = el[1] === "x" ? parseInt(el.slice(2), 16) : parseInt(el.slice(1), 10);
        return Number.isFinite(n) && n > 0 && n < 0x110000 ? String.fromCodePoint(n) : m;
      }
      return Object.prototype.hasOwnProperty.call(nombres, el) ? nombres[el] : m;
    });
  }

  const RE_LOGIN = /login\.php|Centrality\s*-\s*Login/i;
  const RE_GATEWAY = /50[0-9]\s+(Gateway|Server|Bad)/i;
  const RE_HTML = /^\s*(?:﻿)?\s*<(?:!doctype|html|head|body|script|meta|title)\b/i;

  /**
   * ¿La respuesta de Centrality es un CSV utilizable? null = sí; si no, un
   * texto con prefijo estable que el resto de la cadena reconoce.
   *
   * Centrality devuelve HTML en casos MUY distintos y hay que separarlos, o el
   * operador termina reiniciando sesión por nada (hallazgo del motor de
   * despacho):
   *   - SESION_CADUCADA:  devolvió login.php (la cookie venció). El bot busca
   *                       ESTE prefijo en el latido para avisar por Telegram.
   *   - REPORTE_PESADO:   5xx / "504 Gateway" -> el reporte tardó más de lo
   *                       que aguanta el gateway; no es problema de sesión.
   *   - RESPUESTA_HTML:   cualquier otro HTML (ID o parámetros malos).
   *   - HTTP_ERROR:       4xx que no es HTML (ruta mala, sin permiso).
   *   - VACIA:            < 40 bytes: ni siquiera trae un encabezado.
   *
   * `urlFinal` (opcional) es `response.url`: si fetch siguió una redirección
   * a login.php, eso basta para saber que la sesión caducó.
   */
  function clasificarRespuesta(status, contentType, cabezaTexto, urlFinal) {
    const st = Number(status) || 0;
    const ct = String(contentType || "").toLowerCase();
    const cabeza = String(cabezaTexto == null ? "" : cabezaTexto);
    const esHtml = ct.includes("text/html") || RE_HTML.test(cabeza);

    if (st >= 500 || (esHtml && RE_GATEWAY.test(cabeza))) {
      return `REPORTE_PESADO: Centrality respondió ${st >= 500 ? st : "5xx"} — el reporte ` +
             "tardó demasiado. Reduce la ventana de horas o acota la unidad organizativa.";
    }
    if (st === 401 || RE_LOGIN.test(String(urlFinal || "")) || (esHtml && RE_LOGIN.test(cabeza))) {
      return "SESION_CADUCADA: Centrality devolvió el login en vez del CSV — " +
             "inicia sesión en la pestaña de Centrality";
    }
    if (esHtml) {
      const txt = textoPlano(cabeza).slice(0, 100) || "(sin texto)";
      return `RESPUESTA_HTML: Centrality no devolvió CSV (¿ID o parámetros del reporte?). Texto: ${txt}`;
    }
    if (st >= 400) return `HTTP_ERROR: Centrality respondió HTTP ${st}`;
    const n = cabeza.trim().length;
    if (n < 40) return `VACIA: respuesta vacía (${n} caracteres)`;
    return null;
  }

  /** "https://host:puerto" (el puerto sólo si no es el por defecto, igual que
   *  URL.origin). "" si no es http(s) o no es una URL. */
  function origenDe(url) {
    try {
      const u = new URL(String(url || "").trim());
      if (u.protocol !== "https:" && u.protocol !== "http:") return "";
      return u.origin;
    } catch (_) {
      return "";
    }
  }

  /** Patrón de coincidencia de Chrome para permisos y tabs.query. */
  function patronHost(base) {
    const o = origenDe(base);
    return o ? `${o}/*` : "";
  }

  /**
   * URL del bot normalizada, o "" si no es local. La extensión le manda el
   * CSV crudo (con NIS y direcciones) a esta URL: sólo puede ser loopback.
   * Además el bot responde 421 a cualquier Host que no sea 127.0.0.1/localhost.
   */
  function urlBotValida(url) {
    try {
      const u = new URL(String(url || "").trim());
      if (u.protocol !== "http:" || u.username || u.password) return "";
      if (!["127.0.0.1", "localhost", "[::1]"].includes(u.hostname)) return "";
      return u.origin;
    } catch (_) {
      return "";
    }
  }

  /** Bytes -> base64. El respaldo del worker necesita los bytes EXACTOS: el
   *  CSV de Centrality trae UTF-8 con bytes cp1252 sueltos, y decodificarlo a
   *  texto en la pestaña los convertiría en U+FFFD ("Direcci�n") antes de que
   *  el bot pueda recuperarlos. Los mensajes de extensión sólo llevan JSON,
   *  así que base64 es la forma de pasar bytes sin perderlos. */
  function bytesABase64(datos) {
    const u8 = datos instanceof Uint8Array ? datos : new Uint8Array(datos);
    let s = "";
    const TROZO = 0x8000;   // fromCharCode.apply revienta la pila con arrays enormes
    for (let i = 0; i < u8.length; i += TROZO) {
      s += String.fromCharCode.apply(null, u8.subarray(i, i + TROZO));
    }
    return btoa(s);
  }

  function base64ABytes(b64) {
    const bin = atob(String(b64 || ""));
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return u8;
  }

  // ---------------------------------------------------------------------
  // Descubrimiento de reportes y OUs (best-effort sobre HTML de Centrality)
  // ---------------------------------------------------------------------

  /** Extrae el objeto literal de `var config = {...}` contando llaves (fuera
   *  de strings). La regex perezosa del motor de despacho corta en el primer
   *  "}\n", que en un JSON con saltos de línea queda a la mitad. */
  function bloqueConfig(texto) {
    const s = String(texto || "");
    const m = /var\s+config\s*=\s*\{/.exec(s);
    if (!m) return null;
    const ini = s.indexOf("{", m.index);
    let prof = 0;
    let enStr = null;
    let esc = false;
    for (let j = ini; j < s.length; j++) {
      const ch = s[j];
      if (enStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === enStr) enStr = null;
        continue;
      }
      if (ch === "\"" || ch === "'") { enStr = ch; continue; }
      if (ch === "{") prof++;
      else if (ch === "}") {
        prof--;
        if (prof === 0) return { texto: s.slice(ini, j + 1), desde: m.index, hasta: j + 1 };
      }
    }
    return null;
  }

  function arregloBalanceado(s, ini) {
    let prof = 0;
    let enStr = null;
    let esc = false;
    for (let j = ini; j < s.length; j++) {
      const ch = s[j];
      if (enStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === enStr) enStr = null;
        continue;
      }
      if (ch === "\"" || ch === "'") { enStr = ch; continue; }
      if (ch === "[") prof++;
      else if (ch === "]") {
        prof--;
        if (prof === 0) return s.slice(ini, j + 1);
      }
    }
    return null;
  }

  function parseLaxo(txt) {
    try { return JSON.parse(txt); } catch (_) { /* sigue */ }
    try { return JSON.parse(txt.replace(/'/g, "\"")); } catch (_) { return null; }
  }

  function normalizarOU(e) {
    if (Array.isArray(e)) {
      return { id: e[0] == null ? "" : String(e[0]), nombre: e[1] == null ? "" : String(e[1]) };
    }
    if (e && typeof e === "object") {
      const id = [e.id, e.value, e.valor, e.ou, e.codigo].find((x) => x != null && x !== "");
      const nombre = [e.name, e.nombre, e.text, e.display, e.label, e.descripcion]
        .find((x) => x != null && x !== "");
      return { id: id == null ? "" : String(id), nombre: nombre == null ? "" : String(nombre) };
    }
    return { id: e == null ? "" : String(e), nombre: "" };
  }

  function buscarCombos(nodo, out) {
    if (!nodo || typeof nodo !== "object") return out;
    if (Array.isArray(nodo)) { for (const x of nodo) buscarCombos(x, out); return out; }
    if (nodo.xtype === "acstriggercombofield") {
      const st = Array.isArray(nodo.store) ? nodo.store
        : (nodo.store && Array.isArray(nodo.store.data) ? nodo.store.data : null);
      if (st) out.push({ combo: nodo, store: st });
    }
    for (const k of Object.keys(nodo)) if (k !== "store") buscarCombos(nodo[k], out);
    return out;
  }

  /**
   * Catálogo de unidades organizativas: la página de un reporte de Centrality
   * trae `var config = {...}` con un combo `acstriggercombofield` cuyo store es
   * la lista de OUs (hallazgo del motor de despacho). Devuelve [{id, nombre}]
   * o null si la página no lo trae.
   */
  function extraerOUs(html) {
    const b = bloqueConfig(html);
    if (!b) return null;
    let combos = [];
    const cfg = parseLaxo(b.texto);
    if (cfg) combos = buscarCombos(cfg, []);
    if (!combos.length && /acstriggercombofield/.test(b.texto)) {
      // config no es JSON estricto: se rescatan los arreglos `store: [...]`
      const re = /["']?store["']?\s*:\s*\[/g;
      let m;
      while ((m = re.exec(b.texto))) {
        const arr = arregloBalanceado(b.texto, m.index + m[0].length - 1);
        const v = arr && parseLaxo(arr);
        if (Array.isArray(v) && v.length) combos.push({ combo: {}, store: v });
      }
    }
    if (!combos.length) return null;
    // si hay varios combos, el de la OU es el que lo dice en su etiqueta
    const etiqueta = (c) => `${c.combo.fieldLabel || ""} ${c.combo.name || ""} ${c.combo.label || ""}`;
    const elegido = combos.find((c) => /unidad|organiz|\bou\b/i.test(etiqueta(c))) || combos[0];
    return elegido.store.map(normalizarOU).filter((o) => o.id !== "").slice(0, 2000);
  }

  function agregarReporte(mapa, id, nombre) {
    const k = String(id || "").trim();
    if (!/^\d{1,7}$/.test(k)) return;
    const n = textoPlano(nombre || "").slice(0, 120);
    const previo = mapa.get(k);
    if (previo === undefined || (!previo && n)) mapa.set(k, n);
  }

  /**
   * Pares {id, nombre} de reportes desde el HTML del listado de reports.php.
   * Best-effort: el formato del listado no está documentado, así que se
   * prueban los patrones típicos (enlaces/onclick con id=N, menús ExtJS con
   * {id, text}, un <select name="id">) y al final cualquier `?id=N` con el
   * texto que lo sigue. El bloque `var config` se excluye: ahí viven las OUs
   * con su propio {id, name} y se colarían como "reportes".
   */
  function extraerReportes(html) {
    let s = String(html || "");
    const b = bloqueConfig(s);
    if (b) s = s.slice(0, b.desde) + s.slice(b.hasta);
    const mapa = new Map();

    // 1. <a href="...?id=432..."> / onclick="...id=432..."
    const reA = /<a\b([^>]*)>([\s\S]*?)<\/a>/gi;
    let m;
    while ((m = reA.exec(s))) {
      const attrs = decodificarEntidades(m[1]);
      const idm = /(?:href|onclick)\s*=\s*(?:"[^"]*?|'[^']*?)[?&'"(,\s]id=(\d+)/i.exec(attrs);
      if (!idm) continue;
      const titulo = /\btitle\s*=\s*["']([^"']*)["']/i.exec(attrs);
      agregarReporte(mapa, idm[1], textoPlano(m[2]) || (titulo ? titulo[1] : ""));
    }

    // 2. <select name="id"><option value="432">Tareas abiertas</option>
    const reSel = /<select\b[^>]*\bname\s*=\s*["']?id["']?[^>]*>([\s\S]*?)<\/select>/gi;
    while ((m = reSel.exec(s))) {
      const reOpt = /<option\b[^>]*\bvalue\s*=\s*["']?(\d+)["']?[^>]*>([\s\S]*?)<\/option>/gi;
      let o;
      while ((o = reOpt.exec(m[1]))) agregarReporte(mapa, o[1], o[2]);
    }

    // 3. JSON / config ExtJS: {id: 432, text: "Tareas abiertas"} (en ambos órdenes)
    const STR = `"((?:[^"\\\\]|\\\\.)*)"|'((?:[^'\\\\]|\\\\.)*)'`;
    const NOM = `(?:name|nombre|text|title|titulo|label|descripcion)`;
    const reJ1 = new RegExp(`["']?\\bid["']?\\s*:\\s*["']?(\\d+)["']?\\s*,\\s*["']?${NOM}["']?\\s*:\\s*(?:${STR})`, "gi");
    const reJ2 = new RegExp(`["']?\\b${NOM}["']?\\s*:\\s*(?:${STR})\\s*,\\s*["']?id["']?\\s*:\\s*["']?(\\d+)`, "gi");
    const desescapar = (x) => {
      try { return JSON.parse(`"${x}"`); } catch (_) { return String(x).replace(/\\(.)/g, "$1"); }
    };
    while ((m = reJ1.exec(s))) agregarReporte(mapa, m[1], desescapar(m[2] != null ? m[2] : m[3]));
    while ((m = reJ2.exec(s))) agregarReporte(mapa, m[3], desescapar(m[1] != null ? m[1] : m[2]));

    // 4. cualquier ?id=N / &id=N restante, con el primer texto que lo sigue
    const reG = /[?&]id=(\d+)/g;
    while ((m = reG.exec(s))) {
      if (mapa.has(m[1])) continue;
      const tramo = s.slice(m.index, m.index + 400);
      const t = />([^<>]{2,})</.exec(tramo);
      agregarReporte(mapa, m[1], t ? t[1] : "");
    }

    return [...mapa.entries()]
      .map(([id, nombre]) => ({ id, nombre }))
      .sort((a, b2) => Number(a.id) - Number(b2.id));
  }

  globalThis.Plantillas = {
    fmtFecha,
    expandir,
    urlReporte,
    ventana,
    clasificarRespuesta,
    origenDe,
    patronHost,
    // agregados (no estaban en el contrato mínimo, pero son puros y testeables)
    urlBotValida,
    bytesABase64,
    base64ABytes,
    textoPlano,
    extraerReportes,
    extraerOUs,
  };
})();
