#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Sincronización Automática del CSV con Google Drive (vía Google Apps Script)
===========================================================================
Envía `reporte_jurisprudencia.csv` por HTTPS a un Web App de Google Apps Script
para crear o sobrescribir siempre el mismo archivo en Google Drive.

Variables de entorno (en GitHub Secrets o archivo `.env` local):
  - DRIVE_WEBHOOK_URL   : URL del despliegue Web App de Apps Script (termina en /exec)
  - DRIVE_WEBHOOK_TOKEN : Token secreto definido en el Apps Script
  - DRIVE_FILENAME      : (Opcional) Nombre del archivo en Drive (por defecto el nombre del CSV)
"""

import json
import os
import sys

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV_PATH = os.path.join(BASE_DIR, "reporte_jurisprudencia.csv")
DEFAULT_ENV_FILES = (".env", "drive.env")


def _parse_env_file(path):
    """Lee pares CLAVE=VALOR simples desde un archivo .env sin dependencias externas."""
    values = {}
    if not os.path.isfile(path):
        return values
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                if key:
                    values[key] = val
    except OSError:
        pass
    return values


def _get_config_value(key, default=""):
    """Obtiene una variable de entorno o, si falta, la busca en .env / drive.env."""
    env_val = os.environ.get(key, "").strip()
    if env_val:
        return env_val
    for fname in DEFAULT_ENV_FILES:
        parsed = _parse_env_file(os.path.join(BASE_DIR, fname))
        if parsed.get(key, "").strip():
            return parsed[key].strip()
    return default


def sync_csv_to_drive(csv_path=DEFAULT_CSV_PATH, timeout=90, quiet_if_unconfigured=False):
    """
    Sube o sobrescribe el archivo CSV en Google Drive mediante el webhook de Apps Script.
    Nunca lanza excepciones hacia el llamador: devuelve True si tuvo éxito o False si falló/se omitió.
    """
    webhook_url = _get_config_value("DRIVE_WEBHOOK_URL")
    webhook_token = _get_config_value("DRIVE_WEBHOOK_TOKEN")

    if not webhook_url and not webhook_token:
        if not quiet_if_unconfigured:
            print("ℹ️  Google Drive: sincronización omitida (faltan DRIVE_WEBHOOK_URL o DRIVE_WEBHOOK_TOKEN).")
        return False

    if not webhook_url or not webhook_token:
        faltante = "DRIVE_WEBHOOK_TOKEN" if webhook_url else "DRIVE_WEBHOOK_URL"
        print(f"⚠️  Google Drive: falta configurar {faltante}; sincronización omitida.")
        return False

    if not os.path.isfile(csv_path):
        print(f"⚠️  Google Drive: no se encontró el archivo CSV para subir: {csv_path}")
        return False

    drive_filename = _get_config_value("DRIVE_FILENAME") or os.path.basename(csv_path)

    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            csv_text = f.read()
    except Exception as exc:
        print(f"⚠️  Google Drive: no se pudo leer el CSV ({exc}).")
        return False

    payload = json.dumps(
        {
            "token": webhook_token,
            "filename": drive_filename,
            "csv_text": csv_text,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    try:
        resp = requests.post(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        print(f"⚠️  Google Drive: error de red al enviar el CSV ({exc}).")
        return False

    try:
        data = resp.json()
    except ValueError:
        print(
            f"⚠️  Google Drive: respuesta inesperada (HTTP {resp.status_code}, no es JSON). "
            "Verifica que DRIVE_WEBHOOK_URL termine en '/exec' y que el acceso sea 'Cualquier persona'."
        )
        return False

    if resp.status_code >= 400 or not data.get("ok"):
        err_msg = data.get("error") or f"HTTP {resp.status_code}"
        print(f"⚠️  Google Drive: el webhook rechazó la actualización ({err_msg}).")
        return False

    accion = "actualizado" if data.get("action") == "updated" else "creado"
    file_url = data.get("url", "")
    size_kb = len(payload) / 1024.0
    if file_url:
        print(f"☁️  Google Drive: CSV {accion} ({size_kb:.1f} KB) → {file_url}")
    else:
        print(f"☁️  Google Drive: CSV {accion} correctamente ({size_kb:.1f} KB).")
    return True


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV_PATH
    ok = sync_csv_to_drive(target, quiet_if_unconfigured=False)
    sys.exit(0 if ok else 1)
