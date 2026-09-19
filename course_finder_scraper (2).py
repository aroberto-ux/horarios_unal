"""
Scraper del "Course Finder" (endpoint JSON tras CloudFront)
=============================================================

Equivalente en espíritu a sia_scraper.py de horarios_unal, pero mucho más
simple: esa URL

    https://d3mq7oen5a8j4f.cloudfront.net/course-finder?filters=...&page=1&limit=20&sort=relevance

devuelve JSON directo (no una app JSF/ADF que solo renderiza en navegador),
así que no hace falta Selenium: basta con `requests`, paginando.

Genera los MISMOS TRES archivos "útiles" del repo original:

    grupos.csv     -> una fila por GRUPO/sección ofertada
    horarios.csv   -> una fila por SESIÓN (día/hora/salón) <- el más útil
                       para armar un horario o pasarlo a una hoja de cálculo
    catalogo.json  -> estructura anidada: asignatura > grupos > sesiones

Además guarda:
    course_finder_raw.json -> TODAS las respuestas crudas del servidor, tal
                               cual, página por página. Es la red de
                               seguridad si el mapeo de campos de abajo no
                               coincide con el formato real.

--------------------------------------------------------------------------
LO QUE TODAVÍA NO PUDE VERIFICAR EN VIVO
--------------------------------------------------------------------------
No tengo salida de red hacia cloudfront.net desde mi entorno, así que NO
he visto una respuesta real de este endpoint. Lo que hice para compensar:

  1. `extraer_registros()` prueba varias claves típicas donde podría venir
     la lista de cursos ("data", "results", "items", "courses"...).
  2. Cada campo (código, nombre, profesor, salón, día, hora...) se busca
     con VARIOS nombres alternativos a la vez (español/inglés,
     snake_case/camelCase) mediante `_get()`, así que aunque no sepa el
     nombre exacto de la clave, hay buenas chances de que lo encuentre solo.
  3. Al terminar la página 1, el script IMPRIME el primer registro crudo
     completo y sus llaves de nivel superior — o sea, en el primer segundo
     de ejecución vas a ver la forma real del JSON.

Si después de correrlo ves columnas vacías en grupos.csv/horarios.csv que
no deberían estarlo, mándame lo que se imprimió en consola (o
course_finder_raw.json) y ajusto las listas de nombres candidatos en
`_get()` / `_primera_lista()` en un momento — son las únicas líneas que
dependen de adivinar el formato.

Instalación:
    pip install requests

Uso:
    python course_finder_scraper.py
"""

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Configuración de la búsqueda (ajusta a gusto)
# ---------------------------------------------------------------------------

BASE_URL = "https://d3mq7oen5a8j4f.cloudfront.net/course-finder"

FILTERS = {
    "typologies": [],
    "level": ["Pregrado"],
    "faculty": ["2055 - Facultad de Ingeniería"],
    "sede": ["1101 - Bogotá"],
    "plan": ["2542 - INGENIERÍA CIVIL"],
    "credits": [],
    "has_available": None,
}

LIMIT = 20
SORT = "relevance"
MAX_PAGINAS = 300        # tope de seguridad
PAUSA_ENTRE_PAGINAS = 0.4  # segundos, cortesía con el servidor
REINTENTOS_POR_PAGINA = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    # Si el servidor responde 403, algunos endpoints tras CloudFront exigen
    # un Referer del sitio que los consume. Descomenta y ajusta si hace falta:
    # "Referer": "https://REEMPLAZA-CON-EL-SITIO-QUE-USA-ESTE-ENDPOINT/",
}

CARPETA_SALIDA = Path(__file__).resolve().parent
OUTPUT_RAW_JSON = CARPETA_SALIDA / "course_finder_raw.json"
OUTPUT_GRUPOS_CSV = CARPETA_SALIDA / "grupos.csv"
OUTPUT_HORARIOS_CSV = CARPETA_SALIDA / "horarios.csv"
OUTPUT_CATALOGO_JSON = CARPETA_SALIDA / "catalogo.json"

# Claves candidatas donde podría venir la lista de cursos dentro del JSON
CLAVES_LISTA_CURSOS = ["data", "results", "items", "courses", "records", "content", "asignaturas"]
# Claves candidatas para metadatos de paginación (para saber cuándo parar)
CLAVES_TOTAL = ["total", "totalCount", "total_count", "totalItems", "total_items",
                 "totalRecords", "total_records", "count"]


# ---------------------------------------------------------------------------
# Modelo de datos (mismo espíritu que el Sesion/Grupo/Asignatura del repo)
# ---------------------------------------------------------------------------

@dataclass
class Sesion:
    dia: str = ""
    hora_inicio: str = ""
    hora_fin: str = ""
    salon: str = ""
    edificio: str = ""


@dataclass
class Grupo:
    grupo: str = ""
    profesores: str = ""
    cupos_disponibles: str = ""
    cupos_totales: str = ""
    jornada: str = ""
    sesiones: List[Sesion] = field(default_factory=list)


@dataclass
class Asignatura:
    codigo: str = ""
    nombre: str = ""
    tipologia: str = ""
    creditos: str = ""
    facultad: str = ""
    plan: str = ""
    grupos: List[Grupo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Resolución flexible de nombres de campo
# ---------------------------------------------------------------------------

def _normalizar_clave(k: str) -> str:
    return k.lower().replace("_", "").replace("-", "").replace(" ", "")


def _get(d: Any, *claves: str, default: str = "") -> Any:
    """Busca la primera clave presente en `d`, probando varios nombres
    alternativos (insensible a mayúsculas/guiones/camelCase vs snake_case)."""
    if not isinstance(d, dict):
        return default
    indice = {_normalizar_clave(k): k for k in d.keys()}
    for clave in claves:
        real = indice.get(_normalizar_clave(clave))
        if real is not None and d[real] not in (None, ""):
            return d[real]
    return default


def _primera_lista(d: Any, *claves: str) -> list:
    for clave in claves:
        v = _get(d, clave, default=None)
        if isinstance(v, list):
            return v
    return []


# ---------------------------------------------------------------------------
# Parsing de un registro crudo -> Asignatura/Grupo/Sesion
# ---------------------------------------------------------------------------

def parsear_sesion(raw: dict) -> Sesion:
    return Sesion(
        dia=str(_get(raw, "dia", "day", "weekday", "diaSemana")),
        hora_inicio=str(_get(raw, "hora_inicio", "horaInicio", "start_time", "startTime", "inicio")),
        hora_fin=str(_get(raw, "hora_fin", "horaFin", "end_time", "endTime", "fin")),
        salon=str(_get(raw, "salon", "aula", "room", "classroom", "salonNombre")),
        edificio=str(_get(raw, "edificio", "building", "bloque")),
    )


def parsear_grupo(raw: dict) -> Grupo:
    sesiones_raw = _primera_lista(
        raw, "sesiones", "sessions", "schedule", "horarios", "meetings", "horario"
    )
    return Grupo(
        grupo=str(_get(raw, "grupo", "group", "numero_grupo", "groupNumber", "seccion", "section")),
        profesores=str(_get(raw, "profesor", "profesores", "teacher", "teachers",
                             "instructor", "instructors", "docente")),
        cupos_disponibles=str(_get(raw, "cupos_disponibles", "cuposDisponibles",
                                    "available", "available_slots", "vacantes", "disponibles")),
        cupos_totales=str(_get(raw, "cupos_totales", "cuposTotales", "capacity",
                                "total_slots", "cupos", "capacidad")),
        jornada=str(_get(raw, "jornada", "shift", "schedule_type")),
        sesiones=[parsear_sesion(s) for s in sesiones_raw if isinstance(s, dict)],
    )


def parsear_asignatura(raw: dict) -> Asignatura:
    grupos_raw = _primera_lista(raw, "grupos", "groups", "sections", "offerings", "grupos_ofertados")
    return Asignatura(
        codigo=str(_get(raw, "codigo", "code", "course_code", "courseCode", "id")),
        nombre=str(_get(raw, "nombre", "name", "course_name", "courseName", "title", "titulo")),
        tipologia=str(_get(raw, "tipologia", "typology", "type", "typologies")),
        creditos=str(_get(raw, "creditos", "credits", "credit_hours", "creditCount")),
        facultad=str(_get(raw, "facultad", "faculty")),
        plan=str(_get(raw, "plan", "program", "plan_estudios", "planEstudios")),
        grupos=[parsear_grupo(g) for g in grupos_raw if isinstance(g, dict)],
    )


# ---------------------------------------------------------------------------
# Peticiones HTTP
# ---------------------------------------------------------------------------

def pedir_pagina(session: requests.Session, page: int) -> dict:
    params = {
        "filters": json.dumps(FILTERS, ensure_ascii=False),
        "page": page,
        "limit": LIMIT,
        "sort": SORT,
    }
    ultimo_error = None
    for intento in range(1, REINTENTOS_POR_PAGINA + 1):
        try:
            resp = session.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504):
                ultimo_error = f"HTTP {resp.status_code}"
                time.sleep(1.5 * intento)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            ultimo_error = str(e)
            time.sleep(1.5 * intento)
    raise RuntimeError(f"No se pudo obtener la página {page} tras {REINTENTOS_POR_PAGINA} intentos: {ultimo_error}")


def extraer_registros(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for clave in CLAVES_LISTA_CURSOS:
            valor = payload.get(clave)
            if isinstance(valor, list):
                return valor
            if isinstance(valor, dict):
                for subclave in CLAVES_LISTA_CURSOS:
                    subvalor = valor.get(subclave)
                    if isinstance(subvalor, list):
                        return subvalor
    return []


def extraer_total_declarado(payload) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for clave in CLAVES_TOTAL:
        v = payload.get(clave)
        if isinstance(v, int):
            return v
        # a veces el total viene en un sub-objeto de metadatos
        for subclave in ("meta", "pagination", "paging"):
            sub = payload.get(subclave)
            if isinstance(sub, dict) and isinstance(sub.get(clave), int):
                return sub[clave]
    return None


# ---------------------------------------------------------------------------
# Recolección de todas las páginas
# ---------------------------------------------------------------------------

def recolectar_todo() -> tuple[list, list]:
    """Devuelve (payloads_crudos, registros_crudos_desempaquetados)."""
    session = requests.Session()
    payloads_crudos: list = []
    registros: list = []

    print(f"Consultando {BASE_URL}")
    print(f"Filtros: {json.dumps(FILTERS, ensure_ascii=False)}\n")

    for page in range(1, MAX_PAGINAS + 1):
        try:
            payload = pedir_pagina(session, page)
        except RuntimeError as e:
            print(f" ! {e}")
            break

        payloads_crudos.append(payload)
        pagina_registros = extraer_registros(payload)

        if page == 1:
            print("--- Diagnóstico de la página 1 (para ajustar el mapeo si hace falta) ---")
            if isinstance(payload, dict):
                print(f"Llaves de nivel superior: {list(payload.keys())}")
            if pagina_registros:
                print("Primer registro crudo:")
                print(json.dumps(pagina_registros[0], ensure_ascii=False, indent=2)[:2000])
            else:
                print("(No se identificó ninguna lista de cursos en la respuesta; "
                      "revisa course_finder_raw.json)")
            print("--- fin del diagnóstico ---\n")

        print(f" Página {page}: {len(pagina_registros)} registros")

        if not pagina_registros:
            break

        registros.extend(pagina_registros)

        total_declarado = extraer_total_declarado(payload)
        if total_declarado is not None and len(registros) >= total_declarado:
            break
        if total_declarado is None and len(pagina_registros) < LIMIT:
            break

        time.sleep(PAUSA_ENTRE_PAGINAS)

    return payloads_crudos, registros


# ---------------------------------------------------------------------------
# Escritura de salidas
# ---------------------------------------------------------------------------

def horario_resumen(grupo: Grupo) -> str:
    partes = []
    for s in grupo.sesiones:
        trozo = " ".join(x for x in [s.dia, f"{s.hora_inicio}-{s.hora_fin}".strip("-")] if x)
        if s.salon:
            trozo += f" ({s.salon})"
        if trozo:
            partes.append(trozo)
    return "; ".join(partes)


def escribir_catalogo_json(asignaturas: List[Asignatura]):
    data = [asdict(a) for a in asignaturas]
    OUTPUT_CATALOGO_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def escribir_grupos_csv(asignaturas: List[Asignatura]):
    import csv
    with OUTPUT_GRUPOS_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "nombre", "tipologia", "creditos", "facultad", "plan",
                    "grupo", "profesores", "cupos_disponibles", "cupos_totales",
                    "jornada", "horario_resumen"])
        for a in asignaturas:
            if not a.grupos:
                w.writerow([a.codigo, a.nombre, a.tipologia, a.creditos, a.facultad, a.plan,
                            "", "", "", "", "", ""])
                continue
            for g in a.grupos:
                w.writerow([a.codigo, a.nombre, a.tipologia, a.creditos, a.facultad, a.plan,
                            g.grupo, g.profesores, g.cupos_disponibles, g.cupos_totales,
                            g.jornada, horario_resumen(g)])


def escribir_horarios_csv(asignaturas: List[Asignatura]):
    import csv
    with OUTPUT_HORARIOS_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["codigo", "nombre", "grupo", "profesores", "dia",
                    "hora_inicio", "hora_fin", "salon", "edificio"])
        for a in asignaturas:
            for g in a.grupos:
                for s in g.sesiones:
                    w.writerow([a.codigo, a.nombre, g.grupo, g.profesores, s.dia,
                                s.hora_inicio, s.hora_fin, s.salon, s.edificio])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    payloads_crudos, registros_crudos = recolectar_todo()

    OUTPUT_RAW_JSON.write_text(
        json.dumps(payloads_crudos, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nGuardado: {OUTPUT_RAW_JSON}  ({len(payloads_crudos)} páginas crudas)")

    if not registros_crudos:
        print(
            "\n! No se recolectó ningún curso. Revisa course_finder_raw.json para\n"
            "  ver la respuesta real del servidor (puede ser un 403/bloqueo, o que\n"
            "  la forma del JSON no coincida con las claves candidatas de arriba)."
        )
        return

    asignaturas = [parsear_asignatura(r) for r in registros_crudos if isinstance(r, dict)]

    escribir_catalogo_json(asignaturas)
    escribir_grupos_csv(asignaturas)
    escribir_horarios_csv(asignaturas)

    total_grupos = sum(len(a.grupos) for a in asignaturas)
    total_sesiones = sum(len(g.sesiones) for a in asignaturas for g in a.grupos)

    print(f"Guardado: {OUTPUT_CATALOGO_JSON}  ({len(asignaturas)} asignaturas)")
    print(f"Guardado: {OUTPUT_GRUPOS_CSV}  ({total_grupos} grupos)")
    print(f"Guardado: {OUTPUT_HORARIOS_CSV}  ({total_sesiones} sesiones)")

    if total_grupos == 0:
        print(
            "\n(aviso: se encontraron asignaturas pero 0 grupos — es probable que\n"
            " este endpoint de listado no incluya grupos/sesiones anidados y haga\n"
            " falta una segunda llamada por curso. Compárteme un registro de\n"
            " course_finder_raw.json y agrego ese paso.)"
        )


if __name__ == "__main__":
    main()
