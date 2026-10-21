/**
 * Página de opciones.
 *
 * Todo texto que viene de datos (respuestas del worker, nombres de reportes,
 * OUs, errores) se inserta con textContent, nunca con innerHTML: el listado
 * de Centrality es HTML de un tercero y un nombre de reporte podría traer
 * marcado.
 */
(() => {
  "use strict";

  const P = globalThis.Plantillas;
  const $ = (id) => document.getElementById(id);
  const ETIQUETAS = { casos: "Informe de casos", reclamos: "Reclamos", abiertas: "Tareas abiertas (432)" };
  let ultimoDescubrir = null;

  function nodo(tag, cls, texto) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (texto != null) el.textContent = String(texto);
    return el;
  }

  function limpiar(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  /** sendMessage con un reintento: si el worker justo se estaba reiniciando,
   *  el primer intento falla con "Receiving end does not exist". */
  async function msg(m, intentos = 2) {
    for (let i = 0; ; i++) {
      try {
        return await chrome.runtime.sendMessage(m);
      } catch (e) {
        const sinReceptor = /Receiving end does not exist|Could not establish connection/i.test(String(e && e.message));
        if (!sinReceptor || i + 1 >= intentos) throw e;
        await new Promise((r) => setTimeout(r, 400));
      }
    }
  }

  function aviso(cls, texto) {
    const root = $("aviso");
    limpiar(root);
    if (texto) root.appendChild(nodo("div", `banner ${cls}`, texto));
  }

  function estadoGuardado(cls, texto) {
    const el = $("estadoGuardado");
    el.className = `pill ${cls || ""}`.trim();
    el.textContent = texto;
  }

  function marcar(id, mal) {
    const el = $(`f-${id}`);
    if (el) el.classList.toggle("error", !!mal);
  }

  // -------------------------------------------------------------------
  // Fuentes
  // -------------------------------------------------------------------

  function filaFuente(f) {
    const tr = document.createElement("tr");
    tr.dataset.clave = f.clave;

    const tdNombre = nodo("td");
    tdNombre.appendChild(nodo("b", "", ETIQUETAS[f.clave] || f.clave));
    tdNombre.appendChild(nodo("div", "tiny mono", f.clave));

    const tdActiva = nodo("td");
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.className = "f-activo";
    chk.checked = !!f.activo;
    tdActiva.appendChild(chk);

    const tdId = nodo("td");
    const id = document.createElement("input");
    id.type = "text";
    id.className = "f-reporteId mono";
    id.inputMode = "numeric";
    id.placeholder = "ID";
    id.value = f.reporteId == null ? "" : String(f.reporteId);
    id.style.width = "80px";
    tdId.appendChild(id);

    const tdParams = nodo("td");
    const params = document.createElement("input");
    params.type = "text";
    params.className = "f-params mono";
    params.spellcheck = false;
    params.value = f.params == null ? "" : String(f.params);
    tdParams.appendChild(params);

    const tdVentana = nodo("td");
    const ventana = document.createElement("input");
    ventana.type = "number";
    ventana.className = "f-ventanaHoras";
    ventana.min = "0";
    ventana.max = "744";
    ventana.step = "1";
    ventana.value = String(f.ventanaHoras ?? 0);
    ventana.style.width = "80px";
    tdVentana.appendChild(ventana);

    tr.append(tdNombre, tdActiva, tdId, tdParams, tdVentana);
    for (const el of [chk, id, params, ventana]) {
      el.addEventListener("input", () => estadoGuardado("warn", "cambios sin guardar"));
    }
    return tr;
  }

  function pintarFuentes(fuentes) {
    const tb = $("fuentes");
    limpiar(tb);
    for (const f of fuentes || []) tb.appendChild(filaFuente(f));
  }

  function leerFuentes(errores) {
    const out = [];
    for (const tr of $("fuentes").querySelectorAll("tr")) {
      const clave = tr.dataset.clave;
      const activo = tr.querySelector(".f-activo").checked;
      const reporteId = tr.querySelector(".f-reporteId").value.trim();
      const params = tr.querySelector(".f-params").value.trim();
      const ventanaHoras = Number(tr.querySelector(".f-ventanaHoras").value || 0);
      const tdId = tr.querySelector(".f-reporteId").parentElement;
      const tdV = tr.querySelector(".f-ventanaHoras").parentElement;
      const idMal = (reporteId && !/^\d{1,7}$/.test(reporteId)) || (activo && !reporteId);
      const vMal = !Number.isInteger(ventanaHoras) || ventanaHoras < 0 || ventanaHoras > 744;
      // {desde}/{hasta} sin ventana dejaría la plantilla sin resolver
      const sinVentana = /\{(desde|hasta)\}/.test(params) && ventanaHoras === 0 && activo;
      tdId.classList.toggle("error", !!idMal);
      tdV.classList.toggle("error", vMal || sinVentana);
      if (idMal) errores.push(`${clave}: ${activo && !reporteId ? "activa sin ID de reporte" : "ID de reporte no numérico"}`);
      if (vMal) errores.push(`${clave}: ventana en horas inválida`);
      if (sinVentana) errores.push(`${clave}: usa {desde}/{hasta} con ventana 0`);
      out.push({ clave, activo, reporteId, params, ventanaHoras });
    }
    return out;
  }

  // -------------------------------------------------------------------
  // Formulario
  // -------------------------------------------------------------------

  function leerFormulario() {
    const errores = [];
    const baseCruda = $("centralityBase").value.trim();
    const base = P.origenDe(baseCruda);
    const botUrl = P.urlBotValida($("botUrl").value.trim());
    const intervaloMin = Number($("intervaloMin").value);
    const timeoutS = Number($("timeoutS").value);
    const maxFilas = Number($("maxFilasGrilla").value);
    const ou = $("ou").value.trim();

    const chequeos = {
      centralityBase: !baseCruda || !!base,
      botUrl: !!botUrl,
      intervaloMin: Number.isInteger(intervaloMin) && intervaloMin >= 1 && intervaloMin <= 120,
      timeoutS: Number.isFinite(timeoutS) && timeoutS >= 30 && timeoutS <= 240,
      maxFilasGrilla: Number.isInteger(maxFilas) && maxFilas >= 50 && maxFilas <= 20000,
      ou: !ou || /^[\w.-]{1,40}$/.test(ou),
    };
    const textos = {
      centralityBase: "dirección base de Centrality inválida (https://host:puerto)",
      botUrl: "la URL del bot debe ser http://127.0.0.1:<puerto> o http://localhost:<puerto>",
      intervaloMin: "intervalo entre 1 y 120 min",
      timeoutS: "tiempo máximo entre 30 y 240 s",
      maxFilasGrilla: "máximo de filas entre 50 y 20000",
      ou: "OU con caracteres raros",
    };
    for (const [k, ok] of Object.entries(chequeos)) {
      marcar(k, !ok);
      if (!ok) errores.push(textos[k]);
    }
    const fuentes = leerFuentes(errores);
    if (fuentes.some((f) => f.activo && /\{ou\}/.test(f.params)) && !ou) {
      marcar("ou", true);
      errores.push("una fuente activa usa {ou} y la OU está vacía");
    }
    return {
      errores,
      cfg: {
        centralityBase: base,
        botUrl,
        rutaReportes: $("rutaReportes").value.trim() || "/backend/saesa/reports.php",
        token: $("token").value.trim(),
        activo: $("activo").value === "true",
        intervaloMin,
        ou,
        timeoutMs: Math.round(timeoutS * 1000),
        capturarGrilla: $("capturarGrilla").checked,
        maxFilasGrilla: maxFilas,
        fuentes,
      },
    };
  }

  function pintarFormulario(c) {
    $("centralityBase").value = c.centralityBase || "";
    $("botUrl").value = c.botUrl || "http://127.0.0.1:8771";
    $("rutaReportes").value = c.rutaReportes || "/backend/saesa/reports.php";
    $("token").value = c.token || "";
    $("activo").value = String(c.activo !== false);
    $("intervaloMin").value = String(c.intervaloMin ?? 5);
    $("ou").value = c.ou == null ? "" : String(c.ou);
    $("timeoutS").value = String(Math.round((c.timeoutMs ?? 240000) / 1000));
    $("capturarGrilla").checked = c.capturarGrilla !== false;
    $("maxFilasGrilla").value = String(c.maxFilasGrilla ?? 3000);
    pintarFuentes(c.fuentes || []);
  }

  async function cargar() {
    try {
      const e = await msg({ tipo: "estado" });
      pintarFormulario((e && e.cfg) || {});
      if (e && e.cfg && e.cfg.centralityBase && !e.permisoHost) {
        aviso("warn", "Falta el permiso para la dirección de Centrality: pulsa Guardar y acepta el permiso de Chrome.");
      }
    } catch (err) {
      aviso("bad", `No se pudo leer la configuración: ${String(err && err.message)}`);
    }
  }

  async function guardarCfg(cfg, concedido) {
    try {
      const r = await msg({ tipo: "guardar", cfg });
      if (!r || !r.ok) throw new Error((r && r.error) || "sin respuesta del worker");
      pintarFormulario(r.cfg || cfg);
      if (cfg.centralityBase && !concedido) {
        estadoGuardado("warn", "guardado sin permiso");
        aviso("warn", "Configuración guardada, pero Chrome NO concedió el permiso para Centrality: " +
                      "sin él la extensión no puede leer reportes. Vuelve a pulsar Guardar y acepta.");
      } else {
        estadoGuardado("good", "guardado");
        aviso("good", "Configuración guardada y alarma reprogramada.");
      }
    } catch (err) {
      estadoGuardado("bad", "error");
      aviso("bad", `No se pudo guardar: ${String(err && err.message)}`);
    }
  }

  $("guardar").addEventListener("click", () => {
    const { cfg, errores } = leerFormulario();
    if (errores.length) {
      estadoGuardado("bad", "revisa los campos");
      aviso("bad", `No se guardó: ${errores.join("; ")}.`);
      return;
    }
    $("guardar").disabled = true;
    estadoGuardado("info", "guardando…");
    // permissions.request SIN ningún await antes: Chrome sólo lo acepta
    // dentro del gesto del usuario, y un await previo puede perderlo.
    const patron = P.patronHost(cfg.centralityBase);
    let pedido;
    try {
      pedido = patron ? chrome.permissions.request({ origins: [patron] }) : Promise.resolve(true);
    } catch (err) {
      pedido = Promise.reject(err);
    }
    pedido
      .then((concedido) => guardarCfg(cfg, !!concedido), (err) => {
        aviso("warn", `Chrome no pudo pedir el permiso: ${String(err && err.message)}`);
        return guardarCfg(cfg, false);
      })
      .finally(() => { $("guardar").disabled = false; });
  });

  // -------------------------------------------------------------------
  // Descubrir
  // -------------------------------------------------------------------

  function pintarLista(root, items, filtro) {
    limpiar(root);
    const f = String(filtro || "").trim().toLowerCase();
    const vis = (items || []).filter((x) => !f || String(x.id).includes(f) ||
                                            String(x.nombre || "").toLowerCase().includes(f));
    if (!vis.length) {
      root.appendChild(nodo("div", "empty", items && items.length ? "Nada coincide con el filtro" : "No se encontró nada"));
      return;
    }
    for (const x of vis.slice(0, 500)) {
      const row = nodo("div", "row");
      const name = nodo("div", "name");
      name.appendChild(nodo("b", "", x.nombre || "(sin nombre)"));
      row.appendChild(name);
      const right = nodo("div", "right");
      right.appendChild(nodo("span", "pill mono", x.id));
      row.appendChild(right);
      root.appendChild(row);
    }
  }

  function pintarDescubrir() {
    const r = ultimoDescubrir || {};
    const filtro = $("filtro").value;
    pintarLista($("listaReportes"), r.reportes || [], filtro);
    pintarLista($("listaOus"), r.ous || [], filtro);
  }

  $("descubrir").addEventListener("click", async () => {
    const b = $("descubrir");
    b.disabled = true;
    b.textContent = "Buscando…";
    try {
      const r = await msg({ tipo: "descubrir" });
      if (!r || !r.ok) throw new Error((r && r.error) || "sin respuesta");
      ultimoDescubrir = r;
      $("cardDescubrir").classList.remove("oculto");
      const meta = `${(r.reportes || []).length} reportes · ${(r.ous || []).length} OUs` +
                   (r.ousDesde ? ` (OUs desde ${r.ousDesde})` : "") + (r.titulo ? ` · página: ${r.titulo}` : "");
      $("descubrirMeta").textContent = meta;
      pintarDescubrir();
      aviso("info", "Busca el «informe de casos en periodos» y la bandeja de reclamos; copia sus ID en la tabla de fuentes.");
    } catch (err) {
      aviso("bad", `No se pudo descubrir: ${String(err && err.message)}`);
    } finally {
      b.disabled = false;
      b.textContent = "Descubrir reportes";
    }
  });

  $("filtro").addEventListener("input", pintarDescubrir);

  for (const id of ["centralityBase", "botUrl", "rutaReportes", "token", "activo", "intervaloMin", "ou",
                    "timeoutS", "capturarGrilla", "maxFilasGrilla"]) {
    $(id).addEventListener("input", () => estadoGuardado("warn", "cambios sin guardar"));
    $(id).addEventListener("change", () => estadoGuardado("warn", "cambios sin guardar"));
  }

  cargar();
})();
