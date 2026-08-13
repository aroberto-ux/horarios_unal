#!/usr/bin/env python3
"""
Migra cupos_historial.csv (un solo archivo de ~15 MB, reescrito entero en
cada commit) a historial/YYYY-MM-DD.csv, un archivo por día.

POR QUÉ
    Git guarda un blob nuevo completo cada vez que un archivo cambia. Con un
    CSV monolítico y 48 commits al día eso son ~800 MB diarios de objetos.
    En la semana de inscripciones, midiendo cada 5 min, serían ~190 GB.
    Particionando, cada commit solo toca el archivo del día en curso.

QUÉ CAMBIA EN EL ESQUEMA (además del particionado)
    - se elimina cupos_totales   -> era "NA" en el 100% de las 104.500 filas
    - se elimina profesores_norm -> es derivable de profesores_raw con
                                    normalizar_profesores(); además ya estaba
                                    desincronizada en 21.286 filas
    - se elimina el BOM          -> la primera columna se llamaba "\ufeffrun_id"
    - se unifica el offset a -05:00 (las filas legacy venían en +00:00, lo que
      rompe el orden lexicográfico al mezclarlas)
    - se deduplica por (run_id, codigo, actividad, grupo): había 11.805 filas
      repetidas dentro de una misma corrida

Uso:
    python migrar_particiones.py            # simulacro, no escribe nada
    python migrar_particiones.py --aplicar  # escribe historial/ y borra el CSV

El original NO se borra sin --aplicar, y aun con --aplicar queda como
cupos_historial_pre_particion.csv hasta que verifiques y lo quites a mano.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ORIGEN = RAIZ / "cupos_historial.csv"
DESTINO = RAIZ / "historial"
RESPALDO = RAIZ / "cupos_historial_pre_particion.csv"

TZ_BOGOTA = timezone(timedelta(hours=-5))  # Colombia no tiene horario de verano

# Esquema v3: v2 menos las dos columnas muertas/derivables.
CAMPOS = ["run_id", "ts_lectura", "codigo", "actividad", "grupo",
          "profesores_raw", "cupos_disponibles", "orden_lectura"]


def parsear_ts(ts: str) -> datetime | None:
    """ISO-8601 con offset -> datetime consciente de zona. None si no parsea."""
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        pass
    # el SIA a veces entrega "...+00:00" sin segundos; fromisoformat de
    # Python <3.11 es quisquilloso con eso
    for formato in ("%Y-%m-%dT%H:%M%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(ts, formato)
        except ValueError:
            continue
    return None


def migrar(aplicar: bool) -> int:
    if not ORIGEN.exists():
        print(f"No existe {ORIGEN.name}; nada que migrar.")
        return 0

    por_dia: dict[str, list[dict]] = defaultdict(list)
    vistos: set[tuple] = set()
    stats = Counter()

    with open(ORIGEN, encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f):
            stats["leidas"] += 1

            momento = parsear_ts(fila.get("ts_lectura") or fila.get("fecha_hora"))
            if momento is None:
                stats["ts_ilegible"] += 1
                continue

            # Todo a hora de Bogotá: un offset único ordena bien como texto y
            # el corte por día coincide con el día real de inscripciones.
            local = momento.astimezone(TZ_BOGOTA)

            clave = (fila.get("run_id", ""), fila.get("codigo", ""),
                     fila.get("actividad", ""), fila.get("grupo", ""))
            if clave in vistos:
                stats["duplicadas"] += 1
                continue
            vistos.add(clave)

            por_dia[local.date().isoformat()].append({
                "run_id": fila.get("run_id", ""),
                "ts_lectura": local.isoformat(timespec="seconds"),
                "codigo": fila.get("codigo", ""),
                "actividad": fila.get("actividad", ""),
                "grupo": fila.get("grupo", ""),
                "profesores_raw": fila.get("profesores_raw", ""),
                "cupos_disponibles": fila.get("cupos_disponibles", ""),
                "orden_lectura": fila.get("orden_lectura", ""),
            })
            stats["escritas"] += 1

    print(f"  leídas      {stats['leidas']:>7}")
    print(f"  duplicadas  {stats['duplicadas']:>7}  (descartadas)")
    print(f"  ts ilegible {stats['ts_ilegible']:>7}  (descartadas)")
    print(f"  escritas    {stats['escritas']:>7}  en {len(por_dia)} archivos")

    if not aplicar:
        mayor = max(por_dia, key=lambda d: len(por_dia[d]))
        print(f"\n  simulacro: no se escribió nada.")
        print(f"  día más pesado: {mayor} con {len(por_dia[mayor])} filas "
              f"(~{len(por_dia[mayor]) * 120 / 1e6:.1f} MB)")
        print("  vuelve a correr con --aplicar para escribir.")
        return 0

    DESTINO.mkdir(exist_ok=True)
    for dia, filas in sorted(por_dia.items()):
        # newline="" y lineterminator="\n": sin CRLF, que es lo que ensucia
        # los diffs cuando el runner es Linux y tú abres el archivo en Windows.
        with open(DESTINO / f"{dia}.csv", "w", encoding="utf-8",
                  newline="") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS, lineterminator="\n")
            w.writeheader()
            w.writerows(sorted(filas, key=lambda r: (r["ts_lectura"],
                                                     r["codigo"], r["grupo"])))

    ORIGEN.replace(RESPALDO)
    print(f"\n  -> {len(por_dia)} archivos en {DESTINO.name}/")
    print(f"  -> original respaldado como {RESPALDO.name}")
    print("     verifícalo, y cuando estés tranquilo: git rm "
          f"{RESPALDO.name} cupos_historial_v1.csv")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--aplicar", action="store_true",
                   help="escribir de verdad (por defecto solo simula)")
    sys.exit(migrar(p.parse_args().aplicar))
