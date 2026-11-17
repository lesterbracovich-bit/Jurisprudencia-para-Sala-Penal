#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Extractor Automático Web del TSJ - Exclusivo para Sentencias de Nulidad Penal
=============================================================================
Se ejecuta desde el Self-Hosted Runner local en Venezuela.
"""

import csv
import difflib
import functools
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
import urllib3
from bs4 import BeautifulSoup
from google import genai
from playwright.sync_api import sync_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
print = functools.partial(print, flush=True)

# ─── Configuración General ────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "jurisprudencia_ia.db")
CSV_PATH = os.path.join(BASE_DIR, "reporte_jurisprudencia.csv")

TZ_VENEZUELA = ZoneInfo("America/Caracas")
FECHA_INICIO_INICIAL = date(2026, 9, 9)

MAX_MAXIMAS_POR_SENTENCIA = 3
FILTRAR_SOLO_CON_INDICADORES_NULIDAD = True
MAX_CHARS_SENTENCIA = 45_000

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

PREFERRED_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-4-hydrogen",
]

MESES_NOMBRE_A_NUM = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

SALAS_OBJETIVO = [
    {
        "nombre_bd": "Sala de Casación Penal",
        "menu_texto": "Penal",
        "codigo": "scp",
        "url": "https://www.tsj.gob.ve/es/web/tsj/decisiones#5",
    },
    {
        "nombre_bd": "Sala Constitucional",
        "menu_texto": "Constitucional",
        "codigo": "scon",
        "url": "https://www.tsj.gob.ve/es/web/tsj/decisiones#1",
    },
]

KEYWORDS_MATERIA_PENAL = [
    "penal",
    "código orgánico procesal penal",
    "codigo organico procesal penal",
    "copp",
    "ministerio público",
    "ministerio publico",
    "fiscalía",
    "fiscalia",
    "imputad",
    "acusad",
    "delito",
    "privación judicial preventiva de libertad",
    "privacion judicial preventiva de libertad",
    "audiencia preliminar",
    "juicio oral",
    "tribunal de control",
    "tribunal de juicio",
    "tribunal de ejecución",
    "corte de apelaciones",
    "hábeas corpus",
    "habeas corpus",
    "sobreseimiento",
    "casación penal",
]

KEYWORDS_NULIDAD = [
    "nulidad",
    "nulo",
    "nula",
    "anula",
    "anulación",
    "anulacion",
    "nulidad de oficio",
    "nulidad absoluta",
    "repone la causa",
    "reponer la causa",
    "reposición de la causa",
    "reposicion de la causa",
    "declara con lugar",
    "inmotivación",
    "inmotivacion",
    "incongruencia omisiva",
    "artículo 174",
    "artículo 175",
    "artículo 179",
    "artículo 180",
]


def init_client():
    if not API_KEY:
        sys.exit(1)
    return genai.Client(api_key=API_KEY)


def select_model(client):
    for model_name in PREFERRED_MODELS:
        try:
            response = client.models.generate_content(model=model_name, contents="Responde solo: OK")
            if response and response.text:
                return model_name, model_name
        except Exception:
            continue
    return PREFERRED_MODELS[0], PREFERRED_MODELS[0]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sentencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT NOT NULL,
            sala TEXT NOT NULL,
            fecha TEXT,
            tema TEXT,
            extracto TEXT,
            contenido_citado TEXT,
            ratificaciones TEXT,
            archivo_origen TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sentencias_num_sala ON sentencias(numero, sala)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS estado_bloques (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archivo TEXT NOT NULL,
            bloque_idx INTEGER NOT NULL,
            total_bloques INTEGER NOT NULL,
            estado TEXT NOT NULL,
            sentencias_detectadas INTEGER DEFAULT 0,
            error_detalle TEXT,
            fecha_actualizacion TEXT,
            UNIQUE(archivo, bloque_idx)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS control_fechas_web (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ultima_fecha_revisada TEXT NOT NULL,
            fecha_actualizacion TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def obtener_rango_fechas(conn):
    hoy_ve = datetime.now(TZ_VENEZUELA).date()
    cursor = conn.cursor()
    cursor.execute("SELECT ultima_fecha_revisada FROM control_fechas_web WHERE id = 1")
    row = cursor.fetchone()
    if not row or not row[0]:
        fecha_inicio = FECHA_INICIO_INICIAL
    else:
        ultima = datetime.strptime(row[0], "%Y-%m-%d").date()
        fecha_inicio = ultima + timedelta(days=1)
    return fecha_inicio, hoy_ve


def actualizar_ultima_fecha_revisada(conn, fecha_obj):
    ahora_str = datetime.now(TZ_VENEZUELA).strftime("%Y-%m-%d %H:%M:%S")
    fecha_str = fecha_obj.strftime("%Y-%m-%d")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO control_fechas_web (id, ultima_fecha_revisada, fecha_actualizacion)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ultima_fecha_revisada = excluded.ultima_fecha_revisada,
            fecha_actualizacion = excluded.fecha_actualizacion
    """, (fecha_str, ahora_str))
    conn.commit()


def url_ya_procesada(conn, identificador_origen):
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM estado_bloques WHERE archivo = ? AND estado = 'EXITOSO' LIMIT 1", (identificador_origen,))
    return cursor.fetchone() is not None


def guardar_estado_bloque(conn, archivo, estado, sentencias_detectadas=0, error_detalle=""):
    ahora_str = datetime.now(TZ_VENEZUELA).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO estado_bloques (archivo, bloque_idx, total_bloques, estado, sentencias_detectadas, error_detalle, fecha_actualizacion)
        VALUES (?, 1, 1, ?, ?, ?, ?)
        ON CONFLICT(archivo, bloque_idx) DO UPDATE SET
            estado = excluded.estado,
            sentencias_detectadas = excluded.sentencias_detectadas,
            error_detalle = excluded.error_detalle,
            fecha_actualizacion = excluded.fecha_actualizacion
    """, (archivo, estado, sentencias_detectadas, error_detalle, ahora_str))
    conn.commit()


def limpiar_y_validar_registro(s):
    if not isinstance(s, dict):
        return None
    raw_numero = str(s.get("numero", "")).replace(".", "").strip()
    num_limpio = raw_numero.lstrip("0") or "0"
    if not num_limpio.isdigit() or num_limpio == "0":
        return None
    sala = str(s.get("sala", "")).strip()
    if sala not in {"Sala de Casación Penal", "Sala Constitucional"}:
        return None
    contenido_citado = str(s.get("contenido_citado", "")).strip().strip('"“”«»\'')
    if len(contenido_citado) < 150:
        return None
    return {
        "numero": num_limpio,
        "sala": sala,
        "fecha": str(s.get("fecha", "")).strip(),
        "tema": str(s.get("tema", "")).strip(),
        "extracto": str(s.get("extracto", "")).strip(),
        "contenido_citado": contenido_citado,
        "ratificaciones": str(s.get("ratificaciones", "") or "").strip(),
    }


def insert_sentencias(conn, sentencias, archivo_origen):
    cursor = conn.cursor()
    inserted = 0
    updated = 0
    duplicated = 0
    for s in sentencias[:MAX_MAXIMAS_POR_SENTENCIA]:
        reg = limpiar_y_validar_registro(s)
        if not reg:
            continue
        cursor.execute(
            "SELECT id FROM sentencias WHERE numero = ? AND sala = ?",
            (reg["numero"], reg["sala"]),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                """
                INSERT INTO sentencias (numero, sala, fecha, tema, extracto, contenido_citado, ratificaciones, archivo_origen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (reg["numero"], reg["sala"], reg["fecha"], reg["tema"], reg["extracto"], reg["contenido_citado"], reg["ratificaciones"], archivo_origen),
            )
            inserted += 1
        else:
            duplicated += 1
    conn.commit()
    return inserted, updated, duplicated


def descargar_texto_sentencia(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0"}
    for _ in range(2):
        try:
            resp = requests.get(url, headers=headers, verify=False, timeout=25)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()
                return "\n".join([re.sub(r"\s+", " ", ln).strip() for ln in soup.get_text(separator="\n").splitlines() if ln.strip()])
        except Exception:
            time.sleep(2)
    return ""


def extraer_metadatos_cabecera(texto, url, sala_nombre, fecha_obj):
    numero = ""
    m_url = re.search(r"/\d+-([A-Za-z.]*0*\d+)-\d{5,6}-\d{4}", url)
    if m_url:
        numero = re.sub(r"\D", "", m_url.group(1)).lstrip("0")
    return {"numero": numero, "sala": sala_nombre, "fecha": fecha_obj.strftime("%d/%m/%Y")}


def evaluar_pertinencia_y_nulidad(texto_completo, resumen_portal, sala_nombre):
    texto_busqueda = f"{resumen_portal}\n{texto_completo}".lower()
    if sala_nombre == "Sala Constitucional" and sum(1 for kw in KEYWORDS_MATERIA_PENAL if kw in texto_busqueda) < 2:
        return False, False, 0
    tiene_nulidad = any(kw in texto_busqueda for kw in KEYWORDS_NULIDAD)
    return True, tiene_nulidad, 1


def recortar_seccion_relevante(texto):
    if len(texto) <= MAX_CHARS_SENTENCIA:
        return texto
    return texto[:3500] + "\n\n[...]\n\n" + texto[-(MAX_CHARS_SENTENCIA - 4000):]


def cargar_pagina_tsj_con_reintentos(page, url, menu_texto):
    for intento in range(1, 4):
        try:
            print(f"    🔄 Cargando {url} (intento {intento}/3)...")
            page.goto(url, timeout=60000, wait_until="commit")
            # Esperar a que cargue el DOM general sin exigir un texto estricto
            page.wait_for_load_state("domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)

            # Intentar hacer clic en la pestaña de la sala si existe
            enlace_sala = page.locator(f"a:text-is('{menu_texto}')").first
            if enlace_sala.count() > 0:
                enlace_sala.click(timeout=10000)
                page.wait_for_timeout(3000)

            return True
        except Exception as e:
            print(f"    ⚠ Intento {intento} demorado o reintentando: {str(e)[:80]}")
            time.sleep(4)
    return False


def obtener_dias_publicados_en_vista(page, anio_objetivo):
    datos_dom = page.evaluate("""() => {
        const mesesValid = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'setiembre', 'octubre', 'noviembre', 'diciembre'];
        const hallados = [];
        const candidatos = Array.from(document.querySelectorAll('tr, div, li'));
        for (const cont of candidatos) {
            const textoDirecto = (cont.innerText || '').trim();
            if (!textoDirecto || textoDirecto.length > 250) continue;
            const lineas = textoDirecto.split(/\\n+/).map(s => s.trim()).filter(Boolean);
            if (lineas.length < 2) continue;
            const posibleMes = lineas[0].toLowerCase();
            if (!mesesValid.includes(posibleMes)) continue;
            const dias = [];
            for (let i = 1; i < lineas.length; i++) {
                if (/^(0?[1-9]|[12][0-9]|3[01])$/.test(lineas[i])) dias.push(lineas[i]);
            }
            if (dias.length > 0) hallados.push({ mes_lower: posibleMes, dias: dias });
        }
        return hallados;
    }""")

    resultado = []
    vistos = set()
    for item in datos_dom:
        mes_num = MESES_NOMBRE_A_NUM.get(item["mes_lower"])
        if not mes_num:
            continue
        for d_str in item["dias"]:
            try:
                f_obj = date(anio_objetivo, mes_num, int(d_str))
                if f_obj not in vistos:
                    vistos.add(f_obj)
                    resultado.append((f_obj, item["mes_lower"], d_str))
            except ValueError:
                continue
    return sorted(resultado, key=lambda x: x[0])


def buscar_decisiones_en_portal_tsj(fecha_inicio, fecha_fin):
    resultados_por_fecha = {}
    anios_necesarios = sorted({y for y in range(fecha_inicio.year, fecha_fin.year + 1)})
    portal_accesible = False

    print("\n🌐 Iniciando navegador local en segundo plano para consultar el TSJ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        page = context.new_page()

        for sala_info in SALAS_OBJETIVO:
            sala_nombre = sala_info["nombre_bd"]
            menu_texto = sala_info["menu_texto"]
            codigo_sala = sala_info["codigo"]
            url_sala = sala_info["url"]

            print(f"\n🏛️  Consultando {sala_nombre} ({url_sala})...")
            if not cargar_pagina_tsj_con_reintentos(page, url_sala, menu_texto):
                print(f"    ❌ No fue posible conectar con {sala_nombre}.")
                continue

            portal_accesible = True

            for anio in anios_necesarios:
                try:
                    select_anio = page.locator("select").first
                    if select_anio.count() > 0:
                        select_anio.select_option(label=str(anio))
                        page.wait_for_timeout(3000)
                except Exception:
                    pass

                dias_disponibles = obtener_dias_publicados_en_vista(page, anio)
                dias_en_rango = [f for f in dias_disponibles if fecha_inicio <= f[0] <= fecha_fin]

                for f_obj, mes_txt, dia_txt in dias_en_rango:
                    fecha_ddmmyyyy = f_obj.strftime("%d/%m/%Y")
                    resultados_por_fecha.setdefault(f_obj, [])
                    try:
                        hrefs_antes = set(page.evaluate(f"() => Array.from(document.querySelectorAll('a[href*=\"/decisiones/{codigo_sala}/\"]')).map(a => a.href)"))
                        
                        page.evaluate("""([mesTarget, diaTarget]) => {
                            const contenedores = Array.from(document.querySelectorAll('tr, div, li'));
                            for (const cont of contenedores) {
                                const txt = (cont.innerText || '').trim();
                                if (!txt || txt.length > 250) continue;
                                const lineas = txt.split(/\\n+/).map(s => s.trim()).filter(Boolean);
                                if (lineas.length < 2 || lineas[0].toLowerCase() !== mesTarget.toLowerCase()) continue;
                                for (const el of cont.querySelectorAll('a, button, span, div')) {
                                    if ((el.innerText || '').trim() === diaTarget && el.children.length === 0) {
                                        el.click();
                                        return true;
                                    }
                                }
                            }
                            return false;
                        }""", [mes_txt, dia_txt])

                        enlaces_dia = {}
                        for _ in range(8):
                            page.wait_for_timeout(1000)
                            items_dom = page.evaluate(f"""() => {{
                                return Array.from(document.querySelectorAll('a[href*="/decisiones/{codigo_sala}/"]')).map(a => {{
                                    const box = a.closest('tr, li, div');
                                    return {{ href: a.href, resumen: box ? box.innerText.replace(/\\s+/g, ' ').trim() : a.innerText.trim() }};
                                }});
                            }}""")
                            nuevos = [it for it in items_dom if it["href"] not in hrefs_antes]
                            if nuevos or (items_dom and not hrefs_antes):
                                for it in (nuevos if nuevos else items_dom):
                                    full_url = urljoin("https://historico.tsj.gob.ve", it["href"])
                                    enlaces_dia[full_url] = {"url": full_url, "sala": sala_nombre, "fecha": f_obj, "resumen": it["resumen"]}
                                break

                        resultados_por_fecha[f_obj].extend(enlaces_dia.values())
                    except Exception:
                        pass

        browser.close()
    return resultados_por_fecha, portal_accesible


EXTRACTION_PROMPT_WEB = """Actúa como Magistrado Auxiliar experto en Derecho Procesal Penal venezolano.
Analiza la siguiente sentencia del TSJ y extrae un JSON puro con hasta 3 máximas de nulidad penal.

Criterios:
- Solo sentencias que decreten nulidades o reposiciones en materia penal.
- Extrae la redacción propia de la Sala (sin comillas ni citas textuales de otros fallos).
- Claves exactas por objeto: "numero", "sala", "fecha", "tema", "extracto", "contenido_citado", "ratificaciones".

Devuelve ÚNICAMENTE el JSON puro ([{{...}}]), sin bloques markdown ni texto adicional.

TEXTO DE LA SENTENCIA:
"""

JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def extraer_maximas_con_ia(client, primary_model, fallback_model, texto_sentencia, meta):
    prompt = EXTRACTION_PROMPT_WEB.format(sala=meta["sala"], numero_sugerido=meta["numero"], fecha=meta["fecha"], url=meta["url"]) + "\n" + texto_sentencia
    current_model = primary_model
    for attempt in range(1, 3):
        try:
            response = client.models.generate_content(model=current_model, contents=prompt)
            raw = (response.text or "").strip()
            match = JSON_ARRAY_RE.search(raw)
            if match:
                data = json.loads(match.group())
                if isinstance(data, list):
                    return data[:MAX_MAXIMAS_POR_SENTENCIA], True, ""
        except Exception as e:
            if current_model != fallback_model:
                current_model = fallback_model
            time.sleep(2 * attempt)
    return [], False, "Error IA"


def count_sentencias(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), COUNT(DISTINCT numero || '_' || sala) FROM sentencias")
    return cursor.fetchone()


def export_csv(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT id, numero, sala, fecha, COALESCE(tema, ''), extracto, contenido_citado, COALESCE(ratificaciones, ''), archivo_origen FROM sentencias ORDER BY id")
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["ID", "Número", "Sala", "Fecha", "Tema", "Extracto", "Contenido Citado", "Ratificaciones", "Origen"])
        writer.writerows(cursor.fetchall())


def main():
    print("=" * 65)
    print("  EXTRACTOR WEB TSJ - EXCLUSIVO SENTENCIAS DE NULIDAD PENAL")
    print("=" * 65)
    conn = init_db()
    fecha_inicio, fecha_fin = obtener_rango_fechas(conn)
    print(f"🕒 Fecha actual (Venezuela): {fecha_fin.strftime('%d/%m/%Y')}")
    print(f"📅 Rango de revisión programado: {fecha_inicio.strftime('%d/%m/%Y')} -> {fecha_fin.strftime('%d/%m/%Y')}")

    decisiones_por_dia, portal_accesible = buscar_decisiones_en_portal_tsj(fecha_inicio, fecha_fin)
    if not portal_accesible:
        print("\n⚠️  El portal del TSJ no respondió. No se modificará la fecha de control.")
        export_csv(conn)
        conn.close()
        return

    if not any(decisiones_por_dia.values()):
        print("\nℹ️  No se hallaron sentencias en el rango.")
        actualizar_ultima_fecha_revisada(conn, fecha_fin)
        export_csv(conn)
        conn.close()
        return

    client = init_client()
    primary_model, fallback_model = select_model(client)

    todo_sin_errores = True
    for dia_actual in sorted(decisiones_por_dia.keys()):
        items_dia = decisiones_por_dia[dia_actual]
        if not items_dia:
            continue
        print(f"\n📆 Día {dia_actual.strftime('%d/%m/%Y')} ({len(items_dia)} sentencias)")
        for item in items_dia:
            url = item["url"]
            if url_ya_procesada(conn, url):
                continue
            texto_completo = descargar_texto_sentencia(url)
            if not texto_completo or len(texto_completo) < 500:
                continue
            es_penal, tiene_nulidad, _ = evaluar_pertinencia_y_nulidad(texto_completo, item.get("resumen", ""), item["sala"])
            if not es_penal or (FILTRAR_SOLO_CON_INDICADORES_NULIDAD and not tiene_nulidad):
                guardar_estado_bloque(conn, url, "EXITOSO", 0, "Omitida por filtros")
                continue

            meta = extraer_metadatos_cabecera(texto_completo, url, item["sala"], dia_actual)
            meta["url"] = url
            maximas, exito, err = extraer_maximas_con_ia(client, primary_model, fallback_model, recortar_seccion_relevante(texto_completo), meta)
            if exito:
                guardar_estado_bloque(conn, url, "EXITOSO", len(maximas))
                if maximas:
                    insert_sentencias(conn, maximas, url)
            else:
                todo_sin_errores = False
                guardar_estado_bloque(conn, url, "ERROR", 0, err)
            time.sleep(2)

    if todo_sin_errores:
        actualizar_ultima_fecha_revisada(conn, fecha_fin)

    total_ext, sent_unicas = count_sentencias(conn)
    export_csv(conn)
    conn.close()
    print(f"\n✨ Ejecución finalizada. Total en BD: {total_ext} extractos ({sent_unicas} sentencias únicas).")


if __name__ == "__main__":
    main()
