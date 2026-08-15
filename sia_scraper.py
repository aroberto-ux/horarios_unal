"""
Scraper del Catálogo de Asignaturas SIA - Universidad Nacional de Colombia
===========================================================================

Extrae, para TODAS las asignaturas de un plan de estudios (tipología
"TODAS MENOS LIBRE ELECCIÓN"):

    - código y nombre de la asignatura
    - tipología y créditos
    - cada grupo ofertado
    - profesor de cada grupo
    - cupos totales / disponibles
    - cada sesión de clase: día, hora inicio, hora fin, salón y edificio

Genera tres archivos:

    grupos.csv     -> una fila por GRUPO (horario resumido en una celda)
    horarios.csv   -> una fila por SESIÓN (día/hora/salón) <- el más útil
                      para armar tu horario o pasarlo a una hoja de cálculo
    catalogo.json  -> estructura anidada completa (asignatura > grupos > sesiones)

Instalación:
    pip install selenium

Requiere Chrome instalado. Selenium 4.6+ gestiona el chromedriver automáticamente.

Uso:
    python sia_scraper.py

Reanudación:
    Si el script se interrumpe, al volver a correrlo lee catalogo.json y se
    salta las asignaturas ya procesadas. Para empezar de cero, borra
    catalogo.json (o pon REANUDAR = False).
"""

import csv
import gzip
import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
import re
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

import historial

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
    NoSuchElementException,
    InvalidSessionIdException,
    WebDriverException,
)

BASE_URL = (
    "https://sia.unal.edu.co/Catalogo/facespublico/public/servicioPublico.jsf?taskflowId=task-flow-AC_CatalogoAsignaturas"
)

# ---------------------------------------------------------------------------
# Parámetros de búsqueda (ajustar según necesidad)
# ---------------------------------------------------------------------------
NIVEL_ESTUDIO = "Pregrado"
SEDE = "1101 SEDE BOGOTÁ"
FACULTAD = "2055 FACULTAD DE INGENIERÍA"
# Uno o varios planes de estudio. El scraper los recorre todos y junta el
# catálogo en los mismos archivos; las asignaturas compartidas (p. ej. los
# inglés) no se raspan dos veces, solo se les anota cada plan donde aparecen.
PLANES_ESTUDIOS = [
    "2542 INGENIERÍA CIVIL",
    # "2545 INGENIERÍA MECÁNICA",   # <- agrega más planes así
]
TIPOLOGIA = "TODAS MENOS LIBRE ELECCIÓN"

# IDs de los combos (tomados del HTML actual; si la página cambia de versión
# pueden cambiar — verificar con "Inspeccionar elemento" si el script falla)
ID_NIVEL = "pt1:r1:0:soc1::content"
ID_SEDE = "pt1:r1:0:soc9::content"
ID_FACULTAD = "pt1:r1:0:soc2::content"
ID_PLAN = "pt1:r1:0:soc3::content"
ID_TIPOLOGIA = "pt1:r1:0:soc4::content"

# ---------------------------------------------------------------------------
# Salidas y comportamiento
# ---------------------------------------------------------------------------
# Todas las salidas se guardan JUNTO AL ARCHIVO .py, no en el directorio
# desde el que se ejecuta. Si no fuera así, al lanzar el script desde otra
# carpeta (p. ej. la terminal abierta en la carpeta de VS Code) los archivos
# terminarían ahí y parecería que no se generaron.
CARPETA_SALIDA = Path(__file__).resolve().parent

OUTPUT_GRUPOS_CSV = CARPETA_SALIDA / "grupos.csv"
OUTPUT_HORARIOS_CSV = CARPETA_SALIDA / "horarios.csv"
OUTPUT_JSON = CARPETA_SALIDA / "catalogo.json"
OUTPUT_HTML = CARPETA_SALIDA / "horario.html"
OUTPUT_HISTORIAL = CARPETA_SALIDA / "cupos_historial.csv"
OUTPUT_STATS = CARPETA_SALIDA / "estadisticas.html"

# Series ya colapsadas y compactadas para la página web. El historial crudo
# pasa de 3 MB y crece ~50 KB por snapshot; la página lo descargaba entero y
# parseaba 24.000 filas en el navegador en cada carga. Este archivo trae lo
# mismo que la página necesita, ~20 veces más pequeño.
OUTPUT_SERIES = CARPETA_SALIDA / "series.json"

# Bitácora de corridas: una fila por ejecución en modo cupos, con conteos de
# asignaturas esperadas/leídas/fallidas. En el análisis, TODA corrida cuyo
# run_id aparezca en cupos_historial.csv pero NO tenga fila aquí (o la tenga
# con completo=0) debe tratarse como barrido incompleto: el scraper recorre
# las asignaturas en orden, así que lo que falta no es aleatorio.
OUTPUT_RUNS = CARPETA_SALIDA / "runs.csv"

# Texto crudo de cada página de detalle, comprimido, un .jsonl.gz por corrida.
# La semana de inscripción no se repite: si el SIA cambia de formato y el
# parser falla en silencio, con esto se puede re-parsear después.
# Desactivable con la variable de entorno SIA_GUARDAR_TEXTO=0.
CARPETA_TEXTOS = CARPETA_SALIDA / "textos"
GUARDAR_TEXTO_CRUDO = os.environ.get("SIA_GUARDAR_TEXTO", "1") != "0"

# Sharding opcional para paralelizar: SIA_SHARD="1/4" procesa solo la 1.ª de
# cada 4 asignaturas, "2/4" la 2.ª, etc. Sirve para repartir el barrido entre
# varios procesos o máquinas y comprimir la ventana temporal del snapshot.
# Cada shard debe escribir en su propia copia del repo para no pisarse.
SIA_SHARD = os.environ.get("SIA_SHARD", "").strip()


def _calcular_run_id() -> str:
    """Identificador único de ESTE barrido, común a todas sus filas.

    Ojo con GITHUB_RUN_ID: desde que el workflow pasó al esquema de "bucle
    largo" (una corrida de Actions viva 5h30m que barre cada 30 min), ese
    identificador ya NO distingue un snapshot de otro: los ~11 barridos de
    una corrida lo comparten. Y como generar_series_json() colapsa por
    run_id —a propósito, para juntar las filas de teórica/taller/laboratorio
    de un mismo grupo—, sin este desempate todos esos barridos se fundirían
    en un solo punto con la marca de tiempo del primero, y la página se
    quedaría "actualizada" a la hora en que arrancó el bucle.

    Como el bucle relanza el proceso en cada vuelta, basta con sellar el
    identificador con el instante de arranque: los barridos van separados
    por INTERVALO_MIN (30 min), así que la resolución de minutos sobra.
    """
    base = os.environ.get("GITHUB_RUN_ID")
    if base:
        rid = f"gh-{base}"
        intento = os.environ.get("GITHUB_RUN_ATTEMPT")
        if intento and intento != "1":
            rid += f".{intento}"
        rid += "-" + datetime.now(timezone.utc).strftime("%m%dT%H%M")
    else:
        rid = "local-" + datetime.now().strftime("%Y%m%dT%H%M%S")
    if SIA_SHARD:
        rid += "-s" + SIA_SHARD.replace("/", "de")
    return rid


RUN_ID = _calcular_run_id()


def _tz_bogota():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Bogota")
    except Exception:
        return None  # sin tzdata: se usa la zona local con offset explícito


def ahora_iso() -> str:
    """Instante actual en ISO 8601 CON offset (p. ej. 2026-08-10T14:32:07-05:00).

    Los runners de GitHub corren en UTC y Bogotá es UTC-5; guardar el offset
    hace las marcas inambiguas se corran donde se corran."""
    tz = _tz_bogota()
    dt = datetime.now(tz) if tz else datetime.now().astimezone()
    return dt.isoformat(timespec="seconds")


def _parse_ts(ts: str) -> datetime:
    """Parsea marcas viejas ('2026-07-21T04:35+00:00') y nuevas. Ordenar por
    texto NO sirve cuando se mezclan offsets +00:00 y -05:00."""
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)

# Modo snapshot de cupos (python sia_scraper.py cupos): raspa TODO de nuevo
# (ignora la reanudación, porque el punto es medir los cupos frescos), corre
# sin ventana, y añade una fila por grupo al historial con fecha y hora.
MODO_CUPOS = False

# Rutas del navegador. Déjalas vacías para autodetección (lo normal).
# En Raspberry Pi, si la autodetección falla, apunta a Chromium a mano:
#   CHROME_BINARY = "/usr/bin/chromium-browser"
#   CHROMEDRIVER_PATH = "/usr/bin/chromedriver"
CHROME_BINARY = ""
CHROMEDRIVER_PATH = ""

CHECKPOINT_EVERY = 5      # guarda avance cada N asignaturas
REANUDAR = True           # saltar asignaturas ya presentes en catalogo.json
GUARDAR_DEBUG_SI_FALLA = True
MAX_ARCHIVOS_DEBUG = 6    # tope para no llenar la carpeta de diagnósticos
_debug_generados = 0

# Timeouts (segundos). ADF puede tardar bastante en combos pesados.
TIMEOUT_COMBO_DEFAULT = 20
TIMEOUT_COMBO_PLAN = 40
TIMEOUT_PPR_OVERLAY = 40
TIMEOUT_NAV = 20
TIMEOUT_TABLA = 30        # espera a que ADF termine de renderizar las filas

MAX_REINICIOS_DRIVER = 3
MAX_PAGINAS = 100         # tope de seguridad para la paginación


# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------

@dataclass
class Sesion:
    """Una franja de clase concreta: día, hora y lugar.

    Formato real de la línea de ubicación en el SIA (4 partes separadas
    por punto):
        PASA A SER 309. 409-306. 409 - Luis Enrique Orduz Espinosa. SALON.
        ^ nombre        ^ aula   ^ edificio                         ^ tipo
    """
    dia: str = ""
    hora_inicio: str = ""
    hora_fin: str = ""
    salon: str = ""            # código del aula, p. ej. "409-306"
    salon_nombre: str = ""     # descripción, p. ej. "SALON DE CLASE (SALA TIC)"
    edificio: str = ""         # p. ej. "409 - Luis Enrique Orduz Espinosa"
    tipo_espacio: str = ""     # SALON / AUDITORIO / LABORATORIO / ...
    lugar_raw: str = ""


@dataclass
class Grupo:
    grupo: str = ""
    actividad: str = ""        # CLASE TEORICA / LABORATORIO / TALLER / ...
    profesores: str = "No informado"
    # "NA" = no se pudo leer (distinto de "0", que es un agotado legítimo)
    cupos_disponibles: str = "NA"
    # El SIA público normalmente solo muestra disponibles; si algún día
    # expone el total, el parser lo captura. Mientras tanto, la capacidad
    # se estima como el máximo observado en la serie (con el barrido de
    # línea base ANTES del primer turno como referencia principal).
    cupos_totales: str = "NA"
    fecha: str = ""
    duracion: str = ""
    jornada: str = ""
    sesiones: List[Sesion] = field(default_factory=list)


@dataclass
class Asignatura:
    codigo: str = ""
    nombre: str = ""
    tipologia: str = ""
    creditos: str = ""
    facultad: str = ""
    planes: List[str] = field(default_factory=list)
    grupos: List[Grupo] = field(default_factory=list)
    # Momento EXACTO en que se leyó esta página y posición dentro del
    # barrido. Un barrido completo tarda muchos minutos: sin esto, la
    # primera y la última asignatura parecerían medidas al mismo tiempo.
    ts_lectura: str = ""
    orden_lectura: int = -1


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _detectar_navegador():
    """Ubica el navegador y su driver.

    En Windows/Mac basta con Selenium Manager (descarga el chromedriver solo).
    En Raspberry Pi y otros ARM eso NO funciona: Google no publica ni Chrome
    ni chromedriver para ARM Linux, así que hay que usar el Chromium del
    sistema y el chromedriver que viene con él.
    """
    import shutil

    binario = CHROME_BINARY or None
    driver_path = CHROMEDRIVER_PATH or None

    if binario is None:
        for cand in ("chromium-browser", "chromium", "google-chrome",
                     "google-chrome-stable"):
            hallado = shutil.which(cand)
            if hallado:
                binario = hallado
                break

    if driver_path is None:
        for cand in ("chromedriver", "chromium.chromedriver"):
            hallado = shutil.which(cand)
            if hallado:
                driver_path = hallado
                break
        if driver_path is None:
            for ruta in ("/usr/lib/chromium-browser/chromedriver",
                         "/usr/lib/chromium/chromedriver",
                         "/usr/bin/chromedriver"):
                if os.path.exists(ruta):
                    driver_path = ruta
                    break

    return binario, driver_path


def crear_driver(headless: bool = False):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-features=RendererCodeIntegrity,AutomationControlled")

    # Imprescindible en equipos con poca RAM (Raspberry Pi, contenedores):
    # /dev/shm es diminuto y Chrome se cae con "session deleted" sin esto.
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")

    try:
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
    except Exception:
        pass  # Chromium viejo no siempre las acepta

    binario, driver_path = _detectar_navegador()
    if binario:
        options.binary_location = binario

    if driver_path:
        from selenium.webdriver.chrome.service import Service
        driver = webdriver.Chrome(service=Service(driver_path), options=options)
    else:
        # Selenium Manager resuelve el driver (Windows / macOS / Linux x86_64)
        driver = webdriver.Chrome(options=options)

    driver.set_page_load_timeout(60)
    return driver


def sesion_viva(driver) -> bool:
    try:
        _ = driver.current_url
        return True
    except (InvalidSessionIdException, WebDriverException):
        return False


# ---------------------------------------------------------------------------
# Utilidades de espera / selección
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """minúsculas, sin tildes, espacios colapsados — para comparar textos."""
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def esperar_overlay_ppr_desaparezca(driver, timeout=TIMEOUT_PPR_OVERLAY):
    """ADF muestra un glass-pane bloqueante mientras procesa un partial submit.
    Hay que esperar a que desaparezca antes de leer el DOM resultante."""
    try:
        WebDriverWait(driver, timeout).until_not(
            EC.presence_of_element_located((
                By.XPATH,
                "//*[contains(@id,'BlockingGlassPane') or "
                "contains(@class,'AFBlockingGlassPane')]",
            ))
        )
    except TimeoutException:
        pass


# --- Lectura del estado de los combos vía JavaScript ---------------------
#
# Todas las consultas de estado se hacen con execute_script en lugar de
# find_element + Select. Motivo: una llamada JS se ejecuta de forma atómica
# dentro del navegador, así que es IMPOSIBLE que el nodo se vuelva "stale"
# a mitad de la operación (que es exactamente lo que rompía el script:
# ADF reemplazaba el <select> entre el find_element y el select_by_visible_text).

_JS_NORMALIZAR = r"""
    const norm = (s) => (s || '')
        .normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
        .replace(/\s+/g, ' ').trim().toLowerCase();
"""

_JS_ESTADO_COMBO = _JS_NORMALIZAR + r"""
    const sel = document.getElementById(arguments[0]);
    if (!sel) { return {existe: false}; }
    return {
        existe: true,
        habilitado: !sel.disabled,
        n_opciones: sel.options.length,
        seleccionado: sel.selectedIndex >= 0
            ? sel.options[sel.selectedIndex].text.trim() : ''
    };
"""

_JS_SELECCIONAR = _JS_NORMALIZAR + r"""
    const sel = document.getElementById(arguments[0]);
    if (!sel) { return {ok: false, motivo: 'el combo no existe'}; }
    const objetivo = norm(arguments[1]);

    let elegida = null;
    for (const op of sel.options) {              // coincidencia exacta
        if (norm(op.text) === objetivo) { elegida = op; break; }
    }
    if (!elegida) {                              // coincidencia parcial
        for (const op of sel.options) {
            if (norm(op.text).includes(objetivo)) { elegida = op; break; }
        }
    }
    if (!elegida) {
        return {ok: false, motivo: 'sin coincidencia',
                opciones: Array.from(sel.options).slice(0, 15).map(o => o.text.trim())};
    }

    sel.value = elegida.value;
    // ADF escucha 'change' para lanzar su refresco parcial
    sel.dispatchEvent(new Event('input',  {bubbles: true}));
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: true, texto: elegida.text.trim()};
"""


def _estado_combo(driver, select_id) -> dict:
    """Lee existencia/habilitado/nº de opciones/selección actual sin riesgo de stale."""
    try:
        estado = driver.execute_script(_JS_ESTADO_COMBO, select_id)
        return estado if isinstance(estado, dict) else {"existe": False}
    except WebDriverException:
        return {"existe": False}


def esperar_combo_habilitado(driver, select_id, timeout=TIMEOUT_COMBO_DEFAULT) -> bool:
    """Espera a que el combo exista, esté habilitado y tenga opciones cargadas."""
    esperar_overlay_ppr_desaparezca(driver, timeout=timeout)
    limite = time.time() + timeout
    while time.time() < limite:
        e = _estado_combo(driver, select_id)
        if e.get("existe") and e.get("habilitado") and e.get("n_opciones", 0) > 1:
            return True
        time.sleep(0.3)
    return False


def esperar_combo_estable(driver, select_id, timeout=TIMEOUT_COMBO_DEFAULT,
                          lecturas_iguales=3, pausa=0.35) -> bool:
    """Espera a que el combo deje de cambiar.

    No basta con que exista y tenga opciones: ADF puede estar a punto de
    reemplazar el nodo entero por el refresco parcial que disparó el combo
    anterior. Si seleccionamos justo en ese instante, la referencia queda
    'stale'. Aquí esperamos a leer el mismo número de opciones varias veces
    seguidas antes de tocarlo.
    """
    anterior, iguales = -1, 0
    limite = time.time() + timeout
    while time.time() < limite:
        n = _estado_combo(driver, select_id).get("n_opciones", -1)
        if n > 1 and n == anterior:
            iguales += 1
            if iguales >= lecturas_iguales:
                return True
        else:
            iguales = 0
        anterior = n
        time.sleep(pausa)
    return False


def _seleccionar_por_js(driver, select_id, texto_visible) -> dict:
    """Selecciona la opción resolviendo la coincidencia dentro del navegador."""
    try:
        r = driver.execute_script(_JS_SELECCIONAR, select_id, texto_visible)
        return r if isinstance(r, dict) else {"ok": False, "motivo": "respuesta inválida"}
    except WebDriverException as e:
        return {"ok": False, "motivo": str(e)[:120]}


def seleccionar(driver, select_id, texto_visible, timeout=TIMEOUT_COMBO_DEFAULT,
                reintentos=4) -> str:
    """Selecciona una opción de un combo ADF de forma resistente a 'stale'.

    Intento 1: API de Selenium (respeta mejor los eventos nativos).
    Intentos siguientes: JavaScript, que no deja ventana para que el nodo
    se vuelva stale.
    En todos los casos se VERIFICA al final que la selección quedó aplicada.
    """
    ultimo_error = "desconocido"

    for intento in range(1, reintentos + 1):
        usar_js = intento >= 2

        try:
            listo = esperar_combo_habilitado(driver, select_id, timeout)
            estable = esperar_combo_estable(driver, select_id, timeout)

            if not listo:
                ultimo_error = "el combo nunca se habilitó ni cargó opciones"
                print(f"  ~ {select_id}: {ultimo_error} ({intento}/{reintentos})...")
                time.sleep(1.0)
                continue
            if not estable and not usar_js:
                # aún se está recargando: con Selenium es riesgoso, mejor
                # dejar que el siguiente intento lo haga por JS
                ultimo_error = "el combo seguía recargándose"
                print(f"  ~ {select_id}: {ultimo_error}, se usará JavaScript "
                      f"({intento}/{reintentos})...")
                time.sleep(1.0)
                continue

            if usar_js:
                r = _seleccionar_por_js(driver, select_id, texto_visible)
                if not r.get("ok"):
                    if r.get("motivo") == "sin coincidencia":
                        raise NoSuchElementException(
                            f"No se encontró la opción '{texto_visible}' en {select_id}. "
                            f"Primeras opciones disponibles: {r.get('opciones')}"
                        )
                    ultimo_error = r.get("motivo", "fallo de JS")
                    print(f"  ~ {select_id}: {ultimo_error} ({intento}/{reintentos})...")
                    time.sleep(1.0)
                    continue
                texto_exacto = r["texto"]
            else:
                texto_exacto = _resolver_texto_opcion(driver, select_id, texto_visible)
                Select(driver.find_element(By.ID, select_id)).select_by_visible_text(texto_exacto)

            esperar_overlay_ppr_desaparezca(driver, timeout=timeout)

            # Verificación final: ¿de verdad quedó seleccionado?
            actual = _estado_combo(driver, select_id).get("seleccionado", "")
            if actual and normalizar(texto_exacto) == normalizar(actual):
                if normalizar(texto_exacto) != normalizar(texto_visible):
                    print(f"  ~ '{texto_visible}' no coincidía exacto; "
                          f"se usó '{texto_exacto}'")
                return texto_exacto

            ultimo_error = f"la selección no se aplicó (quedó '{actual}')"
            print(f"  ~ {select_id}: {ultimo_error} ({intento}/{reintentos})...")

        except StaleElementReferenceException:
            ultimo_error = "el elemento cambió mientras se seleccionaba"
            print(f"  ~ {select_id}: {ultimo_error}, se usará JavaScript "
                  f"({intento}/{reintentos})...")
        except NoSuchElementException as e:
            # opción inexistente: reintentar no ayuda
            guardar_debug(driver, f"combo_{select_id.replace(':', '_')}")
            raise RuntimeError(str(e))

        time.sleep(1.0)

    guardar_debug(driver, f"combo_{select_id.replace(':', '_')}")
    raise RuntimeError(
        f"No se pudo seleccionar '{texto_visible}' en {select_id} tras "
        f"{reintentos} intentos. Último problema: {ultimo_error}"
    )


def _resolver_texto_opcion(driver, select_id, texto_visible) -> str:
    """Texto EXACTO de la opción a seleccionar (para la ruta con Selenium)."""
    combo = Select(driver.find_element(By.ID, select_id))
    textos = [op.text for op in combo.options]

    for t in textos:
        if t.strip() == texto_visible.strip():
            return t

    objetivo = normalizar(texto_visible)
    for t in textos:
        if objetivo in normalizar(t):
            return t

    raise NoSuchElementException(
        f"No se encontró la opción '{texto_visible}' en {select_id}. "
        f"Primeras opciones disponibles: {[t.strip() for t in textos][:15]}"
    )


def hay_dialogo_sesion_caducada(driver) -> bool:
    return len(driver.find_elements(By.XPATH, "//*[contains(text(),'Página Caducada')]")) > 0


def manejar_sesion_caducada(driver) -> bool:
    if hay_dialogo_sesion_caducada(driver):
        try:
            driver.find_element(By.XPATH, "//button[contains(text(),'Aceptar')]").click()
            time.sleep(1)
        except NoSuchElementException:
            pass
        return True
    return False


def guardar_debug(driver, motivo="debug"):
    global _debug_generados
    if not GUARDAR_DEBUG_SI_FALLA or _debug_generados >= MAX_ARCHIVOS_DEBUG:
        return
    _debug_generados += 1
    ts = int(time.time())
    png = CARPETA_SALIDA / f"debug_{motivo}_{ts}.png"
    html = CARPETA_SALIDA / f"debug_{motivo}_{ts}.html"
    try:
        driver.save_screenshot(str(png))
        html.write_text(driver.page_source, encoding="utf-8")
        print("  -> Archivos de diagnóstico guardados en:")
        print(f"       {png}")
        print(f"       {html}")
    except Exception as e:
        print(f"  ! No se pudo guardar debug: {e}")


# ---------------------------------------------------------------------------
# Configuración de filtros y búsqueda
# ---------------------------------------------------------------------------

def configurar_filtros(driver, plan, reintentos=3):
    """Aplica los cinco filtros y lanza la búsqueda.

    Si algo falla a medio camino (típico en ADF: un combo se recarga y deja
    la página en un estado inconsistente), se recarga la página y se empieza
    la secuencia de cero, que es más confiable que intentar remendarla.
    """
    ultimo_error = None
    for intento in range(1, reintentos + 1):
        try:
            driver.get(BASE_URL)
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.ID, ID_NIVEL))
            )
            time.sleep(2)

            seleccionar(driver, ID_NIVEL, NIVEL_ESTUDIO)
            seleccionar(driver, ID_SEDE, SEDE)
            seleccionar(driver, ID_FACULTAD, FACULTAD)
            # El combo de Plan de Estudios es el más lento (lista larga,
            # depende de las tres selecciones anteriores)
            seleccionar(driver, ID_PLAN, plan, timeout=TIMEOUT_COMBO_PLAN)
            seleccionar(driver, ID_TIPOLOGIA, TIPOLOGIA)

            WebDriverWait(driver, TIMEOUT_NAV).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//a[.//text()='Mostrar'] | //button[.//text()='Mostrar']")
                )
            ).click()

            WebDriverWait(driver, TIMEOUT_NAV).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(text(),'Resultado de la consulta')]")
                )
            )
            esperar_overlay_ppr_desaparezca(driver)
            return

        except (RuntimeError, TimeoutException, StaleElementReferenceException,
                NoSuchElementException) as e:
            ultimo_error = e
            print(f"  ! Falló la configuración de filtros "
                  f"(intento {intento}/{reintentos}): {type(e).__name__}")
            if intento < reintentos:
                print("  ! Recargando la página y empezando de cero...")
                time.sleep(2)

    guardar_debug(driver, "configurar_filtros")
    raise RuntimeError(f"No se pudieron configurar los filtros: {ultimo_error}")



# ---------------------------------------------------------------------------
# Paginación + recolección de códigos
# ---------------------------------------------------------------------------

# Los códigos del SIA no son solo dígitos: hay muchos con sufijo, como
# "1000003-B" (20 de las 69 asignaturas de Ing. Civil son así). Un regex de
# solo dígitos descartaba silenciosamente casi un tercio del plan.
CODIGO_RE = re.compile(r"^\d{4,}(?:-[A-Za-z0-9]+)?$")

# La tabla de resultados NO es un <table> normal: ADF la renderiza como
#   <div role="grid" id="pt1:r1:0:t4">
#     <table class="af_column_column-header-table">   <- solo encabezados
#     <div class="af_table_data-body">
#        <table class="af_table_data-table"> ... <tr class="af_table_data-row">
#            <td><span><a class="af_commandLink">2015938</a></span></td>
#
# Por eso "following::table[1]" agarraba la tabla de ENCABEZADOS (sin enlaces)
# y devolvía cero. Estos selectores apuntan al cuerpo de datos real.
XPATHS_ENLACES_CODIGO = [
    "//div[@role='grid']//a[contains(@class,'af_commandLink')]",
    "//div[contains(@class,'af_table_data-body')]//a",
    "//tr[contains(@class,'af_table_data-row')]//a",
    "//a[contains(@class,'af_commandLink')]",
]

# Lee los códigos directamente en el navegador: una sola llamada, sin riesgo
# de 'stale' y sin depender de que el texto sea "visible" para Selenium.
_JS_LEER_CODIGOS = r"""
    const selectores = arguments[0];
    const patron = /^\d{4,}(-[A-Za-z0-9]+)?$/;
    let salida = [];
    for (const sel of selectores) {
        const nodos = document.evaluate(
            sel, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        const encontrados = [];
        for (let i = 0; i < nodos.snapshotLength; i++) {
            const t = (nodos.snapshotItem(i).textContent || '').trim();
            if (patron.test(t)) { encontrados.push(t); }
        }
        if (encontrados.length > 0) { salida = encontrados; break; }
    }
    // total de filas que ADF dice tener, para saber si faltan por renderizar
    const tabla = document.querySelector("div[role='grid'] table[_rowcount]");
    const total = tabla ? parseInt(tabla.getAttribute('_rowcount'), 10) : -1;
    return {codigos: salida, total_declarado: isNaN(total) ? -1 : total};
"""


def _leer_codigos_js(driver) -> dict:
    try:
        r = driver.execute_script(_JS_LEER_CODIGOS, XPATHS_ENLACES_CODIGO)
        if isinstance(r, dict):
            return r
    except WebDriverException:
        pass
    return {"codigos": [], "total_declarado": -1}


def esperar_filas_resultados(driver, timeout=TIMEOUT_TABLA) -> List[str]:
    """Espera a que la tabla de resultados TERMINE de renderizar sus filas.

    Esta es la causa raíz del "0 asignaturas": el encabezado
    "Resultado de la consulta" aparece de inmediato, pero ADF puebla el cuerpo
    de la tabla un instante después. El código leía el DOM en ese hueco y se
    encontraba la tabla vacía.

    Estrategia: esperar a que aparezca al menos una fila y luego a que el
    número de filas se estabilice (o alcance el total que ADF declara).
    """
    limite = time.time() + timeout
    anterior, estables = -1, 0
    mejor: List[str] = []
    total_max = -1   # el mayor "_rowcount" visto: ADF puede reportarlo tarde

    while time.time() < limite:
        r = _leer_codigos_js(driver)
        codigos = r.get("codigos", []) or []
        total = r.get("total_declarado", -1)
        if total > total_max:
            total_max = total

        if len(codigos) > len(mejor):
            mejor = codigos

        if codigos:
            # ¿ya están todas las que ADF dice que hay?
            if total_max > 0 and len(codigos) >= total_max:
                return codigos
            # ¿el conteo dejó de crecer? (solo damos por bueno un conteo
            # estable si ADF no declaró un total mayor; si lo declaró,
            # seguimos esperando a que aparezcan las filas que faltan)
            if len(codigos) == anterior:
                estables += 1
                if estables >= 3 and total_max <= 0:
                    return codigos
            else:
                estables = 0
        anterior = len(codigos)
        time.sleep(0.4)

    if total_max > 0 and len(mejor) < total_max:
        print(f"    (aviso: ADF declara {total_max} filas pero solo se "
              f"pudieron leer {len(mejor)})")
    return mejor


def _forzar_render_filas(driver, objetivo: int, intentos=6):
    """Si ADF renderiza solo las filas visibles, hace scroll dentro de la
    tabla para obligarlo a cargar el resto."""
    script = """
        const db = document.querySelector("div[class*='af_table_data-body']");
        if (db) { db.scrollTop = db.scrollHeight; return true; }
        window.scrollTo(0, document.body.scrollHeight);
        return false;
    """
    for _ in range(intentos):
        actuales = len(_leer_codigos_js(driver).get("codigos", []))
        if objetivo <= 0 or actuales >= objetivo:
            return
        try:
            driver.execute_script(script)
        except WebDriverException:
            return
        time.sleep(0.8)


def codigos_en_pagina_actual(driver) -> List[str]:
    """Códigos de asignatura visibles en la página actual de resultados."""
    esperar_overlay_ppr_desaparezca(driver, timeout=TIMEOUT_COMBO_DEFAULT)

    codigos = esperar_filas_resultados(driver)

    # Si ADF declara más filas de las que encontramos, forzamos el render
    total = _leer_codigos_js(driver).get("total_declarado", -1)
    if total > 0 and len(codigos) < total:
        print(f"    (ADF declara {total} filas y van {len(codigos)}; "
              f"haciendo scroll para cargar el resto...)")
        _forzar_render_filas(driver, total)
        codigos = _leer_codigos_js(driver).get("codigos", []) or codigos

    if not codigos:
        # Último recurso: la vía clásica de Selenium, por si el JS falló
        for xp in XPATHS_ENLACES_CODIGO:
            encontrados = []
            for a in driver.find_elements(By.XPATH, xp):
                try:
                    t = a.text.strip()
                except StaleElementReferenceException:
                    continue
                if CODIGO_RE.match(t):
                    encontrados.append(t)
            if encontrados:
                return encontrados

    # quitar duplicados conservando el orden
    vistos, unicos = set(), []
    for c in codigos:
        if c not in vistos:
            vistos.add(c)
            unicos.append(c)
    return unicos


# Controles de paginación de la tabla ADF. Hasta ahora solo se conocía el de
# "Siguiente", y eso bastaba mientras el listado cupiera en una sola página.
# Cuando NO cabe, la recolección termina parada en la última página y ya no
# hay forma de volver: todas las asignaturas de páginas anteriores quedaban
# inalcanzables y se contaban como fallidas. Por eso hacen falta los tres.
_XPATHS_PAGINACION = {
    "siguiente": [
        "//a[normalize-space(text())='Siguiente']",
        "//a[contains(@title,'Siguiente')]",
        "//a[normalize-space(text())='>']",
        "//a[contains(@class,'af_table_link-next')]",
        "//*[contains(@id,'::nextIcon')]",
        "//a[normalize-space(text())='Next']",
    ],
    "anterior": [
        "//a[normalize-space(text())='Anterior']",
        "//a[contains(@title,'Anterior')]",
        "//a[normalize-space(text())='<']",
        "//a[contains(@class,'af_table_link-previous')]",
        "//*[contains(@id,'::prevIcon')]",
        "//a[normalize-space(text())='Previous']",
    ],
    "primera": [
        "//a[normalize-space(text())='Primera']",
        "//a[contains(@title,'Primera')]",
        "//a[normalize-space(text())='<<']",
        "//a[contains(@class,'af_table_link-first')]",
        "//*[contains(@id,'::firstIcon')]",
        "//a[normalize-space(text())='First']",
    ],
}


def _control_paginacion(driver, cual: str):
    """Devuelve el control de paginación pedido si existe y está activo."""
    for xp in _XPATHS_PAGINACION.get(cual, []):
        for el in driver.find_elements(By.XPATH, xp):
            try:
                if not (el.is_displayed() and el.is_enabled()):
                    continue
                clase = (el.get_attribute("class") or "").lower()
                if "disabled" in clase:
                    continue
                return el
            except StaleElementReferenceException:
                continue
    return None


def _control_siguiente(driver):
    return _control_paginacion(driver, "siguiente")


def _clic_paginacion(driver, el) -> bool:
    """Clic en un control de paginación + espera del refresco parcial."""
    try:
        driver.execute_script("arguments[0].click();", el)
    except WebDriverException:
        return False
    esperar_overlay_ppr_desaparezca(driver)
    time.sleep(0.5)
    return True


def hay_paginacion(driver) -> bool:
    return any(_control_paginacion(driver, c) is not None
               for c in ("siguiente", "anterior", "primera"))


def ir_a_primera_pagina(driver, max_saltos=MAX_PAGINAS) -> bool:
    """Rebobina el listado a la página 1.

    Se llama SIEMPRE al terminar de recolectar códigos: la recolección deja
    el navegador en la última página, y el bucle de clics recorre las
    asignaturas en el orden original (o sea, empezando por la primera).
    """
    primera = _control_paginacion(driver, "primera")
    if primera is not None and _clic_paginacion(driver, primera):
        return True
    # Sin botón "Primera": retroceder de a una hasta que se acabe
    for _ in range(max_saltos):
        anterior = _control_paginacion(driver, "anterior")
        if anterior is None:
            return True          # ya no se puede retroceder: estamos en la 1
        if not _clic_paginacion(driver, anterior):
            return False
    return False


# Cuánto esperar entre dos lecturas del listado para considerarlo asentado,
# y cuántas veces reintentar. 12 x 1.2 s = ~15 s de margen, que sobra: en las
# corridas buenas la tabla se estabiliza en la segunda lectura.
ESPERA_ESTABILIDAD_S = 1.2
INTENTOS_ESTABILIDAD = 12

# Si el listado trae menos de este porcentaje de la última corrida buena,
# es basura y hay que rehacer la búsqueda. No se pone un margen por arriba
# porque la oferta puede crecer de verdad; para eso está la doble lectura.
UMBRAL_LISTADO = 0.85

# Qué parte del listado tiene que coincidir con el plan ya conocido. En las
# corridas buenas el solape es del 100%; en las fallidas, del 0% (el listado
# era de posgrado, o de otra facultad). Cualquier umbral intermedio separa
# los dos casos, así que 0.5 deja sitio de sobra a que la oferta cambie.
UMBRAL_SOLAPE = 0.5

# Metadatos de la última recolección, para runs.csv.
INFO_LISTADO = {"paginas": 0, "intentos": 1}


def esperar_listado_estable(driver, verbose=True) -> List[str]:
    """Devuelve los códigos de la página actual, pero solo cuando la tabla
    deja de moverse.

    El SIA es una app ADF que refresca la tabla por partes. Tras pulsar
    "Mostrar", el conjunto de filas puede cambiar durante uno o dos segundos
    más. Leer en ese hueco es lo que producía listados de 4, 52 o 101
    asignaturas donde había 65: demasiado pronto se recogen menos filas de
    las que hay; justo durante un re-render se mezclan dos conjuntos de
    resultados distintos. Después el bucle intenta hacer clic en códigos que
    ya no están en la tabla y falla la corrida entera.

    El criterio es simple: dos lecturas consecutivas idénticas (misma lista
    y mismo orden) separadas por ESPERA_ESTABILIDAD_S.
    """
    previo = None
    for intento in range(INTENTOS_ESTABILIDAD):
        actuales = codigos_en_pagina_actual(driver)
        if actuales and actuales == previo:
            return actuales
        previo = actuales
        time.sleep(ESPERA_ESTABILIDAD_S)
    if verbose:
        print(f"  ! El listado no se asentó tras "
              f"{INTENTOS_ESTABILIDAD * ESPERA_ESTABILIDAD_S:.0f} s; "
              f"sigo con {len(previo or [])} códigos.")
    return previo or []


def _n_asignaturas_de_referencia() -> Optional[int]:
    """Cuántas asignaturas cabe esperar, según la última corrida COMPLETA.

    Se usa solo como piso de cordura, nunca como techo: la oferta puede
    crecer legítimamente entre semestres.
    """
    if OUTPUT_RUNS.exists():
        try:
            with open(OUTPUT_RUNS, encoding="utf-8-sig") as f:
                buenas = [int(r["n_esperadas"]) for r in csv.DictReader(f)
                          if r.get("completo") == "1"
                          and (r.get("n_esperadas") or "").isdigit()]
            if buenas:
                return buenas[-1]
        except (OSError, ValueError, KeyError):
            pass
    if OUTPUT_JSON.exists():
        try:
            with open(OUTPUT_JSON, encoding="utf-8") as f:
                n = len(json.load(f))
            return n or None
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return None


def _codigos_de_referencia() -> Optional[set]:
    """Los códigos del plan según el último catálogo publicado.

    Solo tiene sentido con UN plan configurado: con varios, catalogo.json
    mezcla los códigos de todos y el solape deja de discriminar.
    """
    if len(PLANES_ESTUDIOS) != 1 or not OUTPUT_JSON.exists():
        return None
    try:
        with open(OUTPUT_JSON, encoding="utf-8") as f:
            codigos = {a["codigo"] for a in json.load(f) if a.get("codigo")}
        return codigos or None
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        return None


def recolectar_con_verificacion(driver, plan, intentos=3,
                                verbose=True) -> List[str]:
    """Recolecta los códigos y no se los cree hasta comprobarlos.

    Tres comprobaciones, porque ninguna sola basta:

    0. ¿ES ESTE PLAN? Se compara el listado con los códigos del último
       catálogo publicado. En las corridas buenas el solape es del 100%; en
       las fallidas fue del 0%, porque el SIA devolvía el listado de
       posgrado o de otra facultad. Es la señal más limpia que hay, y la
       única que detecta un listado equivocado pero ESTABLE.

    1. DOBLE LECTURA. Se recorre el listado entero dos veces seguidas y las
       dos listas tienen que ser idénticas. Si la tabla está mutando, las
       dos lecturas discrepan y se detecta sin necesidad de saber cuántas
       asignaturas "debería" haber. Es la comprobación que de verdad
       distingue un listado inestable de una oferta que cambió.
    2. PISO DE CORDURA. Además, el total no puede caer por debajo del 85% de
       la última corrida completa. Esto ataja el caso extremo (recolectar 4
       asignaturas) sin esperar a la segunda lectura.

    Si algo no cuadra, se rehacen los filtros y se vuelve a intentar. Vale
    la pena: una recolección cuesta segundos y una corrida fallida cuesta
    el barrido completo.
    """
    referencia = _n_asignaturas_de_referencia()
    piso = int(referencia * UMBRAL_LISTADO) if referencia else 0
    conocidos = _codigos_de_referencia()
    mejor: List[str] = []

    for intento in range(1, intentos + 1):
        INFO_LISTADO["intentos"] = intento
        primera = recolectar_todos_los_codigos(driver, verbose=verbose)
        segunda = recolectar_todos_los_codigos(driver, verbose=False)

        if len(primera) > len(mejor):
            mejor = primera

        solape = None
        if conocidos:
            solape = len(set(primera) & conocidos) / len(conocidos)

        if not primera:
            motivo = "el listado salió vacío"
        elif solape is not None and solape < UMBRAL_SOLAPE:
            motivo = (f"el listado no es de este plan: solo el "
                      f"{solape:.0%} de las asignaturas conocidas aparece "
                      f"(p. ej. {', '.join(primera[:3])})")
        elif primera != segunda:
            motivo = (f"dos lecturas del listado no coinciden "
                      f"({len(primera)} vs {len(segunda)} asignaturas)")
        elif len(primera) < piso:
            motivo = (f"el listado trae {len(primera)} asignaturas y la "
                      f"última corrida buena trajo {referencia}")
        else:
            if referencia and len(primera) != referencia and verbose:
                print(f"  (ojo: {len(primera)} asignaturas frente a "
                      f"{referencia} de la última corrida completa; "
                      f"la oferta pudo cambiar)")
            return primera

        print(f"  ! {motivo}. Rehaciendo la búsqueda "
              f"({intento}/{intentos})...")
        guardar_debug(driver, f"listado_inestable_{len(primera)}")
        if intento < intentos:
            configurar_filtros(driver, plan)

    print(f"  ! El listado nunca cuadró tras {intentos} intentos; sigo con "
          f"el mejor obtenido ({len(mejor)} asignaturas).")
    return mejor


def recolectar_todos_los_codigos(driver, verbose=True) -> List[str]:
    """Recorre TODAS las páginas de la tabla de resultados y devuelve la lista
    completa de códigos, sin duplicados y en orden de aparición."""
    vistos: List[str] = []
    set_vistos = set()

    for pagina in range(1, MAX_PAGINAS + 1):
        # No basta con codigos_en_pagina_actual(): hay que esperar a que la
        # tabla deje de cambiar antes de fiarse de lo que dice.
        actuales = esperar_listado_estable(driver, verbose=verbose)
        nuevos = [c for c in actuales if c not in set_vistos]
        for c in nuevos:
            set_vistos.add(c)
            vistos.append(c)
        if verbose:
            print(f"  Página {pagina}: {len(actuales)} códigos "
                  f"({len(nuevos)} nuevos) | acumulado: {len(vistos)}")

        if not actuales and pagina == 1:
            guardar_debug(driver, "sin_asignaturas")
            break

        siguiente = _control_siguiente(driver)
        if siguiente is None:
            break

        if not _clic_paginacion(driver, siguiente):
            break

        # Si tras avanzar no aparece nada nuevo, cortamos para no ciclar
        if not [c for c in esperar_listado_estable(driver, verbose=False)
                if c not in set_vistos]:
            break

    # IMPRESCINDIBLE: el bucle de arriba deja el listado en la última página.
    # Sin este rebobinado, click_asignatura_por_codigo() solo alcanza las
    # asignaturas de esa página (y de las siguientes, que ya no hay), y el
    # resto del plan se pierde. Era la causa de los barridos de 10-25 materias.
    if pagina > 1 or hay_paginacion(driver):
        if verbose:
            print(f"  Listado paginado ({pagina} páginas); volviendo a la primera...")
        if not ir_a_primera_pagina(driver):
            print("  ! No se pudo volver a la primera página del listado.")

    INFO_LISTADO["paginas"] = pagina
    return vistos


# --- Clic en una asignatura -----------------------------------------------
#
# El clic se hace por JavaScript usando EXACTAMENTE el mismo criterio con el
# que se recolectaron los códigos (textContent). Antes la recolección usaba
# textContent pero el clic usaba XPath text(), que solo ve el texto directo
# del <a>: si el código viene envuelto en un <span>, o la fila está fuera
# del área visible, la recolección lo encontraba pero el clic no — y la
# asignatura se omitía tras 3 reintentos sin dejar rastro.

_JS_CLICK_CODIGO = r"""
    const objetivo = (arguments[0] || '').trim();
    const nodos = document.querySelectorAll(
        "div[role='grid'] a, a[class*='af_commandLink']");
    for (const a of nodos) {
        if ((a.textContent || '').trim() === objetivo) {
            try { a.scrollIntoView({block: 'center'}); } catch (e) {}
            a.click();
            return true;
        }
    }
    return false;
"""

_JS_EXISTE_CODIGO = r"""
    const objetivo = (arguments[0] || '').trim();
    const nodos = document.querySelectorAll(
        "div[role='grid'] a, a[class*='af_commandLink']");
    for (const a of nodos) {
        if ((a.textContent || '').trim() === objetivo) { return true; }
    }
    return false;
"""


def _js_click_codigo(driver, codigo: str) -> bool:
    try:
        return bool(driver.execute_script(_JS_CLICK_CODIGO, codigo))
    except WebDriverException:
        return False


def _js_existe_codigo(driver, codigo: str) -> bool:
    try:
        return bool(driver.execute_script(_JS_EXISTE_CODIGO, codigo))
    except WebDriverException:
        return False


def ir_a_pagina_con_codigo(driver, codigo: str, max_saltos=MAX_PAGINAS) -> bool:
    """Busca el código en el listado, avanzando páginas si hace falta.

    Da la vuelta: si llega al final sin encontrarlo, rebobina a la página 1 y
    sigue buscando. Antes solo avanzaba, así que un código que hubiera quedado
    atrás era irrecuperable — y como cada 'Volver' del SIA puede dejar el
    listado en cualquier página, eso pasaba constantemente.
    """
    dio_vuelta = False
    for _ in range(max_saltos * 2):
        if _js_existe_codigo(driver, codigo):
            return True
        siguiente = _control_siguiente(driver)
        if siguiente is None:
            if dio_vuelta:
                return False          # ya recorrimos el listado entero
            dio_vuelta = True
            if not ir_a_primera_pagina(driver):
                return False
            continue
        if not _clic_paginacion(driver, siguiente):
            return False
    return False


def _xpath_enlace_codigo(codigo: str) -> str:
    """(respaldo Selenium) enlace de un código en la tabla de resultados."""
    return (
        f"//div[@role='grid']//a[normalize-space(.)='{codigo}']"
        f" | //a[contains(@class,'af_commandLink')][normalize-space(.)='{codigo}']"
    )


# Marcadores de que ya estamos en la página de detalle. Se aceptan varios
# porque el texto exacto puede variar entre asignaturas.
_XP_DETALLE = " | ".join([
    "//*[contains(text(),'Información de la asignatura')]",
    "//*[contains(text(),'Informacion de la asignatura')]",
    "//*[contains(text(),'Contenido de la asignatura')]",
    "//*[contains(text(),'Tipología:')]",
    "//*[contains(text(),'Créditos:')]",
])


def click_asignatura_por_codigo(driver, codigo: str):
    # 1) clic por JS con el mismo criterio de la recolección (textContent)
    hizo_click = _js_click_codigo(driver, codigo)

    # 2) si no está en el DOM actual, buscar en otras páginas del listado
    if not hizo_click:
        if ir_a_pagina_con_codigo(driver, codigo):
            hizo_click = _js_click_codigo(driver, codigo)

    # 3) último recurso: la vía Selenium con normalize-space(.) — el punto
    #    "." incluye el texto de nodos anidados, a diferencia de text()
    if not hizo_click:
        enlaces = driver.find_elements(By.XPATH, _xpath_enlace_codigo(codigo))
        if enlaces:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});"
                    "arguments[0].click();", enlaces[0])
                hizo_click = True
            except WebDriverException:
                pass

    if not hizo_click:
        print(f"  ! El enlace de {codigo} no aparece en el listado.")
        guardar_debug(driver, f"sin_enlace_{codigo}")
        raise NoSuchElementException(f"Enlace de {codigo} no encontrado")

    # 4) confirmar que llegamos al detalle
    try:
        WebDriverWait(driver, TIMEOUT_NAV).until(
            EC.presence_of_element_located((By.XPATH, _XP_DETALLE))
        )
    except TimeoutException:
        print(f"  ! Tras hacer clic en {codigo} no se reconoció la página de detalle.")
        guardar_debug(driver, f"tras_click_{codigo}")
        raise

    esperar_overlay_ppr_desaparezca(driver)


def en_listado_resultados(driver) -> bool:
    """¿Seguimos en la tabla de resultados? Evita reconstruir la búsqueda
    entera (que es lentísima) cuando en realidad no nos hemos movido."""
    try:
        return bool(driver.find_elements(
            By.XPATH, "//div[@role='grid']//a[contains(@class,'af_commandLink')]"
        ))
    except WebDriverException:
        return False


def volver_a_resultados(driver):
    boton = WebDriverWait(driver, TIMEOUT_NAV).until(
        EC.element_to_be_clickable(
            (By.XPATH, "(//a[.//text()='Volver'] | //button[.//text()='Volver'])[1]")
        )
    )
    driver.execute_script("arguments[0].click();", boton)
    WebDriverWait(driver, TIMEOUT_NAV).until(
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(text(),'Resultado de la consulta')]")
        )
    )
    esperar_overlay_ppr_desaparezca(driver)


# ---------------------------------------------------------------------------
# Parsing del detalle de una asignatura
# ---------------------------------------------------------------------------

# El parser trabaja sobre el TEXTO de la página (driver.find_element(TAG_NAME,
# "body").text), cuyo formato real es:
#
#     Contenido de la asignatura
#     CLASE TEORICA (2015966)          <- tipo de actividad
#     (1) Grupo 1                      <- OJO: viene con sangría
#     Profesor: Nombre Uno. NOMBRE DOS.
#     Fecha:02/02/2026 - 30/05/2026
#     LUNES de 07:00 a 08:00.          <- día y hora
#     PASA A SER 309. 409-306. 409 - Luis Enrique Orduz Espinosa. SALON.
#                                      ^ la UBICACIÓN va en la línea siguiente
#     Duración: Semestral
#     Jornada: DIURNO
#     Cupos disponibles: 5
#
# Dos detalles rompían el parser anterior: la sangría de "(1) Grupo 1" (el
# split exigía "(" justo después del salto de línea) y que el salón no está
# en la misma línea que el horario.

DIAS = r"(LUNES|MARTES|MI[EÉ]RCOLES|JUEVES|VIERNES|S[AÁ]BADO|DOMINGO)"

RE_SESION = re.compile(
    rf"^\s*{DIAS}\s+de\s+(\d{{1,2}}[:.]\d{{2}})\s+a\s+(\d{{1,2}}[:.]\d{{2}})\s*\.?\s*$",
    re.IGNORECASE,
)
RE_GRUPO = re.compile(r"^\s*\((\w+)\)\s*(.*)$")
RE_ACTIVIDAD = re.compile(r"^\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s/\-]{3,})\s*\(([\w\-]+)\)\s*$")


def _limpiar(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip(" .,;-|")


def _parsear_ubicacion(linea: str) -> Sesion:
    """Descompone la línea de ubicación en aula, edificio y tipo de espacio."""
    s = Sesion(lugar_raw=linea.strip())
    partes = [p.strip() for p in linea.strip().rstrip(".").split(". ") if p.strip()]

    if len(partes) >= 4:
        s.salon_nombre, s.salon, s.edificio, s.tipo_espacio = (
            partes[0], partes[1], partes[2], partes[-1]
        )
    elif len(partes) == 3:
        s.salon_nombre, s.salon, s.edificio = partes
    elif len(partes) == 2:
        s.salon_nombre, s.salon = partes
    elif partes:
        s.salon_nombre = partes[0]
    return s


def _parsear_profesores(texto: str) -> str:
    """'Paula Solarte Blandon. NICOLAS GUTIERREZ ARIAS.' -> separados por ';'"""
    nombres = [p.strip() for p in texto.rstrip(".").split(". ") if p.strip()]
    return "; ".join(nombres) if nombres else "No informado"


def _inicio_de_los_grupos(body_text: str) -> int:
    """Posición donde arranca la lista de grupos.

    Lo normal es que el SIA rotule esa sección con "Contenido de la
    asignatura". Pero NO siempre: Álgebra Lineal, Taller de proyectos
    interdisciplinarios y Trabajo de Grado de posgrado traen los grupos sin
    ese rótulo y sin sangría, así:

        Facultad: FACULTAD DE INGENIERÍA
        CLASE TEORICA (2024045)
        (1) 2024045-1 Taller de proyectos interdisciplinarios

    El parser cortaba ahí y las guardaba con cero grupos, así que
    desaparecían de horarios.csv, del historial y de la página. Eran 42
    grupos por barrido.

    Respaldo: la primera línea que sea cabecera de actividad
    ("CLASE TEORICA (2024045)") o de grupo ("(1) Grupo 1"). Empezar unas
    líneas antes de la cuenta es inofensivo: las líneas de la cabecera
    ("Tipología:", "Créditos:") no casan con ningún patrón de grupo.
    """
    ancla = body_text.find("Contenido de la asignatura")
    if ancla != -1:
        return ancla
    desplazamiento = 0
    for linea in body_text.splitlines(keepends=True):
        if RE_ACTIVIDAD.match(linea) or RE_GRUPO.match(linea):
            return desplazamiento
        desplazamiento += len(linea)
    return -1


def parsear_texto_detalle(body_text: str, codigo: str) -> Asignatura:
    """Parser puro (sin Selenium) para poder probarlo con texto guardado."""
    asig = Asignatura(codigo=codigo)

    m = re.search(r"Volver\s*\n\s*(.+?)\s*\(", body_text)
    asig.nombre = _limpiar(m.group(1)) if m else codigo
    m = re.search(r"Tipolog[ií]a:\s*(.+)", body_text)
    asig.tipologia = _limpiar(m.group(1)) if m else ""
    m = re.search(r"Cr[eé]ditos:\s*(\d+)", body_text)
    asig.creditos = m.group(1) if m else ""
    m = re.search(r"Facultad:\s*(.+)", body_text)
    asig.facultad = _limpiar(m.group(1)) if m else ""

    ini = _inicio_de_los_grupos(body_text)
    if ini == -1:
        return asig
    fin = len(body_text)
    for marca in ("Prerrequisitos", "Correquisitos"):
        p = body_text.find(marca, ini)
        if p != -1:
            fin = min(fin, p)
    seccion = body_text[ini:fin]

    actividad_actual = ""
    grupo_actual: Optional[Grupo] = None
    sesion_pendiente: Optional[Sesion] = None

    for linea in seccion.splitlines():
        limpia = linea.strip()
        if not limpia:
            continue

        # ¿día + horas? -> nueva sesión
        m = RE_SESION.match(linea)
        if m and grupo_actual is not None:
            sesion_pendiente = Sesion(
                dia=_limpiar(m.group(1)).upper(),
                hora_inicio=m.group(2).replace(".", ":"),
                hora_fin=m.group(3).replace(".", ":"),
            )
            grupo_actual.sesiones.append(sesion_pendiente)
            continue

        # la línea siguiente a una sesión es su ubicación
        if sesion_pendiente is not None:
            if not limpia.startswith(("Duración", "Duracion", "Jornada",
                                      "Cupos", "Fecha")):
                u = _parsear_ubicacion(limpia)
                sesion_pendiente.salon = u.salon
                sesion_pendiente.salon_nombre = u.salon_nombre
                sesion_pendiente.edificio = u.edificio
                sesion_pendiente.tipo_espacio = u.tipo_espacio
                sesion_pendiente.lugar_raw = u.lugar_raw
                sesion_pendiente = None
                continue
            sesion_pendiente = None

        # ¿nuevo grupo?
        m = RE_GRUPO.match(linea)
        if m:
            grupo_actual = Grupo(
                grupo=_limpiar(f"({m.group(1)}) {m.group(2)}"),
                actividad=actividad_actual,
            )
            asig.grupos.append(grupo_actual)
            continue

        # ¿cabecera de actividad? (p. ej. "CLASE TEORICA (2015966)")
        m = RE_ACTIVIDAD.match(linea)
        if m and "Contenido" not in limpia:
            actividad_actual = _limpiar(m.group(1))
            continue

        if grupo_actual is None:
            continue

        if limpia.startswith("Profesor"):
            grupo_actual.profesores = _parsear_profesores(
                limpia.split(":", 1)[1] if ":" in limpia else ""
            )
        elif limpia.startswith("Fecha"):
            grupo_actual.fecha = _limpiar(
                limpia.split(":", 1)[1] if ":" in limpia else "")
        elif limpia.startswith(("Cupos totales", "Cupos total")):
            m2 = re.search(r"(\d+)", limpia)
            grupo_actual.cupos_totales = m2.group(1) if m2 else "NA"
        elif limpia.startswith("Cupos disponibles"):
            m2 = re.search(r"(\d+)", limpia)
            # "NA" y no "": un fallo de lectura debe ser distinguible de 0
            grupo_actual.cupos_disponibles = m2.group(1) if m2 else "NA"
        elif limpia.startswith(("Duración", "Duracion")):
            grupo_actual.duracion = _limpiar(
                limpia.split(":", 1)[1] if ":" in limpia else "")
        elif limpia.startswith("Jornada"):
            grupo_actual.jornada = _limpiar(
                limpia.split(":", 1)[1] if ":" in limpia else "")

    return asig


def parsear_detalle(driver, codigo: str) -> Asignatura:
    body_text = driver.find_element(By.TAG_NAME, "body").text
    asig = parsear_texto_detalle(body_text, codigo)
    # Atributo transitorio (no es campo del dataclass, así que no entra al
    # JSON): el texto crudo para archivarlo comprimido en modo cupos.
    asig._texto_crudo = body_text

    if not asig.grupos:
        # O la asignatura no tiene grupos programados este semestre, o el
        # formato cambió. Guardamos el texto para poder revisarlo.
        global _debug_generados
        if _debug_generados < MAX_ARCHIVOS_DEBUG:
            guardar_debug(driver, f"sin_grupos_{codigo}")
            try:
                (CARPETA_SALIDA / f"debug_texto_{codigo}.txt").write_text(
                    body_text, encoding="utf-8")
                print(f"       (texto guardado en debug_texto_{codigo}.txt)")
            except OSError:
                pass

    return asig


# ---------------------------------------------------------------------------
# Guardado / reanudación
# ---------------------------------------------------------------------------

def guardar(asignaturas: List[Asignatura]):
    # --- JSON anidado (fuente de verdad y base para reanudar) ---
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump([asdict(a) for a in asignaturas], f, ensure_ascii=False, indent=2)

    # --- CSV: una fila por GRUPO ---
    campos_g = [
        "codigo_asignatura", "asignatura", "planes", "tipologia", "creditos",
        "grupo", "actividad", "profesores", "cupos_disponibles",
        "jornada", "duracion", "fecha", "horario_resumido", "salones",
    ]
    with open(OUTPUT_GRUPOS_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos_g)
        w.writeheader()
        for a in asignaturas:
            for g in a.grupos:
                resumen = "; ".join(
                    f"{s.dia} {s.hora_inicio}-{s.hora_fin}" for s in g.sesiones
                )
                # salones únicos, conservando el orden de aparición
                vistos: List[str] = []
                for s in g.sesiones:
                    etiqueta = _limpiar(f"{s.salon} ({s.edificio})"
                                        if s.edificio else s.salon)
                    if etiqueta and etiqueta not in vistos:
                        vistos.append(etiqueta)
                w.writerow({
                    "codigo_asignatura": a.codigo,
                    "asignatura": a.nombre,
                    "planes": " | ".join(a.planes),
                    "tipologia": a.tipologia,
                    "creditos": a.creditos,
                    "grupo": g.grupo,
                    "actividad": g.actividad,
                    "profesores": g.profesores,
                    "cupos_disponibles": g.cupos_disponibles,
                    "jornada": g.jornada,
                    "duracion": g.duracion,
                    "fecha": g.fecha,
                    "horario_resumido": resumen,
                    "salones": "; ".join(vistos),
                })

    # --- CSV: una fila por SESIÓN (el más útil para armar el horario) ---
    campos_h = [
        "codigo_asignatura", "asignatura", "planes", "tipologia", "creditos",
        "grupo", "actividad", "profesores", "cupos_disponibles", "jornada",
        "dia", "hora_inicio", "hora_fin",
        "salon", "edificio", "tipo_espacio", "salon_nombre", "fecha",
    ]
    n_sesiones = 0
    with open(OUTPUT_HORARIOS_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos_h)
        w.writeheader()
        for a in asignaturas:
            for g in a.grupos:
                # los grupos sin horario informado igual aparecen, con una
                # fila vacía, para que no desaparezcan del reporte
                filas = g.sesiones or [Sesion()]
                for s in filas:
                    n_sesiones += 1
                    w.writerow({
                        "codigo_asignatura": a.codigo,
                        "asignatura": a.nombre,
                        "planes": " | ".join(a.planes),
                        "tipologia": a.tipologia,
                        "creditos": a.creditos,
                        "grupo": g.grupo,
                        "actividad": g.actividad,
                        "profesores": g.profesores,
                        "cupos_disponibles": g.cupos_disponibles,
                        "jornada": g.jornada,
                        "dia": s.dia,
                        "hora_inicio": s.hora_inicio,
                        "hora_fin": s.hora_fin,
                        "salon": s.salon,
                        "edificio": s.edificio,
                        "tipo_espacio": s.tipo_espacio,
                        "salon_nombre": s.salon_nombre,
                        "fecha": g.fecha,
                    })

    generar_html(asignaturas)

    n_grupos = sum(len(a.grupos) for a in asignaturas)
    con_grupos = sum(1 for a in asignaturas if a.grupos)
    print(f"  -> Guardado: {len(asignaturas)} asignaturas "
          f"({con_grupos} con grupos programados) / "
          f"{n_grupos} grupos / {n_sesiones} sesiones")


def cargar_previo() -> Dict[str, Asignatura]:
    if not (REANUDAR and os.path.exists(OUTPUT_JSON)):
        return {}
    try:
        with open(OUTPUT_JSON, encoding="utf-8") as f:
            datos = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"! No se pudo leer {OUTPUT_JSON} ({e}); se empieza de cero.")
        return {}

    previo: Dict[str, Asignatura] = {}
    try:
        for d in datos:
            grupos = [
                Grupo(
                    grupo=g.get("grupo", ""),
                    actividad=g.get("actividad", ""),
                    profesores=g.get("profesores", "No informado"),
                    cupos_disponibles=g.get("cupos_disponibles", "NA"),
                    cupos_totales=g.get("cupos_totales", "NA"),
                    fecha=g.get("fecha", ""),
                    duracion=g.get("duracion", ""),
                    jornada=g.get("jornada", ""),
                    sesiones=[Sesion(**s) for s in g.get("sesiones", [])],
                )
                for g in d.get("grupos", [])
            ]
            previo[d["codigo"]] = Asignatura(
                codigo=d["codigo"],
                nombre=d.get("nombre", ""),
                tipologia=d.get("tipologia", ""),
                creditos=d.get("creditos", ""),
                facultad=d.get("facultad", ""),
                planes=list(d.get("planes", [])),
                grupos=grupos,
            )
    except (TypeError, KeyError) as e:
        # catalogo.json de una versión anterior con otros campos
        print(f"! {OUTPUT_JSON} tiene un formato antiguo ({e}); se ignora "
              f"y se vuelve a extraer todo.")
        return {}

    if previo:
        print(f"Reanudando: {len(previo)} asignaturas ya estaban en {OUTPUT_JSON}.")
    return previo




# ---------------------------------------------------------------------------
# Armador de horario (HTML interactivo)
# ---------------------------------------------------------------------------
# Genera horario.html: un archivo autocontenido que se abre en el navegador,
# lista todas las asignaturas con sus grupos, y permite armar el horario
# semanal marcando en rojo (achurado) los cruces entre materias.

PLANTILLA_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Armador de horario — SIA UNAL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{
  --papel:#FAFBF7; --tinta:#1E2A24; --tinta-suave:#5A6B60;
  --linea:#E3E9DD; --linea-fuerte:#C9D4C0;
  --verde:#4C7A2E; --verde-claro:#EDF3E4;
  --rojo:#C24438; --rojo-claro:#FBEAE8;
  --b1:#DCE8F4; --b1i:#2C5578; --b2:#F3E6D0; --b2i:#7A5A1E;
  --b3:#E4DCF0; --b3i:#54407A; --b4:#D9EFE3; --b4i:#22633E;
  --b5:#F4DEDC; --b5i:#83403A; --b6:#EDEBD2; --b6i:#6A6320;
  --b7:#DBECF0; --b7i:#2E6470; --b8:#F0E0EC; --b8i:#7A3E67;
  --display:'Space Grotesk',system-ui,sans-serif;
  --cuerpo:'IBM Plex Sans',system-ui,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;
}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{font-family:var(--cuerpo);color:var(--tinta);background:var(--papel);
  display:flex;flex-direction:column;font-size:14px}

/* ---------- cabecera ---------- */
header{display:flex;align-items:center;gap:18px;padding:10px 18px;
  border-bottom:2px solid var(--tinta);background:var(--papel)}
header h1{font-family:var(--display);font-weight:700;font-size:19px;letter-spacing:.01em}
header h1 span{color:var(--verde)}
.contadores{display:flex;gap:8px;margin-left:auto}
.ficha{border:1.5px solid var(--tinta);padding:4px 12px;font-family:var(--mono);
  font-size:12.5px;background:#fff;display:flex;gap:7px;align-items:baseline}
.ficha b{font-size:16px}
.ficha.mal{border-color:var(--rojo);color:var(--rojo);background:var(--rojo-claro)}
header button{font-family:var(--display);font-weight:500;font-size:13px;cursor:pointer;
  border:1.5px solid var(--tinta);background:#fff;color:var(--tinta);padding:6px 13px}
header button:hover{background:var(--tinta);color:var(--papel)}
header button:focus-visible,.grupo:focus-visible{outline:2px solid var(--verde);outline-offset:2px}

/* ---------- estructura ---------- */
main{flex:1;display:flex;min-height:0}
aside{width:340px;min-width:280px;border-right:2px solid var(--tinta);
  display:flex;flex-direction:column;background:#fff}
.busqueda{padding:12px;border-bottom:1px solid var(--linea-fuerte);display:flex;flex-direction:column;gap:9px}
.busqueda input{width:100%;padding:8px 11px;font-family:var(--cuerpo);font-size:14px;
  border:1.5px solid var(--linea-fuerte);background:var(--papel)}
.busqueda input:focus{outline:none;border-color:var(--verde)}
.busqueda label{display:flex;gap:7px;align-items:center;font-size:12.5px;color:var(--tinta-suave);cursor:pointer}
.fila-imp{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.fila-imp button{font-family:var(--display);font-size:12px;cursor:pointer;
  border:1.5px solid var(--tinta);background:#fff;color:var(--tinta);padding:5px 10px}
.fila-imp button:hover{background:var(--tinta);color:var(--papel)}
.chip-extra{font-family:var(--mono);font-size:11px;color:var(--verde)}
.chip-extra a{color:var(--rojo);margin-left:4px}
.busqueda select{padding:6px 8px;font-family:var(--cuerpo);font-size:12.5px;
  border:1.5px solid var(--linea-fuerte);background:var(--papel)}
.lista{flex:1;overflow-y:auto}

/* ---------- lista de asignaturas ---------- */
.asig{border-bottom:1px solid var(--linea)}
.asig>summary{list-style:none;cursor:pointer;padding:10px 12px;display:flex;gap:9px;align-items:baseline}
.asig>summary::-webkit-details-marker{display:none}
.asig>summary:hover{background:var(--papel)}
.asig .cod{font-family:var(--mono);font-size:11px;color:var(--tinta-suave);white-space:nowrap}
.asig .nom{font-weight:600;font-size:13.5px;flex:1}
.asig .cred{font-family:var(--mono);font-size:11px;color:var(--tinta-suave)}
.asig[data-sel="1"]>summary{border-left:4px solid var(--verde);padding-left:8px;background:var(--verde-claro)}
.grupo{padding:8px 12px 8px 24px;border-top:1px dashed var(--linea);cursor:pointer;position:relative}
.grupo:hover{background:var(--papel)}
.grupo.sel{background:var(--verde-claro);border-left:4px solid var(--verde);padding-left:20px}
.grupo .g-cab{display:flex;gap:8px;align-items:baseline}
.grupo .g-num{font-family:var(--mono);font-weight:600;font-size:12px}
.grupo .g-prof{font-size:12px;color:var(--tinta-suave);flex:1;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.grupo .g-hor{font-family:var(--mono);font-size:11px;color:var(--tinta-suave);margin-top:3px}
.tag{font-family:var(--mono);font-size:10px;padding:1px 6px;border:1px solid currentColor;white-space:nowrap}
.tag.cupos{color:var(--verde)}
.tag.sindato{color:#888}
.tag.agotado{color:var(--tinta-suave)}
.tag.cruce{color:var(--rojo);background:var(--rojo-claro)}
.grupo.agotado .g-cab,.grupo.agotado .g-hor{opacity:.5}
.vacio{padding:26px 16px;color:var(--tinta-suave);font-size:13px;text-align:center}

/* ---------- rejilla (papel milimetrado) ---------- */
.zona-grid{flex:1;display:flex;flex-direction:column;min-width:0;padding:14px 16px 16px}
.dias{display:grid;margin-left:52px;grid-auto-flow:column;grid-auto-columns:1fr}
.dias div{font-family:var(--display);font-weight:700;font-size:12.5px;letter-spacing:.06em;
  text-align:center;padding-bottom:6px;color:var(--tinta)}
.rejilla-wrap{flex:1;display:flex;min-height:0;border:2px solid var(--tinta);background:#fff}
.horas{width:52px;position:relative;border-right:1.5px solid var(--linea-fuerte);flex:none}
.horas span{position:absolute;right:7px;transform:translateY(-55%);
  font-family:var(--mono);font-size:10.5px;color:var(--tinta-suave)}
.cols{flex:1;display:grid;grid-auto-flow:column;grid-auto-columns:1fr;position:relative;
  background-image:
    repeating-linear-gradient(to bottom,transparent 0,transparent calc(var(--paso) - 1px),var(--linea) calc(var(--paso) - 1px),var(--linea) var(--paso)),
    repeating-linear-gradient(to bottom,transparent 0,transparent calc(var(--paso)/2 - 1px),color-mix(in srgb,var(--linea) 45%,transparent) calc(var(--paso)/2 - 1px),color-mix(in srgb,var(--linea) 45%,transparent) calc(var(--paso)/2))}
.col{position:relative;border-right:1px solid var(--linea)}
.col:last-child{border-right:none}
.bloque{position:absolute;left:3px;right:3px;border:1.5px solid var(--bi);
  background:var(--bg);color:var(--bi);padding:4px 6px;overflow:hidden;font-size:11px;
  line-height:1.25;cursor:pointer}
.bloque .b-cod{font-family:var(--mono);font-size:9.5px;opacity:.75}
.bloque .b-nom{font-weight:600;font-size:11px}
.bloque .b-det{font-family:var(--mono);font-size:9.5px;margin-top:1px;opacity:.85}
.bloque.cruce{border-color:var(--rojo);
  background-image:repeating-linear-gradient(45deg,transparent 0 6px,rgba(194,68,56,.22) 6px 10px)}
.aviso-cruces{margin-top:10px;border:1.5px solid var(--rojo);background:var(--rojo-claro);
  color:var(--rojo);padding:8px 12px;font-size:12.5px;display:none}
.aviso-cruces.on{display:block}
.aviso-cruces b{font-family:var(--mono)}
.grid-vacia{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  color:var(--tinta-suave);font-size:13.5px;pointer-events:none}

@media (max-width:860px){
  main{flex-direction:column}
  aside{width:100%;max-height:45vh;border-right:none;border-bottom:2px solid var(--tinta)}
}
@media print{
  aside,header button,.busqueda{display:none}
  aside{border:none}
  main{display:block}
  body{background:#fff}
}
@media (prefers-reduced-motion:no-preference){
  .bloque{transition:filter .15s}
  .bloque:hover{filter:brightness(.96)}
}
</style>
</head>
<body>
<header>
  <h1>Armador de horario <span>· SIA UNAL</span></h1>
  <div class="contadores">
    <div class="ficha"><b id="c-cred">0</b> créditos</div>
    <div class="ficha"><b id="c-asig">0</b> asignaturas</div>
    <div class="ficha" id="f-cruce" hidden><b id="c-cruce">0</b> cruces</div>
  </div>
  <button id="btn-imprimir">Imprimir</button>
  <button id="btn-vaciar">Vaciar horario</button>
</header>
<main>
  <aside>
    <div class="busqueda">
      <input id="buscar" type="search" placeholder="Buscar asignatura o profesor…" autocomplete="off">
      <label><input type="checkbox" id="solo-cupos"> Solo grupos con cupos</label>
      <div class="fila-imp">
        <button id="btn-importar" type="button">Importar CSV / Excel</button>
        <input type="file" id="file-importar" accept=".csv,.xlsx,.xls" hidden>
        <span class="chip-extra" id="chip-extra" hidden><b id="n-extra">0</b> importadas
          <a href="#" id="quitar-extra">quitar</a></span>
      </div>
      <select id="filtro-plan" hidden></select>
    </div>
    <div class="lista" id="lista"></div>
  </aside>
  <section class="zona-grid">
    <div class="dias" id="dias"></div>
    <div class="rejilla-wrap">
      <div class="horas" id="horas"></div>
      <div class="cols" id="cols"></div>
    </div>
    <div class="aviso-cruces" id="aviso"></div>
  </section>
</main>
<script>
const BASE = __DATOS__;

/* ---------- datos importados (CSV/Excel), persistidos en el navegador ---- */
let EXTRA = {};
try{ EXTRA = JSON.parse(localStorage.getItem("horario-unal-extra")||"{}"); }catch(e){}
function persistirExtra(){
  try{ localStorage.setItem("horario-unal-extra", JSON.stringify(EXTRA)); }
  catch(e){ alert("El archivo es demasiado grande para guardarlo en el navegador; "+
                  "los datos importados se perderán al cerrar la página."); }
}
function combinar(){
  const m = {};
  for(const a of BASE) m[a.c] = a;
  for(const [c,s] of Object.entries(EXTRA)) m[c] = s;   // lo importado manda
  return Object.values(m).sort((x,y)=>(x.n||"").localeCompare(y.n||"","es"));
}
let DATA = combinar();

/* ---------- utilidades ---------- */
const $ = s => document.querySelector(s);
const DIAS_ORDEN = ["LUNES","MARTES","MIERCOLES","JUEVES","VIERNES","SABADO","DOMINGO"];
const DIA_CORTO = {LUNES:"LUN",MARTES:"MAR",MIERCOLES:"MIÉ",JUEVES:"JUE",VIERNES:"VIE",SABADO:"SÁB",DOMINGO:"DOM"};
const normDia = d => (d||"").normalize("NFD").replace(/[\u0300-\u036f]/g,"").toUpperCase().trim();
const aMin = h => { const m=/^(\d{1,2})[:.](\d{2})$/.exec((h||"").trim()); return m? (+m[1])*60+(+m[2]) : null; };
const fMin = m => String(Math.floor(m/60)).padStart(2,"0")+":"+String(m%60).padStart(2,"0");

/* sesiones válidas (con día y horas) de un grupo */
function sesiones(g){
  return (g.s||[]).map(s=>({dia:normDia(s.d),ini:aMin(s.i),fin:aMin(s.f),salon:s.sa||"",edif:s.e||""}))
                  .filter(s=>s.ini!==null && s.fin!==null && DIAS_ORDEN.includes(s.dia));
}
function chocan(a,b){ return a.dia===b.dia && a.ini<b.fin && b.ini<a.fin; }
function cruceEntre(g1,g2){
  for(const a of sesiones(g1)) for(const b of sesiones(g2)) if(chocan(a,b)) return [a,b];
  return null;
}

/* ---------- estado ---------- */
let seleccion = {};              // codigo -> índice de grupo
try{ seleccion = JSON.parse(localStorage.getItem("horario-unal")||"{}"); }catch(e){}
function persistir(){ try{ localStorage.setItem("horario-unal",JSON.stringify(seleccion)); }catch(e){} }

const seleccionados = () => Object.entries(seleccion)
  .map(([cod,i])=>{ const a=DATA.find(x=>x.c===cod); return a&&a.g[i]? {a,g:a.g[i],gi:i}:null; })
  .filter(Boolean);

/* ---------- dimensiones de la rejilla ---------- */
function rango(){
  let ini=7*60, fin=18*60;
  for(const {g} of seleccionados()) for(const s of sesiones(g)){
    ini=Math.min(ini,s.ini); fin=Math.max(fin,s.fin);
  }
  return {ini:Math.floor(ini/60)*60, fin:Math.ceil(fin/60)*60};
}
function diasVisibles(){
  const base=["LUNES","MARTES","MIERCOLES","JUEVES","VIERNES"];
  const usados=new Set();
  for(const {g} of seleccionados()) for(const s of sesiones(g)) usados.add(s.dia);
  if(usados.has("SABADO")) base.push("SABADO");
  if(usados.has("DOMINGO")){ if(!base.includes("SABADO")) base.push("SABADO"); base.push("DOMINGO"); }
  return base;
}

/* ---------- render: rejilla ---------- */
function pintarRejilla(){
  const sel=seleccionados(), {ini,fin}=rango(), dias=diasVisibles();
  const total=fin-ini;
  $("#cols").style.setProperty("--paso","calc(100%/"+(total/60)+")");

  $("#dias").innerHTML = dias.map(d=>`<div>${DIA_CORTO[d]}</div>`).join("");
  $("#horas").innerHTML = Array.from({length:total/60+1},(_,k)=>
    `<span style="top:${k*100/(total/60)}%">${fMin(ini+k*60)}</span>`).join("");

  /* detectar cruces */
  const conflictos=[]; const enCruce=new Set();
  for(let i=0;i<sel.length;i++) for(let j=i+1;j<sel.length;j++){
    const par=cruceEntre(sel[i].g,sel[j].g);
    if(par){ conflictos.push({x:sel[i],y:sel[j],ses:par});
             enCruce.add(sel[i].a.c); enCruce.add(sel[j].a.c); }
  }

  /* bloques por día, con carriles para solapados */
  const html = dias.map(dia=>{
    const bloques=[];
    sel.forEach((s,idx)=>{ for(const ses of sesiones(s.g)) if(ses.dia===dia)
      bloques.push({...ses,a:s.a,g:s.g,color:(DATA.indexOf(DATA.find(x=>x.c===s.a.c))%8)+1}); });
    bloques.sort((p,q)=>p.ini-q.ini);
    const carril=[], nCarril=[];
    bloques.forEach((b,i)=>{
      let c=0; while(bloques.some((o,j)=>j<i&&nCarril[j]===c&&chocan(o,b))) c++;
      nCarril[i]=c;
    });
    const maxC=Math.max(0,...nCarril)+1;
    const inner=bloques.map((b,i)=>{
      const top=(b.ini-ini)/total*100, alto=(b.fin-b.ini)/total*100;
      const w=100/maxC, x=nCarril[i]*w;
      const mal=enCruce.has(b.a.c)&&bloques.some((o,j)=>j!==i&&chocan(o,b));
      const lugar=[b.salon,b.edif&&"— "+b.edif.split(" - ")[0]].filter(Boolean).join(" ");
      return `<div class="bloque${mal?" cruce":""}" tabindex="0"
        style="--bg:var(--b${b.color});--bi:var(--b${b.color}i);top:${top}%;height:${alto}%;
        left:calc(${x}% + 3px);right:auto;width:calc(${w}% - 6px)"
        title="${b.a.n} — ${b.g.g}\n${fMin(b.ini)}–${fMin(b.fin)}  ${lugar}"
        onclick="quitar('${b.a.c}')">
        <div class="b-cod">${b.a.c}</div><div class="b-nom">${b.a.n}</div>
        <div class="b-det">${fMin(b.ini)}–${fMin(b.fin)}${b.salon?" · "+b.salon:""}</div></div>`;
    }).join("");
    return `<div class="col">${inner}</div>`;
  }).join("");
  $("#cols").innerHTML = html + (sel.length?"":`<div class="grid-vacia">Elige un grupo en la lista para empezar.</div>`);

  /* contadores y aviso */
  $("#c-cred").textContent = sel.reduce((t,s)=>t+(+s.a.cr||0),0);
  $("#c-asig").textContent = sel.length;
  $("#c-cruce").textContent = conflictos.length;
  $("#f-cruce").hidden = !conflictos.length;
  $("#f-cruce").className = "ficha"+(conflictos.length?" mal":"");
  const av=$("#aviso");
  av.className="aviso-cruces"+(conflictos.length?" on":"");
  av.innerHTML = conflictos.map(c=>
    `<b>${c.x.a.n}</b> (${c.x.g.g}) se cruza con <b>${c.y.a.n}</b> (${c.y.g.g}) — ${DIA_CORTO[c.ses[0].dia]} ${fMin(Math.max(c.ses[0].ini,c.ses[1].ini))}–${fMin(Math.min(c.ses[0].fin,c.ses[1].fin))}`
  ).join("<br>");
}

/* ---------- render: lista ---------- */
function pintarLista(){
  const q=($("#buscar").value||"").toLowerCase();
  const soloCupos=$("#solo-cupos").checked;
  const planSel=$("#filtro-plan").value;
  const sel=seleccionados();
  const abiertos=new Set([...document.querySelectorAll(".asig[open]")].map(d=>d.dataset.c));

  $("#lista").innerHTML = DATA.map(a=>{
    if(planSel && !(a.pl||[]).includes(planSel)) return "";
    const coincideA = !q || a.n.toLowerCase().includes(q) || a.c.toLowerCase().includes(q);
    let grupos = a.g.map((g,i)=>({g,i}));
    if(q && !coincideA) grupos = grupos.filter(({g})=>(g.p||"").toLowerCase().includes(q));
    if(!coincideA && !grupos.length) return "";
    if(soloCupos) grupos = grupos.filter(({g})=>(+g.cu||0)>0);
    if(soloCupos && !grupos.length && !(q&&coincideA)) return "";

    const filas = grupos.map(({g,i})=>{
      const yo = seleccion[a.c]===i;
      const otros = sel.filter(s=>s.a.c!==a.c);
      const conflicto = !yo && otros.find(s=>cruceEntre(s.g,g));
      const cupos=+g.cu||0;
      const sinDato = g.cu==="NA" || g.cu===""||g.cu==null;  // "NA" = no leído, no es 0
      const etiqCupos = sinDato ? "sin dato" : (cupos?cupos+" cupos":"sin cupos");
      const hor = sesiones(g).map(s=>`${DIA_CORTO[s.dia]} ${fMin(s.ini)}–${fMin(s.fin)}`).join(" · ")
                  || "sin horario informado";
      return `<div class="grupo${yo?" sel":""}${(cupos||sinDato)?"":" agotado"}" tabindex="0" role="button"
          onclick="alternar('${a.c}',${i})" onkeydown="if(event.key==='Enter')alternar('${a.c}',${i})">
        <div class="g-cab"><span class="g-num">${g.g}</span>
          <span class="g-prof">${(g.p||"").split(";")[0]||"—"}</span>
          ${conflicto?`<span class="tag cruce">cruce</span>`:""}
          <span class="tag ${sinDato?"sindato":(cupos?"cupos":"agotado")}">${etiqCupos}</span></div>
        <div class="g-hor">${hor}</div></div>`;
    }).join("");

    const abierto = abiertos.has(a.c)||seleccion[a.c]!==undefined||q?" open":"";
    return `<details class="asig" data-c="${a.c}" data-sel="${seleccion[a.c]!==undefined?1:0}"${abierto}>
      <summary><span class="cod">${a.c}</span><span class="nom">${a.n}</span>
        <span class="cred">${a.cr||"?"} cr</span></summary>${filas||'<div class="vacio">Sin grupos</div>'}</details>`;
  }).join("") || `<div class="vacio">Nada coincide con la búsqueda.</div>`;
}

/* ---------- acciones ---------- */
window.alternar = (cod,i)=>{
  if(seleccion[cod]===i) delete seleccion[cod]; else seleccion[cod]=i;
  persistir(); pintar();
};
window.quitar = cod=>{ delete seleccion[cod]; persistir(); pintar(); };
$("#btn-vaciar").onclick = ()=>{ seleccion={}; persistir(); pintar(); };
$("#btn-imprimir").onclick = ()=>window.print();
$("#buscar").oninput = pintarLista;
$("#solo-cupos").onchange = pintarLista;

/* ---------- importación de CSV / Excel ----------
   Formato esperado: el de horarios.csv (una fila por sesión). Encabezados
   reconocidos con flexibilidad: codigo_asignatura/codigo, asignatura/nombre,
   creditos, tipologia, planes/plan, grupo, profesores/profesor,
   cupos_disponibles/cupos, jornada, dia, hora_inicio/inicio, hora_fin/fin,
   salon/aula, edificio. Un Excel guardado desde ese CSV también funciona. */
const MAPK = {codigoasignatura:"c",codigo:"c",asignatura:"n",nombre:"n",
  creditos:"cr",tipologia:"t",planes:"pl",plan:"pl",grupo:"g",
  profesores:"p",profesor:"p",cuposdisponibles:"cu",cupos:"cu",jornada:"j",
  dia:"d",horainicio:"i",inicio:"i",horafin:"f",fin:"f",
  salon:"sa",aula:"sa",edificio:"e"};
const normKey = k => (k||"").normalize("NFD").replace(/[\u0300-\u036f]/g,"")
  .toLowerCase().replace(/[^a-z0-9]/g,"");

function parseCSV(txt){
  txt = txt.replace(/^\uFEFF/,"");
  const fin1 = txt.indexOf("\n");
  const linea1 = fin1>=0 ? txt.slice(0,fin1) : txt;
  const delim = (linea1.match(/;/g)||[]).length > (linea1.match(/,/g)||[]).length ? ";" : ",";
  const filas=[]; let fila=[], campo="", enComillas=false;
  for(let k=0;k<txt.length;k++){
    const ch=txt[k];
    if(enComillas){
      if(ch==='"'){ if(txt[k+1]==='"'){campo+='"';k++;} else enComillas=false; }
      else campo+=ch;
    }
    else if(ch==='"') enComillas=true;
    else if(ch===delim){ fila.push(campo); campo=""; }
    else if(ch==="\n"||ch==="\r"){
      if(ch==="\r"&&txt[k+1]==="\n")k++;
      fila.push(campo); campo="";
      if(fila.some(x=>x!=="")) filas.push(fila);
      fila=[];
    }
    else campo+=ch;
  }
  if(campo!==""||fila.length){ fila.push(campo); if(fila.some(x=>x!=="")) filas.push(fila); }
  const cab = filas.shift()||[];
  return filas.map(f=>Object.fromEntries(cab.map((h,i)=>[h,f[i]??""])));
}

function filasASujetos(rows){
  const subs={};
  for(const cruda of rows){
    const r={};
    for(const [k,v] of Object.entries(cruda)){
      const nk=MAPK[normKey(k)];
      if(nk) r[nk]=String(v??"").trim();
    }
    if(!r.c) continue;
    let s=subs[r.c];
    if(!s) s=subs[r.c]={c:r.c,n:r.n||r.c,cr:r.cr||"",t:r.t||"",pl:[],g:[],_g:{},_s:{}};
    if(r.pl) for(const p of r.pl.split("|")){
      const pp=p.trim(); if(pp && !s.pl.includes(pp)) s.pl.push(pp);
    }
    const gk=r.g||"(1) Grupo 1";
    let g=s._g[gk];
    if(!g){ g={g:gk,p:r.p||"",cu:r.cu||"",j:r.j||"",s:[]}; s._g[gk]=g; s.g.push(g); }
    if(r.d && r.i && r.f){
      const clave=gk+"|"+r.d+r.i+r.f;
      if(!s._s[clave]){ s._s[clave]=1;
        g.s.push({d:r.d,i:r.i,f:r.f,sa:r.sa||"",e:r.e||""}); }
    }
  }
  for(const s of Object.values(subs)){ delete s._g; delete s._s; }
  return subs;
}

function cargarXLSX(){
  if(window.XLSX) return Promise.resolve(window.XLSX);
  return new Promise((res,rej)=>{
    const sc=document.createElement("script");
    sc.src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js";
    sc.onload=()=>res(window.XLSX);
    sc.onerror=()=>rej(new Error("no se pudo descargar el lector de Excel"));
    document.head.appendChild(sc);
  });
}

async function importarArchivo(file){
  try{
    let rows;
    if(/\.(xlsx|xls)$/i.test(file.name)){
      const X=await cargarXLSX();
      const wb=X.read(await file.arrayBuffer());
      rows=X.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]],{defval:""});
    }else{
      rows=parseCSV(await file.text());
    }
    const nuevos=filasASujetos(rows);
    const n=Object.keys(nuevos).length;
    if(!n){
      alert("No se reconocieron materias en el archivo.\n"+
            "Se espera el formato de horarios.csv: columnas codigo_asignatura, "+
            "asignatura, grupo, dia, hora_inicio, hora_fin…");
      return;
    }
    for(const [c,s] of Object.entries(nuevos)){
      const prev=EXTRA[c];
      if(prev) for(const p of (prev.pl||[])) if(!s.pl.includes(p)) s.pl.push(p);
      EXTRA[c]=s;
    }
    persistirExtra();
    recombinar();
    alert(n+" materias importadas ("+rows.length+" filas leídas).");
  }catch(err){
    const esExcel=/\.(xlsx|xls)$/i.test(file.name);
    alert("No se pudo importar: "+err.message+
          (esExcel?"\nEl lector de Excel necesita internet; sin conexión, guarda el archivo como CSV.":""));
  }
}

function actualizarFiltroPlan(){
  const sel=$("#filtro-plan");
  const antes=sel.value;
  const planes=[...new Set(DATA.flatMap(a=>a.pl||[]))].sort();
  sel.hidden = planes.length<2;
  sel.innerHTML = `<option value="">Todos los planes (${DATA.length})</option>`+
    planes.map(p=>`<option value="${p}"${p===antes?" selected":""}>${p}</option>`).join("");
}
function actualizarChip(){
  const n=Object.keys(EXTRA).length;
  $("#chip-extra").hidden=!n;
  $("#n-extra").textContent=n;
}
function recombinar(){
  DATA=combinar();
  actualizarFiltroPlan();
  actualizarChip();
  pintar();
}

$("#btn-importar").onclick=()=>$("#file-importar").click();
$("#file-importar").onchange=e=>{
  const f=e.target.files[0];
  if(f) importarArchivo(f);
  e.target.value="";
};
$("#quitar-extra").onclick=e=>{
  e.preventDefault();
  EXTRA={}; persistirExtra(); recombinar();
};
$("#filtro-plan").onchange=pintarLista;

function pintar(){ pintarLista(); pintarRejilla(); }
recombinar();
</script>
</body>
</html>
"""


def generar_html(asignaturas: List[Asignatura]):
    """Escribe horario.html con los datos embebidos."""
    datos = []
    for a in sorted(asignaturas, key=lambda x: (x.nombre or x.codigo).lower()):
        datos.append({
            "c": a.codigo,
            "n": a.nombre or a.codigo,
            "cr": a.creditos,
            "t": a.tipologia,
            "pl": a.planes,
            "g": [
                {
                    "g": g.grupo,
                    "p": g.profesores,
                    "cu": g.cupos_disponibles,
                    "j": g.jornada,
                    "s": [
                        {"d": s.dia, "i": s.hora_inicio, "f": s.hora_fin,
                         "sa": s.salon, "e": s.edificio}
                        for s in g.sesiones
                    ],
                }
                for g in a.grupos
            ],
        })

    # "</" dentro de un <script> cerraria el tag prematuramente
    json_seguro = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")
    html = PLANTILLA_HTML.replace("__DATOS__", json_seguro)
    OUTPUT_HTML.write_text(html, encoding="utf-8")




# ---------------------------------------------------------------------------
# Historial de cupos y estadísticas
# ---------------------------------------------------------------------------
# cupos_historial.csv es un registro acumulativo (append-only): cada corrida
# en modo "cupos" añade una fila por grupo con la fecha/hora de la medición.
# estadisticas.html se regenera leyendo ese historial completo.

# Esquema v2 del historial. Cambios frente al v1
# (fecha_hora,codigo,asignatura,grupo,cupos_disponibles):
#   run_id          identifica la corrida; cruzar con runs.csv para saber si
#                   el barrido fue completo.
#   ts_lectura      instante de lectura de ESA asignatura, no de la corrida.
#   actividad       (codigo, grupo) NO es clave única: "Grupo 1" de CLASE
#                   TEORICA y "Grupo 1" de LABORATORIO son series distintas.
#   profesores_raw  quién dictaba el grupo EN ESE MOMENTO. grupos.csv se
#                   sobrescribe en cada corrida; si reasignan un docente a
#                   mitad de semana, solo el historial conserva el vínculo.
#   profesores_norm sin tildes, minúsculas, orden alfabético, separados por
#                   ";" — para cruzar con la página de calificaciones.
#   cupos_totales   "NA" mientras el SIA no lo exponga; la capacidad se
#                   estima con el barrido de línea base.
#   orden_lectura   posición dentro del barrido, para estimar y corregir el
#                   desfase entre la primera y la última asignatura.
# Se eliminó "asignatura" (el nombre): es redundante con codigo (está en
# catalogo.json) y solo infla el archivo.
HIST_CAMPOS = [
    "run_id", "ts_lectura", "codigo", "actividad", "grupo",
    "profesores_raw", "profesores_norm", "cupos_disponibles",
    "cupos_totales", "orden_lectura",
]

HIST_CAMPOS_V1 = ["fecha_hora", "codigo", "asignatura", "grupo",
                  "cupos_disponibles"]


def normalizar_profesores(profesores: str) -> str:
    """'María PÉREZ; juan lopez' -> 'juan lopez;maria perez' (clave de cruce)."""
    nombres = [normalizar(p) for p in (profesores or "").split(";")]
    nombres = sorted(n for n in nombres if n and n != "no informado")
    return ";".join(nombres) if nombres else "NA"


def _leer_encabezado_historial() -> Optional[List[str]]:
    if not OUTPUT_HISTORIAL.exists():
        return None
    with open(OUTPUT_HISTORIAL, encoding="utf-8-sig", newline="") as f:
        fila = next(csv.reader(f), None)
    return [c.strip() for c in fila] if fila else None


def _indice_actividad_catalogo() -> Dict[tuple, dict]:
    """(codigo, grupo) -> {actividad, profesores} desde catalogo.json, para
    retro-llenar el historial v1. Si un (codigo, grupo) existe en varias
    actividades, se marca AMBIGUA en vez de adivinar."""
    indice: Dict[tuple, dict] = {}
    if not OUTPUT_JSON.exists():
        return indice
    try:
        with open(OUTPUT_JSON, encoding="utf-8") as f:
            datos = json.load(f)
    except (json.JSONDecodeError, OSError):
        return indice
    for a in datos:
        for g in a.get("grupos", []):
            clave = (a.get("codigo", ""), g.get("grupo", ""))
            info = {"actividad": g.get("actividad") or "NA",
                    "profesores": g.get("profesores") or "NA"}
            if clave in indice and indice[clave]["actividad"] != info["actividad"]:
                indice[clave] = {"actividad": "AMBIGUA", "profesores": "AMBIGUA"}
            else:
                indice.setdefault(clave, info)
    return indice


def migrar_historial_v1() -> bool:
    """Convierte cupos_historial.csv del esquema v1 al v2, respaldando el
    original como cupos_historial_v1.csv. Retro-llena actividad y profesores
    desde catalogo.json cuando el mapeo es inequívoco.

    Limitaciones honestas de los datos viejos (quedan marcadas como tal):
    - ts_lectura hereda la marca única de la corrida (no había una por fila);
    - run_id se reconstruye agrupando por esa marca ("legacy-...");
    - profesores reflejan la asignación ACTUAL del catálogo, no la histórica.
    """
    encabezado = _leer_encabezado_historial()
    if encabezado is None or encabezado == HIST_CAMPOS:
        return False
    if encabezado != HIST_CAMPOS_V1:
        raise RuntimeError(
            f"{OUTPUT_HISTORIAL} tiene un encabezado desconocido: {encabezado}. "
            "Revísalo a mano antes de seguir (no se toca nada).")

    respaldo = CARPETA_SALIDA / "cupos_historial_v1.csv"
    n_resp = 2
    while respaldo.exists():
        respaldo = CARPETA_SALIDA / f"cupos_historial_v1_{n_resp}.csv"
        n_resp += 1

    indice = _indice_actividad_catalogo()
    filas_v2, orden_por_run, avisos = [], {}, 0
    with open(OUTPUT_HISTORIAL, encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f):
            ts = (fila.get("fecha_hora") or "").strip()
            run = "legacy-" + re.sub(r"[^0-9T]", "", ts)
            codigo = (fila.get("codigo") or "").strip()
            grupo = (fila.get("grupo") or "").strip()
            orden = orden_por_run.setdefault(run, {})
            if codigo not in orden:
                orden[codigo] = len(orden)
            extra = indice.get((codigo, grupo),
                               {"actividad": "NA", "profesores": "NA"})
            if extra["actividad"] in ("NA", "AMBIGUA"):
                avisos += 1
            cupos = (fila.get("cupos_disponibles") or "").strip() or "NA"
            filas_v2.append({
                "run_id": run, "ts_lectura": ts, "codigo": codigo,
                "actividad": extra["actividad"], "grupo": grupo,
                "profesores_raw": extra["profesores"],
                "profesores_norm": normalizar_profesores(
                    extra["profesores"] if extra["profesores"] not in
                    ("NA", "AMBIGUA") else ""),
                "cupos_disponibles": cupos, "cupos_totales": "NA",
                "orden_lectura": orden[codigo],
            })

    os.replace(OUTPUT_HISTORIAL, respaldo)
    with open(OUTPUT_HISTORIAL, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HIST_CAMPOS)
        w.writeheader()
        w.writerows(filas_v2)
    print(f"  -> Historial migrado a v2: {len(filas_v2)} filas, "
          f"{len(orden_por_run)} corridas legacy. Original respaldado en "
          f"{respaldo.name}.")
    if avisos:
        print(f"     ({avisos} filas quedaron con actividad NA/AMBIGUA: "
              f"grupos que ya no están en catalogo.json o que existen en "
              f"varias actividades)")
    return True


def _asegurar_esquema_historial():
    """Antes de escribir: si el archivo está en v1, se migra solo (con
    respaldo). En Actions nadie corre comandos a mano, así que esto tiene
    que ser automático."""
    encabezado = _leer_encabezado_historial()
    if encabezado is not None and encabezado != HIST_CAMPOS:
        print("  ! cupos_historial.csv está en el esquema viejo; migrando...")
        migrar_historial_v1()


def registrar_historial_asignatura(asig: Asignatura) -> int:
    """Añade al historial las filas de UNA asignatura, en el momento en que
    se leyó. Escribir incrementalmente (y no todo al final) garantiza que un
    timeout o una caída dejen datos parciales identificables por run_id, en
    vez de perder el barrido entero."""
    return historial.anexar([{
        "run_id": RUN_ID,
        "ts_lectura": asig.ts_lectura,
        "codigo": asig.codigo,
        "actividad": g.actividad or "NA",
        "grupo": g.grupo,
        "profesores_raw": g.profesores,
        "cupos_disponibles": g.cupos_disponibles or "NA",
        "orden_lectura": asig.orden_lectura,
    } for g in asig.grupos])


def guardar_texto_crudo(codigo: str, ts: str, texto: str):
    """Archiva el texto de la página en textos/run-<RUN_ID>.jsonl.gz.
    gzip en modo 'at' concatena miembros válidos, así que se puede añadir
    asignatura por asignatura sin releer el archivo."""
    if not GUARDAR_TEXTO_CRUDO:
        return
    try:
        CARPETA_TEXTOS.mkdir(exist_ok=True)
        ruta = CARPETA_TEXTOS / f"run-{RUN_ID}.jsonl.gz"
        linea = json.dumps({"codigo": codigo, "ts": ts, "texto": texto},
                           ensure_ascii=False)
        with gzip.open(ruta, "at", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError as e:
        print(f"  ! No se pudo archivar el texto crudo de {codigo}: {e}")


RUNS_CAMPOS = ["run_id", "ts_inicio", "ts_fin", "planes", "shard",
               "n_esperadas", "n_leidas", "n_fallidas", "codigos_fallidos",
               "completo", "n_paginas", "intentos_listado"]


def _asegurar_esquema_runs():
    """Añade las columnas nuevas a runs.csv si viene del esquema viejo.

    Sin esto, DictWriter escribiría 12 valores bajo una cabecera de 10 y el
    archivo quedaría corrupto en silencio. Las filas viejas se quedan con
    las columnas nuevas vacías, que es lo honesto: de esas corridas no
    tenemos el dato.
    """
    if not OUTPUT_RUNS.exists():
        return
    try:
        with open(OUTPUT_RUNS, encoding="utf-8-sig", newline="") as f:
            lector = csv.DictReader(f)
            cabecera = lector.fieldnames or []
            if all(c in cabecera for c in RUNS_CAMPOS):
                return
            filas = list(lector)
    except OSError:
        return
    with open(OUTPUT_RUNS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=RUNS_CAMPOS)
        w.writeheader()
        for fila in filas:
            w.writerow({c: fila.get(c, "") for c in RUNS_CAMPOS})
    print(f"  -> runs.csv migrado al esquema nuevo ({len(filas)} filas "
          f"conservadas).")


def registrar_run(info: dict):
    """Añade la fila de esta corrida a runs.csv."""
    _asegurar_esquema_runs()
    nuevo = not OUTPUT_RUNS.exists()
    with open(OUTPUT_RUNS, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=RUNS_CAMPOS)
        if nuevo:
            w.writeheader()
        w.writerow({c: info.get(c, "") for c in RUNS_CAMPOS})
    estado = "COMPLETO" if info.get("completo") == 1 else "INCOMPLETO"
    print(f"  -> runs.csv: {info.get('run_id')} {estado} "
          f"({info.get('n_leidas')}/{info.get('n_esperadas')} asignaturas, "
          f"{info.get('n_fallidas')} fallidas)")


def _sparkline_svg(valores: List[int], ancho=110, alto=26) -> str:
    """Mini-gráfica de la serie de cupos (escala 0..máximo, para que la
    línea de 'agotado' siempre sea el piso)."""
    if not valores:
        return ""
    tope = max(max(valores), 1)
    n = len(valores)
    puntos = []
    for i, v in enumerate(valores):
        x = 2 + (ancho - 4) * (i / max(n - 1, 1))
        y = 2 + (alto - 4) * (1 - v / tope)
        puntos.append(f"{x:.1f},{y:.1f}")
    color = "#C24438" if valores[-1] == 0 else "#4C7A2E"
    circulo = (f'<circle cx="{puntos[-1].split(",")[0]}" '
               f'cy="{puntos[-1].split(",")[1]}" r="2.4" fill="{color}"/>')
    if n == 1:
        return (f'<svg width="{ancho}" height="{alto}" viewBox="0 0 {ancho} {alto}">'
                f'{circulo}</svg>')
    return (f'<svg width="{ancho}" height="{alto}" viewBox="0 0 {ancho} {alto}">'
            f'<polyline points="{" ".join(puntos)}" fill="none" '
            f'stroke="{color}" stroke-width="1.6"/>{circulo}</svg>')


def reparsear_textos() -> int:
    """Recupera historial perdido re-parseando textos/*.jsonl.gz.

    Para esto se guardaba el texto crudo. Cuando el parser falla en silencio
    —como pasó con las asignaturas sin el rótulo "Contenido de la
    asignatura"—, la medición no se pierde: está archivada. Al arreglar el
    parser, este comando vuelve a leer lo archivado y añade al historial las
    filas que faltaban, con el run_id y la marca de tiempo ORIGINALES.

    Solo AÑADE lo que no está. La clave es (run_id, codigo, ts_lectura), no
    (run_id, codigo): el bucle largo del workflow hace hasta 11 barridos con
    el MISMO GITHUB_RUN_ID, así que run_id identifica una corrida de Actions,
    no una medición. Con la clave corta se recuperaba un solo barrido de cada
    corrida. El ts sí es único por lectura, y es el mismo string que se
    archivó en el .jsonl.gz, así que la comparación es exacta.

    Correrlo dos veces no duplica nada.

    orden_lectura queda en -1 ("desconocido"): el orden de las líneas del
    .jsonl.gz no reconstruye de forma fiable el del barrido, y prefiero un
    hueco declarado a un número inventado.
    """
    if not CARPETA_TEXTOS.is_dir():
        print("No hay carpeta textos/; nada que re-parsear.")
        return 0

    ya_estan = set()
    for fila in historial.leer():
        ya_estan.add((fila.get("run_id", ""), fila.get("codigo", ""),
                      fila.get("ts_lectura", "")))

    nuevas, por_codigo, corridas = 0, {}, set()
    pendientes: List[dict] = []

    if True:
        for ruta in sorted(CARPETA_TEXTOS.glob("run-*.jsonl.gz")):
            run_id = ruta.name[len("run-"):-len(".jsonl.gz")]
            try:
                with gzip.open(ruta, "rt", encoding="utf-8") as f:
                    lineas = list(f)
            except OSError as e:
                print(f"  ! No se pudo leer {ruta.name}: {e}")
                continue

            for linea in lineas:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    reg = json.loads(linea)
                except json.JSONDecodeError:
                    continue
                codigo = reg.get("codigo", "")
                ts = reg.get("ts", "")
                if not codigo or (run_id, codigo, ts) in ya_estan:
                    continue
                asig = parsear_texto_detalle(reg.get("texto", ""), codigo)
                if not asig.grupos:
                    continue
                for g in asig.grupos:
                    pendientes.append({
                        "run_id": run_id,
                        "ts_lectura": ts,
                        "codigo": codigo,
                        "actividad": g.actividad or "NA",
                        "grupo": g.grupo,
                        "profesores_raw": g.profesores,
                        "cupos_disponibles": g.cupos_disponibles or "NA",
                        "orden_lectura": -1,
                    })
                    nuevas += 1
                por_codigo[codigo] = por_codigo.get(codigo, 0) + 1
                corridas.add(run_id)
                ya_estan.add((run_id, codigo, ts))

    historial.anexar(pendientes)

    if nuevas:
        print(f"  -> Historial recuperado: {nuevas} filas nuevas "
              f"en {len(corridas)} corridas.")
        for cod, n in sorted(por_codigo.items(), key=lambda kv: -kv[1]):
            print(f"     {cod:12} {n} mediciones")
    else:
        print("  -> Nada que recuperar: el historial ya está completo.")
    return nuevas


def generar_series_json():
    """Compacta cupos_historial.csv en series.json para la página web.

    Tres decisiones de formato, todas por tamaño:

    1. Se colapsa por CORRIDA tomando el mínimo de cupos. Un grupo aparece
       una vez por actividad (teórica, taller, laboratorio) pero comparten la
       misma inscripción, así que son el mismo punto; el mínimo es el cupo
       que de verdad restringe. La página ya hacía esto en JavaScript en cada
       carga — aquí se hace una sola vez.
    2. Los instantes van como epoch en MINUTOS (no en milisegundos): el
       muestreo es de 30 min, los milisegundos son ruido y cuestan 5 dígitos
       por punto.
    3. Cada serie se guarda delta-codificada respecto al punto anterior. Los
       cupos casi no cambian entre snapshots, así que la mayoría de los
       deltas son ceros de un carácter.

    Las lecturas "NA" se omiten, igual que antes: NA no es 0.
    """
    if not historial.particiones():
        print("No hay historial todavía; corre primero: python sia_scraper.py cupos")
        return

    # clave -> {corrida: (epoch_min, cupos)}
    crudo: Dict[str, Dict[str, tuple]] = {}
    corridas = set()
    if True:
        for fila in historial.leer():
            try:
                cupos = int(fila["cupos_disponibles"])
            except (ValueError, TypeError, KeyError):
                continue
            ts = fila.get("ts_lectura") or fila.get("fecha_hora") or ""
            momento = _parse_ts(ts)
            if momento == datetime.min.replace(tzinfo=timezone.utc):
                continue
            minutos = int(momento.timestamp() // 60)
            clave = f"{fila['codigo']}|{fila['grupo']}"
            corrida = fila.get("run_id") or ts
            corridas.add(corrida)
            puntos = crudo.setdefault(clave, {})
            previo = puntos.get(corrida)
            if previo is None:
                puntos[corrida] = (minutos, cupos)
            else:
                puntos[corrida] = (min(previo[0], minutos),
                                   min(previo[1], cupos))

    series = {}
    t_min = t_max = None
    for clave, puntos in crudo.items():
        orden = sorted(puntos.values())
        if not orden:
            continue
        t0, c0 = orden[0]
        dt, dc = [], []
        t_ant, c_ant = t0, c0
        for t, c in orden[1:]:
            dt.append(t - t_ant)
            dc.append(c - c_ant)
            t_ant, c_ant = t, c
        series[clave] = {"t0": t0, "c0": c0, "dt": dt, "dc": dc}
        t_min = t0 if t_min is None else min(t_min, t0)
        t_max = t_ant if t_max is None else max(t_max, t_ant)

    salida = {
        "version": 1,
        "unidad_t": "epoch_min",
        "generado": ahora_iso(),
        "n_snapshots": len(corridas),
        "t_ini": t_min,
        "t_fin": t_max,
        "series": series,
    }
    with open(OUTPUT_SERIES, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, separators=(",", ":"))

    kb_json = OUTPUT_SERIES.stat().st_size / 1024
    print(f"  -> series.json: {len(series)} series, {len(corridas)} snapshots "
          f"({kb_json:.0f} KB; historial: {historial.resumen()})")


def generar_estadisticas():
    """Lee cupos_historial.csv completo y escribe estadisticas.html."""
    if not historial.particiones():
        print("No hay historial todavía; corre primero: python sia_scraper.py cupos")
        return

    # nombres de asignatura desde el catálogo (el historial v2 ya no los trae)
    nombres: Dict[str, str] = {}
    if OUTPUT_JSON.exists():
        try:
            with open(OUTPUT_JSON, encoding="utf-8") as f:
                for a in json.load(f):
                    nombres[a.get("codigo", "")] = a.get("nombre", "")
        except (json.JSONDecodeError, OSError):
            pass

    series: Dict[tuple, list] = {}
    marcas = set()
    if True:
        for fila in historial.leer():
            try:
                # "NA" (fallo de lectura) se salta aquí; en el análisis serio
                # se cuenta aparte, no se confunde con 0
                cupos = int(fila["cupos_disponibles"])
            except (ValueError, TypeError):
                continue
            # compatible con v1 (fecha_hora, sin actividad) y v2
            ts = fila.get("ts_lectura") or fila.get("fecha_hora") or ""
            actividad = fila.get("actividad") or ""
            if fila.get("asignatura"):
                nombres.setdefault(fila["codigo"], fila["asignatura"])
            clave = (fila["codigo"], actividad, fila["grupo"])
            series.setdefault(clave, []).append((ts, cupos))
            marcas.add(fila.get("run_id") or ts)
    todas_ts = sorted({ts for s in series.values() for ts, _ in s},
                      key=_parse_ts)

    filas_html = []
    agotados = 0
    for (cod, actividad, grupo), serie in sorted(
            series.items(), key=lambda kv: (nombres.get(kv[0][0], ""), kv[0])):
        # ordenar por instante REAL: mezclar offsets +00:00 (historial viejo)
        # y -05:00 (nuevo) rompe el orden lexicográfico
        serie.sort(key=lambda p: _parse_ts(p[0]))
        etiqueta_grupo = grupo
        if actividad and actividad not in ("NA", "AMBIGUA", "CLASE TEORICA"):
            etiqueta_grupo = f"{grupo} · {actividad.title()}"
        grupo = etiqueta_grupo
        vals = [v for _, v in serie]
        actual, inicial = vals[-1], vals[0]
        delta = actual - (vals[-2] if len(vals) > 1 else inicial)
        if actual == 0:
            agotados += 1
        flecha = ("▼ " + str(delta)) if delta < 0 else (
                 ("▲ +" + str(delta)) if delta > 0 else "=")
        clase = "baja" if delta < 0 else ("sube" if delta > 0 else "igual")
        filas_html.append(
            f'<tr data-buscar="{nombres.get(cod,"").lower()} {cod.lower()}">'
            f'<td class="mono">{cod}</td><td>{nombres.get(cod,"")}</td>'
            f'<td class="mono">{grupo}</td>'
            f'<td class="num {"cero" if actual==0 else ""}">{actual}</td>'
            f'<td class="num {clase}">{flecha}</td>'
            f'<td class="num">{min(vals)}–{max(vals)}</td>'
            f'<td>{_sparkline_svg(vals)}</td>'
            f'<td class="num mono">{len(vals)}</td></tr>')

    html = PLANTILLA_STATS.replace("__FILAS__", "\n".join(filas_html))
    html = html.replace("__N_GRUPOS__", str(len(series)))
    html = html.replace("__N_MEDICIONES__", str(len(marcas)))
    html = html.replace("__AGOTADOS__", str(agotados))
    html = html.replace("__RANGO__",
        f"{todas_ts[0][:16].replace('T',' ')} → {todas_ts[-1][:16].replace('T',' ')}"
        if todas_ts else "—")
    OUTPUT_STATS.write_text(html, encoding="utf-8")
    print(f"  -> Estadísticas regeneradas: {OUTPUT_STATS}")


PLANTILLA_STATS = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Evolución de cupos — SIA UNAL</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--papel:#FAFBF7;--tinta:#1E2A24;--suave:#5A6B60;--linea:#E3E9DD;
  --verde:#4C7A2E;--verde-claro:#EDF3E4;--rojo:#C24438;--rojo-claro:#FBEAE8;
  --display:'Space Grotesk',system-ui,sans-serif;--cuerpo:'IBM Plex Sans',system-ui,sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,Consolas,monospace}
*{box-sizing:border-box;margin:0}
body{font-family:var(--cuerpo);background:var(--papel);color:var(--tinta);font-size:14px;padding:18px}
h1{font-family:var(--display);font-size:20px;border-bottom:2px solid var(--tinta);padding-bottom:8px}
h1 span{color:var(--verde)}
.resumen{display:flex;gap:10px;margin:14px 0;flex-wrap:wrap}
.ficha{border:1.5px solid var(--tinta);background:#fff;padding:6px 14px;font-family:var(--mono);font-size:12.5px}
.ficha b{font-size:17px;display:block}
.ficha.mal{border-color:var(--rojo);color:var(--rojo);background:var(--rojo-claro)}
input{padding:8px 11px;font-size:14px;border:1.5px solid var(--linea);background:#fff;
  width:100%;max-width:420px;margin-bottom:12px;font-family:var(--cuerpo)}
input:focus{outline:none;border-color:var(--verde)}
table{border-collapse:collapse;width:100%;background:#fff;border:2px solid var(--tinta)}
th{font-family:var(--display);font-size:11.5px;letter-spacing:.05em;text-align:left;
  padding:7px 10px;border-bottom:2px solid var(--tinta);position:sticky;top:0;background:var(--papel)}
td{padding:5px 10px;border-bottom:1px solid var(--linea);font-size:12.5px}
tr:hover td{background:var(--verde-claro)}
.mono{font-family:var(--mono);font-size:11.5px}
.num{text-align:right;font-family:var(--mono)}
.cero{color:var(--rojo);font-weight:600}
.baja{color:var(--rojo)} .sube{color:var(--verde)} .igual{color:var(--suave)}
.pie{margin-top:12px;color:var(--suave);font-size:12px}
</style>
</head>
<body>
<h1>Evolución de cupos <span>· SIA UNAL</span></h1>
<div class="resumen">
  <div class="ficha"><b>__N_GRUPOS__</b>grupos seguidos</div>
  <div class="ficha"><b>__N_MEDICIONES__</b>mediciones</div>
  <div class="ficha mal"><b>__AGOTADOS__</b>agotados ahora</div>
  <div class="ficha"><b style="font-size:12px">__RANGO__</b>periodo</div>
</div>
<input id="q" type="search" placeholder="Filtrar por asignatura o código…">
<table>
<thead><tr><th>Código</th><th>Asignatura</th><th>Grupo</th><th>Cupos</th>
<th>Δ última</th><th>Mín–Máx</th><th>Tendencia</th><th>N</th></tr></thead>
<tbody id="cuerpo">
__FILAS__
</tbody>
</table>
<p class="pie">Cada corrida de <code>python sia_scraper.py cupos</code> añade una medición.
El CSV crudo está en <code>cupos_historial.csv</code> — ábrelo en Excel para tablas
dinámicas o gráficas más elaboradas.</p>
<script>
document.getElementById("q").addEventListener("input", e=>{
  const q=e.target.value.toLowerCase();
  for(const tr of document.querySelectorAll("#cuerpo tr"))
    tr.style.display = !q || (tr.dataset.buscar||"").includes(q) ? "" : "none";
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    resultados: Dict[str, Asignatura] = {} if MODO_CUPOS else cargar_previo()
    driver = None
    reinicios = 0
    plan_actual = PLANES_ESTUDIOS[0]

    # Bitácora de la corrida (se escribe en runs.csv pase lo que pase)
    run = {
        "run_id": RUN_ID, "ts_inicio": ahora_iso(), "ts_fin": "",
        "planes": " | ".join(PLANES_ESTUDIOS), "shard": SIA_SHARD or "1/1",
        "n_esperadas": 0, "n_leidas": 0, "n_fallidas": 0,
        "codigos_fallidos": [], "completo": 0,
        "n_paginas": "", "intentos_listado": "",
    }
    orden_global = 0
    termino_normal = False

    # El timeout de GitHub Actions manda SIGTERM: convertirlo en
    # KeyboardInterrupt para que el guardado de emergencia y runs.csv
    # alcancen a ejecutarse antes de morir.
    def _sigterm(_señal, _marco):
        raise KeyboardInterrupt("SIGTERM")
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, OSError):
        pass  # p. ej. si no estamos en el hilo principal

    def nuevo_driver():
        nonlocal driver
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        driver = crear_driver(headless=MODO_CUPOS)
        return driver

    def preparar():
        configurar_filtros(driver, plan_actual)
        return recolectar_con_verificacion(driver, plan_actual, verbose=False)

    try:
        driver = nuevo_driver()
        print(f"Los archivos se guardarán en: {CARPETA_SALIDA}\n")

        for n_plan, plan in enumerate(PLANES_ESTUDIOS, 1):
            plan_actual = plan
            print(f"===== Plan {n_plan}/{len(PLANES_ESTUDIOS)}: {plan} =====")
            print("Configurando filtros...")
            configurar_filtros(driver, plan)
            print("Recolectando códigos de todas las páginas de resultados...")
            codigos = recolectar_con_verificacion(driver, plan)
            print(f"\nSe encontraron {len(codigos)} asignaturas en {plan}.\n")

            if not codigos:
                print("No se encontró ninguna asignatura de este plan. Revisa "
                      "los archivos debug_*.png/html.")
                continue

            # a las ya raspadas (por otro plan o por reanudación) solo se les
            # anota este plan, sin volver a visitarlas
            pendientes = []
            for c in codigos:
                if c in resultados:
                    if plan not in resultados[c].planes:
                        resultados[c].planes.append(plan)
                else:
                    pendientes.append(c)
            if len(pendientes) < len(codigos):
                print(f"{len(codigos) - len(pendientes)} ya estaban procesadas; "
                      f"quedan {len(pendientes)}.\n")

            if SIA_SHARD:
                m = re.match(r"^(\d+)/(\d+)$", SIA_SHARD)
                if not m or not (1 <= int(m.group(1)) <= int(m.group(2))):
                    raise SystemExit(f"SIA_SHARD inválido: '{SIA_SHARD}' "
                                     f"(formato esperado: '2/4')")
                idx, total = int(m.group(1)) - 1, int(m.group(2))
                pendientes = [c for i, c in enumerate(pendientes)
                              if i % total == idx]
                print(f"Shard {SIA_SHARD}: este proceso toma "
                      f"{len(pendientes)} asignaturas.\n")

            run["n_esperadas"] += len(pendientes)
            run["n_paginas"] = INFO_LISTADO["paginas"]
            run["intentos_listado"] = INFO_LISTADO["intentos"]
            procesadas = 0
            for i, codigo in enumerate(pendientes, 1):
                print(f"[{i}/{len(pendientes)}] Procesando {codigo}...")
                intentos = 0
                exito = False

                while intentos < 3 and not exito:
                    if driver is None or not sesion_viva(driver):
                        if reinicios >= MAX_REINICIOS_DRIVER:
                            print("  ! Se agotaron los reintentos de reinicio del navegador.")
                            raise SystemExit(1)
                        reinicios += 1
                        print(f"  ! Navegador desconectado. Reiniciando "
                              f"({reinicios}/{MAX_REINICIOS_DRIVER})...")
                        driver = nuevo_driver()
                        preparar()

                    try:
                        click_asignatura_por_codigo(driver, codigo)
                        asig = parsear_detalle(driver, codigo)
                        # instante y posición de ESTA lectura, no de la corrida
                        asig.ts_lectura = ahora_iso()
                        asig.orden_lectura = orden_global
                        orden_global += 1
                        asig.planes = [plan]
                        resultados[codigo] = asig
                        run["n_leidas"] += 1
                        if MODO_CUPOS:
                            # escritura incremental: si el proceso muere, lo
                            # ya leído queda en disco con su run_id
                            registrar_historial_asignatura(asig)
                            guardar_texto_crudo(
                                codigo, asig.ts_lectura,
                                getattr(asig, "_texto_crudo", ""))
                        n_ses = sum(len(g.sesiones) for g in asig.grupos)
                        print(f"    {asig.nombre or codigo}: {len(asig.grupos)} grupos, "
                              f"{n_ses} sesiones")
                        volver_a_resultados(driver)
                        exito = True
                    except (TimeoutException, StaleElementReferenceException,
                            NoSuchElementException, RuntimeError):
                        intentos += 1
                        if manejar_sesion_caducada(driver):
                            print("  ! Sesión caducada, reconfigurando filtros...")
                            preparar()
                        elif not en_listado_resultados(driver):
                            # Solo si de verdad nos perdimos: primero el botón
                            # Volver (barato); reconstruir la búsqueda entera
                            # es el último recurso, porque es lentísimo.
                            try:
                                volver_a_resultados(driver)
                            except Exception:
                                try:
                                    print("  ! Reconstruyendo la búsqueda...")
                                    preparar()
                                except Exception:
                                    pass
                        print(f"  ! Reintentando {codigo} ({intentos}/3)...")
                    except (InvalidSessionIdException, WebDriverException):
                        intentos += 1
                        # Reintentar contra un driver muerto quema los 3
                        # intentos en un segundo. Si la sesión ya no responde,
                        # hay que levantar el navegador de nuevo aquí mismo.
                        print(f"  ! Falla del navegador en {codigo} "
                              f"({intentos}/3), se reintentará...")
                        if not sesion_viva(driver):
                            if reinicios >= MAX_REINICIOS_DRIVER:
                                print("  ! Se agotaron los reintentos de reinicio.")
                                raise SystemExit(1)
                            reinicios += 1
                            print(f"  ! Reiniciando navegador "
                                  f"({reinicios}/{MAX_REINICIOS_DRIVER})...")
                            driver = nuevo_driver()
                            preparar()
                        else:
                            time.sleep(2)

                if not exito:
                    print(f"  ! No se pudo procesar {codigo}, se omite.")
                    run["n_fallidas"] += 1
                    run["codigos_fallidos"].append(codigo)

                procesadas += 1
                if procesadas % CHECKPOINT_EVERY == 0:
                    guardar(list(resultados.values()))

            guardar(list(resultados.values()))
            print()

        termino_normal = True
        if MODO_CUPOS:
            # el historial ya se escribió incrementalmente; aquí solo se
            # regeneran las vistas derivadas
            generar_series_json()
            generar_estadisticas()

        print("Proceso terminado.")
        print(f"Archivos: {OUTPUT_GRUPOS_CSV}, {OUTPUT_HORARIOS_CSV}, {OUTPUT_JSON}")
        print(f"Armador de horario: abre {OUTPUT_HTML} en tu navegador.")
        if MODO_CUPOS:
            print(f"Historial de cupos: {historial.resumen()}")
            print(f"Bitácora de corridas: {OUTPUT_RUNS}")
            print(f"Estadísticas: abre {OUTPUT_STATS} en tu navegador.")
            print(f"Series para la web: {OUTPUT_SERIES}")

    except KeyboardInterrupt:
        print("\nInterrumpido (Ctrl-C o SIGTERM); guardando lo extraído...")
        guardar(list(resultados.values()))
        if MODO_CUPOS and resultados:
            generar_series_json()
            generar_estadisticas()
    finally:
        # runs.csv se escribe SIEMPRE (fin normal, interrupción o excepción).
        # completo=1 exige: terminar sin interrupciones, cero fallidas y
        # tantas leídas como esperadas. Si el proceso muere tan fuerte que ni
        # esto corre (SIGKILL), la corrida queda en el historial sin fila en
        # runs.csv — que es, a propósito, la otra señal de "incompleto".
        if MODO_CUPOS:
            run["ts_fin"] = ahora_iso()
            run["completo"] = int(
                termino_normal and run["n_fallidas"] == 0
                and run["n_leidas"] == run["n_esperadas"]
                and run["n_esperadas"] > 0)
            run["codigos_fallidos"] = ";".join(run["codigos_fallidos"])
            try:
                registrar_run(run)
            except OSError as e:
                print(f"  ! No se pudo escribir runs.csv: {e}")
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    import sys as _sys
    modo = _sys.argv[1].lower() if len(_sys.argv) > 1 else ""
    if modo == "cupos":
        # Snapshot de cupos: raspa todo de nuevo (headless), añade la medición
        # al historial y regenera estadisticas.html.
        MODO_CUPOS = True
        main()
    elif modo in ("stats", "estadisticas"):
        # Regenera estadisticas.html y series.json desde el historial.
        generar_series_json()
        generar_estadisticas()
    elif modo == "migrar":
        print("El historial vive particionado en historial/AAAA-MM-DD.csv.\n"
              "La migración desde el archivo monolítico se hace una sola vez "
              "con: python migrar_particiones.py --aplicar")
    elif modo in ("reparsear", "recuperar"):
        # Re-parsea textos/*.jsonl.gz y añade al historial lo que el parser
        # no supo leer en su momento. Útil tras arreglar un bug del parser.
        if reparsear_textos():
            generar_series_json()
            generar_estadisticas()
    elif modo == "html":
        # Regenera horario.html desde catalogo.json sin volver a raspar:
        #     python sia_scraper.py html
        previos = cargar_previo()
        if previos:
            generar_html(list(previos.values()))
            print(f"horario.html regenerado con {len(previos)} asignaturas: {OUTPUT_HTML}")
        else:
            print("No hay catalogo.json con datos; corre primero el scraper.")
    else:
        main()
