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

Descarga cada archivo (buscado por NOMBRE en la raíz de la carpeta) al
directorio actual, sobrescribiendo si ya existe. Si algún archivo no
aparece, se avisa y se sigue con los demás.

Para descargar TODO el contenido de una subcarpeta (p. ej. "historial/",
que tiene un archivo por día y no se puede listar por nombre de antemano),
usa el token especial @historial:

    python bajar_drive.py catalogo.json horario.html @historial

Eso descarga cada archivo dentro de la subcarpeta "historial" de Drive al
directorio local ./historial/, conservando sus nombres.
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
_MIME_CARPETA = "application/vnd.google-apps.folder"

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


def _buscar_id(nombre: str, carpeta_id: str) -> Optional[str]:
    servicio = _get_servicio()
    nombre_escapado = nombre.replace("'", "\\'")
    query = (
        f"name = '{nombre_escapado}' and '{carpeta_id}' in parents "
        "and trashed = false"
    )
    resultado = (
        servicio.files()
        .list(q=query, spaces="drive", fields="files(id, name)")
        .execute()
    )
    archivos = resultado.get("files", [])
    return archivos[0]["id"] if archivos else None


def _id_subcarpeta(nombre: str) -> Optional[str]:
    servicio = _get_servicio()
    query = (
        f"name = '{nombre}' and '{CARPETA_DRIVE_ID}' in parents "
        f"and mimeType = '{_MIME_CARPETA}' and trashed = false"
    )
    resultado = servicio.files().list(
        q=query, spaces="drive", fields="files(id, name)"
    ).execute()
    archivos = resultado.get("files", [])
    return archivos[0]["id"] if archivos else None


def _descargar_por_id(file_id: str, destino: Path) -> None:
    servicio = _get_servicio()
    request = servicio.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    listo = False
    while not listo:
        _, listo = downloader.next_chunk()
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(buffer.getvalue())


def bajar_archivo(nombre: str, destino: Optional[Path] = None) -> bool:
    """Descarga un archivo suelto de la RAÍZ de la carpeta configurada."""
    destino = destino or (Path.cwd() / nombre)
    try:
        file_id = _buscar_id(nombre, CARPETA_DRIVE_ID)
        if not file_id:
            print(f"  ! {nombre} no existe en la carpeta de Drive, se omite.")
            return False
        _descargar_por_id(file_id, destino)
        print(f"  -> Descargado: {nombre}")
        return True
    except HttpError as e:
        print(f"  ! Error de la API de Drive bajando {nombre}: {e}")
    except RuntimeError as e:
        print(f"  ! {e}")
    return False


def bajar_subcarpeta(nombre: str, destino_dir: Path) -> int:
    """Descarga TODOS los archivos dentro de una subcarpeta de Drive
    (p. ej. 'historial') a un directorio local, conservando sus nombres.
    Usa esto para el historial particionado, donde no se sabe de antemano
    cuántos archivos AAAA-MM-DD.csv existen."""
    try:
        carpeta_id = _id_subcarpeta(nombre)
        if not carpeta_id:
            print(f"  ! La subcarpeta '{nombre}' no existe en Drive todavía, se omite.")
            return 0

        servicio = _get_servicio()
        archivos, token = [], None
        while True:
            resp = servicio.files().list(
                q=f"'{carpeta_id}' in parents and trashed = false",
                spaces="drive", fields="nextPageToken, files(id, name)",
                pageToken=token, pageSize=1000,
            ).execute()
            archivos.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                break

        destino_dir.mkdir(parents=True, exist_ok=True)
        for archivo in archivos:
            _descargar_por_id(archivo["id"], destino_dir / archivo["name"])
        print(f"  -> Descargados {len(archivos)} archivos de la subcarpeta '{nombre}'.")
        return len(archivos)
    except HttpError as e:
        print(f"  ! Error de la API de Drive bajando la subcarpeta '{nombre}': {e}")
    except RuntimeError as e:
        print(f"  ! {e}")
    return 0


def bajar_varios(nombres) -> bool:
    exito_total = True
    for nombre in nombres:
        if not bajar_archivo(nombre):
            exito_total = False
    return exito_total


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python bajar_drive.py archivo1 [archivo2 ...] [@historial]")
        raise SystemExit(1)

    args = sys.argv[1:]
    ok = True

    if "@historial" in args:
        args = [a for a in args if a != "@historial"]
        bajar_subcarpeta("historial", CARPETA_SCRIPT / "historial")

    if args:
        ok = bajar_varios(args)

    raise SystemExit(0 if ok else 1)
