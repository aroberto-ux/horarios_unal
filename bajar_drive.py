"""
Descarga archivos desde una carpeta de Google Drive usando una cuenta de
servicio. Es el complemento de subir_drive.py: usa las MISMAS credenciales
y el MISMO ID de carpeta, pero para bajar en vez de subir.

Pensado para correr dentro de un GitHub Action: lee las credenciales desde
credenciales_drive.json (que el workflow escribe a partir de un secret) y
el ID de la carpeta desde la variable de entorno SIA_DRIVE_FOLDER_ID.

Instalación:
    pip install google-api-python-client google-auth google-auth-httplib2

Uso:
    python bajar_drive.py catalogo.json horario.html grupos.csv ...

Descarga cada archivo (buscado por NOMBRE dentro de la carpeta) al
directorio actual, sobrescribiendo si ya existe localmente. Si algún
archivo no aparece en Drive, se avisa y se sigue con los demás (no se
detiene el resto de la descarga por uno faltante).
"""

import io
import os
import sys
from pathlib import Path
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

CARPETA_SCRIPT = Path(__file__).resolve().parent
CREDENCIALES = CARPETA_SCRIPT / "credenciales_drive.json"
CARPETA_DRIVE_ID = os.environ.get("SIA_DRIVE_FOLDER_ID", "")

# Basta con permiso de lectura para este script (a diferencia de
# subir_drive.py, que necesita escritura).
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_servicio = None


def _get_servicio():
    global _servicio
    if _servicio is not None:
        return _servicio
    if not CARPETA_DRIVE_ID:
        raise RuntimeError(
            "Falta SIA_DRIVE_FOLDER_ID (variable de entorno con el ID de "
            "la carpeta de Drive)."
        )
    if not CREDENCIALES.exists():
        raise RuntimeError(f"No se encontró {CREDENCIALES}.")
    creds = service_account.Credentials.from_service_account_file(
        str(CREDENCIALES), scopes=SCOPES
    )
    _servicio = build("drive", "v3", credentials=creds)
    return _servicio


def _buscar_id(nombre: str) -> Optional[str]:
    servicio = _get_servicio()
    nombre_escapado = nombre.replace("'", "\\'")
    query = (
        f"name = '{nombre_escapado}' and '{CARPETA_DRIVE_ID}' in parents "
        "and trashed = false"
    )
    resultado = (
        servicio.files()
        .list(q=query, spaces="drive", fields="files(id, name)")
        .execute()
    )
    archivos = resultado.get("files", [])
    return archivos[0]["id"] if archivos else None


def bajar_archivo(nombre: str, destino: Optional[Path] = None) -> bool:
    destino = destino or (Path.cwd() / nombre)
    try:
        servicio = _get_servicio()
        file_id = _buscar_id(nombre)
        if not file_id:
            print(f"  ! {nombre} no existe en la carpeta de Drive, se omite.")
            return False

        request = servicio.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        listo = False
        while not listo:
            _, listo = downloader.next_chunk()

        destino.write_bytes(buffer.getvalue())
        print(f"  -> Descargado: {nombre}")
        return True
    except HttpError as e:
        print(f"  ! Error de la API de Drive bajando {nombre}: {e}")
    except RuntimeError as e:
        print(f"  ! {e}")
    return False


def bajar_varios(nombres) -> bool:
    exito_total = True
    for nombre in nombres:
        if not bajar_archivo(nombre):
            exito_total = False
    return exito_total


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python bajar_drive.py archivo1 [archivo2 ...]")
        raise SystemExit(1)
    ok = bajar_varios(sys.argv[1:])
    raise SystemExit(0 if ok else 1)
