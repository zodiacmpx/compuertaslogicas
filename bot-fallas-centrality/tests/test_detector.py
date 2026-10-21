"""Tests del detector (SPEC §4.6): cuándo se avisa, cuándo se difiere y cuándo se calla."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta

from bot_fallas.detector import Detector, _ccd
from bot_fallas.estado import EstadoBot, Seguimiento
from bot_fallas.modelo import Calle, Falla, Snapshot

T0 = datetime(2026, 10, 21, 14, 0, 0)
MIN = timedelta(minutes=1)

# Sub-árbol de DEFAULTS (SPEC §4.10) que usa el detector. Se replica aquí
# para no depender de config.py.
_DEFAULTS = {
    "filtros": {"min_clientes": 1, "min_reclamos_sin_clientes": 3, "incluir_programadas": False,
                "ccd": [], "zonales": [], "comunas": [], "empresas": [],
                "incluir_si_desconocido": True},
    "niveles": {"masivo": 1000, "alta": 300, "media": 50},
    "avisos": {"min_intervalo_actualizacion_min": 10, "delta_clientes_abs": 50,
               "delta_clientes_rel": 0.25, "min_calles_nuevas": 3,
               "minutos_ausencia_normalizar": 15, "evaluaciones_ausencia_normalizar": 2,
               "expiracion_sin_datos_min": 720, "retener_normalizadas_h": 24,
               "resumen_al_iniciar": True, "max_calles_mensaje": 15, "avisar_normalizadas": True,
               "avisar_reposicion_parcial": True, "alerta_sin_datos_min": 30,
               "recalculo_periodico_s": 120},
}


def cfg_prueba(**secciones) -> dict:
    cfg = copy.deepcopy(_DEFAULTS)
    for seccion, valores in secciones.items():
        cfg[seccion].update(valores)
    return cfg


def nivel(clientes):
    if clientes is None:
        return ""
    n = _DEFAULTS["niveles"]
    if clientes >= n["masivo"]:
        return "masivo"
    if clientes >= n["alta"]:
        return "alta"
    if clientes >= n["media"]:
        return "media"
    return "baja"


def falla(caso="40808515", clientes=120, fuente="informe_casos", calles=(),
          inicio=T0 - timedelta(hours=1), fuentes=("casos",), **kw) -> Falla:
    f = Falla(caso=caso, clientes=clientes,
              clientes_fuente=fuente if clientes is not None else "",
              calles=[Calle(n, "Osorno", 1) for n in calles],
              calles_fuente="reclamos" if calles else "",
              comunas=["Osorno"], inicio=inicio, fuentes=list(fuentes), nivel=nivel(clientes))
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def fd(*fallas) -> dict:
    return {f.caso: f for f in fallas}


def completas(*fuentes, ventana_desde=None, recibido=T0) -> dict:
    return {fu: Snapshot(fuente=fu, recibido=recibido, completa=True, ventana_desde=ventana_desde)
            for fu in fuentes}


CASOS = completas("casos")


class Base(unittest.TestCase):
    cfg_extra: dict = {}

    def setUp(self):
        self.estado = EstadoBot()
        self.det = Detector(self.estado, cfg_prueba(**self.cfg_extra))

    def iniciar(self, ahora=T0 - 30 * MIN):
        """Consume la primera evaluación con una foto completa sin fallas."""
        self.assertEqual(self.det.evaluar({}, CASOS, ahora), [])
        self.assertTrue(self.estado.iniciado)

    def ev(self, fallas, ahora, comp=CASOS):
        return self.det.evaluar(fallas, comp, ahora)

    def tipos(self, eventos):
        return [(e.tipo, e.caso) for e in eventos]

    def seg(self, caso="40808515") -> Seguimiento:
        return self.estado.casos[caso]


# --------------------------------------------------------------------------


class TestPrimeraEvaluacion(Base):
    def test_resumen_inicio_sin_nueva(self):
        a, b = falla("1", 120), falla("2", 1500)
        evs = self.ev(fd(a, b), T0)
        self.assertEqual([e.tipo for e in evs], ["resumen_inicio"])
        self.assertEqual([f.caso for f in evs[0].fallas], ["2", "1"])   # clientes desc
        self.assertTrue(self.estado.iniciado)
        self.assertTrue(self.seg("1").notificada and self.seg("2").notificada)
        self.assertEqual(self.seg("2").clientes_notificados, 1500)

    def test_segunda_evaluacion_no_renotifica(self):
        a = falla("1", 120)
        self.ev(fd(a), T0)
        self.assertEqual(self.ev(fd(a), T0 + 5 * MIN), [])

    def test_sin_datos_no_gasta_la_primera(self):
        # El timer puede recalcular antes de la primera ingesta: no debe
        # dejar el bot "iniciado" o la primera ingesta real avisaría todo.
        self.assertEqual(self.det.evaluar({}, {}, T0), [])
        self.assertFalse(self.estado.iniciado)
        evs = self.ev(fd(falla("1", 120)), T0 + MIN)
        self.assertEqual([e.tipo for e in evs], ["resumen_inicio"])

    def test_primera_con_foto_vacia_y_luego_nueva(self):
        self.iniciar()
        evs = self.ev(fd(falla("1", 120)), T0)
        self.assertEqual(self.tipos(evs), [("nueva", "1")])

    def test_resumen_desactivado_igual_marca_notificadas(self):
        self.det.cfg["avisos"]["resumen_al_iniciar"] = False
        self.assertEqual(self.ev(fd(falla("1", 120)), T0), [])
        self.assertTrue(self.seg("1").notificada)
        self.assertEqual(self.ev(fd(falla("1", 120)), T0 + MIN), [])

    def test_primera_filtrada_no_entra_al_resumen(self):
        evs = self.ev(fd(falla("1", 120), falla("2", 500, programada=True)), T0)
        self.assertEqual([f.caso for f in evs[0].fallas], ["1"])
        self.assertFalse(self.seg("2").notificada)

    def test_primera_cerrada_se_normaliza_en_silencio(self):
        evs = self.ev(fd(falla("1", 120), falla("2", 500, abierta=False)), T0)
        self.assertEqual([f.caso for f in evs[0].fallas], ["1"])
        self.assertTrue(self.seg("2").normalizada)

    def test_resumen_va_al_final(self):
        # Estado heredado sin `iniciado` pero con un caso ya avisado que ahora cierra.
        self.estado.casos["9"] = Seguimiento(caso="9", primera_vista=T0, ultima_vista=T0,
                                             notificada=True, clientes_notificados=80)
        evs = self.ev(fd(falla("9", 80, abierta=False), falla("1", 120)), T0)
        self.assertEqual(self.tipos(evs), [("normalizada", "9"), ("resumen_inicio", "")])


class TestNueva(Base):
    def setUp(self):
        super().setUp()
        self.iniciar()

    def test_nueva_y_estado(self):
        f = falla("1", 420, calles=["AV. MATTA", "LOS CARRERA"])
        evs = self.ev(fd(f), T0)
        self.assertEqual(self.tipos(evs), [("nueva", "1")])
        e = evs[0]
        self.assertIs(e.falla, f)
        self.assertEqual(e.ts, T0)
        self.assertIn("informe de casos", e.motivo)
        s = self.seg("1")
        self.assertTrue(s.notificada)
        self.assertEqual(s.ultimo_aviso, T0)
        self.assertEqual(s.clientes_notificados, 420)
        self.assertEqual(s.max_clientes, 420)
        self.assertEqual(s.nivel_notificado, "alta")
        self.assertEqual(s.calles_notificadas, ["AV. MATTA|OSORNO", "LOS CARRERA|OSORNO"])
        self.assertEqual(s.primera_vista, T0)
        self.assertEqual(s.resumen["clientes"], 420)

    def test_nuevas_ordenadas_por_clientes(self):
        evs = self.ev(fd(falla("1", 50), falla("2", 2000),
                         falla("3", None, clientes_reclamando=5), falla("4", 300)), T0)
        self.assertEqual([e.caso for e in evs], ["2", "4", "1", "3"])

    def test_filtrada_y_luego_pasa_se_avisa_como_nueva(self):
        self.assertEqual(self.ev(fd(falla("1", 2, fuente="nis_reclamos", clientes_reclamando=2)), T0), [])
        self.assertFalse(self.seg("1").notificada)
        evs = self.ev(fd(falla("1", 4, fuente="nis_reclamos", clientes_reclamando=4)), T0 + MIN)
        self.assertEqual(self.tipos(evs), [("nueva", "1")])

    def test_fuentes_completas_solo_las_completas(self):
        self.ev(fd(falla("1", 120, fuentes=("casos", "grilla"))), T0, comp=completas("casos"))
        self.assertEqual(self.seg("1").fuentes_completas, ["casos"])
        self.ev(fd(falla("1", 120, fuentes=("casos", "reclamos"))), T0 + MIN,
                comp=completas("casos", "reclamos"))
        self.assertEqual(self.seg("1").fuentes_completas, ["casos", "reclamos"])


class TestFiltros(Base):
    def pasa(self, f, **filtros):
        self.det.cfg["filtros"].update(filtros)
        return self.det.pasa_filtros(f)

    def test_programada(self):
        self.assertFalse(self.pasa(falla(programada=True)))
        self.assertTrue(self.pasa(falla(programada=True), incluir_programadas=True))

    def test_min_clientes_con_total(self):
        self.assertFalse(self.pasa(falla(clientes=0)))
        self.assertTrue(self.pasa(falla(clientes=1)))
        self.assertFalse(self.pasa(falla(clientes=99), min_clientes=100))
        self.assertFalse(self.pasa(falla(clientes=99, fuente="grilla"), min_clientes=100))
        self.assertTrue(self.pasa(falla(clientes=100, fuente="grilla"), min_clientes=100))

    def test_con_total_manda_el_total_no_los_reclamos(self):
        self.assertFalse(self.pasa(falla(clientes=0, clientes_reclamando=50)))

    def test_sin_total_usa_min_reclamos(self):
        self.assertFalse(self.pasa(falla(clientes=2, fuente="nis_reclamos", clientes_reclamando=2)))
        self.assertTrue(self.pasa(falla(clientes=3, fuente="nis_reclamos", clientes_reclamando=3)))
        self.assertFalse(self.pasa(falla(clientes=None, clientes_reclamando=2)))
        self.assertTrue(self.pasa(falla(clientes=None, clientes_reclamando=3)))
        # min_clientes no aplica a un mínimo por NIS
        self.assertTrue(self.pasa(falla(clientes=3, fuente="nis_reclamos", clientes_reclamando=3),
                                  min_clientes=100))

    def test_ccd(self):
        self.assertTrue(self.pasa(falla(ccd="SUR"), ccd=["CCD Sur"]))
        self.assertTrue(self.pasa(falla(ccd="ccd_sur"), ccd=["SUR"]))
        self.assertFalse(self.pasa(falla(ccd="NORTE"), ccd=["ccd_sur"]))
        self.assertTrue(self.pasa(falla(ccd=""), ccd=["SUR"]))                 # desconocido
        self.assertFalse(self.pasa(falla(ccd=""), ccd=["SUR"], incluir_si_desconocido=False))

    def test_comunas(self):
        f = falla(comunas=["Osorno", "Río Bueno"])
        self.assertTrue(self.pasa(f, comunas=["RIO BUENO"]))
        self.assertTrue(self.pasa(f, comunas=["osorno", "Temuco"]))
        self.assertFalse(self.pasa(f, comunas=["Temuco"]))
        self.assertTrue(self.pasa(falla(comunas=[]), comunas=["Temuco"]))
        self.assertFalse(self.pasa(falla(comunas=[]), comunas=["Temuco"], incluir_si_desconocido=False))

    def test_zonales_y_empresas(self):
        f = falla(zonales=["OSORNO"], empresa="SAESA")
        self.assertTrue(self.pasa(f, zonales=["Osorno"], empresas=["saesa"]))
        self.assertFalse(self.pasa(f, empresas=["FRONTEL"]))
        self.assertFalse(self.pasa(f, zonales=["CAUTIN CORDILLERA"]))
        self.assertTrue(self.pasa(falla(empresa=""), empresas=["FRONTEL"]))

    def test_listas_vacias_no_filtran(self):
        self.assertTrue(self.pasa(falla(ccd="NORTE", comunas=["Temuco"]),
                                  ccd=[], comunas=[""], incluir_si_desconocido=False))

    def test_ccd_normalizado(self):
        for v in ("ccd_sur", "CCD Sur", "SUR", "sur", " ccd-sur "):
            self.assertEqual(_ccd(v), "SUR", v)
        for v in ("ccd_norte", "CCD Norte", "NORTE", "norte"):
            self.assertEqual(_ccd(v), "NORTE", v)
        self.assertEqual(_ccd(""), "")
        self.assertEqual(_ccd("centro"), "CENTRO")    # mal escrito: excluye, no se apaga

    def test_filtro_de_lista_aplica_en_evaluar(self):
        self.det.cfg["filtros"]["ccd"] = ["SUR"]
        self.iniciar()
        evs = self.ev(fd(falla("1", 120, ccd="NORTE"), falla("2", 120, ccd="SUR")), T0)
        self.assertEqual(self.tipos(evs), [("nueva", "2")])


class TestActualizada(Base):
    def setUp(self):
        super().setUp()
        self.iniciar()

    def avisar(self, **kw):
        f = falla("1", **kw)
        self.assertEqual(self.tipos(self.ev(fd(f), T0)), [("nueva", "1")])
        return f

    def test_delta_chico_no_avisa(self):
        self.avisar(clientes=400)
        self.assertEqual(self.ev(fd(falla("1", 440)), T0 + 20 * MIN), [])

    def test_delta_abs_y_rel(self):
        self.avisar(clientes=400)
        evs = self.ev(fd(falla("1", 520)), T0 + 20 * MIN)
        self.assertEqual(self.tipos(evs), [("actualizada", "1")])
        self.assertEqual(evs[0].anterior["clientes"], 400)
        self.assertEqual(evs[0].anterior["nivel"], "alta")
        s = self.seg("1")
        self.assertEqual(s.clientes_notificados, 520)
        self.assertEqual(s.ultimo_aviso, T0 + 20 * MIN)

    def test_delta_relativo_insuficiente(self):
        self.avisar(clientes=1200)
        self.assertEqual(self.ev(fd(falla("1", 1290)), T0 + 20 * MIN), [])   # +90 < 25 %

    def test_delta_absoluto_insuficiente(self):
        self.avisar(clientes=100)
        self.assertEqual(self.ev(fd(falla("1", 140)), T0 + 20 * MIN), [])    # +40 < 50

    def test_sube_nivel(self):
        self.avisar(clientes=280)
        evs = self.ev(fd(falla("1", 310)), T0 + 20 * MIN)
        self.assertEqual(self.tipos(evs), [("actualizada", "1")])
        self.assertEqual(evs[0].anterior["nivel"], "media")
        self.assertEqual(self.seg("1").nivel_notificado, "alta")

    def test_de_sin_dato_a_total(self):
        self.avisar(clientes=None, clientes_reclamando=5)
        evs = self.ev(fd(falla("1", 300)), T0 + 20 * MIN)
        self.assertEqual(self.tipos(evs), [("actualizada", "1")])
        self.assertIsNone(evs[0].anterior["clientes"])

    def test_calles_nuevas(self):
        self.avisar(clientes=120, calles=["AV. MATTA"])
        dos = falla("1", 120, calles=["AV. MATTA", "LOS CARRERA", "O'HIGGINS"])
        self.assertEqual(self.ev(fd(dos), T0 + 20 * MIN), [])              # 2 < 3
        tres = falla("1", 120, calles=["AV. MATTA", "LOS CARRERA", "O'HIGGINS", "BULNES"])
        evs = self.ev(fd(tres), T0 + 21 * MIN)
        self.assertEqual(self.tipos(evs), [("actualizada", "1")])
        self.assertEqual(evs[0].anterior["nuevas_calles"], ["LOS CARRERA", "O'HIGGINS", "BULNES"])
        self.assertEqual(evs[0].anterior["calles"], ["AV. MATTA|OSORNO"])
        self.assertEqual(len(self.seg("1").calles_notificadas), 4)
        # las mismas calles otra vez: nada
        self.assertEqual(self.ev(fd(tres), T0 + 40 * MIN), [])

    def test_variante_de_nombre_no_es_calle_nueva(self):
        self.avisar(clientes=120, calles=["AV. MATTA", "LOS CARRERA", "BULNES"])
        otra = falla("1", 120, calles=["Av.  Matta", "los carrera", "Bulnes"])
        self.assertEqual(self.ev(fd(otra), T0 + 20 * MIN), [])

    def test_peligro_es_urgente(self):
        self.avisar(clientes=120)
        evs = self.ev(fd(falla("1", 120, peligro=True)), T0 + 2 * MIN)     # dentro del intervalo
        self.assertEqual(self.tipos(evs), [("actualizada", "1")])
        self.assertTrue(self.seg("1").peligro_notificado)
        self.assertEqual(self.ev(fd(falla("1", 120, peligro=True)), T0 + 3 * MIN), [])

    def test_rate_limit_difiere_sin_perder(self):
        self.avisar(clientes=400)
        self.assertEqual(self.ev(fd(falla("1", 900)), T0 + 3 * MIN), [])
        s = self.seg("1")
        self.assertEqual(s.clientes_notificados, 400)      # el estado NO se movió
        self.assertEqual(s.ultimo_aviso, T0)
        self.assertEqual(self.ev(fd(falla("1", 900)), T0 + 9 * MIN), [])
        evs = self.ev(fd(falla("1", 900)), T0 + 10 * MIN)  # se cumple el intervalo
        self.assertEqual(self.tipos(evs), [("actualizada", "1")])
        self.assertEqual(evs[0].anterior["clientes"], 400)

    def test_pasar_a_masivo_salta_rate_limit(self):
        self.avisar(clientes=400)
        evs = self.ev(fd(falla("1", 1500)), T0 + 2 * MIN)
        self.assertEqual(self.tipos(evs), [("actualizada", "1")])
        self.assertEqual(self.seg("1").nivel_notificado, "masivo")

    def test_subir_a_alta_no_es_urgente(self):
        self.avisar(clientes=280)
        self.assertEqual(self.ev(fd(falla("1", 320)), T0 + 2 * MIN), [])
        self.assertEqual(self.tipos(self.ev(fd(falla("1", 320)), T0 + 10 * MIN)),
                         [("actualizada", "1")])

    def test_motivo_auditable(self):
        self.avisar(clientes=400)
        e = self.ev(fd(falla("1", 1500)), T0 + 20 * MIN)[0]
        self.assertIn("400 → 1500", e.motivo)
        self.assertIn("alta → masivo", e.motivo)


class TestReposicionParcial(Base):
    def setUp(self):
        super().setUp()
        self.iniciar()
        self.ev(fd(falla("1", 2300)), T0)

    def test_baja_grande(self):
        evs = self.ev(fd(falla("1", 800)), T0 + 20 * MIN)
        self.assertEqual(self.tipos(evs), [("reposicion_parcial", "1")])
        self.assertEqual(evs[0].anterior["clientes"], 2300)
        s = self.seg("1")
        self.assertEqual(s.clientes_notificados, 800)
        self.assertEqual(s.nivel_notificado, "alta")
        self.assertEqual(s.max_clientes, 2300)

    def test_baja_a_cero_no_es_reposicion(self):
        self.assertEqual(self.ev(fd(falla("1", 0)), T0 + 20 * MIN), [])

    def test_reposicion_respeta_rate_limit(self):
        self.assertEqual(self.ev(fd(falla("1", 800)), T0 + 5 * MIN), [])
        self.assertEqual(self.tipos(self.ev(fd(falla("1", 800)), T0 + 10 * MIN)),
                         [("reposicion_parcial", "1")])

    def test_sin_aviso_de_reposicion_mueve_la_base(self):
        self.det.cfg["avisos"]["avisar_reposicion_parcial"] = False
        self.assertEqual(self.ev(fd(falla("1", 800)), T0 + 20 * MIN), [])
        self.assertEqual(self.seg("1").clientes_notificados, 800)
        self.assertEqual(self.seg("1").ultimo_aviso, T0)            # no fue un aviso
        self.assertEqual(self.ev(fd(falla("1", 900)), T0 + 21 * MIN), [])   # +100 < 25 % de 800

    def test_minimo_por_nis_no_es_reposicion(self):
        # El informe de casos quedó viejo y el agregador cae a contar NIS.
        nis = falla("1", 5, fuente="nis_reclamos", clientes_reclamando=5)
        self.assertEqual(self.ev(fd(nis), T0 + 20 * MIN), [])
        self.assertEqual(self.seg("1").clientes_notificados, 2300)
        self.assertEqual(self.seg("1").nivel_notificado, "masivo")
        # y cuando vuelve el informe con lo mismo, tampoco hay "actualizada"
        self.assertEqual(self.ev(fd(falla("1", 2300)), T0 + 40 * MIN), [])


class TestCierreYReapertura(Base):
    def setUp(self):
        super().setUp()
        self.iniciar()

    def test_cierre_explicito_normaliza(self):
        self.ev(fd(falla("1", 400)), T0)
        self.ev(fd(falla("1", 900)), T0 + 3 * MIN)      # diferido, pero sube el máximo
        cerrada = falla("1", 0, abierta=False, cierre=T0 + 30 * MIN)
        evs = self.ev(fd(cerrada), T0 + 31 * MIN)
        self.assertEqual(self.tipos(evs), [("normalizada", "1")])
        e = evs[0]
        self.assertIs(e.falla, cerrada)
        self.assertEqual(e.motivo, "cierre informado por informe de casos")
        self.assertEqual(e.anterior["max_clientes"], 900)
        s = self.seg("1")
        self.assertTrue(s.normalizada)
        self.assertEqual(s.normalizada_en, T0 + 31 * MIN)
        # sigue en el reporte por periodo como cerrada: ni aviso ni se refresca la fecha
        self.assertEqual(self.ev(fd(cerrada), T0 + 40 * MIN), [])
        self.assertEqual(self.seg("1").normalizada_en, T0 + 31 * MIN)

    def test_cierre_varias_fuentes_en_motivo(self):
        self.ev(fd(falla("1", 400, fuentes=("casos", "reclamos"))), T0)
        evs = self.ev(fd(falla("1", 0, abierta=False, fuentes=("casos", "reclamos"))), T0 + MIN)
        self.assertEqual(evs[0].motivo, "cierre informado por informe de casos y reclamos")

    def test_cierre_de_no_notificada_es_silencio(self):
        evs = self.ev(fd(falla("1", 400, abierta=False)), T0)
        self.assertEqual(evs, [])
        self.assertTrue(self.seg("1").normalizada)
        self.assertFalse(self.seg("1").notificada)

    def test_cierre_de_filtrada_es_silencio(self):
        self.ev(fd(falla("1", 400, programada=True)), T0)
        self.assertEqual(self.ev(fd(falla("1", 400, programada=True, abierta=False)), T0 + MIN), [])
        self.assertTrue(self.seg("1").normalizada)

    def test_cierre_sin_avisar_normalizadas(self):
        self.det.cfg["avisos"]["avisar_normalizadas"] = False
        self.ev(fd(falla("1", 400)), T0)
        self.assertEqual(self.ev(fd(falla("1", 0, abierta=False)), T0 + MIN), [])
        self.assertTrue(self.seg("1").normalizada)

    def test_reapertura(self):
        self.ev(fd(falla("1", 400)), T0)
        self.ev(fd(falla("1", 0, abierta=False)), T0 + 10 * MIN)
        evs = self.ev(fd(falla("1", 350)), T0 + 20 * MIN)
        self.assertEqual(self.tipos(evs), [("nueva", "1")])
        self.assertEqual(evs[0].motivo, "reapertura")
        s = self.seg("1")
        self.assertFalse(s.normalizada)
        self.assertIsNone(s.normalizada_en)
        self.assertTrue(s.notificada)
        self.assertEqual(s.clientes_notificados, 350)

    def test_reapertura_de_silenciosa_es_nueva_comun(self):
        self.ev(fd(falla("1", 400, abierta=False)), T0)
        evs = self.ev(fd(falla("1", 400)), T0 + 10 * MIN)
        self.assertEqual(self.tipos(evs), [("nueva", "1")])
        self.assertNotEqual(evs[0].motivo, "reapertura")


class TestAusencia(Base):
    def setUp(self):
        super().setUp()
        self.iniciar()

    def avisar(self, f, comp=CASOS, ahora=T0):
        self.assertEqual(self.tipos(self.ev(fd(f), ahora, comp)), [("nueva", f.caso)])

    def ausente(self, minutos, comp=CASOS):
        return self.ev({}, T0 + minutos * MIN, comp)

    def test_normaliza_tras_minutos_y_evaluaciones(self):
        self.avisar(falla("1", 400))
        self.assertEqual(self.ausente(5), [])       # ausente_desde = +5, eval 1
        self.assertEqual(self.ausente(10), [])      # eval 2, 5 min
        self.assertEqual(self.ausente(19), [])      # 14 min
        evs = self.ausente(20)                      # 15 min y 4 evaluaciones
        self.assertEqual(self.tipos(evs), [("normalizada", "1")])
        e = evs[0]
        self.assertEqual(e.falla.caso, "1")
        self.assertEqual(e.falla.clientes, 400)
        self.assertIn("dejó de aparecer en informe de casos hace 20 min", e.motivo)
        self.assertIn("no hay cierre explícito", e.motivo)
        self.assertTrue(self.seg("1").normalizada)
        self.assertEqual(self.ausente(30), [])      # ya normalizada: no se repite

    def test_minutos_sin_evaluaciones_suficientes(self):
        self.det.cfg["avisos"]["evaluaciones_ausencia_normalizar"] = 3
        self.avisar(falla("1", 400))
        self.assertEqual(self.ausente(1), [])
        self.assertEqual(self.ausente(30), [])      # 29 min pero sólo 2 evaluaciones
        self.assertEqual(self.tipos(self.ausente(31)), [("normalizada", "1")])

    def test_evaluaciones_sin_minutos_suficientes(self):
        self.avisar(falla("1", 400))
        for m in range(1, 15):
            self.assertEqual(self.ausente(m), [], m)
        self.assertEqual(self.seg("1").evaluaciones_ausente, 14)

    def test_reaparecer_reinicia_el_conteo(self):
        f = falla("1", 400)
        self.avisar(f)
        self.ausente(5)
        self.ausente(10)
        self.ev(fd(f), T0 + 12 * MIN)
        self.assertIsNone(self.seg("1").ausente_desde)
        self.assertEqual(self.seg("1").evaluaciones_ausente, 0)
        self.assertEqual(self.ausente(15), [])
        self.assertEqual(self.ausente(25), [])
        self.assertEqual(self.tipos(self.ausente(30)), [("normalizada", "1")])

    def test_grilla_sola_nunca_normaliza(self):
        f = falla("1", 400, fuente="grilla", fuentes=("grilla",))
        self.avisar(f, comp={})
        for m in (5, 30, 60, 300, 700):
            self.assertEqual(self.ausente(m, comp=CASOS), [], m)   # casos fresca, pero nunca la vio
        self.assertEqual(self.seg("1").evaluaciones_ausente, 0)
        self.assertFalse(self.seg("1").normalizada)

    def test_fuente_completa_vieja_no_cuenta(self):
        self.avisar(falla("1", 400))
        for m in (5, 20, 60):
            self.assertEqual(self.ausente(m, comp={}), [], m)     # casos no está fresca
        self.assertEqual(self.seg("1").evaluaciones_ausente, 0)
        self.assertIsNone(self.seg("1").ausente_desde)

    def test_fuente_completa_que_no_la_vio_no_cuenta(self):
        self.avisar(falla("1", 400, fuente="nis_reclamos", clientes_reclamando=400,
                          fuentes=("reclamos",)), comp=completas("reclamos"))
        for m in (5, 20, 60):
            self.assertEqual(self.ausente(m, comp=CASOS), [], m)
        self.assertFalse(self.seg("1").normalizada)
        # cuando reclamos (que sí la vio) viene fresca sin ella, ahí sí cuenta
        self.assertEqual(self.ausente(61, comp=completas("reclamos")), [])
        self.assertEqual(self.tipos(self.ausente(76, comp=completas("reclamos"))),
                         [("normalizada", "1")])

    def test_ventana_protege_inicio_antiguo(self):
        # Reporte de casos de las últimas 72 h; la falla empezó hace 71,5 h.
        f = falla("1", 400, inicio=T0 - timedelta(hours=71, minutes=30))

        def comp(m):
            ahora = T0 + m * MIN
            return completas("casos", ventana_desde=ahora - timedelta(hours=72), recibido=ahora)

        self.avisar(f, comp=comp(0))
        for m in (5, 10, 20, 40, 60):
            self.assertEqual(self.ausente(m, comp=comp(m)), [], m)
        self.assertFalse(self.seg("1").normalizada)
        self.assertEqual(self.seg("1").evaluaciones_ausente, 0)

    def test_ventana_no_protege_inicio_reciente(self):
        f = falla("1", 400, inicio=T0 - timedelta(hours=10))
        comp = completas("casos", ventana_desde=T0 - timedelta(hours=72))
        self.avisar(f, comp=comp)
        self.assertEqual(self.ausente(5, comp=comp), [])
        self.assertEqual(self.tipos(self.ausente(20, comp=comp)), [("normalizada", "1")])

    def test_ventana_sin_inicio_no_normaliza(self):
        f = falla("1", 400, inicio=None)
        comp = completas("casos", ventana_desde=T0 - timedelta(hours=72))
        self.avisar(f, comp=comp)
        for m in (5, 20, 60):
            self.assertEqual(self.ausente(m, comp=comp), [], m)

    def test_ventana_solo_excluye_esa_fuente(self):
        f = falla("1", 400, inicio=T0 - timedelta(hours=71, minutes=30),
                  fuentes=("casos", "reclamos"))
        comp = {**completas("casos", ventana_desde=T0 - timedelta(hours=72)),
                **completas("reclamos")}
        self.avisar(f, comp=comp)
        self.assertEqual(self.ausente(5, comp=comp), [])
        e = self.ausente(20, comp=comp)[0]
        self.assertEqual(e.tipo, "normalizada")
        self.assertIn("en reclamos", e.motivo)
        self.assertNotIn("informe de casos", e.motivo)

    def test_expiracion_sin_fuentes(self):
        self.avisar(falla("1", 400, fuente="grilla", fuentes=("grilla",)), comp={})
        self.assertEqual(self.ausente(720, comp={}), [])
        self.assertIn("1", self.estado.casos)                  # justo 720: todavía no
        self.assertEqual(self.ausente(721, comp={}), [])       # sin aviso
        self.assertNotIn("1", self.estado.casos)

    def test_ausente_no_notificada_se_normaliza_en_silencio(self):
        self.ev(fd(falla("1", 400, programada=True)), T0)
        self.assertEqual(self.ausente(5), [])
        self.assertEqual(self.ausente(20), [])
        self.assertTrue(self.seg("1").normalizada)

    def test_ausencia_sin_avisar_normalizadas(self):
        self.det.cfg["avisos"]["avisar_normalizadas"] = False
        self.avisar(falla("1", 400))
        self.ausente(5)
        self.assertEqual(self.ausente(20), [])
        self.assertTrue(self.seg("1").normalizada)


class TestPurga(Base):
    def test_purga_normalizadas_viejas(self):
        self.iniciar()
        self.ev(fd(falla("1", 400)), T0)
        self.ev(fd(falla("1", 0, abierta=False)), T0 + 30 * MIN)
        normalizada = T0 + 30 * MIN
        self.ev({}, normalizada + timedelta(hours=24))
        self.assertIn("1", self.estado.casos)
        self.ev({}, normalizada + timedelta(hours=24, minutes=1))
        self.assertNotIn("1", self.estado.casos)

    def test_no_purga_activas(self):
        self.iniciar()
        f = falla("1", 400)
        self.ev(fd(f), T0)
        self.ev(fd(f), T0 + timedelta(hours=30))
        self.assertIn("1", self.estado.casos)


class TestOrdenEventos(Base):
    def test_normalizadas_nuevas_actualizadas(self):
        self.iniciar()
        self.ev(fd(falla("A", 400), falla("B", 300)), T0)
        evs = self.ev(fd(falla("A", 0, abierta=False), falla("B", 600),
                         falla("C", 50), falla("D", 2000)), T0 + 20 * MIN)
        self.assertEqual(self.tipos(evs), [("normalizada", "A"), ("nueva", "D"),
                                           ("nueva", "C"), ("actualizada", "B")])

    def test_ausencia_tambien_va_primero(self):
        self.iniciar()
        self.ev(fd(falla("A", 400), falla("B", 300)), T0)
        self.ev(fd(falla("B", 300)), T0 + 5 * MIN)
        evs = self.ev(fd(falla("B", 2300), falla("C", 80)), T0 + 20 * MIN)
        self.assertEqual(self.tipos(evs), [("normalizada", "A"), ("nueva", "C"),
                                           ("actualizada", "B")])


if __name__ == "__main__":
    unittest.main()
