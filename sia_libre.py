#!/usr/bin/env python3
"""
Catálogo de asignaturas de LIBRE ELECCIÓN — una foto al día
===========================================================

Hermano de sia_scraper.py. Aquel barre el plan (tipología "TODAS MENOS LIBRE
ELECCIÓN") cada 30 minutos porque lo que interesa ahí es la CARRERA DE CUPOS.
Las electivas son otro problema: son cientos, casi nadie pelea por ellas al
segundo, y un barrido completo tarda entre 30 y 90 minutos. Medirlas cada
media hora sería tirar horas de Actions para ver el mismo número. Por eso
este script corre UNA VEZ AL DÍA.

Todo el trabajo pesado (driver, combos ADF, paginación, parser del detalle,
escritura de CSV/JSON) es el de sia_scraper.py: aquí solo se cambian los
filtros de búsqueda y las rutas de salida. Si mañana el SIA cambia el formato
del detalle, se arregla en un solo sitio y los dos scrapers se enteran.

Lo único realmente nuevo es el formulario. Al elegir la tipología
"LIBRE ELECCIÓN" el SIA despliega tres combos más que no existen en el otro
modo:

    * ¿Por qué deseas buscar?   -> "Por facultad y plan"
    * ¿Porque sede?             -> "1101 SEDE BOGOTÁ"
    * ¿Por qué facultad?        -> "2000 SEDE BOGOTÁ"

Sus IDs NO están documentados y, a diferencia de los cinco de arriba, no los
tenemos verificados contra el HTML. Así que no se codifican a mano: se buscan
por la ETIQUETA visible del combo y, si eso falla, por descarte (los <select>
que no existían antes de elegir la tipología). Si aun así el SIA los cambia,
`python sia_libre.py combos` imprime todos los combos con su id, su etiqueta y
sus opciones para poder fijarlos a mano en IDS_FIJOS.

Uso:
    python sia_libre.py            # barrido completo (lo que corre el workflow)
    python sia_libre.py listado    # solo los códigos, sin abrir cada detalle
    python sia_libre.py combos     # diagnóstico del formulario (con ventana)
    python sia_libre.py series     # regenera series_libre.json desde el CSV

Salidas (todas con sufijo _libre para no pisar jamás las del plan):
    catalogo_libre.json    estructura anidada; también sirve para reanudar
    grupos_libre.csv       una fila por grupo
    horarios_libre.csv     una fila por sesión  <- lo que lee index.html
    cupos_libre.csv        histórico, una fila por grupo y por día
    series_libre.json      lo mismo, compactado para la web
    horario_libre.html     armador independiente, solo con electivas

Variables de entorno útiles:
    SIA_LIBRE_LIMITE_MIN   corta el barrido a los N minutos (def. 260)
    SIA_LIBRE_MAX          procesa solo las N primeras (para probar)
    SIA_LIBRE_VENTANA      "1" para ver el navegador
"""

import csv
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import sia_scraper as sia

# ---------------------------------------------------------------------------
# Qué se busca
# ---------------------------------------------------------------------------
# Los cuatro primeros filtros son los mismos del plan: se heredan de
# sia_scraper para no tener dos sitios que actualizar cuando cambie la sede o
# se agregue un plan.
NIVEL_ESTUDIO = sia.NIVEL_ESTUDIO
SEDE = sia.SEDE
FACULTAD = sia.FACULTAD
PLAN = sia.PLANES_ESTUDIOS[0]

TIPOLOGIA = "LIBRE ELECCIÓN"

# Los tres combos que solo aparecen con esa tipología. Deja alguno en "" para
# no tocarlo y quedarte con lo que el SIA traiga por defecto.
CRITERIO_BUSQUEDA = "Por facultad y plan"
SEDE_BUSQUEDA = "1101 SEDE BOGOTÁ"
FACULTAD_BUSQUEDA = "2000 SEDE BOGOTÁ"

# Rescate manual: si algún día la detección automática falla, corre
# `python sia_libre.py combos`, copia los IDs que imprime y ponlos aquí.
IDS_FIJOS = {
    "criterio": "",
    "sede": "",
    "facultad": "",
}

# Etiquetas con las que se reconoce cada combo. Se comparan sin tildes, sin
# espacios y sin signos, así que "¿Porque sede?" y "¿Por qué sede?" son lo
# mismo — que es justo la clase de errata que trae el formulario del SIA.
CLAVES_COMBO = {
    "criterio": ("porquedeseasbuscar", "deseasbuscar", "criteriodebusqueda"),
    "sede": ("porquesede", "quesede"),
    "facultad": ("porquefacultad", "quefacultad"),
}

IDS_BASE = (sia.ID_NIVEL, sia.ID_SEDE, sia.ID_FACULTAD, sia.ID_PLAN,
            sia.ID_TIPOLOGIA)

# ---------------------------------------------------------------------------
# Salidas y comportamiento
# ---------------------------------------------------------------------------
CARPETA = Path(__file__).resolve().parent

SAL = {
    "OUTPUT_JSON": CARPETA / "catalogo_libre.json",
    "OUTPUT_GRUPOS_CSV": CARPETA / "grupos_libre.csv",
    "OUTPUT_HORARIOS_CSV": CARPETA / "horarios_libre.csv",
    "OUTPUT_HTML": CARPETA / "horario_libre.html",
    "OUTPUT_HISTORIAL": CARPETA / "cupos_libre.csv",
    "OUTPUT_SERIES": CARPETA / "series_libre.json",
}

# Tope de tiempo. El job de Actions tiene 6 h; cortar a las ~4½ h deja margen
# para commitear lo conseguido en vez de que GitHub mate el proceso a secas.
LIMITE_MINUTOS = int(os.environ.get("SIA_LIBRE_LIMITE_MIN", "260"))
MAX_ASIGNATURAS = int(os.environ.get("SIA_LIBRE_MAX", "0"))  # 0 = sin tope
HEADLESS = os.environ.get("SIA_LIBRE_VENTANA", "0") != "1"

CHECKPOINT_EVERY = 10     # guardar en disco cada N asignaturas
MAX_FALLOS_SEGUIDOS = 8   # tras tantos fallos consecutivos, abortar el barrido
UMBRAL_LISTADO = 0.85     # piso de cordura frente al catálogo de ayer
TIMEOUT_COMBO_NUEVO = 25  # espera a que ADF dibuje los combos de libre elección

# Qué estrategia de clic está funcionando ahora mismo (solo para el log).
ESTRATEGIA = {"usada": None}


@contextmanager
def salidas_libre():
    """Apunta las rutas de sia_scraper a los archivos _libre mientras dure el
    bloque.

    Parece un truco sucio y es exactamente lo contrario: es lo que permite
    reutilizar guardar(), cargar_previo(), registrar_historial_asignatura() y
    generar_series_json() tal cual, sin copiar sesenta líneas de escritura de
    CSV que mañana habría que arreglar por duplicado. Lo importante es que
    NINGUNA de esas funciones puede correr fuera de este contexto: guardar()
    reescribe horario.html, y sin el desvío el armador del plan quedaría con
    puras electivas.
    """
    previo = {k: getattr(sia, k) for k in SAL}
    previo["REANUDAR"] = sia.REANUDAR
    for k, v in SAL.items():
        setattr(sia, k, v)
    sia.REANUDAR = True
    try:
        yield
    finally:
        for k, v in previo.items():
            setattr(sia, k, v)


# ---------------------------------------------------------------------------
# Inventario de combos del formulario
# ---------------------------------------------------------------------------

# Se lee todo de una sola pasada dentro del navegador, igual que hace
# sia_scraper: una llamada a execute_script es atómica, así que ADF no puede
# reemplazar el <select> a mitad de la lectura y dejarnos una referencia
# muerta.
_JS_INVENTARIO = r"""
const salida = [];
for (const s of document.querySelectorAll('select')) {
  let etq = '';
  if (s.id) {
    try {
      const l = document.querySelector('label[for="' + CSS.escape(s.id) + '"]');
      if (l) etq = l.textContent;
    } catch (e) {}
  }
  if (!etq) {
    // ADF no siempre pone el 'for': se busca la etiqueta más cercana hacia
    // arriba, que es donde la deja su layout de tabla.
    let n = s.parentElement, k = 0;
    while (n && k < 6 && !etq) {
      const l = n.querySelector('label');
      if (l) etq = l.textContent;
      n = n.parentElement; k++;
    }
  }
  const r = s.getBoundingClientRect();
  salida.push({
    id: s.id || '',
    etiqueta: (etq || '').replace(/\s+/g, ' ').trim(),
    n: s.options.length,
    sel: s.selectedIndex >= 0 ? s.options[s.selectedIndex].text.trim() : '',
    visible: !!(s.offsetParent || r.width > 0 || r.height > 0),
    opciones: Array.from(s.options).slice(0, 12).map(o => o.text.trim())
  });
}
return salida;
"""


def inventario_combos(driver) -> List[dict]:
    try:
        inv = driver.execute_script(_JS_INVENTARIO)
        return inv if isinstance(inv, list) else []
    except WebDriverException:
        return []


def _compacto(texto: str) -> str:
    """'¿Por qué deseas buscar?' -> 'porquedeseasbuscar'.

    Quitar espacios y signos es lo que hace que 'Porque sede' y 'Por qué sede'
    caigan en la misma cadena; con solo bajar a minúsculas y quitar tildes,
    las dos formas seguirían siendo distintas.
    """
    return re.sub(r"[^a-z0-9]", "", sia.normalizar(texto))


def buscar_combo(driver, cual: str, usados: set, previos: set,
                 timeout=TIMEOUT_COMBO_NUEVO) -> Optional[str]:
    """Localiza uno de los combos de libre elección.

    Dos estrategias, en este orden:

    1. Por ETIQUETA. Es la buena: sobrevive a que ADF renumere los IDs.
    2. Por DESCARTE. Si la etiqueta no aparece (ADF a veces la pinta un
       instante después que el <select>, o la deja sin 'for'), se toma el
       primer combo que no existía antes de elegir la tipología y que aún no
       hemos usado. El orden del DOM coincide con el visual, así que
       criterio -> sede -> facultad salen en ese orden si se piden en ese
       orden, que es como los llama configurar_filtros_libre().
    """
    if IDS_FIJOS.get(cual):
        return IDS_FIJOS[cual]

    claves = CLAVES_COMBO[cual]
    limite = time.time() + timeout
    candidato_por_descarte = None

    while time.time() < limite:
        inv = inventario_combos(driver)
        for c in inv:
            if not c["id"] or c["id"] in usados or not c["visible"]:
                continue
            etq = _compacto(c["etiqueta"])
            if etq and any(k in etq for k in claves):
                return c["id"]
        if candidato_por_descarte is None:
            nuevos = [c["id"] for c in inv
                      if c["id"] and c["id"] not in previos
                      and c["id"] not in usados and c["visible"] and c["n"] > 1]
            if nuevos:
                candidato_por_descarte = nuevos[0]
        time.sleep(0.4)

    if candidato_por_descarte:
        print(f"  ~ El combo de {cual} no se reconoció por su etiqueta; "
              f"se usa {candidato_por_descarte} (por descarte).")
    return candidato_por_descarte


def imprimir_inventario(driver, titulo: str):
    print(f"\n--- {titulo} ---")
    for c in inventario_combos(driver):
        if not c["id"]:
            continue
        marca = " " if c["visible"] else "·"
        print(f" {marca} {c['id']}")
        print(f"     etiqueta : {c['etiqueta'] or '(sin etiqueta)'}")
        print(f"     opciones : {c['n']} | actual: {c['sel']}")
        if c["opciones"]:
            print(f"     primeras : {c['opciones'][:6]}")


# ---------------------------------------------------------------------------
# Configuración de filtros
# ---------------------------------------------------------------------------

def seleccionar_estricto(driver, select_id, texto, **kw) -> str:
    """Como sia.seleccionar(), pero exige que la opción elegida sea LA pedida.

    seleccionar() cae a coincidencia por subcadena cuando la exacta falla, y
    aquí eso es una trampa mortal: en el combo de tipología conviven

        LIBRE ELECCIÓN
        TODAS MENOS LIBRE ELECCIÓN   <- contiene a la anterior

    así que un espacio raro o una tilde distinta en el HTML bastan para que el
    scraper elija la segunda, raspe el plan entero y lo escriba en los archivos
    _libre como si fueran electivas. Nadie se enteraría: el barrido termina
    bien, publica 65 asignaturas y la página las muestra. Mejor reventar.
    """
    elegido = sia.seleccionar(driver, select_id, texto, **kw)
    if sia.normalizar(elegido) != sia.normalizar(texto):
        raise RuntimeError(
            f"En {select_id} se pidió «{texto}» y el SIA seleccionó "
            f"«{elegido}». No sigo: con la tipología equivocada este barrido "
            f"publicaría las asignaturas del plan como si fueran electivas. "
            f"Corre `python sia_libre.py combos` para ver las opciones reales.")
    return elegido


def _codigos_del_plan() -> set:
    """Los códigos que ya raspa sia_scraper.py (catalogo.json del plan)."""
    ruta = CARPETA / "catalogo.json"
    if not ruta.exists():
        return set()
    try:
        with open(ruta, encoding="utf-8") as f:
            return {a["codigo"] for a in json.load(f) if a.get("codigo")}
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        return set()


# ---------------------------------------------------------------------------
# Clic al detalle
# ---------------------------------------------------------------------------
# El listado de libre elección no se comporta como el del plan: hay anclas con
# el mismo texto que el código y a.click() no siempre dispara el enlace de ADF.
# Estas tres estrategias se prueban en orden y la que funcione se escribe en el
# log, así la primera corrida sirve de diagnóstico.

_JS_ENLACES_CODIGO = r"""
const objetivo = (arguments[0] || '').trim();
const nodos = document.querySelectorAll("div[role='grid'] a, a[class*='af_commandLink']");
const salida = [];
let i = 0;
for (const a of nodos) {
  if ((a.textContent || '').trim() === objetivo) {
    const r = a.getBoundingClientRect();
    salida.push({
      pos: salida.length,
      id: (a.id || '').slice(0, 60),
      clase: (a.className || '').slice(0, 50),
      visible: !!a.offsetParent && r.width > 0 && r.height > 0,
      onclick: !!(a.onclick || a.getAttribute('onclick')),
      href: (a.getAttribute('href') || '').slice(0, 30)
    });
  }
  i++;
}
return salida;
"""

_JS_CLICK_ENLACE = r"""
const objetivo = (arguments[0] || '').trim();
const cual = arguments[1];
const modo = arguments[2];
const iguales = [];
for (const a of document.querySelectorAll("div[role='grid'] a, a[class*='af_commandLink']")) {
  if ((a.textContent || '').trim() === objetivo) iguales.push(a);
}
const a = iguales[cual];
if (!a) return false;
try { a.scrollIntoView({block: 'center'}); } catch (e) {}
if (modo === 'evento') {
  // Algunos af_commandLink solo reaccionan a una secuencia de ratón de
  // verdad; a.click() no dispara sus manejadores.
  for (const t of ['mouseover', 'mousedown', 'mouseup', 'click']) {
    a.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
  }
} else {
  a.click();
}
return true;
"""


def enlaces_del_codigo(driver, codigo: str) -> List[dict]:
    try:
        r = driver.execute_script(_JS_ENLACES_CODIGO, codigo)
        return r if isinstance(r, list) else []
    except WebDriverException:
        return []


def esperar_detalle(driver, segundos: float) -> bool:
    try:
        WebDriverWait(driver, segundos).until(
            EC.presence_of_element_located((By.XPATH, sia._XP_DETALLE)))
        return True
    except TimeoutException:
        return False


def diagnostico_click(driver, codigo: str, enlaces: List[dict]):
    """Qué hay detrás de un clic que no llevó a ninguna parte."""
    print(f"  ?? Diagnóstico de {codigo}:")
    print(f"     anclas con ese texto: {len(enlaces)}")
    for e in enlaces:
        print(f"       #{e['pos']} visible={e['visible']} onclick={e['onclick']} "
              f"href={e['href']!r} id={e['id']!r}")
    try:
        print(f"     ¿seguimos en el listado?: {sia.en_listado_resultados(driver)}")
        texto = driver.find_element(By.TAG_NAME, "body").text
        lineas = [l.strip() for l in texto.splitlines() if l.strip()][:12]
        print("     primeras líneas de la página en la que estamos:")
        for l in lineas:
            print(f"       | {l[:100]}")
    except WebDriverException:
        pass


def click_detalle(driver, codigo: str, espera=10.0, diagnosticar=False) -> str:
    """Abre el detalle del código. Devuelve la estrategia que funcionó."""
    enlaces = enlaces_del_codigo(driver, codigo)

    if not enlaces:
        # No está en esta página: que sia_scraper lo busque paginando.
        sia.click_asignatura_por_codigo(driver, codigo)
        return "sia_scraper (con paginación)"

    # Las anclas visibles primero: en las tablas de ADF la copia oculta suele
    # ir antes en el DOM y es la que no reacciona.
    orden = sorted(range(len(enlaces)), key=lambda i: (not enlaces[i]["visible"], i))

    for cual in orden:
        driver.execute_script(_JS_CLICK_ENLACE, codigo, cual, "click")
        if esperar_detalle(driver, espera):
            return f"a.click() en el ancla #{cual}" + \
                   ("" if enlaces[cual]["visible"] else " (oculta)")
        if not sia.en_listado_resultados(driver):
            break   # nos movimos a otro sitio: no seguir pulsando a ciegas

    if sia.en_listado_resultados(driver):
        driver.execute_script(_JS_CLICK_ENLACE, codigo, orden[0], "evento")
        if esperar_detalle(driver, espera):
            return f"evento de ratón en el ancla #{orden[0]}"

    if diagnosticar:
        diagnostico_click(driver, codigo, enlaces)
    sia.guardar_debug(driver, f"click_libre_{codigo}")
    raise TimeoutException(
        f"Ninguna estrategia de clic abrió el detalle de {codigo}")


def verificar_formulario(driver, esperado: Dict[str, str]):
    """Lee el formulario tal y como quedó y comprueba que dice lo que pedimos.

    Es la guarda que faltaba. Sin ella, un formulario mal configurado produce
    un barrido perfectamente normal —sin errores, sin avisos— de asignaturas
    que no son las que se pidieron. Pasó de verdad: nueve asignaturas de
    posgrado de la FACULTAD DE MINAS (Medellín) publicadas como electivas de
    Ingeniería Civil en Bogotá. El único síntoma fue que en la web salían
    cuatro electivas raras.

    Además imprime SIEMPRE el estado completo del formulario: son ocho líneas
    por corrida y son las que permiten entender de un vistazo qué buscó el
    scraper cuando algo sale raro.
    """
    inv = {c["id"]: c for c in inventario_combos(driver)}
    print("\n  Formulario tal y como quedó:")
    for c in inventario_combos(driver):
        if c["id"] and c["visible"] and c["n"] > 1:
            marca = "*" if c["id"] in esperado else " "
            print(f"   {marca} {c['etiqueta'][:34]:36} = {c['sel'][:40]}")

    malos = []
    for cid, valor in esperado.items():
        actual = inv.get(cid, {}).get("sel", "(no está)")
        if sia.normalizar(actual) != sia.normalizar(valor):
            malos.append(f"«{inv.get(cid, {}).get('etiqueta', cid)}» "
                         f"pedimos «{valor}» y quedó «{actual}»")
    if malos:
        raise RuntimeError(
            "El formulario no quedó como se pidió, no busco:\n     - "
            + "\n     - ".join(malos))
    print()


def configurar_filtros_libre(driver, reintentos=3):
    """Los cinco filtros de siempre + los tres de libre elección, y a buscar."""
    ultimo_error = None

    for intento in range(1, reintentos + 1):
        try:
            driver.get(sia.BASE_URL)
            time.sleep(1.5)

            sia.seleccionar(driver, sia.ID_NIVEL, NIVEL_ESTUDIO)
            sia.seleccionar(driver, sia.ID_SEDE, SEDE)
            sia.seleccionar(driver, sia.ID_FACULTAD, FACULTAD)
            sia.seleccionar(driver, sia.ID_PLAN, PLAN,
                            timeout=sia.TIMEOUT_COMBO_PLAN)

            # Foto de los combos ANTES de la tipología: lo que aparezca
            # después es, por definición, del formulario de libre elección.
            previos = {c["id"] for c in inventario_combos(driver) if c["id"]}
            # Estricto: aquí «LIBRE ELECCIÓN» no puede acabar siendo
            # «TODAS MENOS LIBRE ELECCIÓN» (ver seleccionar_estricto).
            seleccionar_estricto(driver, sia.ID_TIPOLOGIA, TIPOLOGIA)

            # Lo que el formulario DEBE decir cuando terminemos.
            esperado = {
                sia.ID_NIVEL: NIVEL_ESTUDIO, sia.ID_SEDE: SEDE,
                sia.ID_FACULTAD: FACULTAD, sia.ID_PLAN: PLAN,
                sia.ID_TIPOLOGIA: TIPOLOGIA,
            }
            usados = set(IDS_BASE)
            pendientes = (
                ("criterio", CRITERIO_BUSQUEDA),
                ("sede", SEDE_BUSQUEDA),
                ("facultad", FACULTAD_BUSQUEDA),
            )
            for cual, valor in pendientes:
                if not valor:
                    continue
                # Se buscan de uno en uno y no todos de golpe: elegir el
                # criterio dispara otro refresco parcial y es ESE refresco el
                # que dibuja los combos de sede y facultad.
                cid = buscar_combo(driver, cual, usados, previos)
                if not cid:
                    # Antes esto seguía "con lo que el SIA traiga por defecto",
                    # y el defecto resultó ser otra sede entera.
                    raise RuntimeError(
                        f"No apareció el combo «{cual}» tras elegir "
                        f"{TIPOLOGIA}. Corre `python sia_libre.py combos` "
                        f"para ver qué desplegables hay ahora.")
                try:
                    seleccionar_estricto(driver, cid, valor)
                    usados.add(cid)
                    esperado[cid] = valor
                except RuntimeError:
                    # NO se prueba con otro combo. Ir tanteando desplegables
                    # hasta que alguno acepte el valor fue exactamente lo que
                    # produjo un barrido entero de la FACULTAD DE MINAS: el
                    # formulario acaba en un estado que nadie pidió y el
                    # resultado parece legítimo. Mejor parar y enseñar qué hay.
                    opciones = next((c["opciones"] for c in inventario_combos(driver)
                                     if c["id"] == cid), [])
                    print(f"  ! El combo «{cual}» ({cid}) no acepta «{valor}».")
                    print(f"    Sus opciones son: {opciones}")
                    raise

            verificar_formulario(driver, esperado)

            WebDriverWait(driver, sia.TIMEOUT_NAV).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//a[.//text()='Mostrar'] | //button[.//text()='Mostrar']")
                )
            ).click()

            WebDriverWait(driver, sia.TIMEOUT_NAV).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(text(),'Resultado de la consulta')]")
                )
            )
            sia.esperar_overlay_ppr_desaparezca(driver)
            return

        except (RuntimeError, TimeoutException, StaleElementReferenceException,
                NoSuchElementException) as e:
            ultimo_error = e
            print(f"  ! Falló la configuración de filtros "
                  f"(intento {intento}/{reintentos}): {type(e).__name__}")
            if intento < reintentos:
                print("  ! Recargando la página y empezando de cero...")
                time.sleep(2)

    sia.guardar_debug(driver, "filtros_libre")
    raise RuntimeError(f"No se pudieron configurar los filtros: {ultimo_error}")


# ---------------------------------------------------------------------------
# Listado
# ---------------------------------------------------------------------------

def _n_referencia() -> Optional[int]:
    """Cuántas electivas trajo el barrido de ayer. Solo como piso, nunca como
    techo: la oferta cambia entre semestres y a media inscripción."""
    ruta = SAL["OUTPUT_JSON"]
    if not ruta.exists():
        return None
    try:
        with open(ruta, encoding="utf-8") as f:
            return len(json.load(f)) or None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def recolectar_libre(driver, intentos=3) -> List[str]:
    """Igual que recolectar_con_verificacion() del plan, menos la prueba del
    solape: aquí no hay una lista canónica de códigos contra la cual comparar,
    porque cualquier asignatura de la sede puede entrar y salir de la oferta.

    Quedan las dos comprobaciones que sí aplican: doble lectura idéntica (la
    tabla de ADF sigue mutando un par de segundos tras pulsar Mostrar) y piso
    del 85% respecto de ayer.
    """
    referencia = _n_referencia()
    piso = int(referencia * UMBRAL_LISTADO) if referencia else 0
    del_plan = _codigos_del_plan()
    mejor: List[str] = []

    for intento in range(1, intentos + 1):
        primera = sia.recolectar_todos_los_codigos(driver)
        segunda = sia.recolectar_todos_los_codigos(driver, verbose=False)
        if len(primera) > len(mejor):
            mejor = primera

        # ¿Nos devolvieron el listado del PLAN? Es lo que pasa si la tipología
        # acabó en «TODAS MENOS LIBRE ELECCIÓN». Comparar con catalogo.json es
        # la señal más limpia que hay: las electivas de otras facultades no
        # tienen por qué coincidir con el plan de Civil.
        solape = (len(set(primera) & del_plan) / len(del_plan)) if del_plan and primera else 0

        if not primera:
            motivo = "el listado salió vacío"
        elif solape > 0.5:
            raise RuntimeError(
                f"El listado coincide en un {solape:.0%} con las asignaturas "
                f"del plan (catalogo.json): esto NO es libre elección. Lo más "
                f"probable es que la tipología quedara en «TODAS MENOS LIBRE "
                f"ELECCIÓN». No escribo nada; revisa con "
                f"`python sia_libre.py combos`.")
        elif primera != segunda:
            motivo = (f"dos lecturas no coinciden ({len(primera)} vs "
                      f"{len(segunda)} asignaturas)")
        elif len(primera) < piso:
            motivo = (f"el listado trae {len(primera)} y ayer hubo "
                      f"{referencia}")
        else:
            if referencia and len(primera) != referencia:
                print(f"  (ojo: {len(primera)} electivas frente a "
                      f"{referencia} de ayer; la oferta pudo cambiar)")
            return primera

        print(f"  ! {motivo}. Rehaciendo la búsqueda ({intento}/{intentos})...")
        sia.guardar_debug(driver, f"listado_libre_{len(primera)}")
        if intento < intentos:
            configurar_filtros_libre(driver)

    print(f"  ! El listado nunca cuadró; sigo con el mejor obtenido "
          f"({len(mejor)} asignaturas).")
    return mejor


# ---------------------------------------------------------------------------
# Reanudación
# ---------------------------------------------------------------------------

def cargar_para_reanudar() -> Dict[str, "sia.Asignatura"]:
    """Reanuda solo si el catálogo en disco es de HOY.

    El barrido es diario y completo, así que reanudar sobre el archivo de ayer
    sería publicar cupos viejos con fecha de hoy — el error más caro posible
    en este repo. Solo tiene sentido cuando el job se murió a media mañana y
    Actions lo reintenta el mismo día.
    """
    if os.environ.get("SIA_LIBRE_DESDE_CERO") == "1":
        print("SIA_LIBRE_DESDE_CERO=1: se ignora el catálogo en disco.")
        return {}
    ruta = SAL["OUTPUT_JSON"]
    if not ruta.exists():
        return {}
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(datos, list) or not datos:
        return {}

    hoy = sia.ahora_iso()[:10]
    fechas = [d.get("ts_lectura", "")[:10] for d in datos if d.get("ts_lectura")]
    if not fechas or max(fechas) != hoy:
        print(f"El catálogo en disco es de {max(fechas) if fechas else '¿?'}; "
              f"hoy es {hoy}: se raspa todo de nuevo.")
        return {}

    with salidas_libre():
        previo = sia.cargar_previo()
    # cargar_previo() no restaura ts_lectura (no le hace falta al plan), pero
    # aquí sí: es el sello con el que se decide si el archivo es de hoy.
    por_codigo = {d["codigo"]: d.get("ts_lectura", "") for d in datos
                  if d.get("codigo")}
    for cod, asig in previo.items():
        asig.ts_lectura = por_codigo.get(cod, "")
    return previo


# ---------------------------------------------------------------------------
# Barrido
# ---------------------------------------------------------------------------

def barrer(driver, codigos: List[str],
           resultados: Dict[str, "sia.Asignatura"]) -> dict:
    """Abre el detalle de cada código y lo guarda. Devuelve un resumen."""
    limite = time.time() + LIMITE_MINUTOS * 60
    reinicios = 0
    seguidas = 0
    orden = 0
    resumen = {"leidas": 0, "fallidas": [], "cortado": False}

    pendientes = [c for c in codigos if c not in resultados]
    if len(pendientes) < len(codigos):
        print(f"{len(codigos) - len(pendientes)} ya estaban en el catálogo de "
              f"hoy; quedan {len(pendientes)}.\n")
    if MAX_ASIGNATURAS:
        pendientes = pendientes[:MAX_ASIGNATURAS]
        print(f"SIA_LIBRE_MAX={MAX_ASIGNATURAS}: se procesan solo "
              f"{len(pendientes)}.\n")

    def preparar():
        configurar_filtros_libre(driver)
        recolectar_libre(driver, intentos=1)

    for i, codigo in enumerate(pendientes, 1):
        if time.time() > limite:
            print(f"\n! Se agotó el presupuesto de {LIMITE_MINUTOS} min con "
                  f"{len(pendientes) - i + 1} asignaturas por leer. Guardo lo "
                  f"que hay y termino ordenadamente.")
            resumen["cortado"] = True
            break

        print(f"[{i}/{len(pendientes)}] Procesando {codigo}...")
        intentos, exito = 0, False

        while intentos < 3 and not exito:
            if driver is None or not sia.sesion_viva(driver):
                if reinicios >= sia.MAX_REINICIOS_DRIVER:
                    print("  ! Se agotaron los reintentos de reinicio.")
                    resumen["cortado"] = True
                    return resumen
                reinicios += 1
                print(f"  ! Navegador caído. Reiniciando "
                      f"({reinicios}/{sia.MAX_REINICIOS_DRIVER})...")
                driver = nuevo_driver(driver)
                preparar()

            try:
                estrategia = click_detalle(driver, codigo,
                                           diagnosticar=(resumen["leidas"] == 0
                                                         and intentos == 0))
                if estrategia != ESTRATEGIA["usada"]:
                    # Solo se anuncia cuando cambia: en un barrido de cientos
                    # no hace falta repetirlo en cada línea.
                    print(f"    (clic: {estrategia})")
                    ESTRATEGIA["usada"] = estrategia
                asig = sia.parsear_detalle(driver, codigo)
                asig.ts_lectura = sia.ahora_iso()
                asig.orden_lectura = orden
                asig.planes = [PLAN]
                orden += 1
                resultados[codigo] = asig
                resumen["leidas"] += 1
                with salidas_libre():
                    sia.registrar_historial_asignatura(asig)
                n_ses = sum(len(g.sesiones) for g in asig.grupos)
                print(f"    {asig.nombre or codigo}: {len(asig.grupos)} grupos, "
                      f"{n_ses} sesiones")
                sia.volver_a_resultados(driver)
                exito = True

            except (TimeoutException, StaleElementReferenceException,
                    NoSuchElementException, RuntimeError):
                intentos += 1
                if sia.manejar_sesion_caducada(driver):
                    print("  ! Sesión caducada, reconfigurando filtros...")
                    preparar()
                elif not sia.en_listado_resultados(driver):
                    try:
                        sia.volver_a_resultados(driver)
                    except Exception:
                        try:
                            print("  ! Reconstruyendo la búsqueda...")
                            preparar()
                        except Exception:
                            pass
                else:
                    # El listado de electivas ocupa muchas páginas y
                    # click_asignatura_por_codigo() solo sabe avanzar. Si el
                    # código quedó atrás, hay que rebobinar o no se encuentra
                    # nunca más.
                    sia.ir_a_primera_pagina(driver)
                print(f"  ! Reintentando {codigo} ({intentos}/3)...")

            except (InvalidSessionIdException, WebDriverException):
                intentos += 1
                print(f"  ! Falla del navegador en {codigo} ({intentos}/3)...")
                if not sia.sesion_viva(driver):
                    if reinicios >= sia.MAX_REINICIOS_DRIVER:
                        print("  ! Se agotaron los reintentos de reinicio.")
                        resumen["cortado"] = True
                        return resumen
                    reinicios += 1
                    driver = nuevo_driver(driver)
                    preparar()
                else:
                    time.sleep(2)

        if not exito:
            print(f"  ! No se pudo procesar {codigo}, se omite.")
            resumen["fallidas"].append(codigo)
            seguidas += 1
            # Con 54 electivas a ~70 s por fallo, insistir cuesta una hora de
            # Actions para acabar con el mismo catálogo vacío. Si se cae todo
            # de seguido no es mala suerte: es que algo estructural cambió.
            if seguidas >= MAX_FALLOS_SEGUIDOS:
                print(f"\n! {seguidas} asignaturas seguidas sin poder leerse. "
                      f"Algo cambió en el SIA: paro aquí en vez de gastar el "
                      f"resto del presupuesto. Mira el diagnóstico de más "
                      f"arriba y los debug_click_libre_*.html.")
                resumen["cortado"] = True
                break
        else:
            seguidas = 0

        if i % CHECKPOINT_EVERY == 0:
            with salidas_libre():
                sia.guardar(list(resultados.values()))

    return resumen


def nuevo_driver(anterior=None):
    if anterior is not None:
        try:
            anterior.quit()
        except Exception:
            pass
    return sia.crear_driver(headless=HEADLESS)


# ---------------------------------------------------------------------------
# Modos
# ---------------------------------------------------------------------------

def modo_combos():
    """Diagnóstico: imprime los combos del formulario en cada etapa.

    Es la herramienta a la que hay que volver el día que el SIA cambie de
    versión y el barrido empiece a traer cero electivas.
    """
    driver = sia.crear_driver(headless=os.environ.get("SIA_LIBRE_VENTANA") != "1")
    try:
        driver.get(sia.BASE_URL)
        time.sleep(2)
        imprimir_inventario(driver, "Formulario recién cargado")

        sia.seleccionar(driver, sia.ID_NIVEL, NIVEL_ESTUDIO)
        sia.seleccionar(driver, sia.ID_SEDE, SEDE)
        sia.seleccionar(driver, sia.ID_FACULTAD, FACULTAD)
        sia.seleccionar(driver, sia.ID_PLAN, PLAN, timeout=sia.TIMEOUT_COMBO_PLAN)
        previos = {c["id"] for c in inventario_combos(driver) if c["id"]}

        sia.seleccionar(driver, sia.ID_TIPOLOGIA, TIPOLOGIA)
        time.sleep(2)
        imprimir_inventario(driver, f"Tras elegir «{TIPOLOGIA}»")

        cid = buscar_combo(driver, "criterio", set(IDS_BASE), previos)
        print(f"\n  -> combo de criterio detectado: {cid or '(ninguno)'}")
        if cid and CRITERIO_BUSQUEDA:
            sia.seleccionar(driver, cid, CRITERIO_BUSQUEDA)
            time.sleep(2)
            imprimir_inventario(driver, f"Tras elegir «{CRITERIO_BUSQUEDA}»")
            usados = set(IDS_BASE) | {cid}
            for cual in ("sede", "facultad"):
                otro = buscar_combo(driver, cual, usados, previos, timeout=8)
                print(f"  -> combo de {cual} detectado: {otro or '(ninguno)'}")
                if otro:
                    usados.add(otro)

        print("\nSi alguno salió '(ninguno)' o con el id equivocado, cópialo "
              "de la lista de arriba a IDS_FIJOS en sia_libre.py.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def modo_listado():
    """Solo los códigos: sirve para comprobar los filtros en dos minutos en vez
    de esperar el barrido entero."""
    driver = nuevo_driver()
    try:
        configurar_filtros_libre(driver)
        codigos = recolectar_libre(driver)
        print(f"\n{len(codigos)} asignaturas de libre elección:")
        for c in codigos:
            print(f"  {c}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def modo_detalle(codigo: str):
    """Abre UNA asignatura y cuenta qué pasa. Diagnóstico de 2 minutos para el
    fallo «tras hacer clic no se reconoció la página de detalle»."""
    driver = nuevo_driver()
    try:
        configurar_filtros_libre(driver)
        codigos = sia.recolectar_todos_los_codigos(driver)
        if codigo not in codigos:
            print(f"{codigo} no está en el listado. Los primeros son: "
                  f"{codigos[:8]}")
            return
        enlaces = enlaces_del_codigo(driver, codigo)
        print(f"\nAnclas con el texto «{codigo}»: {len(enlaces)}")
        for e in enlaces:
            print(f"  #{e['pos']} visible={e['visible']} onclick={e['onclick']} "
                  f"href={e['href']!r}\n      id={e['id']!r} clase={e['clase']!r}")
        try:
            print(f"\n-> {click_detalle(driver, codigo, diagnosticar=True)}")
            texto = driver.find_element(By.TAG_NAME, "body").text
            print("\nPrimeras líneas del detalle:")
            for l in [x.strip() for x in texto.splitlines() if x.strip()][:14]:
                print(f"  | {l[:100]}")
        except TimeoutException:
            print("\nNinguna estrategia funcionó; revisa los debug_*.html.")
    finally:
        try: driver.quit()
        except Exception: pass


def main():
    print(f"Los archivos se guardarán en: {CARPETA}\n")
    resultados = cargar_para_reanudar()
    driver = None
    inicio = time.time()

    try:
        driver = nuevo_driver()
        print("Configurando filtros de libre elección...")
        configurar_filtros_libre(driver)
        print("Recolectando códigos...")
        codigos = recolectar_libre(driver)
        print(f"\nSe encontraron {len(codigos)} asignaturas de libre elección.\n")

        if not codigos:
            print("Listado vacío: no se toca nada de lo que ya hay publicado. "
                  "Revisa los debug_*.png/html o corre "
                  "`python sia_libre.py combos`.")
            raise SystemExit(1)

        resumen = barrer(driver, codigos, resultados)

    except KeyboardInterrupt:
        print("\nInterrumpido; guardando lo extraído...")
        resumen = {"leidas": len(resultados), "fallidas": [], "cortado": True}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    if not resultados:
        print("No se extrajo ninguna asignatura; no se escribe nada.")
        raise SystemExit(1)

    with salidas_libre():
        sia.guardar(list(resultados.values()))
        sia.generar_series_json()

    minutos = (time.time() - inicio) / 60
    print(f"\nListo en {minutos:.0f} min: {len(resultados)} asignaturas, "
          f"{sum(len(a.grupos) for a in resultados.values())} grupos.")
    if resumen.get("fallidas"):
        print(f"Fallaron {len(resumen['fallidas'])}: "
              f"{', '.join(resumen['fallidas'][:10])}"
              f"{'...' if len(resumen['fallidas']) > 10 else ''}")
    for k, v in SAL.items():
        print(f"  {v.name}")


if __name__ == "__main__":
    import sys

    modo = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if modo == "combos":
        modo_combos()
    elif modo == "listado":
        modo_listado()
    elif modo == "detalle":
        if len(sys.argv) < 3:
            print("Uso: python sia_libre.py detalle CODIGO   (p. ej. 2017472)")
            raise SystemExit(2)
        modo_detalle(sys.argv[2])
    elif modo in ("series", "stats"):
        with salidas_libre():
            sia.generar_series_json()
    else:
        main()
