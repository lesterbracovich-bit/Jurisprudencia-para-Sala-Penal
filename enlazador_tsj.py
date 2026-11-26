#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Enlazador Oficial de Sentencias del TSJ (Sin API Keys de IA)
============================================================
Recorre los registros almacenados en `jurisprudencia_ia.db` que aún no han
sido revisados, consulta la API pública oficial del Tribunal Supremo de
Justicia (`www.tsj.gob.ve`) por fecha y sala, verifica el enlace HTML en
`historico.tsj.gob.ve` y guarda el URL oficial en la columna `enlace`.

Características clave:
- No requiere ninguna API key externa (ni Gemini, ni OpenRouter, ni NVIDIA).
- Idempotente por defecto: omite cualquier sentencia cuyo `enlace` ya esté
  guardado o ya haya sido marcada como revisada (`ENCONTRADO`, `NO_ENCONTRADO`,
  `SIN_FECHA`). Usa `--reintentar-no-encontrados` si deseas volver a consultar
  las marcadas como `NO_ENCONTRADO`.
- Si falla la red contra el TSJ, deja esa sentencia sin marcar para reintentar
  automáticamente en la siguiente ejecución manual.
"""

from collections import defaultdict
import os
import re
import sqlite3
import sys
import time
import unicodedata

import requests
import urllib3

from extractor_web import (
    DB_PATH as DEFAULT_DB_PATH,
    HEADERS,
    asegurar_columnas_enlace,
    export_csv,
    init_db,
    listar_sentencias_por_fecha,
    parse_fecha,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Mapeo oficial verificado contra https://www.tsj.gob.ve/es/decisiones
SALAS_TSJ = [
    ("001", "tplen", "Sala Plena"),
    ("002", "scc", "Sala de Casación Civil"),
    ("003", "scp", "Sala de Casación Penal"),
    ("004", "scs", "Sala de Casación Social"),
    ("005", "scon", "Sala Constitucional"),
    ("006", "selec", "Sala Electoral"),
    ("007", "spa", "Sala Político-Administrativa"),
]
SALA_NOMBRES = {sid: nombre for sid, _, nombre in SALAS_TSJ}
FALLBACK_SALA_IDS = ("005", "003", "001", "002", "004", "006", "007")


def normalizar_texto_basico(texto):
    """Normaliza tildes, mayúsculas y espacios para comparar nombres de sala."""
    limpio = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("utf-8")
    return re.sub(r"\s+", " ", limpio).strip().lower()


def resolver_sala_id(nombre_sala):
    """Traduce el nombre de sala en BD al código Liferay (`001`..`007`)."""
    s = normalizar_texto_basico(nombre_sala)
    if not s:
        return None
    if "plena" in s or "pleno" in s:
        return "001"
    if "civil" in s:
        return "002"
    if "penal" in s:
        return "003"
    if "social" in s:
        return "004"
    if "constitucional" in s:
        return "005"
    if "electoral" in s:
        return "006"
    if "politico" in s or "administrativa" in s:
        return "007"
    return None


def normalizar_numero(numero):
    """Normaliza ceros a la izquierda para emparejar números de sentencia (ej. 0593 -> 593)."""
    raw = str(numero or "").strip().lower()
    if not raw:
        return ""
    if raw.isdigit():
        return raw.lstrip("0") or "0"
    return re.sub(r"(^|[-/])0+(\d+)", r"\1\2", raw)


def normalizar_fecha_tsj(fecha_str):
    """Convierte la fecha guardada en BD a DD/MM/AAAA o retorna None si no tiene fecha válida."""
    raw = str(fecha_str or "").strip()
    if not raw or raw.lower() in ("s/f", "sin fecha", "none", "null"):
        return None
    dt = parse_fecha(raw)
    if dt is not None:
        return dt.strftime("%d/%m/%Y")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return None


def _listar_con_reintento(session, cache_listas, fecha_tsj, sala_id, max_intentos=2):
    """Consulta y cachea el listado del TSJ para (fecha, sala_id)."""
    key = (fecha_tsj, sala_id)
    if key in cache_listas:
        return cache_listas[key]

    resultado = ([], False)
    for intento in range(1, max_intentos + 1):
        sents, api_ok = listar_sentencias_por_fecha(session, fecha_tsj, sala_id, return_ok=True)
        resultado = (sents, api_ok)
        if api_ok:
            break
        if intento < max_intentos:
            time.sleep(1.5)

    if resultado[1]:
        cache_listas[key] = resultado
    return resultado


def verificar_url_tsj(session, cache_urls, url):
    """
    Comprueba si el HTML oficial en historico.tsj.gob.ve responde 200 OK.
    Retorna: 'OK', 'NOT_FOUND' (404/410) o 'NET_ERROR' (error transitorio).
    """
    if url in cache_urls:
        return cache_urls[url]

    try:
        resp = session.get(
            url,
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Accept": "text/html,application/xhtml+xml,*/*",
            },
            timeout=(10, 20),
            stream=True,
        )
        try:
            code = resp.status_code
        finally:
            resp.close()

        if 200 <= code < 400:
            estado = "OK"
        elif code in (404, 410):
            estado = "NOT_FOUND"
        else:
            return "NET_ERROR"

        cache_urls[url] = estado
        return estado
    except requests.RequestException:
        return "NET_ERROR"


def _coincide_sentencia(item, num_norm):
    """Comprueba si un item de la API del TSJ corresponde al número buscado."""
    cand_norm = normalizar_numero(item.get("numero", ""))
    if cand_norm and cand_norm == num_norm:
        return True
    archivo = str(item.get("archivo", ""))
    if archivo:
        m = re.match(r"^(?:\d+-)?([A-Za-z]?-?\d+)-\d{4,6}-", archivo)
        if m and normalizar_numero(m.group(1)) == num_norm:
            return True
    return False


def _sincronizar_enlaces_locales(conn):
    """
    Copia el enlace entre extractos de la misma sentencia dentro de la BD
    para evitar consultas redundantes al portal TSJ.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT numero, sala, COALESCE(fecha, ''), enlace "
        "FROM sentencias WHERE COALESCE(TRIM(enlace), '') != ''"
    )
    mapa = {}
    for num, sala, fecha, enlace in cur.fetchall():
        key = (normalizar_numero(num), normalizar_texto_basico(sala), normalizar_fecha_tsj(fecha) or str(fecha or "").strip())
        mapa.setdefault(key, enlace.strip())

    if not mapa:
        return 0

    cur.execute(
        "SELECT id, numero, sala, COALESCE(fecha, '') "
        "FROM sentencias WHERE COALESCE(TRIM(enlace), '') = ''"
    )
    actualizados = 0
    for row_id, num, sala, fecha in cur.fetchall():
        key = (normalizar_numero(num), normalizar_texto_basico(sala), normalizar_fecha_tsj(fecha) or str(fecha or "").strip())
        enlace = mapa.get(key)
        if enlace:
            cur.execute(
                "UPDATE sentencias SET enlace = ?, enlace_estado = 'ENCONTRADO' WHERE id = ?",
                (enlace, row_id),
            )
            actualizados += 1
    if actualizados:
        conn.commit()
    return actualizados


def _buscar_en_sala(session, cache_listas, cache_urls, fecha_tsj, sala_id, num_norm):
    """Busca una sentencia en una sala específica y valida el enlace HTML."""
    sents, api_ok = _listar_con_reintento(session, cache_listas, fecha_tsj, sala_id)
    if not api_ok:
        return None, False

    coincidencias = [item for item in sents if _coincide_sentencia(item, num_norm)]
    if not coincidencias:
        return "", True

    vio_error_red = False
    for item in coincidencias:
        url = item.get("url", "")
        if not url:
            continue
        estado_url = verificar_url_tsj(session, cache_urls, url)
        if estado_url == "OK":
            return url, True
        if estado_url == "NET_ERROR":
            vio_error_red = True

    if vio_error_red:
        return None, False
    return "", True


# Correcciones y eliminaciones puntuales idempotentes solicitadas sobre registros históricos
_ELIMINACIONES_SENTENCIAS = (
    ("1309", "Sala Constitucional", "17/06/2005"),
    ("480", "Sala de Casación Penal", "17/11/2024"),
    ("275", "Sala de Casación Penal", "19/07/2018"),
)

_CORRECCIONES_SENTENCIAS = (
    # (num_actual, sala, fecha_actual) -> (num_nuevo, fecha_nueva)
    ("252", "Sala de Casación Penal", "03/07/2016", "252", "04/07/2016"),
    ("353", "Sala de Casación Penal", "12/11/2014", "353", "13/11/2014"),
    ("257", "Sala de Casación Penal", "14/07/2020", "24", "03/07/2020"),
    ("120", "Sala Constitucional", "25/04/2000", "241", "25/04/2000"),
    ("165", "Sala de Casación Penal", "27/04/2009", "165", "28/04/2009"),
)


def aplicar_correcciones_puntuales(conn):
    """Aplica de forma idempotente eliminaciones y correcciones de número/fecha."""
    cur = conn.cursor()
    eliminadas = 0
    corregidas = 0

    for num, sala, fecha in _ELIMINACIONES_SENTENCIAS:
        cur.execute(
            "DELETE FROM sentencias WHERE numero = ? AND sala = ? AND fecha = ?",
            (num, sala, fecha),
        )
        eliminadas += cur.rowcount

    for num_old, sala, fecha_old, num_new, fecha_new in _CORRECCIONES_SENTENCIAS:
        cur.execute(
            "UPDATE sentencias "
            "SET numero = ?, fecha = ?, enlace = '', enlace_estado = '' "
            "WHERE numero = ? AND sala = ? AND fecha = ?",
            (num_new, fecha_new, num_old, sala, fecha_old),
        )
        corregidas += cur.rowcount

    if eliminadas or corregidas:
        conn.commit()
        print(f"🧹 Correcciones puntuales aplicadas: {corregidas} corregida(s), {eliminadas} eliminada(s).")
    return corregidas, eliminadas


def enlazar_sentencias_pendientes(conn, session, reintentar_no_encontrados=False):
    """
    Busca enlaces oficiales del TSJ solo para las sentencias pendientes de revisión.
    Retorna dict con estadísticas de la ejecución.
    """
    asegurar_columnas_enlace(conn)
    aplicar_correcciones_puntuales(conn)
    copiados_local = _sincronizar_enlaces_locales(conn)

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sentencias")
    total_bd = cur.fetchone()[0]

    estados_omitir = ("ENCONTRADO", "SIN_FECHA") if reintentar_no_encontrados else ("ENCONTRADO", "NO_ENCONTRADO", "SIN_FECHA")
    placeholders = ",".join("?" for _ in estados_omitir)
    cur.execute(
        f"""
        SELECT id, numero, sala, COALESCE(fecha, '')
        FROM sentencias
        WHERE COALESCE(TRIM(enlace), '') = ''
          AND COALESCE(UPPER(TRIM(enlace_estado)), '') NOT IN ({placeholders})
        ORDER BY id
        """,
        estados_omitir,
    )
    filas_pendientes = cur.fetchall()
    omitidas = total_bd - len(filas_pendientes)

    print(f"\n📋 Registros totales en BD:        {total_bd}")
    if copiados_local:
        print(f"🔗 Enlaces replicados internamente: {copiados_local}")
    print(f"⏭️  Ya revisados previamente:       {omitidas}")
    print(f"🔎 Extractos pendientes a revisar: {len(filas_pendientes)}")

    stats = {
        "total_bd": total_bd,
        "omitidas": omitidas,
        "pendientes": len(filas_pendientes),
        "encontradas": 0,
        "no_encontradas": 0,
        "sin_fecha": 0,
        "error_red": 0,
        "filas_actualizadas": copiados_local,
    }

    if not filas_pendientes:
        print("\n✨ No hay sentencias nuevas pendientes por enlazar.")
        return stats

    # Agrupar por sentencia única (número normalizado, sala, fecha) para no repetir trabajo
    grupos_por_fecha = defaultdict(lambda: defaultdict(list))
    sin_fecha_ids = []

    for row_id, num, sala, fecha_raw in filas_pendientes:
        fecha_tsj = normalizar_fecha_tsj(fecha_raw)
        num_norm = normalizar_numero(num)
        if not fecha_tsj or not num_norm:
            sin_fecha_ids.append((row_id, num, sala, fecha_raw))
            continue
        grupos_por_fecha[(fecha_tsj, sala)][num_norm].append((row_id, str(num).strip()))

    if sin_fecha_ids:
        cur.executemany(
            "UPDATE sentencias SET enlace_estado = 'SIN_FECHA' WHERE id = ?",
            [(r[0],) for r in sin_fecha_ids],
        )
        conn.commit()
        stats["sin_fecha"] += len(sin_fecha_ids)
        stats["filas_actualizadas"] += len(sin_fecha_ids)
        print(f"\n⚠️  Marcados como SIN_FECHA (se omitirán a futuro): {len(sin_fecha_ids)} registro(s)")

    cache_listas = {}
    cache_urls = {}
    total_lotes = len(grupos_por_fecha)

    for idx_lote, ((fecha_tsj, sala_nombre), nums_dict) in enumerate(sorted(grupos_por_fecha.items()), 1):
        primary_sid = resolver_sala_id(sala_nombre)
        print(f"\n📅 [{idx_lote}/{total_lotes}] {fecha_tsj} · {sala_nombre} ({len(nums_dict)} sentencia(s))")

        for num_norm, items in sorted(nums_dict.items()):
            row_ids = [it[0] for it in items]
            num_orig = items[0][1]
            url_encontrada = None
            estado_final = None

            if primary_sid:
                res_url, ok_api = _buscar_en_sala(session, cache_listas, cache_urls, fecha_tsj, primary_sid, num_norm)
                if not ok_api:
                    stats["error_red"] += 1
                    print(f"   ⚠ Sent. N° {num_orig}: error de red/timeout TSJ (queda pendiente).")
                    continue
                if res_url:
                    url_encontrada = res_url
                    estado_final = "ENCONTRADO"

            # Si no apareció en la sala declarada, probar en las demás salas solo si es unívoca
            if not url_encontrada:
                candidatas = []
                hubo_fallo_red = False
                for alt_sid in FALLBACK_SALA_IDS:
                    if alt_sid == primary_sid:
                        continue
                    alt_url, ok_alt = _buscar_en_sala(session, cache_listas, cache_urls, fecha_tsj, alt_sid, num_norm)
                    if not ok_alt:
                        hubo_fallo_red = True
                    elif alt_url:
                        candidatas.append((alt_sid, alt_url))

                if len(candidatas) == 1:
                    alt_sid, alt_url = candidatas[0]
                    url_encontrada = alt_url
                    estado_final = "ENCONTRADO"
                    print(f"   🔗 Sent. N° {num_orig}: hallada en {SALA_NOMBRES.get(alt_sid, alt_sid)} → {url_encontrada}")
                elif hubo_fallo_red:
                    stats["error_red"] += 1
                    print(f"   ⚠ Sent. N° {num_orig}: error de red consultando salas (queda pendiente).")
                    continue
                else:
                    estado_final = "NO_ENCONTRADO"
                    if len(candidatas) > 1:
                        print(f"   ⚠ Sent. N° {num_orig}: coincidencia ambigua en varias salas → NO_ENCONTRADO")
                    else:
                        print(f"   ⏭️  Sent. N° {num_orig}: no publicada/404 en TSJ → NO_ENCONTRADO")
            else:
                print(f"   ✔ Sent. N° {num_orig} → {url_encontrada}")

            ph_ids = ",".join("?" for _ in row_ids)
            if estado_final == "ENCONTRADO":
                cur.execute(
                    f"UPDATE sentencias SET enlace = ?, enlace_estado = 'ENCONTRADO' WHERE id IN ({ph_ids})",
                    [url_encontrada, *row_ids],
                )
                stats["encontradas"] += 1
            else:
                cur.execute(
                    f"UPDATE sentencias SET enlace_estado = 'NO_ENCONTRADO' WHERE id IN ({ph_ids})",
                    row_ids,
                )
                stats["no_encontradas"] += 1
            stats["filas_actualizadas"] += len(row_ids)

        conn.commit()
        time.sleep(0.2)

    return stats


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    reintentar_no_encontrados = "--reintentar-no-encontrados" in args

    print("=" * 60)
    print("  ENLAZADOR OFICIAL DE SENTENCIAS TSJ")
    print("  API Pública TSJ (Sin API keys de IA)")
    print("=" * 60)
    if reintentar_no_encontrados:
        print("🔄 Modo: Reintentando también sentencias marcadas como NO_ENCONTRADO.")

    conn = init_db()
    session = requests.Session()
    session.verify = False

    try:
        stats = enlazar_sentencias_pendientes(
            conn,
            session,
            reintentar_no_encontrados=reintentar_no_encontrados,
        )
        export_csv(conn)
    finally:
        conn.close()

    print("\n" + "=" * 60)
    print("  RESUMEN ENLAZADOR TSJ")
    print("=" * 60)
    print(f"  ⏭️  Registros omitidos (ya revisados): {stats['omitidas']}")
    print(f"  ✅ Sentencias enlazadas:               {stats['encontradas']}")
    print(f"  ➖ Marcadas NO_ENCONTRADO:             {stats['no_encontradas']}")
    print(f"  ⚠️  Marcadas SIN_FECHA:                {stats['sin_fecha']}")
    print(f"  🔁 Pendientes por error de red TSJ:    {stats['error_red']}")
    print(f"  🗄  Base de datos:                     {DEFAULT_DB_PATH}")
    print("\n✅ Proceso finalizado.")


if __name__ == "__main__":
    main()
