/**
 * Popup: estado del bot local + último ciclo de la extensión.
 *
 * Los datos del bot (/estado, /fallas) ya vienen sin datos personales: sólo
 * casos, conteos, comunas y nombres de calle. Igual todo se inserta con
 * textContent (nunca innerHTML): un nombre de calle viene de Centrality.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const EMOJI_NIVEL = { masivo: "🟣", alta: "🔴", media: "🟠", baja: "🟡", "": "⚪" };
  let botUrl = "http://127.0.0.1:8771";
  let pintando = false;

  function nodo(tag, cls, texto) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (texto != null) el.textContent = String(texto);
    return el;
  }

  function limpiar(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  async function msg(m, intentos = 2) {
    for (let i = 0; ; i++) {
      try {
        return await chrome.runtime.sendMessage(m);
      } catch (e) {
        // el worker pudo estar reiniciándose: un reintento corto basta
        const sinReceptor = /Receiving end does not exist|Could not establish connection/i.test(String(e && e.message));
        if (!sinReceptor || i + 1 >= intentos) throw e;
        await new Promise((r) => setTimeout(r, 400));
      }
    }
  }

  async function getJson(ruta, ms = 5000) {
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), ms);
    try {
      const r = await fetch(`${botUrl}${ruta}`, { signal: ctl.signal, cache: "no-store" });
      if (!r.ok) throw new Error(`${ruta}: HTTP ${r.status}`);
      return await r.json();
    } finally {
      clearTimeout(to);
    }
  }

  function hhmm(v) {
    if (!v) return "—";
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString("es-CL", { hour: "2-digit", minute: "2-digit" });
  }

  function miles(n) {
    return n == null || !Number.isFinite(Number(n)) ? "—" : Number(n).toLocaleString("es-CL");
  }

  function setPill(el, cls, texto) {
    el.className = `pill ${cls || ""}`.trim();
    el.textContent = texto;
  }

  function banner(cls, texto) {
    const root = $("banner");
    limpiar(root);
    if (texto) root.appendChild(nodo("div", `banner ${cls}`, texto));
  }

  function textoError(err) {
    const e = String(err || "");
    if (/SESION_CADUCADA/.test(e)) return "Sesión de Centrality caducada: inicia sesión en la pestaña de Centrality.";
    if (/SIN_PESTANA/.test(e)) return "Abre Centrality e inicia sesión: debe quedar una pestaña abierta.";
    if (/PERMISO/.test(e)) return "Falta el permiso para Centrality: abre Configuración y pulsa Guardar.";
    if (/CONFIG/.test(e) && /dirección base/.test(e)) return "Falta configurar la dirección de Centrality (Configuración).";
    if (/REPORTE_PESADO/.test(e)) return "Centrality cortó un reporte por tardar demasiado: reduce la ventana o acota la OU.";
    return `Último ciclo con error: ${e.slice(0, 220)}`;
  }

  function pintarFuentesBot(salud) {
    const root = $("fuentesBot");
    limpiar(root);
    const fuentes = (salud && salud.fuentes) || {};
    const claves = Object.keys(fuentes);
    if (!claves.length) {
      root.appendChild(nodo("div", "tiny", "El bot aún no recibe datos de ninguna fuente."));
      return;
    }
    for (const k of claves) {
      const f = fuentes[k] || {};
      const row = nodo("div", "row");
      const name = nodo("div", "name");
      name.appendChild(nodo("b", "", k));
      const det = f.ok === false
        ? `error: ${String(f.error || "").slice(0, 120)}`
        : `${f.registros ?? "?"} registros${Array.isArray(f.faltantes) && f.faltantes.length ? ` · faltan: ${f.faltantes.slice(0, 5).join(", ")}` : ""}`;
      name.appendChild(nodo("small", "", det));
      const edad = f.edad_min == null ? null : Math.round(Number(f.edad_min));
      const cls = f.ok === false ? "bad" : f.fresca === false ? "warn" : edad == null ? "" : "good";
      const right = nodo("div", "right");
      right.appendChild(nodo("span", `pill ${cls}`.trim(), edad == null ? "sin dato" : `hace ${edad} min`));
      row.append(name, right);
      root.appendChild(row);
    }
  }

  function pintarCanales(salud) {
    const root = $("canales");
    limpiar(root);
    const canales = (salud && salud.notificadores) || {};
    for (const [k, v] of Object.entries(canales)) {
      const c = v || {};
      const row = nodo("div", "row");
      const name = nodo("div", "name");
      name.appendChild(nodo("b", "", `Canal ${k}`));
      name.appendChild(nodo("small", "", `${c.enviados ?? 0} enviados · ${c.fallidos ?? 0} fallidos` +
                                         (c.ultimo_error ? ` · ${String(c.ultimo_error).slice(0, 80)}` : "")));
      const right = nodo("div", "right");
      right.appendChild(nodo("span", `pill ${c.ultimo_error && !c.ultimo_ok ? "bad" : c.fallidos ? "warn" : "good"}`,
                             c.ultimo_ok ? `ok ${hhmm(c.ultimo_ok)}` : "—"));
      row.append(name, right);
      root.appendChild(row);
    }
  }

  function pintarFallas(lista) {
    const root = $("fallas");
    limpiar(root);
    const fallas = Array.isArray(lista) ? lista : (lista && Array.isArray(lista.fallas) ? lista.fallas : []);
    if (!fallas.length) {
      root.appendChild(nodo("div", "empty", "✅ Sin fallas activas que cumplan los filtros"));
      return;
    }
    for (const f of fallas.slice(0, 6)) {
      const row = nodo("div", "row");
      const name = nodo("div", "name");
      const cl = f.clientes == null ? "clientes s/d"
        : f.clientes_fuente === "nis_reclamos" ? `≥ ${miles(f.clientes)} cl (con reclamo)` : `${miles(f.clientes)} cl`;
      name.appendChild(nodo("b", "", `${EMOJI_NIVEL[f.nivel || ""] || "⚪"} ${f.caso} · ${cl}`));
      const comunas = Array.isArray(f.comunas) ? f.comunas.slice(0, 2).join(", ") : "";
      const calles = Array.isArray(f.calles) ? f.calles : [];
      const top = calles.slice(0, 2).map((c) => c && c.nombre).filter(Boolean).join(", ");
      name.appendChild(nodo("small", "", [comunas, calles.length ? `${calles.length} calles${top ? `: ${top}` : ""}` : "sin calles aún"]
        .filter(Boolean).join(" · ")));
      const right = nodo("div", "right");
      right.appendChild(nodo("span", "pill", f.inicio ? `desde ${hhmm(f.inicio)}` : "—"));
      row.append(name, right);
      root.appendChild(row);
    }
    if (fallas.length > 6) root.appendChild(nodo("div", "tiny", `…y ${fallas.length - 6} más (usa /fallas en Telegram o el Monitor)`));
  }

  function pintarLog(ext) {
    const root = $("log");
    limpiar(root);
    const hist = ((ext && ext.historial) || []).slice(0, 14);
    if (!hist.length) {
      root.appendChild(nodo("div", "empty", "Sin actividad todavía"));
      return;
    }
    for (const h of hist) {
      const line = nodo("div", `logline ${h.nivel || "info"}`);
      line.appendChild(nodo("span", "t", hhmm(h.t)));
      line.appendChild(nodo("span", "", h.msg || ""));
      root.appendChild(line);
    }
  }

  function pintarExtension(ext) {
    const c = (ext && ext.cfg) || {};
    $("kCiclo").textContent = ext && ext.ultima ? hhmm(ext.ultima) : "—";
    const partes = [];
    if (ext && ext.ciclo) partes.push(`ciclo ${ext.ciclo}`);
    if (ext && ext.ultima) partes.push(`último ${hhmm(ext.ultima)}`);
    if (c.activo && ext && ext.proximo) partes.push(`próximo ${hhmm(ext.proximo)}`);
    partes.push(c.activo ? `cada ${c.intervaloMin ?? "—"} min` : "automática pausada");
    if (ext && ext.version) partes.push(`v${ext.version}`);
    $("syncMeta").textContent = partes.join(" · ");
    const corriendo = !!(ext && ext.corriendo && !ext.candadoVencido);
    setPill($("syncEstado"), corriendo ? "info" : ext && ext.ultimoError ? "bad" : c.activo ? "good" : "warn",
            corriendo ? "sincronizando" : ext && ext.ultimoError ? "con error" : c.activo ? "automática" : "pausada");
  }

  async function pintar() {
    if (pintando) return;
    pintando = true;
    try {
      let ext = null;
      try { ext = await msg({ tipo: "estado" }); } catch (_) { ext = null; }
      // sólo loopback: el popup también abre esta URL en una pestaña
      if (ext && ext.cfg && ext.cfg.botUrl) botUrl = globalThis.Plantillas.urlBotValida(ext.cfg.botUrl) || botUrl;
      pintarExtension(ext);
      pintarLog(ext);

      let salud = null;
      let fallas = null;
      try {
        [salud, fallas] = await Promise.all([getJson("/estado"), getJson("/fallas")]);
      } catch (_) {
        salud = null;
      }

      // banner: lo más grave primero
      const c = (ext && ext.cfg) || {};
      if (!salud) {
        banner("bad", `El bot local no responde en ${botUrl}. ¿Está corriendo? (iniciar_bot.bat)`);
      } else if (!c.centralityBase) {
        banner("warn", "Falta configurar la dirección de Centrality: abre Configuración.");
      } else if (ext && !ext.permisoHost) {
        banner("warn", "Falta el permiso para Centrality: abre Configuración y pulsa Guardar.");
      } else if (ext && ext.corriendo && !ext.candadoVencido) {
        banner("info", "Sincronizando con Centrality…");
      } else if (ext && ext.ultimoError) {
        banner(/SESION_CADUCADA|SIN_PESTANA/.test(ext.ultimoError) ? "bad" : "warn", textoError(ext.ultimoError));
      } else {
        banner("", "");
      }

      if (!salud) {
        setPill($("estadoGlobal"), "bad", "bot no responde");
        $("kFallas").textContent = "—";
        $("kCasos").textContent = "—";
        limpiar($("fuentesBot"));
        limpiar($("canales"));
        const root = $("fallas");
        limpiar(root);
        root.appendChild(nodo("div", "empty", "Sin conexión con el bot"));
        return;
      }
      $("kFallas").textContent = String(salud.fallas_activas ?? "—");
      $("kCasos").textContent = String(salud.casos_seguidos ?? "—");
      pintarFuentesBot(salud);
      pintarCanales(salud);
      pintarFallas(fallas);
      const degradado = (ext && ext.ultimoError) || Object.values(salud.fuentes || {}).some((f) => f && f.fresca === false);
      setPill($("estadoGlobal"), degradado ? "warn" : "good", degradado ? "degradado" : "operativo");
    } catch (e) {
      setPill($("estadoGlobal"), "bad", "error");
      banner("bad", `No se pudo armar el resumen: ${String(e && e.message)}`);
    } finally {
      pintando = false;
    }
  }

  $("btnSync").addEventListener("click", async () => {
    const b = $("btnSync");
    b.disabled = true;
    b.textContent = "Sincronizando…";
    banner("info", "Sincronizando con Centrality… (un reporte grande puede tardar más de un minuto)");
    try {
      const r = await msg({ tipo: "sincronizar" });
      if (r && r.omitido) banner("warn", "Ya hay una sincronización en curso; ésta se omitió.");
    } catch (e) {
      banner("bad", `No se pudo sincronizar: ${String(e && e.message)}`);
    } finally {
      b.disabled = false;
      b.textContent = "Sincronizar ahora";
      await pintar();
    }
  });

  $("btnDiag").addEventListener("click", async () => {
    const card = $("cardDiag");
    const root = $("diag");
    const b = $("btnDiag");
    card.classList.remove("oculto");
    limpiar(root);
    root.appendChild(nodo("div", "empty", "Probando cada eslabón…"));
    b.disabled = true;
    try {
      const r = await msg({ tipo: "diagnostico" });
      limpiar(root);
      for (const p of (r && r.pasos) || []) {
        const row = nodo("div", "row");
        const name = nodo("div", "name");
        name.appendChild(nodo("b", "", `${p.ok ? "✅" : "❌"} ${p.n}`));
        name.appendChild(nodo("small", "", p.det || ""));
        const right = nodo("div", "right");
        right.appendChild(nodo("span", `pill ${p.ok ? "good" : "bad"}`, p.ok ? "OK" : "FALLA"));
        row.append(name, right);
        root.appendChild(row);
      }
      if (r && r.error) root.appendChild(nodo("div", "banner bad", r.error));
    } catch (e) {
      limpiar(root);
      root.appendChild(nodo("div", "banner bad", `Diagnóstico falló: ${String(e && e.message)}`));
    } finally {
      b.disabled = false;
    }
  });

  $("btnOpts").addEventListener("click", () => chrome.runtime.openOptionsPage());
  $("btnMonitor").addEventListener("click", () => chrome.tabs.create({ url: `${botUrl}/` }));

  pintar();
  setInterval(pintar, 5000);
})();
