"""Tests de mensajes.py (SPEC §4.7): forma, escape HTML, límite de largo y texto plano."""

from __future__ import annotations

import copy
import re
import unittest
from datetime import datetime, timedelta

from bot_fallas import mensajes
from bot_fallas.mensajes import (EMOJI_NIVEL, LIMITE_TELEGRAM, mensaje_detalle, mensaje_evento,
                                 mensaje_resumen, mensaje_salud)
from bot_fallas.modelo import Calle, Evento, Falla

AHORA = datetime(2026, 10, 21, 14, 44)

# Sub-árbol de DEFAULTS (SPEC §4.10) que usan los mensajes.
_DEFAULTS = {"avisos": {"max_calles_mensaje": 15}}


def cfg_prueba(**avisos) -> dict:
    cfg = copy.deepcopy(_DEFAULTS)
    cfg["avisos"].update(avisos)
    return cfg


CFG = cfg_prueba()

_CALLES_EJEMPLO = [("AV. MATTA", 12), ("LOS CARRERA", 5), ("O'HIGGINS", 4), ("BULNES", 3),
                   ("RAMIREZ", 2), ("MACKENNA", 2), ("COCHRANE", 1), ("ZENTENO", 1)]


def falla_ejemplo(**kw) -> Falla:
    """La falla del ejemplo del SPEC (datos sintéticos)."""
    f = Falla(
        caso="40808515", clientes=1240, clientes_fuente="informe_casos", clientes_reclamando=30,
        reclamos=34, calles=[Calle(n, "Osorno", r) for n, r in _CALLES_EJEMPLO],
        calles_fuente="reclamos", comunas=["Osorno", "Puerto Octay"],
        alimentadores=["A260 Pilauco (SE Osorno)"], alimentadores_cod=["A260"],
        motivo="Sin Suministro", peligro=True, inicio=datetime(2026, 10, 21, 14, 32),
        fuentes=["casos", "reclamos"], nivel="alta",
    )
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def calles(n: int, largo: int = 10, comuna: str = "Osorno") -> list[Calle]:
    return [Calle(f"CALLE {i:03d} " + "X" * max(0, largo - 10), comuna, 1) for i in range(n)]


_ETIQUETA = re.compile(r"</?(b|i|code)>")


class Formato(unittest.TestCase):
    def assertHtmlSano(self, html: str):
        """Etiquetas balanceadas POR LÍNEA, sin '<' sueltos ni entidades partidas."""
        self.assertLessEqual(len(html), LIMITE_TELEGRAM)
        self.assertLessEqual(len(html.encode("utf-16-le")) // 2, LIMITE_TELEGRAM)
        for linea in html.split("\n"):
            for t in ("b", "i", "code"):
                self.assertEqual(linea.count(f"<{t}>"), linea.count(f"</{t}>"), linea)
            resto = _ETIQUETA.sub("", linea)
            self.assertNotIn("<", resto, linea)
            self.assertNotIn(">", resto, linea)
            for m in re.finditer("&", resto):
                self.assertRegex(resto[m.start():], r"^&(amp|lt|gt);", linea)

    def assertTextoPlano(self, texto: str):
        self.assertIsNone(_ETIQUETA.search(texto), texto)
        self.assertNotIn("&amp;", texto)

    def lineas(self, m):
        return m.html.split("\n")


class TestNueva(Formato):
    def test_forma_del_ejemplo(self):
        f = falla_ejemplo()
        m = mensaje_evento(Evento("nueva", f.caso, falla=f, ts=AHORA), CFG, AHORA)
        L = self.lineas(m)
        self.assertEqual(L[0], "🔴 <b>FALLA NUEVA</b> · Caso <code>40808515</code>")
        self.assertEqual(L[1], "👥 <b>1.240 clientes</b> afectados · nivel ALTA")
        self.assertEqual(L[2], "📍 Osorno, Puerto Octay")
        self.assertEqual(L[3], "⚡ A260 Pilauco (SE Osorno)")
        self.assertEqual(L[4], "🕒 Inicio 21-10 14:32 (hace 12 min)")
        self.assertEqual(L[5], "📝 Sin Suministro · ⚠️ <b>CON PELIGRO</b>")
        self.assertEqual(L[6], "🛣️ <b>Calles (8)</b> — según reclamos:")
        self.assertEqual(L[7], "• AV. MATTA — Osorno (12)")
        self.assertEqual(L[8], "• LOS CARRERA — Osorno (5)")
        self.assertEqual(L[-1], "<i>Fuente: informe de casos + reclamos · 14:44</i>")
        self.assertEqual(m.titulo, "Falla nueva · Caso 40808515 · 1.240 clientes")
        self.assertEqual((m.tipo, m.caso), ("nueva", "40808515"))
        self.assertHtmlSano(m.html)
        self.assertTextoPlano(m.texto)
        self.assertIn("👥 1.240 clientes afectados · nivel ALTA", m.texto.split("\n"))

    def test_y_n_calles_mas(self):
        f = falla_ejemplo(calles=calles(20))
        m = mensaje_evento(Evento("nueva", f.caso, falla=f), CFG, AHORA)
        self.assertEqual(sum(1 for x in self.lineas(m) if x.startswith("• ")), 15)
        self.assertIn("…y 5 calles más", self.lineas(m))
        self.assertIn("🛣️ <b>Calles (20)</b>", m.html)
        m3 = mensaje_evento(Evento("nueva", f.caso, falla=falla_ejemplo()),
                            cfg_prueba(max_calles_mensaje=3), AHORA)
        self.assertIn("…y 5 calles más", self.lineas(m3))
        m7 = mensaje_evento(Evento("nueva", f.caso, falla=falla_ejemplo()),
                            cfg_prueba(max_calles_mensaje=7), AHORA)
        self.assertIn("…y 1 calle más", self.lineas(m7))

    def test_fmt_miles_y_singular(self):
        m = mensaje_evento(Evento("nueva", falla=falla_ejemplo(clientes=1234567, nivel="masivo")),
                           CFG, AHORA)
        self.assertIn("<b>1.234.567 clientes</b>", m.html)
        self.assertTrue(m.html.startswith("🟣 "))
        m1 = mensaje_evento(Evento("nueva", falla=falla_ejemplo(clientes=1, nivel="baja")), CFG, AHORA)
        self.assertIn("👥 <b>1 cliente</b> afectado · nivel BAJA", m1.html)

    def test_clientes_por_nis(self):
        f = falla_ejemplo(clientes=5, clientes_fuente="nis_reclamos", nivel="baja")
        m = mensaje_evento(Evento("nueva", falla=f), CFG, AHORA)
        self.assertIn("👥 <b>≥ 5 clientes</b> (con reclamo; el DMS aún no informa total)", m.html)
        self.assertNotIn("nivel", m.html)          # el nivel de un mínimo engaña
        self.assertEqual(m.titulo, "Falla nueva · Caso 40808515 · ≥ 5 clientes")

    def test_clientes_sin_dato(self):
        f = falla_ejemplo(clientes=None, clientes_fuente="", nivel="")
        m = mensaje_evento(Evento("nueva", falla=f), CFG, AHORA)
        self.assertIn("👥 Clientes: sin dato", self.lineas(m))
        self.assertTrue(m.html.startswith(EMOJI_NIVEL[""]))

    def test_calles_de_maestro_sed(self):
        f = falla_ejemplo(calles_fuente="maestro_sed",
                          calles=[Calle("AV. MATTA", "Osorno", 2, 120, "maestro_sed"),
                                  Calle("BULNES", "Osorno", 0, 1, "maestro_sed")])
        m = mensaje_evento(Evento("nueva", falla=f), CFG, AHORA)
        self.assertIn("🛣️ <b>Calles (2)</b> — según maestro de red (SED):", m.html)
        self.assertIn("• AV. MATTA — Osorno (120 clientes)", self.lineas(m))
        self.assertIn("• BULNES — Osorno (1 cliente)", self.lineas(m))

    def test_calles_de_maestro_alimentador(self):
        f = falla_ejemplo(calles_fuente="maestro_alimentador")
        m = mensaje_evento(Evento("nueva", falla=f), CFG, AHORA)
        self.assertIn("— alimentador completo según maestro (puede sobrestimar si la falla es parcial):",
                      m.html)

    def test_sin_calles(self):
        m = mensaje_evento(Evento("nueva", falla=falla_ejemplo(calles=[], sin_calle=3)), CFG, AHORA)
        self.assertIn("🛣️ Calles: sin dirección utilizable aún (3 reclamos sin calle)", self.lineas(m))
        m0 = mensaje_evento(Evento("nueva", falla=falla_ejemplo(calles=[], sin_calle=0)), CFG, AHORA)
        self.assertIn("🛣️ Calles: sin dirección utilizable aún", self.lineas(m0))

    def test_programada_y_sin_peligro(self):
        f = falla_ejemplo(programada=True, peligro=False)
        m = mensaje_evento(Evento("nueva", falla=f), CFG, AHORA)
        self.assertTrue(m.html.startswith("🗓️ PROGRAMADA"))
        self.assertNotIn("PELIGRO", m.html)
        self.assertIn("📝 Sin Suministro", self.lineas(m))

    def test_omite_lineas_sin_dato(self):
        f = falla_ejemplo(comunas=[], alimentadores=[], alimentadores_cod=[], inicio=None,
                          motivo="", peligro=False)
        m = mensaje_evento(Evento("nueva", falla=f), CFG, AHORA)
        for prefijo in ("📍", "⚡", "🕒", "📝"):
            self.assertFalse(any(x.startswith(prefijo) for x in self.lineas(m)), prefijo)

    def test_reabierta(self):
        m = mensaje_evento(Evento("nueva", falla=falla_ejemplo(), motivo="reapertura"), CFG, AHORA)
        self.assertIn("<b>FALLA REABIERTA</b>", m.html)
        self.assertTrue(m.titulo.startswith("Falla reabierta"))


class TestEscapeYLargo(Formato):
    def test_escape_html_de_valores(self):
        f = falla_ejemplo(caso="4080<8515", motivo="Sin <Suministro> & más",
                          comunas=["Osorno & <Cía>"],
                          calles=[Calle("LOS AROMOS & <CIA>", "Osorno", 2)])
        m = mensaje_evento(Evento("nueva", falla=f), CFG, AHORA)
        self.assertIn("• LOS AROMOS &amp; &lt;CIA&gt; — Osorno (2)", self.lineas(m))
        self.assertIn("<code>4080&lt;8515</code>", m.html)
        self.assertIn("Sin &lt;Suministro&gt; &amp; más", m.html)
        self.assertNotIn("<CIA>", m.html)
        self.assertHtmlSano(m.html)
        # el texto plano lleva el valor tal cual (no es HTML)
        self.assertIn("• LOS AROMOS & <CIA> — Osorno (2)", m.texto.split("\n"))
        self.assertIsNone(_ETIQUETA.search(m.texto))

    def test_limite_4096_recorta_calles_y_conserva_pie(self):
        f = falla_ejemplo(calles=calles(300, largo=58, comuna="San Juan de la Costa & <X>" * 2))
        m = mensaje_detalle(f, CFG, AHORA)
        self.assertHtmlSano(m.html)
        L = self.lineas(m)
        mostradas = sum(1 for x in L if x.startswith("• "))
        self.assertLess(mostradas, 40)                        # no cupieron las 40
        self.assertIn(f"…y {300 - mostradas} calles más", L)
        self.assertTrue(L[-1].startswith("<i>Fuente:"))       # el pie sobrevive
        self.assertTrue(L[0].startswith("🔴 <b>DETALLE</b>"))

    def test_limite_con_max_calles_grande_en_config(self):
        f = falla_ejemplo(calles=calles(500, largo=60))
        m = mensaje_evento(Evento("nueva", falla=f), cfg_prueba(max_calles_mensaje=1000), AHORA)
        self.assertHtmlSano(m.html)
        self.assertRegex(m.html, r"…y \d+ calles más")

    def test_nombres_largos_se_acotan(self):
        f = falla_ejemplo(calles=[Calle("A" * 5000, "B" * 5000, 1)], motivo="M" * 5000)
        m = mensaje_evento(Evento("nueva", falla=f), CFG, AHORA)
        self.assertHtmlSano(m.html)
        self.assertTrue(self.lineas(m)[-1].startswith("<i>Fuente:"))

    def test_recorte_extremo_no_corta_etiquetas_ni_entidades(self):
        # Última defensa: aun si la cabeza sola no cupiera, nada queda a medias.
        enorme = mensajes._linea(mensajes._b("&<" * 3000))
        html, texto = mensajes._componer([mensajes._linea("título"), enorme], [], [], 0, mensajes._mas)
        self.assertHtmlSano(html)
        self.assertTrue(html.endswith("…"))
        html2, _ = mensajes._componer([enorme], [], [], 0, mensajes._mas)
        self.assertHtmlSano(html2)
        self.assertTrue(html2.endswith("…"))

    def test_emojis_cuentan_doble(self):
        # Telegram mide en UTF-16: 2100 emojis = 4200 unidades aunque len() diga 2100.
        html, _ = mensajes._componer([mensajes._linea("🟣" * 2100)], [], [], 0, mensajes._mas)
        self.assertLessEqual(len(html.encode("utf-16-le")) // 2, LIMITE_TELEGRAM)


class TestCambios(Formato):
    def test_actualizada(self):
        f = falla_ejemplo(clientes=2300, nivel="masivo")
        ev = Evento("actualizada", f.caso, falla=f,
                    anterior={"clientes": 1240, "nivel": "alta", "calles": [],
                              "nuevas_calles": ["AV. MATTA", "LOS CARRERA", "O'HIGGINS"]})
        m = mensaje_evento(ev, CFG, AHORA)
        L = self.lineas(m)
        self.assertEqual(L[0], "🟣 <b>FALLA ACTUALIZADA</b> · Caso <code>40808515</code>")
        self.assertEqual(L[1], "👥 <b>1.240 → 2.300 clientes</b> (+1.060) · nivel ALTA → MASIVO")
        self.assertEqual(L[2], "🆕 Calles nuevas: AV. MATTA, LOS CARRERA, O'HIGGINS")
        self.assertIn("🛣️ <b>Calles (8)</b> — según reclamos:", L)
        self.assertEqual(m.titulo, "Falla actualizada · Caso 40808515 · 1.240 → 2.300 clientes")
        self.assertHtmlSano(m.html)
        self.assertTextoPlano(m.texto)

    def test_actualizada_sin_cambio_de_clientes(self):
        f = falla_ejemplo()
        ev = Evento("actualizada", f.caso, falla=f,
                    anterior={"clientes": 1240, "nivel": "alta", "nuevas_calles": []})
        L = self.lineas(mensaje_evento(ev, CFG, AHORA))
        self.assertEqual(L[1], "👥 <b>1.240 clientes</b> afectados · nivel ALTA")
        self.assertFalse(any(x.startswith("🆕") for x in L))

    def test_calles_nuevas_max_10(self):
        f = falla_ejemplo()
        nuevas = [f"CALLE {i}" for i in range(13)]
        ev = Evento("actualizada", f.caso, falla=f,
                    anterior={"clientes": 1240, "nivel": "alta", "nuevas_calles": nuevas})
        m = mensaje_evento(ev, CFG, AHORA)
        self.assertIn("CALLE 9 y 3 más", m.html)
        self.assertNotIn("CALLE 10", m.html)

    def test_minimo_por_nis_no_muestra_baja(self):
        f = falla_ejemplo(clientes=5, clientes_fuente="nis_reclamos", nivel="baja")
        ev = Evento("actualizada", f.caso, falla=f,
                    anterior={"clientes": 1240, "nivel": "alta", "nuevas_calles": ["X", "Y", "Z"]})
        m = mensaje_evento(ev, CFG, AHORA)
        self.assertNotIn("→", self.lineas(m)[1])
        self.assertIn("≥ 5 clientes", m.html)

    def test_reposicion_parcial(self):
        f = falla_ejemplo(clientes=800, nivel="alta")
        ev = Evento("reposicion_parcial", f.caso, falla=f,
                    anterior={"clientes": 2300, "nivel": "masivo", "nuevas_calles": []})
        m = mensaje_evento(ev, CFG, AHORA)
        L = self.lineas(m)
        self.assertEqual(L[0], "🔵 <b>REPOSICIÓN PARCIAL</b> · Caso <code>40808515</code>")
        self.assertEqual(L[1], "👥 <b>2.300 → 800 clientes</b> (−1.500) · nivel MASIVO → ALTA")
        self.assertEqual(m.titulo, "Reposición parcial · Caso 40808515 · 2.300 → 800 clientes")
        self.assertHtmlSano(m.html)
        self.assertTextoPlano(m.texto)


class TestNormalizada(Formato):
    def test_cierre_explicito_con_duracion(self):
        f = falla_ejemplo(clientes=0, abierta=False, cierre=datetime(2026, 10, 21, 16, 46))
        ev = Evento("normalizada", f.caso, falla=f, motivo="cierre informado por informe de casos",
                    anterior={"clientes": 2300, "nivel": "masivo", "max_clientes": 2300})
        m = mensaje_evento(ev, CFG, datetime(2026, 10, 21, 16, 50))
        L = self.lineas(m)
        self.assertEqual(L[0], "✅ <b>FALLA NORMALIZADA</b> · Caso <code>40808515</code>")
        self.assertEqual(L[1], "⏱️ Duración 2 h 14 min · inicio 21-10 14:32")
        self.assertEqual(L[2], "👥 Máx. clientes: 2.300")
        self.assertIn("📍 Osorno, Puerto Octay", L)
        self.assertEqual(sum(1 for x in L if x.startswith("• ")), 5)    # top 5
        self.assertIn("…y 3 calles más", L)
        self.assertEqual(L[-1], "<i>cierre informado por informe de casos · 16:50</i>")
        self.assertEqual(m.titulo, "Falla normalizada · Caso 40808515 · 2 h 14 min")
        self.assertHtmlSano(m.html)
        self.assertTextoPlano(m.texto)

    def test_por_ausencia_usa_ahora(self):
        f = falla_ejemplo()     # sin cierre: dura hasta ahora
        ev = Evento("normalizada", f.caso, falla=f,
                    motivo="dejó de aparecer en informe de casos hace 20 min (no hay cierre explícito)",
                    anterior={"clientes": 1240, "max_clientes": None})
        m = mensaje_evento(ev, CFG, datetime(2026, 10, 21, 15, 32))
        self.assertIn("⏱️ Duración 1 h · inicio 21-10 14:32", self.lineas(m))
        self.assertIn("👥 Máx. clientes: 1.240", self.lineas(m))
        self.assertIn("no hay cierre explícito", self.lineas(m)[-1])

    def test_sin_inicio_omite_duracion(self):
        f = falla_ejemplo(inicio=None)
        m = mensaje_evento(Evento("normalizada", f.caso, falla=f), CFG, AHORA)
        self.assertFalse(any("Duración" in x for x in self.lineas(m)))
        self.assertEqual(m.titulo, "Falla normalizada · Caso 40808515")


class TestResumenes(Formato):
    def test_resumen_inicio(self):
        a = falla_ejemplo(comunas=["Osorno"], inicio=AHORA - timedelta(minutes=12))
        b = falla_ejemplo(caso="40808516", clientes=60, nivel="media", comunas=[], calles=[],
                          peligro=False, inicio=None)
        m = mensaje_evento(Evento("resumen_inicio", fallas=[a, b]), CFG, AHORA)
        L = self.lineas(m)
        self.assertEqual(L[0], "🤖 <b>Bot de fallas iniciado</b> — 2 fallas activas (no se re-notifican):")
        self.assertEqual(L[1], "🔴 <code>40808515</code> · 1.240 cl · Osorno · 8 calles · hace 12 min ⚠️")
        self.assertEqual(L[2], "🟠 <code>40808516</code> · 60 cl")
        self.assertEqual(m.titulo, "Bot de fallas iniciado · 2 fallas activas")
        self.assertHtmlSano(m.html)
        self.assertTextoPlano(m.texto)

    def test_resumen_inicio_max_25(self):
        fallas = [falla_ejemplo(caso=str(40800000 + i)) for i in range(30)]
        m = mensaje_evento(Evento("resumen_inicio", fallas=fallas), CFG, AHORA)
        self.assertEqual(sum(1 for x in self.lineas(m) if "<code>408" in x), 25)
        self.assertIn("…y 5 más", self.lineas(m))

    def test_mensaje_resumen_vacio(self):
        m = mensaje_resumen([], CFG, AHORA)
        self.assertEqual(self.lineas(m)[0], "✅ Sin fallas activas que cumplan los filtros")
        self.assertTextoPlano(m.texto)

    def test_mensaje_resumen_ordena_y_titula(self):
        fallas = [falla_ejemplo(caso="1", clientes=10, nivel="baja"),
                  falla_ejemplo(caso="2", clientes=5000, nivel="masivo"),
                  falla_ejemplo(caso="3", clientes=None, clientes_fuente="", nivel="")]
        m = mensaje_resumen(fallas, CFG, AHORA, titulo="Fallas <CCD Sur>")
        L = self.lineas(m)
        self.assertTrue(L[0].startswith("📋 <b>Fallas &lt;CCD Sur&gt;</b> — 3"))
        self.assertEqual([re.search(r"<code>(\d+)</code>", x).group(1) for x in L[1:]], ["2", "1", "3"])
        self.assertIn("s/d cl", L[3])
        self.assertHtmlSano(m.html)

    def test_mensaje_resumen_muchas_no_pasa_el_limite(self):
        fallas = [falla_ejemplo(caso=f"{i:040d}", comunas=["C" * 40]) for i in range(200)]
        m = mensaje_resumen(fallas, CFG, AHORA)
        self.assertHtmlSano(m.html)
        self.assertRegex(m.html, r"…y \d+ más")

    def test_detalle(self):
        f = falla_ejemplo(calles=calles(50), empresa="SAESA", ccd="SUR", zonales=["OSORNO"])
        m = mensaje_detalle(f, CFG, AHORA)
        L = self.lineas(m)
        self.assertEqual(L[0], "🔴 <b>DETALLE</b> · Caso <code>40808515</code>")
        self.assertEqual(sum(1 for x in L if x.startswith("• ")), 40)
        self.assertIn("…y 10 calles más", L)
        self.assertIn("🏢 SAESA", L)
        self.assertIn("🧭 CCD SUR · Zonal OSORNO", L)
        self.assertTrue(m.titulo.startswith("Detalle · Caso 40808515"))
        self.assertHtmlSano(m.html)
        self.assertTextoPlano(m.texto)

    def test_detalle_cerrada(self):
        f = falla_ejemplo(abierta=False, cierre=datetime(2026, 10, 21, 16, 46))
        m = mensaje_detalle(f, CFG, AHORA)
        self.assertIn("✅ Cerrada 21-10 16:46 · duración 2 h 14 min", self.lineas(m))


class TestSalud(Formato):
    def test_tipos(self):
        for tipo, frase in (("sin_datos", "Sin datos de Centrality"),
                            ("datos_recuperados", "Datos de Centrality recuperados"),
                            ("sesion_caducada", "Sesión de Centrality caducada"),
                            ("prueba", "Mensaje de prueba")):
            m = mensaje_salud(tipo, "detalle <x> & y", AHORA)
            self.assertIn(f"<b>{frase}</b>", m.html)
            self.assertIn("detalle &lt;x&gt; &amp; y", m.html)
            self.assertEqual((m.titulo, m.tipo, m.caso), (frase, tipo, ""))
            self.assertHtmlSano(m.html)
            self.assertTextoPlano(m.texto)

    def test_evento_de_salud_delega(self):
        m = mensaje_evento(Evento("sesion_caducada", motivo="SESION_CADUCADA: login"), CFG, AHORA)
        self.assertIn("Sesión de Centrality caducada", m.html)
        self.assertIn("SESION_CADUCADA: login", m.texto)

    def test_tipo_desconocido_no_revienta(self):
        m = mensaje_evento(Evento("otra_cosa", caso="1", motivo="<?>"), CFG, AHORA)
        self.assertHtmlSano(m.html)
        self.assertIn("&lt;?&gt;", m.html)


class TestTextoPlano(Formato):
    def test_todos_los_tipos_sin_etiquetas(self):
        f = falla_ejemplo(calles=[Calle("A & <B>", "Osorno", 1)])
        eventos = [
            Evento("nueva", f.caso, falla=f),
            Evento("actualizada", f.caso, falla=f, anterior={"clientes": 100, "nivel": "media",
                                                             "nuevas_calles": ["A & <B>"]}),
            Evento("reposicion_parcial", f.caso, falla=f, anterior={"clientes": 5000}),
            Evento("normalizada", f.caso, falla=f, motivo="cierre"),
            Evento("resumen_inicio", fallas=[f]),
        ]
        for ev in eventos:
            m = mensaje_evento(ev, CFG, AHORA)
            self.assertTextoPlano(m.texto)
            self.assertNotIn("<", m.titulo)
            self.assertEqual(len(m.html.split("\n")), len(m.texto.split("\n")), ev.tipo)
            self.assertHtmlSano(m.html)


if __name__ == "__main__":
    unittest.main()
