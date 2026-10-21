/**
 * Service worker: puente entre Centrality y el bot local de fallas.
 *
 * Por qué una extensión y no el bot pidiendo los reportes
 * --------------------------------------------------------
 * Los reportes de Centrality exigen sesión autenticada. Si el bot los
 * pidiera, habría que guardarle credenciales de SAESA a un proceso. Acá no:
 * la extensión vive en el navegador donde el despachador YA inició sesión,
 * usa esa cookie y sólo empuja el CSV resultante a 127.0.0.1. Ninguna
 * credencial sale del navegador ni se persiste.
 *
 * El host de Centrality NO está en este archivo (el repo es público): lo
 * ingresa el usuario en Opciones y se concede con optional_host_permissions.
 *
 * Patrones heredados del motor de despacho (verificados en producción):
 *   - la descarga se delega a la pestaña (content.js): el worker muere en
 *     descargas largas;
 *   - todo el estado en chrome.storage (el worker muere y pierde variables);
 *   - candado anti-solapamiento con vencimiento;
 *   - asegurarAlarma() en cada despertar (la alarma se pierde sola);
 *   - reinyección del content script ("Receiving end does not exist");
 *   - latido al bot en cada ciclo (si no, un worker detenido es invisible);
 *   - distinción login / 504 / HTML en la respuesta (plantillas.js).
 */

import "./plantillas.js";
import { leerGrillasExt } from "./grilla.js";

const P = globalThis.Plantillas;

const DEF = {
  botUrl: "http://127.0.0.1:8771",
  centralityBase: "",
  rutaReportes: "/backend/saesa/reports.php",
  intervaloMin: 5,
  activo: true,
  // 240 s y no los 300 del motor de despacho: Chrome corta cualquier evento de
  // un service worker que pase de 5 min, y el worker está esperando la
  // respuesta de la pestaña mientras dura la descarga.
  timeoutMs: 240000,
  token: "",
  ou: "",
  capturarGrilla: true,
  maxFilasGrilla: 3000,
  fuentes: [
    { clave: "casos", activo: false, reporteId: "", params: "param_1={desde}&param_2={hasta}", ventanaHoras: 72 },
    { clave: "reclamos", activo: false, reporteId: "", params: "", ventanaHoras: 0 },
    { clave: "abiertas", activo: false, reporteId: "432", params: "param_1={ou}", ventanaHoras: 0 },
  ],
};

const CLAVES_FUENTE = DEF.fuentes.map((f) => f.clave);
const MARGEN_CANDADO_MS = 60000;
const NOTIF_CADA_MS = 30 * 60000;

/**
 * OJO MV3: el service worker se TERMINA tras ~30 s de inactividad y con él se
 * pierde cualquier variable. Si el candado viviera en memoria, desaparecería
 * a mitad de una corrida y la siguiente alarma solaparía dos descargas de 75 s
 * contra Centrality. Por eso el estado vive en chrome.storage.local.
 */
const ST = "estadoBot";
const EST_DEF = {
  corriendo: false, corriendoDesde: 0, corridaId: "", ciclo: 0,
  ultima: null, ultimoError: null, ultimoInicio: null, ultimoResultado: null,
};

async function leerEstado() {
  const g = await chrome.storage.local.get([ST, "historial"]);
  return { ...EST_DEF, ...(g[ST] || {}), historial: g.historial || [] };
}

async function escribirEstado(parcial) {
  const e = await leerEstado();
  const { historial, ...resto } = { ...e, ...parcial };
  await chrome.storage.local.set({ [ST]: resto });
  return resto;
}

/** Toda escritura del estado pasa por la cola de `enExclusiva`: es un
 *  leer-mezclar-escribir, y dos intercalados (el final de una corrida con la
 *  renovación del candado de otra) se pisarían campos. */
function guardarEstado(parcial) {
  return enExclusiva(() => escribirEstado(parcial));
}

function mezclarFuentes(guardadas) {
  const lista = Array.isArray(guardadas) ? guardadas : [];
  return DEF.fuentes.map((d) => ({ ...d, ...(lista.find((x) => x && x.clave === d.clave) || {}), clave: d.clave }));
}

async function cfg() {
  const g = await chrome.storage.local.get(Object.keys(DEF));
  const c = { ...DEF, ...g };
  c.fuentes = mezclarFuentes(g.fuentes);
  return c;
}

async function log(nivel, msg, extra) {
  const g = await chrome.storage.local.get("historial");
  const h = [{ t: new Date().toISOString(), nivel, msg, ...(extra || {}) },
             ...(g.historial || [])].slice(0, 60);
  await chrome.storage.local.set({ historial: h });
  console.log(`[bot-fallas] ${nivel} ${msg}`);
}

function mensajeDe(e) {
  return String((e && e.message) || e || "error desconocido");
}

function esSinReceptor(e) {
  return /Receiving end does not exist|Could not establish connection/i.test(mensajeDe(e));
}

function cabecerasBot(c, tipo) {
  const h = { "Content-Type": tipo };
  if (c.token) h["X-Bot-Token"] = String(c.token);
  return h;
}

/** POST al bot con plazo. Devuelve {ok, status, json, texto}; sólo lanza si
 *  no hubo respuesta (bot apagado, red). */
async function postBot(c, ruta, cuerpo, tipo, ms = 60000) {
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(`${c.botUrl}${ruta}`, {
      method: "POST", headers: cabecerasBot(c, tipo), body: cuerpo, signal: ctl.signal,
    });
    const texto = await r.text();
    let json = null;
    try { json = JSON.parse(texto); } catch (_) { json = null; }
    return { ok: r.ok, status: r.status, json, texto };
  } catch (e) {
    if (e && e.name === "AbortError") throw new Error(`el bot no respondió en ${Math.round(ms / 1000)} s`);
    throw new Error(`el bot no responde en ${c.botUrl} (${mensajeDe(e)}) — ¿está corriendo? (iniciar_bot.bat)`);
  } finally {
    clearTimeout(to);
  }
}

async function getBot(c, ruta, ms = 8000) {
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(`${c.botUrl}${ruta}`, { signal: ctl.signal, cache: "no-store" });
    const texto = await r.text();
    let json = null;
    try { json = JSON.parse(texto); } catch (_) { json = null; }
    return { ok: r.ok, status: r.status, json };
  } finally {
    clearTimeout(to);
  }
}

/** Reporta el estado al bot. El log de la extensión vive en el storage del
 *  navegador, donde nadie lo mira; con esto el bot puede avisar por Telegram
 *  que la sesión caducó o que la extensión dejó de reportar. */
async function latido(nivel, mensaje, extra) {
  try {
    const [c, e] = await Promise.all([cfg(), leerEstado()]);
    if (!P.urlBotValida(c.botUrl)) return;
    await postBot(c, "/extension/latido", JSON.stringify({
      nivel, mensaje, ciclo: e.ciclo, ultima: e.ultima, ultimoError: e.ultimoError,
      intervaloMin: c.intervaloMin, activo: c.activo,
      version: chrome.runtime.getManifest().version,
      ...(extra || {}),
    }), "application/json", 8000);
  } catch (_) { /* si el bot no está, no hay a quién avisar: no es fatal */ }
}

/** Notificación de Chrome con freno por tipo: una sesión caducada a las 3 AM
 *  no puede sonar cada 5 min hasta la mañana. */
async function notificar(clave, titulo, mensaje) {
  try {
    const g = await chrome.storage.local.get("avisosNotif");
    const avisos = g.avisosNotif || {};
    if (Date.now() - (avisos[clave] || 0) < NOTIF_CADA_MS) return;
    avisos[clave] = Date.now();
    await chrome.storage.local.set({ avisosNotif: avisos });
    await chrome.notifications.create(`bot-fallas-${clave}`, {
      type: "basic", iconUrl: "icon128.png", title: titulo, message: mensaje, priority: 1,
    });
  } catch (e) {
    console.warn("[bot-fallas] no se pudo notificar", mensajeDe(e));
  }
}

/**
 * Mantiene vivo el worker mientras espera a la pestaña. Llamar cualquier API
 * de extensión reinicia el contador de inactividad de MV3.
 *
 * NO usar `chrome.runtime.connect` para esto: un service worker no puede
 * conectarse a sí mismo y produce "Receiving end does not exist" (trampa del
 * motor de despacho).
 */
function mantenerVivo() {
  const id = setInterval(() => { chrome.runtime.getPlatformInfo().catch(() => {}); }, 20000);
  return () => clearInterval(id);
}

function conPlazo(promesa, ms, mensaje) {
  let to;
  const plazo = new Promise((_, rechazar) => { to = setTimeout(() => rechazar(new Error(mensaje)), ms); });
  return Promise.race([promesa, plazo]).finally(() => clearTimeout(to));
}

// ---------------------------------------------------------------------------
// Candado
// ---------------------------------------------------------------------------

// Dentro de UNA vida del worker, dos despertares (alarma + clic manual) pueden
// leer el candado libre a la vez y entrar los dos: leer-y-escribir en storage
// no es atómico. Esta cola los serializa; entre vidas distintas del worker no
// hace falta (Chrome nunca tiene dos instancias del mismo worker).
let _cola = Promise.resolve();
function enExclusiva(fn) {
  const p = _cola.then(fn, fn);
  _cola = p.catch(() => {});
  return p;
}

function candadoVencido(e, c) {
  // El candado se vence solo: si el worker murió a mitad de una corrida,
  // `corriendo` quedaría en true para siempre y no se volvería a sincronizar.
  return e.corriendo && (!e.corriendoDesde || Date.now() - e.corriendoDesde > c.timeoutMs + MARGEN_CANDADO_MS);
}

function tomarCandado(c) {
  return enExclusiva(async () => {
    const e = await leerEstado();
    const vencido = candadoVencido(e, c);
    if (e.corriendo && !vencido) return { ok: false };
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const ciclo = (e.ciclo || 0) + 1;
    await escribirEstado({ corriendo: true, corriendoDesde: Date.now(), corridaId: id, ciclo });
    return { ok: true, id, ciclo, vencido };
  });
}

/** El vencimiento se cuenta desde el ÚLTIMO avance, no desde el inicio: con
 *  tres fuentes de hasta 240 s cada una, un ciclo sano dura más que
 *  timeoutMs + 60 s y, contado desde el inicio, la alarma siguiente lo daría
 *  por muerto y solaparía corridas. Devuelve false si otra corrida tomó el
 *  candado (ésta debe detenerse). */
function renovarCandado(id) {
  return enExclusiva(async () => {
    const e = await leerEstado();
    if (e.corridaId !== id) return false;
    await escribirEstado({ corriendoDesde: Date.now() });
    return true;
  });
}

function soltarCandado(id) {
  return enExclusiva(async () => {
    const e = await leerEstado();
    // una corrida vieja que termina tarde no suelta el candado de la nueva
    if (e.corridaId === id) await escribirEstado({ corriendo: false, corriendoDesde: 0 });
  });
}

// ---------------------------------------------------------------------------
// Pestaña de Centrality
// ---------------------------------------------------------------------------

async function pestanasCentrality(c) {
  const patron = P.patronHost(c.centralityBase);
  if (!patron) return [];
  try {
    return (await chrome.tabs.query({ url: patron })).filter((t) => t.id != null && t.id >= 0);
  } catch (_) {
    return [];
  }
}

async function pestanaCentrality(c) {
  const tabs = await pestanasCentrality(c);
  const utiles = tabs.filter((t) => !t.discarded);
  utiles.sort((a, b) => ((b.status === "complete") - (a.status === "complete")) ||
                        ((b.lastAccessed || 0) - (a.lastAccessed || 0)));
  const tab = utiles[0] || null;
  if (tab && tab.autoDiscardable !== false) {
    // El "Ahorro de memoria" de Chrome descarta pestañas inactivas: de noche
    // la pestaña de Centrality quedaría suspendida y el bot ciego sin aviso.
    try { await chrome.tabs.update(tab.id, { autoDiscardable: false }); } catch (_) { /* best-effort */ }
  }
  return tab;
}

function errorPermiso(e, c) {
  if (/Cannot access|permission|Missing host/i.test(mensajeDe(e))) {
    return new Error(`PERMISO: Chrome no deja entrar a ${P.origenDe(c.centralityBase)} — ` +
                     "abre Opciones y pulsa Guardar para conceder el permiso");
  }
  return e;
}

/** Garantiza que la pestaña tenga el content script. Al recargar la
 *  extensión, las pestañas ya abiertas quedan SIN él hasta recargarlas, y todo
 *  mensaje falla con "Receiving end does not exist": en vez de pedirle al
 *  operador que recargue Centrality (y quizá pierda su trabajo), se inyecta. */
async function asegurarContent(tabId, c) {
  try {
    const r = await chrome.tabs.sendMessage(tabId, { tipo: "ping" }, { frameId: 0 });
    if (r && r.ok) return false;
  } catch (e) {
    if (!esSinReceptor(e)) throw e;
  }
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["plantillas.js", "content.js"] });
  } catch (e) {
    throw errorPermiso(e, c);
  }
  await log("info", "content script inyectado en la pestaña de Centrality");
  return true;
}

async function hablarConPestana(tabId, mensaje, c, ms) {
  // frameId 0: el content script se inyecta sólo en el marco principal
  const enviar = () => conPlazo(chrome.tabs.sendMessage(tabId, mensaje, { frameId: 0 }), ms,
                                "la pestaña de Centrality no respondió a tiempo");
  await asegurarContent(tabId, c);
  try {
    return await enviar();
  } catch (e) {
    // la pestaña pudo navegar entre el ping y el mensaje
    if (!esSinReceptor(e)) throw e;
    await asegurarContent(tabId, c);
    return await enviar();
  }
}

// ---------------------------------------------------------------------------
// Ciclo
// ---------------------------------------------------------------------------

function seg(ms) {
  return `${Math.round((ms || 0) / 1000)} s`;
}

async function sincronizarFuente(tab, f, c) {
  const ahora = new Date();
  const v = Number(f.ventanaHoras) > 0 ? P.ventana(f.ventanaHoras, ahora) : { desde: null, hasta: null };
  if (/\{ou\}/.test(f.params || "") && !String(c.ou || "").trim()) {
    throw new Error("CONFIG: la plantilla usa {ou} pero la unidad organizativa está vacía (Opciones)");
  }
  const url = P.urlReporte(c.centralityBase, c.rutaReportes, f.reporteId, f.params,
                           { desde: v.desde, hasta: v.hasta, ou: String(c.ou || "").trim(), ahora });
  const sinResolver = /%7B(desde|hasta|ou|hoy|ahora)%7D/i.exec(url);
  if (sinResolver) {
    // típicamente {desde} con ventanaHoras = 0: mejor no ejecutar que mandarle
    // a Centrality un literal y recibir un error (o el universo completo)
    throw new Error(`CONFIG: la plantilla usa {${sinResolver[1]}} pero la ventana está en 0 horas`);
  }
  const desde = v.desde ? P.fmtFecha(v.desde) : "";
  const hasta = v.hasta ? P.fmtFecha(v.hasta) : "";

  const r = await hablarConPestana(tab.id, {
    tipo: "sincronizar", url, botUrl: c.botUrl, fuente: f.clave, token: c.token,
    desde, hasta, timeoutMs: c.timeoutMs,
  }, c, c.timeoutMs + 50000);
  if (!r) throw new Error("la pestaña de Centrality no respondió");

  if (r.bloqueado) {
    // Respaldo: el worker no pasa por CORS ni por el bloqueo de red local.
    // La explicación larga va como mucho una vez por hora: con 3 fuentes cada
    // 5 min taparía el historial (60 líneas) y escondería otros errores.
    const g = await chrome.storage.local.get("avisoRespaldo");
    if (Date.now() - (g.avisoRespaldo || 0) > 3600000) {
      await chrome.storage.local.set({ avisoRespaldo: Date.now() });
      await log("warn", `${f.clave}: ${r.error}. Se envía desde el worker. Para evitarlo, agrega ` +
                        `${P.origenDe(c.centralityBase)} a servidor.origenes_permitidos en config.json ` +
                        "o acepta el permiso de red local de Chrome.");
    }
    const cuerpo = r.csvBase64 ? P.base64ABytes(r.csv) : new TextEncoder().encode(String(r.csv || ""));
    const q = new URLSearchParams();
    if (desde) q.set("desde", desde);
    if (hasta) q.set("hasta", hasta);
    const resp = await postBot(c, `/ingest/${f.clave}${q.toString() ? `?${q}` : ""}`, cuerpo, "text/csv");
    if (!resp.ok) {
      throw new Error(`bot HTTP ${resp.status}: ${String((resp.json && resp.json.error) || resp.texto).slice(0, 300)}`);
    }
    return { ...(resp.json || {}), ok: true, bytes: r.bytes, msDescarga: r.msDescarga, via: "worker (respaldo)" };
  }
  if (!r.ok) throw new Error(r.error || "error desconocido en la pestaña");
  return { ...r, via: "pestaña" };
}

function resumenIngesta(r) {
  // sólo conteos y nombres de columnas canónicas: nada de filas
  return {
    ok: true, via: r.via, registros: r.registros ?? null, filas: r.filas_leidas ?? null,
    descartadas: r.filas_descartadas ?? null, fallas: r.fallas_activas ?? null,
    eventos: Array.isArray(r.eventos) ? r.eventos.length : 0,
    faltantes: Array.isArray(r.faltantes) ? r.faltantes.slice(0, 12) : [],
    bytes: r.bytes ?? null, msDescarga: r.msDescarga ?? null,
  };
}

async function capturarGrilla(tab, c) {
  let resultados;
  try {
    resultados = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true }, world: "MAIN",
      func: leerGrillasExt, args: [c.maxFilasGrilla],
    });
  } catch (e) {
    throw errorPermiso(new Error(`no se pudo leer la grilla: ${mensajeDe(e)}`), c);
  }
  const grillas = [];
  let url = "";
  let titulo = "";
  let errores = 0;
  for (const fr of resultados || []) {
    const r = fr && fr.result;
    if (!r) continue;
    if (r.error) { errores++; continue; }
    const conFilas = (r.grillas || []).filter((g) => Array.isArray(g.filas) && g.filas.length);
    if (!conFilas.length) continue;
    grillas.push(...conFilas);
    if (!url || fr.frameId === 0) { url = r.url || ""; titulo = r.titulo || ""; }
  }
  if (!grillas.length) {
    await log("info", errores && !(resultados || []).some((fr) => fr && fr.result && !fr.result.error)
      ? "grilla: la pestaña no expone ExtJS (¿está en otra página?)"
      : "grilla: no hay grillas visibles con filas (normal si la bandeja no está abierta)");
    return { ok: true, omitida: true, grillas: 0 };
  }
  const filas = grillas.reduce((n, g) => n + g.filas.length, 0);
  const resp = await postBot(c, "/ingest/grilla", JSON.stringify({ url, titulo, grillas }), "application/json");
  if (resp.status === 422) {
    // ninguna grilla trae caso: la pestaña está en otra vista. No es error.
    await log("info", `grilla: ${grillas.length} grilla(s) leída(s), ninguna sirve (sin columna de caso)`);
    return { ok: true, sirve: false, grillas: grillas.length, filas };
  }
  if (!resp.ok) {
    throw new Error(`grilla: bot HTTP ${resp.status}: ${String((resp.json && resp.json.error) || resp.texto).slice(0, 200)}`);
  }
  const j = resp.json || {};
  await log("ok", `grilla: ${grillas.length} grilla(s), ${filas} filas → ${j.registros ?? "?"} registros, ` +
                  `${j.fallas_activas ?? "?"} fallas activas`);
  return { ok: true, sirve: true, grillas: grillas.length, filas, registros: j.registros ?? null };
}

function notificarSegunError(msg, c) {
  if (/SESION_CADUCADA/.test(msg)) {
    return notificar("sesion", "Bot de fallas — sesión de Centrality caducada",
      "Inicia sesión en Centrality. Mientras tanto no se están leyendo fallas.");
  }
  if (/REPORTE_PESADO/.test(msg)) {
    return notificar("pesado", "Bot de fallas — el reporte tardó demasiado",
      "Centrality cortó la consulta. Reduce la ventana de horas o acota la OU en Opciones. No es problema de sesión.");
  }
  if (/SIN_PESTANA/.test(msg)) {
    return notificar("pestana", "Bot de fallas — abre Centrality",
      `No hay una pestaña de ${P.origenDe(c.centralityBase) || "Centrality"} abierta: el bot no está recibiendo datos.`);
  }
  if (/PERMISO/.test(msg)) {
    return notificar("permiso", "Bot de fallas — falta permiso",
      "Abre las Opciones de la extensión y pulsa Guardar para conceder el acceso a Centrality.");
  }
  return null;
}

async function correrCiclo(c, idCorrida, res) {
  if (!P.urlBotValida(c.botUrl)) throw new Error("CONFIG: la URL del bot debe ser http://127.0.0.1:<puerto>");
  const patron = P.patronHost(c.centralityBase);
  if (!patron) throw new Error("CONFIG: falta la dirección base de Centrality — ingrésala en Opciones");
  if (!(await chrome.permissions.contains({ origins: [patron] }))) {
    throw new Error(`PERMISO: falta el permiso para ${P.origenDe(c.centralityBase)} — abre Opciones y pulsa Guardar`);
  }
  const tab = await pestanaCentrality(c);
  if (!tab) {
    const suspendidas = (await pestanasCentrality(c)).length > 0;
    throw new Error(suspendidas
      ? "SIN_PESTANA: la pestaña de Centrality está suspendida por Chrome — ábrela para reactivarla"
      : "SIN_PESTANA: abre Centrality e inicia sesión; debe quedar una pestaña abierta");
  }

  const activas = c.fuentes.filter((f) => f.activo && String(f.reporteId || "").trim());
  if (!activas.length && !c.capturarGrilla) {
    await log("warn", "no hay fuentes activas ni captura de grilla: configura las fuentes en Opciones");
  }
  let sesionCaducada = false;
  for (const f of activas) {
    if (!(await renovarCandado(idCorrida))) {
      res.errores.push("otra corrida tomó el candado: ésta se detiene");
      return;
    }
    try {
      const r = await sincronizarFuente(tab, f, c);
      res.fuentes[f.clave] = resumenIngesta(r);
      const falt = res.fuentes[f.clave].faltantes;
      await log("ok", `${f.clave}: ${r.registros ?? "?"} registros (${r.filas_leidas ?? "?"} filas) ` +
                      `en ${seg(r.msDescarga)} vía ${r.via} → ${r.fallas_activas ?? "?"} fallas activas` +
                      (falt.length ? ` · faltan columnas: ${falt.join(", ")}` : ""));
    } catch (e) {
      const msg = `${f.clave}: ${mensajeDe(e)}`;
      res.fuentes[f.clave] = { ok: false, error: msg.slice(0, 300) };
      res.errores.push(msg);
      await log("error", msg.slice(0, 400));
      // sin sesión, las demás fuentes y la grilla fallarían igual
      if (/SESION_CADUCADA/.test(msg)) { sesionCaducada = true; break; }
    }
  }

  if (c.capturarGrilla && !sesionCaducada) {
    if (!(await renovarCandado(idCorrida))) {
      res.errores.push("otra corrida tomó el candado: ésta se detiene");
      return;
    }
    try {
      res.grilla = await capturarGrilla(tab, c);
    } catch (e) {
      const msg = mensajeDe(e);
      res.grilla = { ok: false, error: msg.slice(0, 300) };
      res.errores.push(/^grilla/.test(msg) ? msg : `grilla: ${msg}`);
      await log("error", msg.slice(0, 400));
    }
  }
}

async function ciclo(manual = false) {
  const c = await cfg();
  if (!c.activo && !manual) return { inactivo: true };
  const cand = await tomarCandado(c);
  if (!cand.ok) {
    await log("warn", "ciclo omitido: la corrida anterior sigue en curso");
    return { omitido: true };
  }
  if (cand.vencido) await log("warn", "candado vencido: la corrida anterior no terminó (¿el worker murió a mitad?)");

  const n = cand.ciclo;
  await guardarEstado({ ultimoInicio: new Date().toISOString() });
  await latido("info", `ciclo ${n} iniciado (${manual ? "manual" : "alarma"})`,
               { disparo: manual ? "manual" : "alarma" });
  // el ciclo completo (hasta 3 descargas + grilla) supera de largo los 30 s
  // de inactividad que tolera MV3
  const soltar = mantenerVivo();
  const res = { ciclo: n, fuentes: {}, grilla: null, errores: [] };
  try {
    await correrCiclo(c, cand.id, res);
  } catch (e) {
    res.errores.push(mensajeDe(e));
    await log("error", `ciclo ${n}: ${mensajeDe(e)}`.slice(0, 400));
  } finally {
    soltar();
    const ultimoError = res.errores.length ? res.errores.join(" | ").slice(0, 500) : null;
    await guardarEstado({
      ultima: new Date().toISOString(), ultimoError,
      ultimoResultado: { ciclo: n, fuentes: res.fuentes, grilla: res.grilla },
    });
    for (const m of res.errores) await notificarSegunError(m, c);
    await soltarCandado(cand.id);
    await latido(ultimoError ? "error" : "ok", ultimoError || `ciclo ${n} terminado`,
                 { proximo_en_min: c.intervaloMin });
  }
  return res;
}

// ---------------------------------------------------------------------------
// Diagnóstico
// ---------------------------------------------------------------------------

/**
 * Prueba cada eslabón por separado y dice cuál falla. Cuando la cadena se
 * rompe el síntoma es siempre el mismo ("no avisa") y las causas son muy
 * distintas: sin permiso, sin pestaña, sesión caducada, bot apagado, o Chrome
 * bloqueando el acceso a la red local desde la pestaña.
 *
 * Contra /ping, NUNCA contra /ingest: en el motor de despacho un POST de
 * prueba a /ingest reemplazó la bandeja completa por cero filas. Un chequeo de
 * salud no puede destruir el estado que está verificando.
 */
async function diagnostico() {
  const c = await cfg();
  const pasos = [];
  const paso = (n, ok, det) => pasos.push({ n, ok: !!ok, det: String(det || "") });
  const origen = P.origenDe(c.centralityBase);
  const patron = P.patronHost(c.centralityBase);

  paso("Configuración", !!origen && !!P.urlBotValida(c.botUrl),
       !origen ? "Falta la dirección base de Centrality (Opciones)"
               : !P.urlBotValida(c.botUrl) ? "La URL del bot debe ser http://127.0.0.1:<puerto>"
                                           : `Centrality ${origen} · bot ${c.botUrl}`);

  let permiso = false;
  if (patron) {
    try { permiso = await chrome.permissions.contains({ origins: [patron] }); } catch (_) { permiso = false; }
  }
  paso("Permiso para el host", permiso,
       permiso ? `concedido para ${origen}` : "No concedido: abre Opciones y pulsa Guardar (Chrome lo pide)");

  let tab = null;
  try {
    tab = origen ? await pestanaCentrality(c) : null;
    if (!tab) {
      paso("Pestaña de Centrality", false, "No hay una abierta (o está suspendida): ábrela e inicia sesión");
    } else if (!permiso) {
      paso("Pestaña de Centrality", false, "Hay una abierta, pero sin el permiso de host no se puede usar");
    } else {
      const r = await hablarConPestana(tab.id, { tipo: "ping" }, c, 10000);
      paso("Pestaña de Centrality", !!(r && r.ok),
           r && r.ok ? `activa (${String(r.titulo || tab.title || "").slice(0, 40)})`
                     : "no responde: recarga la pestaña de Centrality");
    }
  } catch (e) {
    paso("Pestaña de Centrality", false, `${mensajeDe(e)} — recarga la pestaña de Centrality`);
  }

  if (origen && permiso) {
    try {
      let ruta = String(c.rutaReportes || "").trim();
      if (!ruta.startsWith("/")) ruta = `/${ruta}`;
      const r = await fetch(`${origen}${ruta}`, { credentials: "include", cache: "no-store" });
      const t = (await r.text()).slice(0, 600);
      const prob = P.clasificarRespuesta(r.status, "text/html", t, r.url);
      const caducada = !!prob && /^SESION_CADUCADA/.test(prob);
      paso("Sesión en Centrality", r.ok && !caducada,
           caducada ? "Redirige al login: inicia sesión en Centrality" : `HTTP ${r.status}`);
    } catch (e) {
      paso("Sesión en Centrality", false, mensajeDe(e));
    }
  } else {
    paso("Sesión en Centrality", false, "No se puede probar sin dirección base y permiso");
  }

  try {
    const r = await getBot(c, "/estado");
    const j = r.json || {};
    const edades = Object.entries(j.fuentes || {})
      .map(([k, v]) => `${k} ${v && v.edad_min != null ? `${Math.round(v.edad_min)} min` : "—"}`)
      .join(", ");
    paso("Bot local (/estado)", r.ok,
         r.ok ? `${j.fallas_activas ?? "?"} fallas activas${edades ? ` · ${edades}` : ""}` : `HTTP ${r.status}`);
  } catch (e) {
    paso("Bot local (/estado)", false, `${mensajeDe(e)} — ¿está corriendo? (iniciar_bot.bat)`);
  }

  try {
    const r = await postBot(c, "/ping", "ping", "text/csv", 8000);
    paso("Escritura al bot (CORS /ping)", r.ok,
         r.status === 401 ? "HTTP 401: el token compartido no coincide con servidor.token_compartido"
                          : `HTTP ${r.status}`);
  } catch (e) {
    paso("Escritura al bot (CORS /ping)", false, mensajeDe(e));
  }

  // el camino real de la ingesta: POST desde la página https de Centrality
  if (tab && permiso) {
    try {
      const r = await hablarConPestana(tab.id, { tipo: "ping", botUrl: c.botUrl, token: c.token }, c, 15000);
      const b = (r && r.bot) || {};
      paso("Envío desde la pestaña (red local)", !!b.ok,
           b.ok ? `HTTP ${b.status}`
                : b.status ? `HTTP ${b.status}${b.status === 403 ? `: agrega ${origen} a servidor.origenes_permitidos` : ""}`
                           : `bloqueado (${b.error || "sin detalle"}): agrega ${origen} a servidor.origenes_permitidos ` +
                             "o acepta el permiso de red local. Mientras tanto se usa el respaldo del worker.");
    } catch (e) {
      paso("Envío desde la pestaña (red local)", false, mensajeDe(e));
    }
  }

  return { pasos, version: chrome.runtime.getManifest().version };
}

async function descubrir() {
  const c = await cfg();
  const patron = P.patronHost(c.centralityBase);
  if (!patron) return { ok: false, error: "Falta la dirección base de Centrality: guárdala primero" };
  if (!(await chrome.permissions.contains({ origins: [patron] }))) {
    return { ok: false, error: "Falta el permiso para el host: pulsa Guardar y acepta el permiso" };
  }
  const tab = await pestanaCentrality(c);
  if (!tab) return { ok: false, error: "Abre Centrality e inicia sesión en una pestaña" };
  const abiertas = c.fuentes.find((f) => f.clave === "abiertas");
  const r = await hablarConPestana(tab.id, {
    tipo: "descubrir", base: c.centralityBase, ruta: c.rutaReportes,
    idReporteOU: (abiertas && abiertas.reporteId) || "432",
  }, c, 130000);
  return r || { ok: false, error: "la pestaña no respondió" };
}

// ---------------------------------------------------------------------------
// Configuración
// ---------------------------------------------------------------------------

function entero(v, min, max, def) {
  const n = Number(v);
  if (!Number.isFinite(n)) return def;
  return Math.min(max, Math.max(min, Math.round(n)));
}

/** Sanea lo que manda Opciones. Se valida acá también (no sólo en la página):
 *  `botUrl` decide a dónde va el CSV crudo con NIS y direcciones. */
function sanearCfg(entrada, actual) {
  const e = entrada || {};
  const errores = [];
  const out = {};
  if ("botUrl" in e) {
    const b = P.urlBotValida(e.botUrl);
    if (!b) errores.push("botUrl: debe ser http://127.0.0.1:<puerto> o http://localhost:<puerto>");
    else out.botUrl = b;
  }
  if ("centralityBase" in e) {
    const s = String(e.centralityBase || "").trim();
    const o = P.origenDe(s);
    if (s && !o) errores.push("centralityBase: no es una URL http(s) válida");
    else out.centralityBase = o;
  }
  if ("rutaReportes" in e) {
    let r = String(e.rutaReportes || "").trim() || DEF.rutaReportes;
    if (!r.startsWith("/")) r = `/${r}`;
    out.rutaReportes = r;
  }
  if ("intervaloMin" in e) out.intervaloMin = entero(e.intervaloMin, 1, 120, DEF.intervaloMin);
  // tope 240 s: el worker espera a la pestaña timeoutMs + 50 s, y Chrome corta
  // una llamada de API de un service worker que pase de 5 min
  if ("timeoutMs" in e) out.timeoutMs = entero(e.timeoutMs, 30000, 240000, DEF.timeoutMs);
  if ("activo" in e) out.activo = e.activo === true || e.activo === "true";
  if ("token" in e) out.token = String(e.token || "").trim();
  if ("ou" in e) out.ou = String(e.ou == null ? "" : e.ou).trim();
  if ("capturarGrilla" in e) out.capturarGrilla = e.capturarGrilla === true || e.capturarGrilla === "true";
  if ("maxFilasGrilla" in e) out.maxFilasGrilla = entero(e.maxFilasGrilla, 50, 20000, DEF.maxFilasGrilla);
  if ("fuentes" in e) {
    const previas = mezclarFuentes(actual && actual.fuentes);
    out.fuentes = previas.map((p) => {
      const n = (Array.isArray(e.fuentes) ? e.fuentes : []).find((x) => x && x.clave === p.clave);
      if (!n) return p;
      const reporteId = String(n.reporteId == null ? "" : n.reporteId).trim();
      if (reporteId && !/^\d{1,7}$/.test(reporteId)) errores.push(`${p.clave}: el ID de reporte debe ser numérico`);
      const activo = n.activo === true || n.activo === "true";
      if (activo && !reporteId) errores.push(`${p.clave}: activa sin ID de reporte`);
      return {
        clave: p.clave, activo, reporteId,
        params: String(n.params == null ? "" : n.params).trim(),
        ventanaHoras: entero(n.ventanaHoras, 0, 24 * 31, p.ventanaHoras),
      };
    });
  }
  return { cfg: out, errores };
}

async function guardar(entrada) {
  const actual = await cfg();
  const { cfg: nuevo, errores } = sanearCfg(entrada, actual);
  if (errores.length) return { ok: false, error: errores.join("; "), errores };
  await chrome.storage.local.set(nuevo);
  await programar();
  return { ok: true, cfg: await cfg() };
}

// ---------------------------------------------------------------------------
// Alarma
// ---------------------------------------------------------------------------

function crearAlarma(c) {
  // `create` con el mismo nombre REEMPLAZA la alarma. En el motor de despacho
  // se hacía clearAll() antes, y eso abría una ventana en la que
  // asegurarAlarma() no la encontraba y creaba otra: se pisaban en bucle.
  const periodo = Math.max(1, Number(c.intervaloMin) || DEF.intervaloMin);
  return chrome.alarms.create("ciclo", { periodInMinutes: periodo, delayInMinutes: periodo });
}

async function programar() {
  const c = await cfg();
  if (c.activo) {
    await crearAlarma(c);
    await log("info", `programado cada ${Math.max(1, c.intervaloMin)} min`);
  } else {
    await chrome.alarms.clear("ciclo");
    await log("info", "sincronización automática pausada");
  }
}

/**
 * Auto-reparación de la alarma. En el motor de despacho se observó: sincronizó
 * al instalarse y NO volvió a ejecutarse en 79 minutos, sin ningún aviso. Si
 * la alarma se pierde (recarga, actualización, o un listener que devolvió
 * antes de que `alarms.create` corriera), la extensión simplemente deja de
 * hacer cosas. Por eso en cada despertar del worker se verifica y se recrea.
 * También se corrige si quedó con otro periodo que el configurado.
 */
let _revisando = false;

async function asegurarAlarma() {
  if (_revisando) return;            // dos despertares seguidos no se pisan
  _revisando = true;
  try {
    const c = await cfg();
    if (!c.activo) return;
    const periodo = Math.max(1, Number(c.intervaloMin) || DEF.intervaloMin);
    const a = await chrome.alarms.get("ciclo");
    if (a && a.periodInMinutes === periodo) return;
    await crearAlarma(c);
    await log("warn", a ? "la alarma tenía otro periodo: corregida" : "la alarma no existía: recreada");
    // como mucho un latido cada 5 min: un bucle de recreación no puede
    // inundar al bot y esconder el problema de fondo
    const g = await chrome.storage.local.get("ultimoAvisoAlarma");
    if (Date.now() - (g.ultimoAvisoAlarma || 0) > 300000) {
      await chrome.storage.local.set({ ultimoAvisoAlarma: Date.now() });
      await latido("warn", "alarma recreada (se había perdido)");
    }
  } catch (e) {
    console.warn("[bot-fallas] asegurarAlarma", mensajeDe(e));
  } finally {
    _revisando = false;
  }
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== "ciclo") return undefined;
  // se retiene la promesa: si el handler devuelve de inmediato, Chrome puede
  // dar por terminado el trabajo del worker
  return (async () => { await asegurarAlarma(); await ciclo(false); })();
});

chrome.runtime.onInstalled.addListener((d) =>
  (async () => {
    await programar();
    // recién instalada no hay host configurado: llevar directo a Opciones
    if (d && d.reason === "install") {
      try { await chrome.runtime.openOptionsPage(); } catch (_) { /* sin UI: no importa */ }
    }
    await ciclo(false);
  })());

chrome.runtime.onStartup.addListener(() =>
  (async () => { await programar(); await latido("info", "Chrome reinició"); })());

// cada vez que el worker revive por cualquier motivo, se revisa la alarma
asegurarAlarma();

chrome.runtime.onMessage.addListener((msg, sender, responder) => {
  // Sólo páginas de esta extensión (popup / opciones); los content scripts
  // responden, no piden. Se filtra por URL y no por `sender.tab`: la página
  // de opciones (options_page) abre en una pestaña y también trae `tab`.
  const s = sender || {};
  const propia = s.id === chrome.runtime.id && String(s.url || "").startsWith(chrome.runtime.getURL(""));
  if (!msg || typeof msg !== "object" || !propia) return false;
  const responderSeguro = (r) => {
    // si el popup se cerró, el canal ya no existe: no es un error
    try { responder(r); } catch (_) { /* canal cerrado */ }
  };
  (async () => {
    try {
      if (msg.tipo === "sincronizar") return responderSeguro(await ciclo(true));
      if (msg.tipo === "estado") {
        const [c, e, a] = await Promise.all([cfg(), leerEstado(), chrome.alarms.get("ciclo")]);
        const patron = P.patronHost(c.centralityBase);
        let permisoHost = false;
        if (patron) {
          try { permisoHost = await chrome.permissions.contains({ origins: [patron] }); } catch (_) { permisoHost = false; }
        }
        return responderSeguro({
          ...e, cfg: c, permisoHost, proximo: a ? a.scheduledTime : null,
          candadoVencido: candadoVencido(e, c), version: chrome.runtime.getManifest().version,
        });
      }
      if (msg.tipo === "guardar") return responderSeguro(await guardar(msg.cfg));
      if (msg.tipo === "diagnostico") return responderSeguro(await diagnostico());
      if (msg.tipo === "descubrir") return responderSeguro(await descubrir());
      return responderSeguro({ ok: false, error: "mensaje desconocido" });
    } catch (e) {
      return responderSeguro({ ok: false, error: mensajeDe(e) });
    }
  })();
  return true;   // respuesta asíncrona
});

// para tests manuales desde la consola del worker
globalThis.__botFallas = { DEF, CLAVES_FUENTE, ciclo, diagnostico, sanearCfg };
