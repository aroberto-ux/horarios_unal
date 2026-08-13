"""
Lectura y escritura del historial de cupos particionado por día.

El historial vive en historial/YYYY-MM-DD.csv (fecha de Bogotá). Un archivo
por día en vez de uno monolítico: git solo reescribe el del día en curso,
así que el coste de un commit deja de crecer con la antigüedad del proyecto.

Este módulo es la ÚNICA puerta al historial. Si mañana quieres pasar a
Parquet o a SQLite, se cambia aquí y ni el scraper ni los generadores de
HTML se enteran.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

CARPETA = Path(__file__).resolve().parent / "historial"
TZ_BOGOTA = timezone(timedelta(hours=-5))  # Colombia: sin horario de verano

CAMPOS = ["run_id", "ts_lectura", "codigo", "actividad", "grupo",
          "profesores_raw", "cupos_disponibles", "orden_lectura"]


def _ruta_del_dia(ts: str) -> Path:
    """La partición a la que pertenece una lectura. Ante un ts ilegible cae
    en el día de hoy: perder la fila sería peor que ponerla un día corrido."""
    try:
        dia = datetime.fromisoformat(ts).astimezone(TZ_BOGOTA).date()
    except (ValueError, TypeError):
        dia = datetime.now(TZ_BOGOTA).date()
    return CARPETA / f"{dia.isoformat()}.csv"


def anexar(filas: list[dict]) -> int:
    """Añade filas al archivo del día correspondiente.

    Las lecturas de un barrido pueden caer a ambos lados de la medianoche
    (el barrido dura minutos), así que se agrupan por destino en vez de
    asumir que todas van al mismo archivo.
    """
    if not filas:
        return 0
    CARPETA.mkdir(exist_ok=True)
    por_ruta: dict[Path, list[dict]] = {}
    for fila in filas:
        por_ruta.setdefault(_ruta_del_dia(fila.get("ts_lectura", "")),
                            []).append(fila)

    n = 0
    for ruta, grupo in por_ruta.items():
        nuevo = not ruta.exists()
        # encoding="utf-8" sin BOM y lineterminator="\n": el BOM rompía a
        # cualquier lector que no pasara encoding="utf-8-sig", y el CRLF
        # ensuciaba los diffs.
        with open(ruta, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore",
                               lineterminator="\n")
            if nuevo:
                w.writeheader()
            w.writerows(grupo)
            n += len(grupo)
    return n


def particiones() -> list[Path]:
    """Archivos del historial, del más viejo al más nuevo."""
    return sorted(CARPETA.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv"))


def leer(desde: str | None = None) -> Iterator[dict]:
    """Recorre el historial entero (o desde una fecha 'YYYY-MM-DD').

    Es un generador: generar_estadisticas() puede procesar meses de datos sin
    cargarlos en memoria, que es justo lo que empezaría a fallar en el runner
    durante inscripciones.
    """
    for ruta in particiones():
        if desde and ruta.stem < desde:
            continue
        with open(ruta, encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)


def resumen() -> str:
    archivos = particiones()
    if not archivos:
        return "historial vacío"
    total = sum(r.stat().st_size for r in archivos)
    return (f"{len(archivos)} días, {total / 1e6:.1f} MB "
            f"({archivos[0].stem} a {archivos[-1].stem})")
