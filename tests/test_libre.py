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
