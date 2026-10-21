/**
 * Lectura de las grillas ExtJS visibles en la pestaña de Centrality.
 *
 * OJO: `leerGrillasExt` tiene que ser AUTOCONTENIDA. El service worker la pasa
 * como `func` a `chrome.scripting.executeScript({world: "MAIN"})`, y Chrome NO
 * la ejecuta como función: la serializa a texto (`toString()`) y la evalúa en
 * la página. Cualquier referencia a algo de afuera (un helper de este módulo,
 * una constante, un import) llega como `ReferenceError` en la página. Por eso
 * los helpers viven DENTRO del cuerpo y el test la reconstruye desde su texto.
 *
 * Por qué `world: "MAIN"`: `window.Ext` existe sólo en el mundo de la página;
 * el mundo aislado de un content script ve el DOM pero no los objetos JS. Y
 * por qué leer el store y no el DOM (como hacía `grillaVisible()` del motor de
 * despacho): ExtJS renderiza sólo las filas a la vista (buffered rendering) y
 * las columnas de íconos no tienen texto; el store tiene los valores crudos de
 * TODAS las filas cargadas.
 *
 * Sólo LEE: no hace clic, no filtra, no pagina, no modifica nada.
 */

/**
 * @param {number} maxFilas máximo de filas por grilla (default 3000)
 * @returns {{url, titulo, grillas: Array<{id, titulo, columnas, filas, total, cargadas}>} | {error: string}}
 */
export function leerGrillasExt(maxFilas) {
  try {
    const w = typeof window !== "undefined" ? window : globalThis;
    const Ext = w && w.Ext;
    if (!Ext || !Ext.ComponentQuery || typeof Ext.ComponentQuery.query !== "function") {
      return { error: "La página no expone ExtJS" };
    }
    const limite = Number(maxFilas) > 0 ? Math.floor(Number(maxFilas)) : 3000;
    const MAX_GRILLAS = 15;

    const p2 = (n) => String(n).padStart(2, "0");
    const fmtFecha = (d) =>
      `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ` +
      `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
    const sinHtml = (s) => String(s == null ? "" : s).replace(/<[^>]*>/g, "").trim();

    // Sólo primitivos: un objeto del store puede ser un record asociado, un
    // componente o algo circular; además sólo lo plano tiene sentido para el
    // contrato del bot. `undefined` = omitir.
    const primitivo = (v) => {
      if (v === null) return null;
      const t = typeof v;
      if (t === "string" || t === "boolean") return v;
      if (t === "number") return Number.isFinite(v) ? v : undefined;
      if (v instanceof Date) return Number.isNaN(v.getTime()) ? undefined : fmtFecha(v);
      return undefined;
    };

    const visible = (g) => {
      if (!g || g.destroyed || g.isDestroyed) return false;
      if (typeof g.isVisible === "function") {
        try { return !!g.isVisible(true); } catch (_) { return false; }
      }
      return true;
    };

    const columnasDe = (g) => {
      let cols = null;
      try { cols = typeof g.getColumns === "function" ? g.getColumns() : null; } catch (_) { cols = null; }
      if (!cols || !cols.length) {
        try {
          const h = g.headerCt;
          cols = h && typeof h.getGridColumns === "function" ? h.getGridColumns() : null;
        } catch (_) { cols = null; }
      }
      if (!cols || !cols.length) cols = Array.isArray(g.columns) ? g.columns : [];
      return cols;
    };

    const tituloDe = (g) => {
      let t = "";
      try { t = sinHtml(typeof g.getTitle === "function" ? g.getTitle() : ""); } catch (_) { t = ""; }
      if (!t) t = sinHtml(g.title);
      if (!t && typeof g.up === "function") {
        // la grilla suele vivir en una pestaña o panel con título propio
        try {
          const p = g.up("panel[title]");
          if (p) t = sinHtml(p.title);
        } catch (_) { /* sin título */ }
      }
      return t;
    };

    const encontradas = Ext.ComponentQuery.query("gridpanel") || [];
    const grillas = [];
    for (const g of encontradas) {
      if (grillas.length >= MAX_GRILLAS) break;
      if (!visible(g)) continue;
      const id = String((typeof g.getId === "function" ? g.getId() : g.id) || "");
      const titulo = tituloDe(g);
      try {
        const columnas = [];
        const claves = [];          // [{dataIndex, clave}]
        const usadas = new Set();
        for (const c of columnasDe(g)) {
          if (!c) continue;
          const di = c.dataIndex == null ? "" : String(c.dataIndex);
          if (!di) continue;      // acciones, numeradores, checkboxes: sin dato
          const texto = sinHtml(c.text || c.header || "");
          const col = { dataIndex: di, texto };
          // las columnas de íconos (📞 ⚠️ ⚡ 👥) sólo dicen qué son en el tooltip
          const tip = sinHtml(typeof c.tooltip === "string" ? c.tooltip : "");
          if (tip) col.tooltip = tip;
          columnas.push(col);
          // dos columnas con el mismo título no se pueden pisar en la fila
          let clave = texto || di;
          if (usadas.has(clave)) clave = usadas.has(di) ? `${clave} (${di})` : di;
          usadas.add(clave);
          claves.push({ di, clave });
        }
        const cubiertos = new Set(claves.map((x) => x.di));

        const store = typeof g.getStore === "function" ? g.getStore() : g.store;
        const recs = store && typeof store.getRange === "function" ? (store.getRange() || []) : [];
        let total = null;
        try {
          if (store && typeof store.getTotalCount === "function") total = store.getTotalCount();
        } catch (_) { total = null; }
        if (typeof total !== "number" || !Number.isFinite(total)) {
          total = store && typeof store.getCount === "function" ? store.getCount() : recs.length;
        }

        const filas = [];
        for (let i = 0; i < recs.length && filas.length < limite; i++) {
          const rec = recs[i];
          if (!rec) continue;
          const data = rec.data && typeof rec.data === "object" ? rec.data : {};
          const fila = {};
          for (const { di, clave } of claves) {
            const v = primitivo(typeof rec.get === "function" ? rec.get(di) : data[di]);
            if (v !== undefined) fila[clave] = v;
          }
          // campos del record que ninguna columna muestra (p. ej. la cantidad
          // de clientes puede venir en el store sin columna visible)
          for (const k of Object.keys(data)) {
            if (cubiertos.has(k) || Object.prototype.hasOwnProperty.call(fila, k)) continue;
            const v = primitivo(data[k]);
            if (v !== undefined) fila[k] = v;
          }
          filas.push(fila);
        }
        grillas.push({ id, titulo, columnas, filas, total, cargadas: recs.length });
      } catch (e) {
        // una grilla rara no tumba a las demás
        grillas.push({ id, titulo, columnas: [], filas: [], total: 0, cargadas: 0,
                       error: String((e && e.message) || e) });
      }
    }
    return {
      url: typeof location !== "undefined" && location ? String(location.href || "") : "",
      titulo: typeof document !== "undefined" && document ? String(document.title || "") : "",
      grillas,
    };
  } catch (e) {
    return { error: `No se pudo leer la grilla: ${String((e && e.message) || e)}` };
  }
}
