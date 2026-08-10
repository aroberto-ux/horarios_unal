"""Tests de regresión del parser del SIA.

Por qué existen: parsear_texto_detalle() trabaja sobre el TEXTO de la página,
no sobre el DOM. Si el SIA cambia una coma de sitio, el parser no explota:
devuelve una asignatura con cero grupos y el barrido publica un snapshot
vacío sin que nadie se entere. Estos tests son la alarma.

Dos capas:

  - Unitarias, con fragmentos que reproducen las rarezas conocidas del
    formato (la sangría de "(1) Grupo 1", la ubicación en la línea SIGUIENTE
    al horario, "Cupos disponibles" ausente).
  - De regresión, contra los textos crudos reales archivados en textos/*.jsonl.gz.
    Son páginas del SIA de verdad, así que si el parser deja de entenderlas
    es porque se rompió algo.

Correr:  pytest -q
"""

import gzip
import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import sia_scraper as sia  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures de texto: copiados de páginas reales, recortados
# ---------------------------------------------------------------------------

PAGINA_MINIMA = """PORTAL DE SERVICIOS ACADÉMICOS
Información de la asignatura
    Volver
Acueductos (2015938)
Tipología: DISCIPLINAR OBLIGATORIA
Créditos:3
INGENIERÍA CIVIL
Facultad: FACULTAD DE INGENIERÍA
  Contenido de la asignatura
  CLASE TEORICA (2015938)
  (1) Grupo 1
  Profesor: Maria Alejandra Caicedo Londoño.
Facultad:
Horarios/Aula: No informado
Fecha:24/08/2026 - 17/12/2026
MARTES de 18:00 a 20:00.
SALON DE CLASE 406-227. 406-227. 406 - Carlos Alfonso Cortés Amador. SALON.
JUEVES de 18:00 a 20:00.
SALON DE CLASE 406-227. 406-227. 406 - Carlos Alfonso Cortés Amador. SALON.
Duración: Semestral
Jornada: DIURNO
Cupos disponibles: 25
"""

PAGINA_DOS_ACTIVIDADES = """    Volver
Mecánica de Fluidos (2015951)
Tipología: DISCIPLINAR OBLIGATORIA
Créditos:3
Facultad: FACULTAD DE INGENIERÍA
  Contenido de la asignatura
  CLASE TEORICA (2015951)
  (1) Grupo 1
  Profesor: Uno Perez. DOS GOMEZ.
Fecha:24/08/2026 - 17/12/2026
LUNES de 07:00 a 09:00.
SALON DE CLASE 401-101. 401-101. 401 - Julio Garavito Armero. SALON.
Duración: Semestral
Jornada: DIURNO
Cupos disponibles: 0
  LABORATORIO (2015952)
  (1) Grupo 1
  Profesor: Tres Ramirez.
Fecha:24/08/2026 - 17/12/2026
VIERNES de 14:00 a 17:00.
LAB HIDRAULICA. 409-101. 409 - Luis Enrique Orduz Espinosa. LABORATORIO.
Duración: Semestral
Jornada: DIURNO
Cupos disponibles: 12
Prerrequisitos
2015940 Física Mecánica
"""

PAGINA_SIN_CUPOS = """    Volver
Trabajo de Grado (2015999)
Tipología: TRABAJO DE GRADO
Créditos:6
Facultad: FACULTAD DE INGENIERÍA
  Contenido de la asignatura
  CLASE TEORICA (2015999)
  (1) Grupo 1
  Profesor: No informado
Fecha:24/08/2026 - 17/12/2026
Duración: Semestral
Jornada: DIURNO
"""


# Recortada de la página real de 2024045 (Taller de proyectos
# interdisciplinarios). Dos rarezas juntas: NO trae la línea "Contenido de la
# asignatura" y la cabecera de actividad viene sin sangría. El parser
# devolvía cero grupos y la materia desaparecía de la página.
PAGINA_SIN_ANCLA = """PORTAL DE SERVICIOS ACADÉMICOS
Información de la asignatura
    Volver
Taller de proyectos interdisciplinarios (2024045)
Tipología: DISCIPLINAR OPTATIVA
Créditos:3
INGENIERÍA CIVIL
Facultad: FACULTAD DE INGENIERÍA
CLASE TEORICA (2024045)
  (1) 2024045-1 Taller de proyectos interdisciplinarios
  Profesor: No informado
Facultad: FACULTAD DE INGENIERÍA
Horarios/Aula: No informado
Fecha:27/08/2026 - 17/12/2026
LUNES de 14:00 a 15:00.
SALON DE CLASE 454-207. 454-207. 454 - Luis Carlos Sarmiento Angulo. SALON.
MIÉRCOLES de 15:00 a 16:00.
SALON DE CLASE 454-207. 454-207. 454 - Luis Carlos Sarmiento Angulo. SALON.
Duración: Semestral
Jornada: DIURNO
Cupos disponibles: 0
"""


# ---------------------------------------------------------------------------
# Cabecera de la asignatura
# ---------------------------------------------------------------------------

def test_lee_cabecera():
    a = sia.parsear_texto_detalle(PAGINA_MINIMA, "2015938")
    assert a.codigo == "2015938"
    assert a.nombre == "Acueductos"
    assert a.tipologia == "DISCIPLINAR OBLIGATORIA"
    assert a.creditos == "3"
    assert a.facultad == "FACULTAD DE INGENIERÍA"


def test_pagina_vacia_no_explota():
    a = sia.parsear_texto_detalle("", "2015938")
    assert a.grupos == []
    assert a.codigo == "2015938"


# ---------------------------------------------------------------------------
# Grupos y sesiones: las rarezas que ya rompieron el parser una vez
# ---------------------------------------------------------------------------

def test_grupo_con_sangria():
    """'(1) Grupo 1' viene indentado. El split exigía '(' pegado al salto."""
    a = sia.parsear_texto_detalle(PAGINA_MINIMA, "2015938")
    assert len(a.grupos) == 1
    assert a.grupos[0].grupo == "(1) Grupo 1"
    assert a.grupos[0].actividad == "CLASE TEORICA"


def test_ubicacion_va_en_la_linea_siguiente():
    """El salón NO está en la misma línea que el día y la hora."""
    s = sia.parsear_texto_detalle(PAGINA_MINIMA, "2015938").grupos[0].sesiones
    assert len(s) == 2
    assert s[0].dia == "MARTES"
    assert (s[0].hora_inicio, s[0].hora_fin) == ("18:00", "20:00")
    assert s[0].salon == "406-227"
    assert s[0].edificio.startswith("406 -")
    assert s[0].tipo_espacio == "SALON"
    assert s[1].dia == "JUEVES"


def test_dia_con_tilde():
    texto = PAGINA_MINIMA.replace("MARTES de 18:00", "MIÉRCOLES de 18:00")
    s = sia.parsear_texto_detalle(texto, "2015938").grupos[0].sesiones
    assert s[0].dia == "MIÉRCOLES"


def test_varias_actividades_son_grupos_distintos():
    """'Grupo 1' de teórica y 'Grupo 1' de laboratorio NO son el mismo grupo."""
    a = sia.parsear_texto_detalle(PAGINA_DOS_ACTIVIDADES, "2015951")
    assert len(a.grupos) == 2
    assert [g.actividad for g in a.grupos] == ["CLASE TEORICA", "LABORATORIO"]
    assert a.grupos[1].sesiones[0].tipo_espacio == "LABORATORIO"


def test_corta_en_prerrequisitos():
    """Lo que viene tras 'Prerrequisitos' no debe leerse como grupos."""
    a = sia.parsear_texto_detalle(PAGINA_DOS_ACTIVIDADES, "2015951")
    assert all("Física" not in g.grupo for g in a.grupos)


def test_grupos_sin_el_rotulo_contenido_de_la_asignatura():
    """No todas las páginas traen 'Contenido de la asignatura'. Si falta, hay
    que localizar los grupos igual — si no, la materia entera se pierde."""
    a = sia.parsear_texto_detalle(PAGINA_SIN_ANCLA, "2024045")
    assert "Contenido de la asignatura" not in PAGINA_SIN_ANCLA
    assert len(a.grupos) == 1
    assert a.grupos[0].actividad == "CLASE TEORICA"
    assert a.grupos[0].grupo.startswith("(1) 2024045-1")
    assert a.grupos[0].cupos_disponibles == "0"
    assert len(a.grupos[0].sesiones) == 2
    assert a.grupos[0].sesiones[0].salon == "454-207"


def test_la_cabecera_no_se_cuela_como_grupo():
    """El respaldo empieza a leer antes de lo normal; 'Tipología:' y compañía
    no deben terminar convertidas en grupos o sesiones."""
    a = sia.parsear_texto_detalle(PAGINA_SIN_ANCLA, "2024045")
    assert a.tipologia == "DISCIPLINAR OPTATIVA"
    assert all("Tipolog" not in g.grupo for g in a.grupos)
    assert all("Crédito" not in g.grupo for g in a.grupos)


def test_el_ancla_normal_sigue_teniendo_prioridad():
    """Cuando el rótulo está, se usa: es más preciso que la heurística."""
    assert sia._inicio_de_los_grupos(PAGINA_MINIMA) == \
        PAGINA_MINIMA.find("Contenido de la asignatura")


def test_pagina_sin_grupos_de_verdad_sigue_dando_cero():
    """El respaldo no debe inventar grupos donde no los hay."""
    texto = """    Volver
Materia Fantasma (9999999)
Tipología: DISCIPLINAR OPTATIVA
Créditos:3
Facultad: FACULTAD DE INGENIERÍA
No hay grupos programados para este periodo.
"""
    a = sia.parsear_texto_detalle(texto, "9999999")
    assert a.grupos == []
    assert a.nombre == "Materia Fantasma"


# ---------------------------------------------------------------------------
# Cupos: NA no es 0. Es la distinción que sostiene todo el análisis.
# ---------------------------------------------------------------------------

def test_cupos_cero_es_un_dato_real():
    a = sia.parsear_texto_detalle(PAGINA_DOS_ACTIVIDADES, "2015951")
    assert a.grupos[0].cupos_disponibles == "0"


def test_cupos_ausentes_son_na():
    a = sia.parsear_texto_detalle(PAGINA_SIN_CUPOS, "2015999")
    assert a.grupos[0].cupos_disponibles == "NA"


def test_cupos_totales_siguen_sin_publicarse():
    """Si algún día el SIA los expone, este test falla y hay que celebrarlo."""
    a = sia.parsear_texto_detalle(PAGINA_MINIMA, "2015938")
    assert a.grupos[0].cupos_totales == "NA"


# ---------------------------------------------------------------------------
# Helpers puros
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("linea,esperado", [
    ("SALON DE CLASE 406-227. 406-227. 406 - Carlos Cortés. SALON.",
     ("406-227", "406 - Carlos Cortés", "SALON")),
    ("LAB HIDRAULICA. 409-101. 409 - Luis Orduz. LABORATORIO.",
     ("409-101", "409 - Luis Orduz", "LABORATORIO")),
])
def test_parsear_ubicacion(linea, esperado):
    s = sia._parsear_ubicacion(linea)
    assert (s.salon, s.edificio, s.tipo_espacio) == esperado


def test_ubicacion_incompleta_no_explota():
    s = sia._parsear_ubicacion("AULA MOVIL.")
    assert s.salon_nombre == "AULA MOVIL"
    assert s.salon == ""


def test_profesores_separados_por_punto():
    assert sia._parsear_profesores("Paula Solarte. NICOLAS GUTIERREZ.") == \
        "Paula Solarte; NICOLAS GUTIERREZ"


def test_normalizar_profesores_es_clave_de_cruce():
    """Debe dar lo mismo escrito de cualquier forma y en cualquier orden."""
    assert sia.normalizar_profesores("María PÉREZ; juan lopez") == \
        sia.normalizar_profesores("Juan López; MARIA PEREZ")


def test_normalizar_profesores_ignora_no_informado():
    assert sia.normalizar_profesores("No informado") == "NA"
    assert sia.normalizar_profesores("") == "NA"


def test_parse_ts_ordena_bien_offsets_mezclados():
    """El historial mezcla marcas +00:00 (viejas) y -05:00 (nuevas).
    Ordenar por texto las pone al revés; por eso existe _parse_ts."""
    antes = "2026-07-21T04:35+00:00"      # 04:35 UTC
    despues = "2026-07-21T00:10-05:00"    # 05:10 UTC, media hora más tarde
    assert antes > despues                # ordenar por texto: al revés
    assert sia._parse_ts(antes) < sia._parse_ts(despues)   # ordenar bien


def test_parse_ts_tolera_basura():
    assert sia._parse_ts("no es una fecha").year == 1


# ---------------------------------------------------------------------------
# Regresión contra páginas reales del SIA
# ---------------------------------------------------------------------------

def _cargar_textos_reales(limite_archivos=4):
    carpeta = RAIZ / "textos"
    if not carpeta.is_dir():
        return []
    registros = []
    for ruta in sorted(carpeta.glob("*.jsonl.gz"))[-limite_archivos:]:
        try:
            with gzip.open(ruta, "rt", encoding="utf-8") as f:
                for linea in f:
                    linea = linea.strip()
                    if linea:
                        registros.append(json.loads(linea))
        except (OSError, json.JSONDecodeError):
            continue
    return registros


TEXTOS_REALES = _cargar_textos_reales()

sin_textos = pytest.mark.skipif(
    not TEXTOS_REALES, reason="no hay textos/*.jsonl.gz archivados")


@sin_textos
def test_la_mayoria_de_las_paginas_tiene_grupos():
    """El canario. Si el SIA cambia de formato, esto cae en picada antes de
    que se publique un snapshot vacío."""
    total = len(TEXTOS_REALES)
    con_grupos = sum(
        1 for r in TEXTOS_REALES
        if sia.parsear_texto_detalle(r["texto"], r["codigo"]).grupos
    )
    assert con_grupos / total >= 0.70, (
        f"solo {con_grupos}/{total} páginas produjeron grupos; "
        f"el formato del SIA probablemente cambió")


@sin_textos
def test_los_campos_de_las_sesiones_son_validos():
    dias_ok = {"LUNES", "MARTES", "MIÉRCOLES", "MIERCOLES", "JUEVES",
               "VIERNES", "SÁBADO", "SABADO", "DOMINGO"}
    revisadas = 0
    for r in TEXTOS_REALES[:400]:
        a = sia.parsear_texto_detalle(r["texto"], r["codigo"])
        for g in a.grupos:
            assert g.cupos_disponibles == "NA" or g.cupos_disponibles.isdigit()
            for s in g.sesiones:
                assert s.dia in dias_ok, f"día raro: {s.dia!r}"
                assert len(s.hora_inicio) == 5 and s.hora_inicio[2] == ":"
                assert s.hora_inicio < s.hora_fin, \
                    f"{r['codigo']}: {s.hora_inicio}-{s.hora_fin}"
                revisadas += 1
    assert revisadas > 0


@sin_textos
def test_el_parser_es_determinista():
    r = TEXTOS_REALES[0]
    a = sia.parsear_texto_detalle(r["texto"], r["codigo"])
    b = sia.parsear_texto_detalle(r["texto"], r["codigo"])
    assert [g.grupo for g in a.grupos] == [g.grupo for g in b.grupos]
    assert [g.cupos_disponibles for g in a.grupos] == \
           [g.cupos_disponibles for g in b.grupos]
