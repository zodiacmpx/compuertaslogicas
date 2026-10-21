/**
 * Content script: corre DENTRO de la pestaña de Centrality, con la sesión del
 * despachador.
 *
 * Sólo LEE. No hace clic, no despacha, no modifica nada en Centrality.
 *
 * Por qué la descarga vive acá y no en el service worker
 * -------------------------------------------------------
 * Hallazgo del motor de despacho (el más caro de su puesta en marcha): MV3
 * termina el service worker durante operaciones largas, y el 432 sobre una OU
 * grande tarda ~75 s. Se observó el fallo exacto: el ciclo arrancaba, quedaba
 * el latido de inicio, y nunca llegaban ni el de término ni el timeout — el
 * worker moría antes del `finally`. Una pestaña abierta no tiene ese límite.
 * El worker sólo despierta por la alarma, delega acá y registra el resultado.
 *
 *   alarma -> service worker -> pestaña (descarga + POST) -> bot 127.0.0.1
 *
 * No se inyecta por manifest (el host de Centrality lo pone el usuario y el
 * repo es público): el worker lo inyecta bajo demanda junto con plantillas.js.
 *
 * Mensajes: ping, sincronizar, descubrir.
 */
(() => {
  "use strict";

  // Idempotencia. El worker reinyecta este archivo cuando un ping falla, y eso
  // pasa en dos situaciones distintas:
  //   1. doble inyección dentro de la misma vida de la extensión -> ya hay un
  //      oyente vivo: NO registrar otro (respondería dos veces).
  //   2. la extensión se recargó -> el oyente viejo quedó huérfano (su
  //      `chrome.runtime` está invalidado) pero la marca global puede seguir
  //      ahí. Si la marca bastara para salir, la pestaña quedaría muda para
  //      siempre. Por eso se verifica que el oyente previo siga vivo.
  const previo = globalThis.__botFallasContent;
  if (previo) {
    let vivo = false;
    try {
      vivo = previo.runtime === chrome.runtime && !!chrome.runtime.id &&
             chrome.runtime.onMessage.hasListener(previo.oyente);
    } catch (_) {
      vivo = false;
    }
    if (vivo) return;
    try { previo.runtime.onMessage.removeListener(previo.oyente); } catch (_) { /* contexto muerto */ }
  }

  const P = globalThis.Plantillas;
  const TIMEOUT_DEF = 240000;
  // Plazos anidados: pestaña (timeoutMs + 45 s) < espera del worker
  // (timeoutMs + 50 s) < vencimiento del candado (timeoutMs + 60 s). Así nunca
  // hay una descarga viva cuando otra corrida toma el candado.
  const TIMEOUT_BOT = 45000;

  function mensajeDe(e) {
    return String((e && e.message) || e || "error desconocido");
  }

  /** fetch con plazo. Un AbortError se traduce a un mensaje que el operador
   *  entiende; cualquier otro error (TypeError de red) se re-lanza tal cual,
   *  porque el llamador distingue "bloqueado" por el tipo. */
  async function conPlazo(url, opciones, ms, queEs) {
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), ms);
    try {
      const r = await fetch(url, { ...opciones, signal: ctl.signal });
      const buf = await r.arrayBuffer();
      return { r, buf };
    } catch (e) {
      if (e && e.name === "AbortError") {
        const err = new Error(`TIMEOUT: ${queEs} superó ${Math.round(ms / 1000)} s`);
        err.name = "Timeout";
        throw err;
      }
      throw e;
    } finally {
      clearTimeout(to);
    }
  }

  function mismoOrigen(url) {
    return P.origenDe(url) !== "" && P.origenDe(url) === location.origin;
  }

  function cabecerasBot(token, tipo) {
    const h = { "Content-Type": tipo };
    if (token) h["X-Bot-Token"] = String(token);
    return h;
  }

  function leerJson(buf) {
    try { return JSON.parse(new TextDecoder("utf-8").decode(buf)); } catch (_) { return null; }
  }

  /**
   * Descarga el reporte con la cookie de la pestaña y empuja los bytes crudos
   * al bot. Se mandan BYTES, no texto: el bot recupera el encoding mixto
   * (UTF-8 con bytes cp1252 sueltos) que trae Centrality.
   */
  async function sincronizar(msg) {
    const { url, fuente, token, desde, hasta } = msg;
    const timeoutMs = Number(msg.timeoutMs) > 0 ? Number(msg.timeoutMs) : TIMEOUT_DEF;
    const botUrl = P.urlBotValida(msg.botUrl);
    // Defensa: esta función manda cookies y luego reenvía el CSV (con NIS y
    // direcciones). Sólo se descarga del origen de la propia pestaña y sólo se
    // reenvía a loopback, aunque el storage estuviera mal configurado.
    if (!mismoOrigen(url)) throw new Error("CONFIG: la URL del reporte no es del origen de esta pestaña");
    if (!botUrl) throw new Error("CONFIG: la URL del bot debe ser http://127.0.0.1:<puerto> o http://localhost:<puerto>");
    if (!/^[a-z]+$/.test(String(fuente || ""))) throw new Error("CONFIG: fuente inválida");

    const t0 = Date.now();
    const { r, buf } = await conPlazo(url, { credentials: "include", cache: "no-store" },
                                      timeoutMs, "el reporte de Centrality");
    const msDescarga = Date.now() - t0;
    const cabeza = new TextDecoder("utf-8").decode(buf.slice(0, 600));
    const problema = P.clasificarRespuesta(r.status, r.headers.get("content-type") || "", cabeza, r.url);
    if (problema) throw new Error(problema);

    const q = new URLSearchParams();
    if (desde) q.set("desde", desde);
    if (hasta) q.set("hasta", hasta);
    const destino = `${botUrl}/ingest/${fuente}${q.toString() ? `?${q}` : ""}`;

    const t1 = Date.now();
    let res;
    try {
      res = await conPlazo(destino, { method: "POST", headers: cabecerasBot(token, "text/csv"), body: buf },
                           TIMEOUT_BOT, "el envío al bot");
    } catch (e) {
      if (e && e.name === "Timeout") throw e;
      // TypeError = la red rechazó el POST antes de que el bot respondiera:
      // Chrome bloqueó el acceso a la red local desde una página https (LNA /
      // Private Network Access) o el preflight CORS dio 403 porque el origen de
      // Centrality no está en `servidor.origenes_permitidos`. El worker no
      // tiene esa restricción: se le devuelve el CSV para que lo envíe él.
      return {
        ok: false, bloqueado: true,
        csv: P.bytesABase64(buf), csvBase64: true,
        bytes: buf.byteLength, msDescarga,
        error: `envío desde la pestaña bloqueado: ${mensajeDe(e)}`,
      };
    }
    const msSubida = Date.now() - t1;
    const j = leerJson(res.buf);
    if (!res.r.ok) {
      const det = (j && j.error) || new TextDecoder("utf-8").decode(res.buf.slice(0, 160));
      return { ok: false, status: res.r.status, bytes: buf.byteLength, msDescarga, msSubida,
               error: `bot HTTP ${res.r.status}: ${String(det).slice(0, 300)}` };
    }
    return { ...(j || {}), ok: true, bytes: buf.byteLength, msDescarga, msSubida };
  }

  /** Prueba el MISMO camino que la ingesta (POST con text/csv + token desde
   *  esta página -> preflight CORS + red privada). Probarlo desde el worker
   *  no sirve: el worker no pasa por CORS. */
  async function probarBot(msg) {
    const botUrl = P.urlBotValida(msg.botUrl);
    if (!botUrl) return { ok: false, error: "URL del bot no local" };
    try {
      const { r } = await conPlazo(`${botUrl}/ping`,
        { method: "POST", headers: cabecerasBot(msg.token, "text/csv"), body: "ping" },
        10000, "el ping al bot");
      return { ok: r.ok, status: r.status };
    } catch (e) {
      return { ok: false, bloqueado: e && e.name !== "Timeout", error: mensajeDe(e) };
    }
  }

  /** Catálogo de OUs: primero en el HTML dado, luego en los <script> de la
   *  página actual (si la pestaña está justo en un reporte). */
  function catalogoOU(html) {
    const desdeHtml = html ? P.extraerOUs(html) : null;
    if (desdeHtml && desdeHtml.length) return desdeHtml;
    const s = [...document.querySelectorAll("script")]
      .map((x) => x.textContent || "")
      .find((t) => /var\s+config\s*=/.test(t));
    return s ? P.extraerOUs(s) : null;
  }

  function tituloHtml(html) {
    const m = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(String(html || ""));
    return m ? P.textoPlano(m[1]).slice(0, 120) : "";
  }

  async function descubrir(msg) {
    const base = P.origenDe(msg.base) || location.origin;
    if (base !== location.origin) throw new Error("CONFIG: la dirección base no es la de esta pestaña");
    let ruta = String(msg.ruta || "/backend/saesa/reports.php").trim();
    if (!ruta.startsWith("/")) ruta = `/${ruta}`;
    const url = `${base}${ruta}`;

    const { r, buf } = await conPlazo(url, { credentials: "include", cache: "no-store" }, 60000,
                                      "el listado de reportes");
    const html = new TextDecoder("utf-8").decode(buf);
    // un listado es HTML por naturaleza: sólo interesa saber si es el login
    const prob = P.clasificarRespuesta(r.status, "text/html", html.slice(0, 600), r.url);
    if (prob && /^(SESION_CADUCADA|REPORTE_PESADO|HTTP_ERROR)/.test(prob)) throw new Error(prob);

    const reportes = P.extraerReportes(html);
    let ous = catalogoOU(html);
    let ousDesde = ous && ous.length ? "listado" : "";
    if (!ous || !ous.length) {
      // El combo de OUs vive en la página de PARÁMETROS de un reporte que pide
      // OU (el 432). Sin `process=1` sólo muestra el formulario: no ejecuta el
      // reporte (el diagnóstico del motor de despacho hace este mismo GET).
      const idOU = /^\d+$/.test(String(msg.idReporteOU || "")) ? String(msg.idReporteOU) : "432";
      try {
        const p = await conPlazo(`${url}?id=${idOU}`, { credentials: "include", cache: "no-store" },
                                 60000, "la página de parámetros");
        ous = catalogoOU(new TextDecoder("utf-8").decode(p.buf));
        if (ous && ous.length) ousDesde = `parámetros del reporte ${idOU}`;
      } catch (_) {
        ous = null;
      }
    }
    return { ok: true, reportes, ous: ous || [], ousDesde, titulo: tituloHtml(html) || document.title, url };
  }

  const oyente = (msg, sender, responder) => {
    if (!msg || typeof msg !== "object") return false;
    if (sender && sender.id && sender.id !== chrome.runtime.id) return false;
    if (msg.tipo === "ping") {
      if (!msg.botUrl) {
        responder({ ok: true, titulo: document.title });
        return false;
      }
      probarBot(msg).then((bot) => responder({ ok: true, titulo: document.title, bot }));
      return true;    // respuesta asíncrona
    }
    if (msg.tipo === "sincronizar") {
      sincronizar(msg)
        .then((r) => responder(r))
        .catch((e) => responder({ ok: false, error: mensajeDe(e) }));
      return true;
    }
    if (msg.tipo === "descubrir") {
      descubrir(msg)
        .then((r) => responder(r))
        .catch((e) => responder({ ok: false, error: mensajeDe(e) }));
      return true;
    }
    // mensaje ajeno: no retener el canal (devolver true lo dejaría colgado)
    return false;
  };

  chrome.runtime.onMessage.addListener(oyente);
  globalThis.__botFallasContent = { runtime: chrome.runtime, oyente, desde: Date.now() };
})();
