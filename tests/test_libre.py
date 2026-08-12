"""Tests de sia_libre.py que no necesitan navegador ni red.

Cubren las dos cosas que pueden romperse en silencio:

  - El RECONOCIMIENTO de los tres combos que solo aparecen con la tipología
    «LIBRE ELECCIÓN». Si la normalización de etiquetas deja de funcionar, el
    scraper cae en la detección por descarte y puede acabar buscando otra cosa
    sin avisar.
  - El DESVÍO DE RUTAS. sia_libre reutiliza guardar() de sia_scraper apuntando
    sus rutas a los archivos _libre. Si ese desvío se filtrara, una corrida de
    electivas reescribiría horarios.csv y horario.html con puras electivas y se
    perdería el catálogo del plan. Es el peor fallo posible del repo, así que
    tiene su propia alarma.

Correr:  pytest -q
"""

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

sia = pytest.importorskip("sia_scraper")
libre = pytest.importorskip("sia_libre")


@pytest.mark.parametrize("etiqueta, cual", [
    ("¿Por qué deseas buscar?", "criterio"),
    ("Por qué deseas buscar", "criterio"),
    ("¿Porque sede?", "sede"),          # la errata que trae hoy el formulario
    ("¿Por qué sede?", "sede"),         # y su versión corregida
    ("¿Por qué facultad?", "facultad"),
    ("¿Porque facultad?", "facultad"),
])
def test_las_etiquetas_del_sia_se_reconocen(etiqueta, cual):
    compacta = libre._compacto(etiqueta)
    assert any(k in compacta for k in libre.CLAVES_COMBO[cual])


@pytest.mark.parametrize("etiqueta, cual", [
    ("Sede", "sede"),                   # el combo de arriba, no el de libre
    ("Facultad", "facultad"),
    ("Plan de estudios", "criterio"),
    ("Tipología de asignatura", "criterio"),
])
def test_los_combos_de_arriba_no_se_confunden(etiqueta, cual):
    """'Facultad' no puede pasar por '¿Por qué facultad?'."""
    compacta = libre._compacto(etiqueta)
    assert not any(k in compacta for k in libre.CLAVES_COMBO[cual])


def test_el_desvio_de_rutas_va_y_vuelve():
    originales = {k: getattr(sia, k) for k in libre.SAL}

    with libre.salidas_libre():
        for k, ruta in libre.SAL.items():
            assert getattr(sia, k) == ruta
            assert "_libre" in ruta.name or ruta.name.startswith("horario_libre")

    for k, ruta in originales.items():
        assert getattr(sia, k) == ruta, f"{k} no volvió a su valor original"


def test_el_desvio_vuelve_aunque_reviente():
    originales = {k: getattr(sia, k) for k in libre.SAL}
    with pytest.raises(ValueError):
        with libre.salidas_libre():
            raise ValueError("algo se rompió a mitad del barrido")
    for k, ruta in originales.items():
        assert getattr(sia, k) == ruta


def test_las_salidas_no_pisan_las_del_plan():
    """Ninguna ruta de electivas puede coincidir con una del plan."""
    del_plan = {sia.OUTPUT_JSON, sia.OUTPUT_GRUPOS_CSV, sia.OUTPUT_HORARIOS_CSV,
                sia.OUTPUT_HTML, sia.OUTPUT_HISTORIAL, sia.OUTPUT_SERIES,
                sia.OUTPUT_STATS, sia.OUTPUT_RUNS}
    assert not (set(libre.SAL.values()) & del_plan)


def test_horarios_libre_usa_el_mismo_encabezado_que_la_pagina_espera():
    """index.html lee horarios_libre.csv con el mismo parser que horarios.csv:
    si el encabezado se separa, la página deja de ver las electivas."""
    real = RAIZ / "horarios.csv"
    if not real.exists():
        pytest.skip("no hay horarios.csv en el repo")
    esperado = real.read_text(encoding="utf-8-sig").splitlines()[0]
    columnas = [c.strip() for c in esperado.split(",")]
    for obligatoria in ("codigo_asignatura", "asignatura", "grupo", "actividad",
                        "cupos_disponibles", "dia", "hora_inicio", "hora_fin"):
        assert obligatoria in columnas


# --- Guardas contra el peor fallo: raspar el plan creyendo que son electivas ---

class _ComboFalso:
    """Imita lo justo de seleccionar(): coincidencia exacta y, si no, parcial."""
    def __init__(self, opciones): self.opciones = opciones
    def __call__(self, driver, select_id, texto, **kw):
        for o in self.opciones:
            if sia.normalizar(o) == sia.normalizar(texto):
                return o
        for o in self.opciones:                      # <- la trampa
            if sia.normalizar(texto) in sia.normalizar(o):
                return o
        raise RuntimeError("sin coincidencia")


def test_no_acepta_todas_menos_libre_eleccion(monkeypatch):
    """Si el SIA no ofrece «LIBRE ELECCIÓN» tal cual, seleccionar() elegiría
    «TODAS MENOS LIBRE ELECCIÓN» por subcadena. Eso debe reventar, no pasar."""
    monkeypatch.setattr(sia, "seleccionar",
                        _ComboFalso(["TODAS MENOS LIBRE ELECCIÓN", "OBLIGATORIA"]))
    with pytest.raises(RuntimeError, match="TODAS MENOS LIBRE"):
        libre.seleccionar_estricto(None, "combo", "LIBRE ELECCIÓN")


def test_acepta_la_opcion_correcta(monkeypatch):
    monkeypatch.setattr(sia, "seleccionar",
                        _ComboFalso(["TODAS MENOS LIBRE ELECCIÓN", "LIBRE ELECCIÓN"]))
    assert libre.seleccionar_estricto(None, "combo", "LIBRE ELECCIÓN") == "LIBRE ELECCIÓN"


def test_tolera_diferencias_de_tilde_y_espacios(monkeypatch):
    """Ser estricto no puede significar ser frágil: «LIBRE ELECCION» sin tilde
    es la misma opción y debe pasar."""
    monkeypatch.setattr(sia, "seleccionar", _ComboFalso(["  LIBRE ELECCION "]))
    assert libre.seleccionar_estricto(None, "combo", "LIBRE ELECCIÓN")


def test_el_listado_del_plan_no_pasa_por_electivas(monkeypatch):
    """La segunda red: aunque la tipología se colara, el listado se compara con
    catalogo.json y se aborta antes de escribir nada."""
    del_plan = {"2015938", "2015939", "2015940", "1000003-B"}
    monkeypatch.setattr(libre, "_codigos_del_plan", lambda: del_plan)
    monkeypatch.setattr(libre, "_n_referencia", lambda: None)
    monkeypatch.setattr(sia, "recolectar_todos_los_codigos",
                        lambda driver, verbose=True: sorted(del_plan))
    with pytest.raises(RuntimeError, match="NO es libre elección"):
        libre.recolectar_libre(None)


def test_un_listado_de_electivas_de_verdad_sí_pasa(monkeypatch):
    del_plan = {"2015938", "2015939", "2015940", "1000003-B"}
    electivas = ["2024001", "2024002", "2024003", "2015938"]   # una compartida
    monkeypatch.setattr(libre, "_codigos_del_plan", lambda: del_plan)
    monkeypatch.setattr(libre, "_n_referencia", lambda: None)
    monkeypatch.setattr(sia, "recolectar_todos_los_codigos",
                        lambda driver, verbose=True: list(electivas))
    assert libre.recolectar_libre(None) == electivas


# --- El clic en cascada (lo que falló en la primera corrida real) ---

class _DriverFalso:
    """Un listado con varias anclas para el mismo código, donde solo algunas
    reaccionan. Reproduce lo que hace ADF con las tablas."""

    def __init__(self, anclas, abre_con=None, abre_con_evento=None):
        self.anclas = anclas               # [{'visible': bool}, ...]
        self.abre_con = abre_con           # índice de ancla que sí navega
        self.abre_con_evento = abre_con_evento
        self.en_detalle = False
        self.clicks = []

    def execute_script(self, script, *args):
        if "salida.push" in script:        # enlaces_del_codigo
            return [{"pos": i, "id": f"a{i}", "clase": "af_commandLink",
                     "visible": a["visible"], "onclick": True, "href": ""}
                    for i, a in enumerate(self.anclas)]
        if "iguales[cual]" in script:      # click
            _, cual, modo = args
            self.clicks.append((cual, modo))
            if modo == "evento" and cual == self.abre_con_evento:
                self.en_detalle = True
            elif modo == "click" and cual == self.abre_con:
                self.en_detalle = True
            return True
        return None

    def find_element(self, *a, **k): raise Exception("sin body")
    def find_elements(self, *a, **k): return [] if self.en_detalle else ["fila"]
    def save_screenshot(self, *a, **k): return True
    @property
    def page_source(self): return "<html></html>"


@pytest.fixture
def sin_esperas(monkeypatch):
    """esperar_detalle sin WebDriverWait: consulta el estado del driver falso."""
    monkeypatch.setattr(libre, "esperar_detalle", lambda d, s: d.en_detalle)
    monkeypatch.setattr(sia, "en_listado_resultados", lambda d: not d.en_detalle)
    monkeypatch.setattr(sia, "guardar_debug", lambda *a, **k: None)


def test_prefiere_el_ancla_visible(sin_esperas):
    """El caso que sospecho: la copia oculta va antes en el DOM y no reacciona."""
    d = _DriverFalso([{"visible": False}, {"visible": True}], abre_con=1)
    assert "#1" in libre.click_detalle(d, "2017472")
    assert d.clicks[0][0] == 1, "debería probar primero la visible"


def test_cae_al_evento_de_raton_si_click_no_dispara(sin_esperas):
    d = _DriverFalso([{"visible": True}], abre_con=None, abre_con_evento=0)
    assert "evento de ratón" in libre.click_detalle(d, "2017472")


def test_prueba_tambien_las_ocultas_antes_de_rendirse(sin_esperas):
    d = _DriverFalso([{"visible": False}, {"visible": False}], abre_con=1)
    assert "#1" in libre.click_detalle(d, "2017472")


def test_si_nada_funciona_revienta_con_claridad(sin_esperas):
    from selenium.common.exceptions import TimeoutException
    d = _DriverFalso([{"visible": True}, {"visible": False}])
    with pytest.raises(TimeoutException, match="2017472"):
        libre.click_detalle(d, "2017472")
    assert len(d.clicks) == 3, "dos anclas + un evento de ratón"


def test_no_sigue_pulsando_si_ya_nos_movimos(sin_esperas, monkeypatch):
    """Si un clic nos llevó a otra página (aunque no sea el detalle), seguir
    pulsando a ciegas sobre un DOM viejo solo genera errores raros."""
    from selenium.common.exceptions import TimeoutException
    d = _DriverFalso([{"visible": True}, {"visible": True}])
    monkeypatch.setattr(sia, "en_listado_resultados", lambda drv: False)
    with pytest.raises(TimeoutException):
        libre.click_detalle(d, "2017472")
    assert len(d.clicks) == 1, "debe parar tras el primer clic"


def test_sin_anclas_delega_en_sia_scraper(sin_esperas, monkeypatch):
    llamado = []
    monkeypatch.setattr(sia, "click_asignatura_por_codigo",
                        lambda d, c: llamado.append(c))
    d = _DriverFalso([])
    assert "sia_scraper" in libre.click_detalle(d, "9999999")
    assert llamado == ["9999999"]


# --- Verificación del formulario (el fallo de la FACULTAD DE MINAS) ---

class _DriverCombos:
    def __init__(self, combos): self.combos = combos
    def execute_script(self, script, *args):
        if "salida.push" in script and "getBoundingClientRect" in script:
            return self.combos
        return []


def _combo(cid, etiqueta, sel):
    return {"id": cid, "etiqueta": etiqueta, "n": 5, "sel": sel,
            "visible": True, "opciones": []}


def test_detecta_que_el_formulario_quedo_en_otra_sede(monkeypatch, capsys):
    """El caso real: se pidió Bogotá y el combo quedó en Medellín. Antes esto
    pasaba en silencio y se publicaban asignaturas de la Facultad de Minas."""
    d = _DriverCombos([
        _combo("soc1", "Nivel de estudio", "Pregrado"),
        _combo("soc6", "¿Porque sede?", "1103 SEDE MEDELLÍN"),
    ])
    with pytest.raises(RuntimeError, match="no quedó como se pidió"):
        libre.verificar_formulario(d, {"soc1": "Pregrado",
                                       "soc6": "1101 SEDE BOGOTÁ"})


def test_detecta_la_tipologia_cambiada(monkeypatch):
    d = _DriverCombos([_combo("soc4", "Tipología de asignatura",
                              "TODAS MENOS LIBRE ELECCIÓN")])
    with pytest.raises(RuntimeError, match="LIBRE ELECCIÓN"):
        libre.verificar_formulario(d, {"soc4": "LIBRE ELECCIÓN"})


def test_detecta_un_combo_que_desaparecio(monkeypatch):
    d = _DriverCombos([_combo("soc1", "Nivel de estudio", "Pregrado")])
    with pytest.raises(RuntimeError, match="no está"):
        libre.verificar_formulario(d, {"soc8": "2000 SEDE BOGOTÁ"})


def test_un_formulario_correcto_pasa_y_se_registra(capsys):
    d = _DriverCombos([
        _combo("soc1", "Nivel de estudio", "Pregrado"),
        _combo("soc4", "Tipología de asignatura", "LIBRE ELECCIÓN"),
        _combo("soc8", "¿Por qué facultad?", "2000 SEDE BOGOTÁ"),
    ])
    libre.verificar_formulario(d, {"soc1": "Pregrado",
                                   "soc4": "LIBRE ELECCIÓN",
                                   "soc8": "2000 SEDE BOGOTÁ"})
    salida = capsys.readouterr().out
    assert "LIBRE ELECCIÓN" in salida and "2000 SEDE BOGOTÁ" in salida, \
        "el estado del formulario debe quedar en el log"


def test_tolera_tildes_y_espacios_al_verificar():
    d = _DriverCombos([_combo("soc4", "Tipología", " LIBRE ELECCION ")])
    libre.verificar_formulario(d, {"soc4": "LIBRE ELECCIÓN"})   # no revienta


# --- Revisión de cordura, con los datos reales de las corridas fallidas ---

def _a(codigo, nombre, tipologia, facultad="FACULTAD DE INGENIERÍA"):
    return sia.Asignatura(codigo=codigo, nombre=nombre, tipologia=tipologia,
                          creditos="3", facultad=facultad, planes=[], grupos=[])


# Lo que de verdad publicó el barrido del 12/08 (catalogo_libre.json)
CORRIDA_MINAS = [
    _a("3011100", "Analítica de negocios", "OBLIGATORIAS", "FACULTAD DE MINAS"),
    _a("3011101", "Analítica descriptiva", "OBLIGATORIAS", "FACULTAD DE MINAS"),
    _a("3010861", "Analitica predictiva", "OBLIGATORIAS", "FACULTAD DE MINAS"),
    _a("3010799", "Productos de datos", "OBLIGATORIAS", "FACULTAD DE MINAS"),
    _a("3008548", "Comportamiento mecánico", "OBLIGATORIAS", "FACULTAD DE MINAS"),
    _a("3008728", "Geotecnia de macizos", "OBLIGATORIAS", "FACULTAD DE MINAS"),
]

CORRIDA_MAESTRIA = [
    _a("2026054", "Inglés Intensivo I", "ELECTIVA DE PREGRADO"),
    _a("2026055", "Inglés Intensivo II", "ELECTIVA DE PREGRADO"),
    _a("2026056", "Inglés Intensivo III", "ELECTIVA DE PREGRADO"),
    _a("2026057", "Intensive English I", "ELECTIVA DE PREGRADO"),
    _a("2018965", "Proyecto de Tesis de Maestría", "ACTIVIDADES ACADÉMICAS"),
    _a("2018966", "Seminario de investigación I", "ACTIVIDADES ACADÉMICAS"),
    _a("2018967", "Seminario de investigación II", "ACTIVIDADES ACADÉMICAS"),
    _a("2018968", "Seminario de investigación III", "ACTIVIDADES ACADÉMICAS"),
    _a("2018969", "Tesis de Maestría", "TESIS-TRAB.FINAL"),
]

ELECTIVAS_DE_VERDAD = [
    _a("1000131-B", "Tango y sociedad", "LIBRE ELECCIÓN"),
    _a("2026054", "Inglés Intensivo I", "ELECTIVA DE PREGRADO"),
    _a("2017472", "Desarrollo Rural", "LIBRE ELECCIÓN"),
    _a("2015229", "Historia del jazz", "LIBRE ELECCIÓN"),
    _a("1000044-B", "Deporte formativo", "LIBRE ELECCIÓN"),
]


def test_caza_la_corrida_de_la_facultad_de_minas():
    motivo = libre.revisar_cordura(CORRIDA_MINAS)
    assert motivo and "OBLIGATORIAS" in motivo and "MINAS" in motivo


def test_caza_la_corrida_de_la_maestria():
    """5 de 9 con tipología imposible: seminarios y tesis no son electivas."""
    motivo = libre.revisar_cordura(CORRIDA_MAESTRIA)
    assert motivo and "TESIS-TRAB.FINAL" in motivo


def test_no_molesta_a_un_barrido_bueno():
    assert libre.revisar_cordura(ELECTIVAS_DE_VERDAD) is None


def test_aguanta_alguna_rareza_suelta():
    """Ser estricto no puede significar abortar por una asignatura rara."""
    mezcla = ELECTIVAS_DE_VERDAD + [_a("999", "Trabajo de grado", "TRABAJO DE GRADO")]
    assert libre.revisar_cordura(mezcla) is None


def test_no_juzga_con_una_muestra_ridicula():
    assert libre.revisar_cordura(CORRIDA_MINAS[:2]) is None


def test_se_puede_desactivar(monkeypatch):
    monkeypatch.setenv("SIA_LIBRE_SIN_CORDURA", "1")
    assert libre.revisar_cordura(CORRIDA_MINAS) is None


@pytest.mark.parametrize("tipologia, sospechosa", [
    ("OBLIGATORIAS", True), ("DISCIPLINAR OBLIGATORIA", True),
    ("ACTIVIDADES ACADÉMICAS", True), ("TESIS-TRAB.FINAL", True),
    ("NIVELACIÓN", True),
    ("LIBRE ELECCIÓN", False), ("ELECTIVA DE PREGRADO", False),
    ("DISCIPLINAR OPTATIVA", False), ("", False),
])
def test_clasificacion_de_tipologias(tipologia, sospechosa):
    assert libre.tipologia_sospechosa(tipologia) is sospechosa


def test_avisa_de_los_desplegables_sin_configurar(capsys):
    """El combo que nadie toca es el sospechoso número uno: debe salir en el
    log con sus opciones, no pasar desapercibido."""
    d = _DriverCombos([
        _combo("soc4", "Tipología de asignatura", "LIBRE ELECCIÓN"),
        {"id": "soc9", "etiqueta": "¿Por qué plan?", "n": 40,
         "sel": "3011 MAESTRÍA EN INGENIERÍA - GEOTECNIA", "visible": True,
         "opciones": ["3011 MAESTRÍA...", "2542 INGENIERÍA CIVIL"]},
    ])
    libre.verificar_formulario(d, {"soc4": "LIBRE ELECCIÓN"})
    salida = capsys.readouterr().out
    assert "NO configura" in salida
    assert "¿Por qué plan?" in salida and "GEOTECNIA" in salida
    assert "2542 INGENIERÍA CIVIL" in salida, "debe listar las opciones"


def test_el_plan_es_el_unico_combo_opcional():
    obligatorios = {"criterio", "sede", "facultad"}
    assert set(libre.CLAVES_COMBO) == obligatorios | {"plan"}
    assert libre.PLAN_BUSQUEDA == libre.PLAN, \
        "el combo de plan debe apuntar al mismo plan de arriba"


@pytest.mark.parametrize("etiqueta", [
    "¿Por qué plan?", "¿Porque plan?", "¿Por qué plan de estudios?",
])
def test_reconoce_las_etiquetas_del_cuarto_combo(etiqueta):
    compacta = libre._compacto(etiqueta)
    assert any(k in compacta for k in libre.CLAVES_COMBO["plan"])


def test_el_combo_de_plan_de_arriba_no_se_confunde():
    """«Plan de estudios» (el de arriba) no puede pasar por «¿Por qué plan?»."""
    compacta = libre._compacto("Plan de estudios")
    assert not any(k in compacta for k in libre.CLAVES_COMBO["plan"])
