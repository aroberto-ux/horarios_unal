#!/usr/bin/env python3
"""
Catálogo de asignaturas de LIBRE ELECCIÓN — una foto al día
"""
# ... (todos los comentarios iguales) ...

import csv
import json
import os
import re
import time
import unicodedata
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

# ===========================================================================
# PARCHE MÍNIMO
# ===========================================================================

def _ahora_iso() -> str:
    return datetime.now().isoformat()

def _normalizar(texto: str) -> str:
    if not texto:
        return ""
    texto = texto.lower()
    texto = ''.join(c for c in unicodedata.normalize('NFKD', texto)
                    if not unicodedata.combining(c))
    return texto.strip()

def _ir_a_primera_pagina(driver):
    try:
        paginador = driver.find_element(By.CLASS_NAME, "pagination")
        enlace_primera = paginador.find_element(By.XPATH, ".//a[text()='1']")
        enlace_primera.click()
        time.sleep(1)
        if hasattr(sia, 'esperar_overlay_ppr_desaparezca'):
            sia.esperar_overlay_ppr_desaparezca(driver)
    except Exception:
        driver.get(sia.BASE_URL)
        configurar_filtros_libre(driver, reintentos=1)

def _guardar_debug(driver, nombre):
    try:
        timestamp = int(time.time())
        png_path = f"debug_{nombre}_{timestamp}.png"
        html_path = f"debug_{nombre}_{timestamp}.html"
        driver.save_screenshot(png_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"  -> Archivos de diagnóstico guardados en:")
        print(f"       {Path(png_path).absolute()}")
        print(f"       {Path(html_path).absolute()}")
    except Exception as e:
        print(f"  ! Error guardando debug: {e}")

# --- Asignar parches ---
for _name, _func in [
    ("ahora_iso", _ahora_iso),
    ("normalizar", _normalizar),
    ("ir_a_primera_pagina", _ir_a_primera_pagina),
    ("guardar_debug", _guardar_debug),
]:
    if not hasattr(sia, _name):
        setattr(sia, _name, _func)

# ===========================================================================
# FIN DEL PARCHE
# ===========================================================================

# ---------------------------------------------------------------------------
# Configuración - CORREGIDO para manejar PLAN_ESTUDIOS o PLANES_ESTUDIOS
# ---------------------------------------------------------------------------
NIVEL_ESTUDIO = sia.NIVEL_ESTUDIO
SEDE = sia.SEDE
FACULTAD = sia.FACULTAD

# CORRECCIÓN: manejar ambos nombres de variable
if hasattr(sia, 'PLAN_ESTUDIOS'):
    PLAN = sia.PLAN_ESTUDIOS
elif hasattr(sia, 'PLANES_ESTUDIOS'):
    # Si es una lista, tomar el primer elemento
    if isinstance(sia.PLANES_ESTUDIOS, list):
        PLAN = sia.PLANES_ESTUDIOS[0]
    else:
        PLAN = sia.PLANES_ESTUDIOS
else:
    raise AttributeError("No se encontró PLAN_ESTUDIOS ni PLANES_ESTUDIOS en sia_scraper")

TIPOLOGIA = "LIBRE ELECCIÓN"
CRITERIO_BUSQUEDA = "Por facultad y plan"
SEDE_BUSQUEDA = "1101 SEDE BOGOTÁ"
FACULTAD_BUSQUEDA = "2000 SEDE BOGOTÁ"

IDS_FIJOS = {"criterio": "", "sede": "", "facultad": ""}
CLAVES_COMBO = {
    "criterio": ("porquedeseasbuscar", "deseasbuscar", "criteriodebusqueda"),
    "sede": ("porquesede", "quesede"),
    "facultad": ("porquefacultad", "quefacultad"),
}
IDS_BASE = (sia.ID_NIVEL, sia.ID_SEDE, sia.ID_FACULTAD, sia.ID_PLAN, sia.ID_TIPOLOGIA)

CARPETA = Path(__file__).resolve().parent
SAL = {
    "OUTPUT_JSON": CARPETA / "catalogo_libre.json",
    "OUTPUT_GRUPOS_CSV": CARPETA / "grupos_libre.csv",
    "OUTPUT_HORARIOS_CSV": CARPETA / "horarios_libre.csv",
    "OUTPUT_HTML": CARPETA / "horario_libre.html",
    "OUTPUT_HISTORIAL": CARPETA / "cupos_libre.csv",
    "OUTPUT_SERIES": CARPETA / "series_libre.json",
}

LIMITE_MINUTOS = int(os.environ.get("SIA_LIBRE_LIMITE_MIN", "260"))
MAX_ASIGNATURAS = int(os.environ.get("SIA_LIBRE_MAX", "0"))
HEADLESS = os.environ.get("SIA_LIBRE_VENTANA", "0") != "1"
CHECKPOINT_EVERY = 10
UMBRAL_LISTADO = 0.85
TIMEOUT_COMBO_NUEVO = 25

# ---------------------------------------------------------------------------
# Contexto para redirigir salidas
# ---------------------------------------------------------------------------
@contextmanager
def salidas_libre():
    previo = {}
    for k in SAL:
        if hasattr(sia, k):
            previo[k] = getattr(sia, k)
    for k, v in SAL.items():
        if hasattr(sia, k):
            setattr(sia, k, v)
    if hasattr(sia, 'REANUDAR'):
        previo['REANUDAR'] = sia.REANUDAR
        sia.REANUDAR = True
    try:
        yield
    finally:
        for k, v in previo.items():
            setattr(sia, k, v)

# ---------------------------------------------------------------------------
# Inventario de combos
# ---------------------------------------------------------------------------
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
    return re.sub(r"[^a-z0-9]", "", _normalizar(texto))

def buscar_combo(driver, cual: str, usados: set, previos: set,
                 timeout=TIMEOUT_COMBO_NUEVO) -> Optional[str]:
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
    elegido = sia.seleccionar(driver, select_id, texto, **kw)
    if _normalizar(elegido) != _normalizar(texto):
        raise RuntimeError(
            f"En {select_id} se pidió «{texto}» y el SIA seleccionó "
            f"«{elegido}». No sigo: con la tipología equivocada este barrido "
            f"publicaría las asignaturas del plan como si fueran electivas. "
            f"Corre `python sia_libre.py combos` para ver las opciones reales.")
    return elegido

def _codigos_del_plan() -> set:
    ruta = CARPETA / "catalogo.json"
    if not ruta.exists():
        return set()
    try:
        with open(ruta, encoding="utf-8") as f:
            return {a["codigo"] for a in json.load(f) if a.get("codigo")}
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        return set()

def configurar_filtros_libre(driver, reintentos=3):
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
            previos = {c["id"] for c in inventario_combos(driver) if c["id"]}
            seleccionar_estricto(driver, sia.ID_TIPOLOGIA, TIPOLOGIA)
            usados = set(IDS_BASE)
            pendientes = (
                ("criterio", CRITERIO_BUSQUEDA),
                ("sede", SEDE_BUSQUEDA),
                ("facultad", FACULTAD_BUSQUEDA),
            )
            for cual, valor in pendientes:
                if not valor:
                    continue
                cid = buscar_combo(driver, cual, usados, previos)
                if not cid:
                    print(f"  ~ No apareció el combo «{cual}»; sigo con lo "
                          f"que el SIA traiga por defecto.")
                    continue
                try:
                    seleccionar_estricto(driver, cid, valor)
                    usados.add(cid)
                except RuntimeError as e:
                    print(f"  ~ Combo «{cual}» ({cid}) descartado: {e}")
                    usados.add(cid)
                    otro = buscar_combo(driver, cual, usados, previos, timeout=8)
                    if otro:
                        print(f"  ~ Reintento de «{cual}» con {otro}")
                        seleccionar_estricto(driver, otro, valor)
                        usados.add(otro)
                    else:
                        raise
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
            if hasattr(sia, 'esperar_overlay_ppr_desaparezca'):
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
    ruta = SAL["OUTPUT_JSON"]
    if not ruta.exists():
        return None
    try:
        with open(ruta, encoding="utf-8") as f:
            return len(json.load(f)) or None
    except (OSError, json.JSONDecodeError, TypeError):
        return None

def recolectar_libre(driver, intentos=3) -> List[str]:
    referencia = _n_referencia()
    piso = int(referencia * UMBRAL_LISTADO) if referencia else 0
    del_plan = _codigos_del_plan()
    mejor: List[str] = []
    for intento in range(1, intentos + 1):
        primera = sia.recolectar_todos_los_codigos(driver)
        segunda = sia.recolectar_todos_los_codigos(driver, verbose=False)
        if len(primera) > len(mejor):
            mejor = primera
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
    hoy = _ahora_iso()[:10]
    fechas = [d.get("ts_lectura", "")[:10] for d in datos if d.get("ts_lectura")]
    if not fechas or max(fechas) != hoy:
        print(f"El catálogo en disco es de {max(fechas) if fechas else '¿?'}; "
              f"hoy es {hoy}: se raspa todo de nuevo.")
        return {}
    with salidas_libre():
        previo = sia.cargar_previo()
    por_codigo = {d["codigo"]: d.get("ts_lectura", "") for d in datos
                  if d.get("codigo")}
    for cod, asig in previo.items():
        asig.ts_lectura = por_codigo.get(cod, "")
    return previo

# ---------------------------------------------------------------------------
# Barrido
# ---------------------------------------------------------------------------
def nuevo_driver(anterior=None):
    if anterior is not None:
        try:
            anterior.quit()
        except Exception:
            pass
    return sia.crear_driver(headless=HEADLESS)

def barrer(driver, codigos: List[str],
           resultados: Dict[str, "sia.Asignatura"]) -> dict:
    limite = time.time() + LIMITE_MINUTOS * 60
    reinicios = 0
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
                if reinicios >= 3:
                    print("  ! Se agotaron los reintentos de reinicio.")
                    resumen["cortado"] = True
                    return resumen
                reinicios += 1
                print(f"  ! Navegador caído. Reiniciando "
                      f"({reinicios}/3)...")
                driver = nuevo_driver(driver)
                preparar()
            try:
                if hasattr(sia, 'click_asignatura_por_codigo'):
                    sia.click_asignatura_por_codigo(driver, codigo)
                else:
                    raise RuntimeError("click_asignatura_por_codigo no existe en sia_scraper")
                
                if hasattr(sia, 'parsear_detalle'):
                    asig = sia.parsear_detalle(driver, codigo)
                else:
                    raise RuntimeError("parsear_detalle no existe en sia_scraper")
                
                asig.ts_lectura = _ahora_iso()
                asig.orden_lectura = orden
                asig.planes = [PLAN]
                orden += 1
                resultados[codigo] = asig
                resumen["leidas"] += 1
                with salidas_libre():
                    sia.registrar_historial_asignatura(asig)
                n_ses = sum(len(g.sesiones) for g in asig.grupos)
                n_grupos = len(asig.grupos)
                print(f"    {asig.nombre or codigo}: {n_grupos} grupos, {n_ses} sesiones")
                sia.volver_a_resultados(driver)
                exito = True
            except (TimeoutException, StaleElementReferenceException,
                    NoSuchElementException, RuntimeError) as e:
                intentos += 1
                print(f"  ! Error: {str(e)[:100]}")
                if hasattr(sia, 'manejar_sesion_caducada') and sia.manejar_sesion_caducada(driver):
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
                    sia.ir_a_primera_pagina(driver)
                print(f"  ! Reintentando {codigo} ({intentos}/3)...")
            except (InvalidSessionIdException, WebDriverException):
                intentos += 1
                print(f"  ! Falla del navegador en {codigo} ({intentos}/3)...")
                if not sia.sesion_viva(driver):
                    if reinicios >= 3:
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
        if i % CHECKPOINT_EVERY == 0:
            with salidas_libre():
                sia.guardar(list(resultados.values()))
    return resumen

# ---------------------------------------------------------------------------
# Modos
# ---------------------------------------------------------------------------
def modo_combos():
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

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
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
    total_grupos = sum(len(a.grupos) for a in resultados.values())
    print(f"\nListo en {minutos:.0f} min: {len(resultados)} asignaturas, {total_grupos} grupos.")
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
    elif modo in ("series", "stats"):
        with salidas_libre():
            sia.generar_series_json()
    else:
        main()
