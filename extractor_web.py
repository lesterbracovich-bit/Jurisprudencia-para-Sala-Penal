#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Extractor Automático Web del TSJ - Exclusivo para Sentencias de Nulidad Penal
=============================================================================
Se ejecuta desde el Self-Hosted Runner local en Venezuela.
Navega en https://www.tsj.gob.ve/es/web/tsj/decisiones#5 (Penal) y #1 (Constitucional),
selecciona el año y hace clic en los círculos verdes de los días publicados dentro del
rango pendiente (iniciando el 09/09/2026).

Filtra sentencias en materia penal donde se decreten NULIDADES y extrae los 2 o 3
extractos más relevantes de la redacción propia de la Sala que expliquen concisamente:
  1) Cuál es el vicio que generó la nulidad.
  2) Por qué dicho vicio es considerado lesivo por la Sala.
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

# ─── Palabras Clave de Filtrado Jurídico ──────────────────────────────────────
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
    "articulo 174",
    "articulo 175",
    "articulo 179",
    "articulo 180",
]


# ─── Cliente Google AI Studio (Gemini) ────────────────────────────────────────
def init_client():
    if not API_KEY:
        print("=" * 60)
        print("❌ ERROR: No se encontró la variable de entorno GEMINI_API_KEY.")
        print("   Configúrala en GitHub -> Settings -> Secrets and variables -> Actions.")
        print("=" * 60)
        sys.exit(1)
    return genai.Client(api_key=API_KEY)


def select_model(client):
    funcionales = []
    for model_name in PREFERRED_MODELS:
        try:
            print(f"  Probando modelo: {model_name} ...", end=" ")
            response = client.models.generate_content(
                model=model_name,
                contents="Responde solo: OK",
            )
            if response and response.text:
                print("✔ disponible")
                funcionales.append(model_name)
        except Exception as e:
            print(f"✘ ({type(e).__name__})")

    if funcionales:
        primario = funcionales[0]
        respaldo = funcionales[1] if len(funcionales) > 1 else primario
        return primario, respaldo

    print("\n  Buscando modelos Gemini disponibles dinámicamente...")
    try:
        for m in client.models.list():
            name = m.name
            if "gemini" not in name.lower() or "embedding" in name.lower():
                continue
            short_name = name.replace("models/", "")
            try:
                resp = client.models.generate_content(
                    model=short_name,
                    contents="Responde solo: OK",
                )
                if resp and resp.text:
                    print(f"  ✔ Modelo dinámico seleccionado: {short_name}")
                    return short_name, short_name
            except Exception:
                continue
    except Exception as e:
        print(f"  Error al listar modelos: {e}")

    print("\n❌ No se encontró ningún modelo Gemini disponible.")
    sys.exit(1)


# ─── Base de Datos y Control de Fechas ────────────────────────────────────────
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
    cursor.execute(
        "SELECT 1 FROM estado_bloques WHERE archivo = ? AND estado = 'EXITOSO' LIMIT 1",
        (identificador_origen,),
    )
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


# ─── Validación y Deduplicación Semántica ─────────────────────────────────────
VALORES_VACIOS = {
    "",
    "sin cita",
    "sin cita textual",
    "sin cita textual expresa",
    "sin cita textual ni desarrollo conceptual",
    "no aplica",
    "ninguno",
}

FRASES_INVALIDAS = [
    "sin cita",
    "omite la transcripcion",
    "omite la transcripción",
    "primero: competente",
]


def normalizar_texto(texto):
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode("utf-8").lower()
    return re.sub(r"[^\w\s]", " ", texto).strip()


def son_mismo_tema(tema1, extracto1, tema2, extracto2, citado1="", citado2=""):
    c1_norm = normalizar_texto(citado1)
    c2_norm = normalizar_texto(citado2)
    if c1_norm and c2_norm:
        if (
            c1_norm in c2_norm
            or c2_norm in c1_norm
            or difflib.SequenceMatcher(None, c1_norm, c2_norm, autojunk=False).ratio() >= 0.65
        ):
            return True

    t1_norm = normalizar_texto(tema1)
    t2_norm = normalizar_texto(tema2)
    e1_norm = normalizar_texto(extracto1)
    e2_norm = normalizar_texto(extracto2)

    if t1_norm and t2_norm:
        stopwords = {"de", "la", "el", "los", "las", "en", "por", "del", "un", "una", "y", "o", "a", "al", "para", "con", "sobre"}
        palabras1 = set(t1_norm.split()) - stopwords
        palabras2 = set(t2_norm.split()) - stopwords
        ratio_tema = difflib.SequenceMatcher(None, t1_norm, t2_norm, autojunk=False).ratio()

        if ratio_tema >= 0.65 or (palabras1 and palabras2 and len(palabras1 & palabras2) >= min(len(palabras1), len(palabras2))):
            return True
        if ratio_tema < 0.45 and (not palabras1 or not palabras2 or not (palabras1 & palabras2)):
            return False

    if difflib.SequenceMatcher(None, e1_norm, e2_norm, autojunk=False).ratio() >= 0.55:
        return True

    palabras_e1 = set(w for w in e1_norm.split() if len(w) > 4)
    palabras_e2 = set(w for w in e2_norm.split() if len(w) > 4)
    if palabras_e1 and palabras_e2:
        jaccard = len(palabras_e1 & palabras_e2) / len(palabras_e1 | palabras_e2)
        if jaccard >= 0.45:
            return True

    return False


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

    contenido_citado = str(s.get("contenido_citado", "")).strip()
    contenido_citado = contenido_citado.strip('"“”«»\'').strip()
    citado_lower = contenido_citado.lower()

    if citado_lower in VALORES_VACIOS or len(contenido_citado) < 150:
        return None
    if contenido_citado.endswith(":") or contenido_citado.endswith("[...]") or contenido_citado.endswith("...:"):
        return None
    if any(frase in citado_lower for frase in FRASES_INVALIDAS):
        return None

    patrones_cita_ajena = [
        r"en\s+sentencia\s+n[°ºo.]?\s*\d+.*?(?:estableci[óo]|se[ñn]al[óo]|indic[óo]|dej[óo]\s+sentado)\s*:",
        r"al\s+respecto\s*,?\s*la\s+sala\s+.*?en\s+decisi[óo]n\s+n[°ºo.]?\s*\d+.*?expres[óo]\s*:",
    ]
    for pat in patrones_cita_ajena:
        if re.search(pat, citado_lower):
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
        registro_valido = limpiar_y_validar_registro(s)
        if registro_valido is None:
            continue

        numero = registro_valido["numero"]
        sala = registro_valido["sala"]
        fecha = registro_valido["fecha"]
        tema = registro_valido["tema"]
        extracto = registro_valido["extracto"]
        contenido_citado = registro_valido["contenido_citado"]
        ratificaciones_str = registro_valido["ratificaciones"]

        cursor.execute(
            "SELECT id, COALESCE(tema, ''), extracto, contenido_citado, fecha, COALESCE(ratificaciones, '') "
            "FROM sentencias WHERE numero = ? AND sala = ?",
            (numero, sala),
        )
        filas_existentes = cursor.fetchall()

        fila_coincidente = None
        for fila in filas_existentes:
            _, ex_tema, ex_extracto, ex_citado, _, _ = fila
            if son_mismo_tema(tema, extracto, ex_tema, ex_extracto, contenido_citado, ex_citado):
                fila_coincidente = fila
                break

        if fila_coincidente is None:
            if len(filas_existentes) >= MAX_MAXIMAS_POR_SENTENCIA:
                duplicated += 1
                continue
            cursor.execute(
                """
                INSERT INTO sentencias (numero, sala, fecha, tema, extracto, contenido_citado, ratificaciones, archivo_origen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (numero, sala, fecha, tema, extracto, contenido_citado, ratificaciones_str, archivo_origen),
            )
            inserted += 1
        else:
            row_id, existing_tema, existing_extracto, existing_citado, existing_fecha, existing_ratif = fila_coincidente
            debe_actualizar = False
            nuevo_tema = existing_tema or tema
            nuevo_citado = existing_citado or ""
            nuevo_extracto = existing_extracto or ""
            nueva_fecha = existing_fecha or fecha
            nuevo_ratif = existing_ratif or ""

            if len(contenido_citado) > len(nuevo_citado):
                nuevo_citado = contenido_citado
                debe_actualizar = True
            if len(extracto) > len(nuevo_extracto):
                nuevo_extracto = extracto
                debe_actualizar = True
            if len(ratificaciones_str) > len(nuevo_ratif):
                nuevo_ratif = ratificaciones_str
                debe_actualizar = True

            if debe_actualizar:
                cursor.execute(
                    """
                    UPDATE sentencias
                    SET tema = ?, contenido_citado = ?, extracto = ?, fecha = ?, ratificaciones = ?, archivo_origen = ?
                    WHERE id = ?
                    """,
                    (nuevo_tema, nuevo_citado, nuevo_extracto, nueva_fecha, nuevo_ratif, archivo_origen, row_id),
                )
                updated += 1
            else:
                duplicated += 1

    conn.commit()
    return inserted, updated, duplicated


# ─── Scraping Directo del Portal TSJ (Conexión Local en Venezuela) ────────────
def descargar_texto_sentencia(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }
    for intento in range(1, 4):
        try:
            resp = requests.get(url, headers=headers, verify=False, timeout=30)
            if resp.status_code != 200:
                continue
            raw_bytes = resp.content
            try:
                html_str = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                html_str = raw_bytes.decode("windows-1252", errors="replace")

            soup = BeautifulSoup(html_str, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()

            texto = soup.get_text(separator="\n")
            lineas = [re.sub(r"\s+", " ", ln).strip() for ln in texto.splitlines() if ln.strip()]
            return "\n".join(lineas)
        except Exception as e:
            if intento == 3:
                print(f"      ⚠ Error descargando {url}: {e}")
            time.sleep(2)
    return ""


def extraer_metadatos_cabecera(texto, url, sala_nombre, fecha_obj):
    numero = ""
    m_url = re.search(r"/\d+-([A-Za-z.]*0*\d+)-\d{5,6}-\d{4}", url)
    if m_url:
        num_candidato = re.sub(r"\D", "", m_url.group(1)).lstrip("0")
        if num_candidato:
            numero = num_candidato

    if not numero:
        patrones = [
            r"Sentencia\s+(?:N[°ºo.]?\s*)?(\d{1,4})\b",
            r"Decisi[óo]n\s+(?:N[°ºo.]?\s*)?(\d{1,4})\b",
            r"N[°ºo.]\s*(\d{1,4})\s+del\s+\d{1,2}",
        ]
        for pat in patrones:
            m_txt = re.search(pat, texto[:4000], re.IGNORECASE)
            if m_txt:
                numero = m_txt.group(1).lstrip("0")
                break

    return {
        "numero": numero,
        "sala": sala_nombre,
        "fecha": fecha_obj.strftime("%d/%m/%Y"),
    }


def evaluar_pertinencia_y_nulidad(texto_completo, resumen_portal, sala_nombre):
    texto_busqueda = f"{resumen_portal}\n{texto_completo}".lower()

    if sala_nombre == "Sala Constitucional":
        coincidencias_penales = sum(1 for kw in KEYWORDS_MATERIA_PENAL if kw in texto_busqueda)
        if coincidencias_penales < 2:
            return False, False, 0

    coincidencias_nulidad = sum(1 for kw in KEYWORDS_NULIDAD if kw in texto_busqueda)
    tiene_nulidad = coincidencias_nulidad >= 1
    return True, tiene_nulidad, coincidencias_nulidad


def recortar_seccion_relevante(texto):
    if len(texto) <= MAX_CHARS_SENTENCIA:
        return texto

    encabezado = texto[:3500]
    patrones_motiva = [
        r"MOTIVACI[ÓO]N\s+PARA\s+DECIDIR",
        r"CONSIDERACIONES\s+PARA\s+DECIDIR",
        r"DE\s+LA\s+NULIDAD\s+DE\s+OFICIO",
        r"PUNTO\s+PREVIO",
        r"FUNDAMENTOS\s+DE\s+LA\s+DECISI[ÓO]N",
        r"PARA\s+DECIDIR\s*,?\s*LA\s+SALA\s+OBSERVA",
    ]
    inicio_motiva = -1
    for pat in patrones_motiva:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            inicio_motiva = m.start()
            break

    if inicio_motiva != -1:
        cuerpo_motiva = texto[inicio_motiva : inicio_motiva + (MAX_CHARS_SENTENCIA - 4000)]
        return f"{encabezado}\n\n[...SECCIÓN MOTIVA Y DISPOSITIVA DEL FALLO...]\n\n{cuerpo_motiva}"

    cierre = texto[-(MAX_CHARS_SENTENCIA - 4000) :]
    return f"{encabezado}\n\n[...DESARROLLO Y DECISIÓN DEL FALLO...]\n\n{cierre}"


def cargar_pagina_tsj_con_reintentos(page, url, menu_texto):
    for intento in range(1, 4):
        try:
            print(f"    🔄 Cargando {url} (intento {intento}/3)...")
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_selector("text=Seleccione un Año", timeout=35000)

            enlace_sala = page.locator(f"a:text-is('{menu_texto}')").first
            if enlace_sala.count() > 0:
                enlace_sala.click(timeout=10000)
                page.wait_for_timeout(2000)

            return True
        except Exception as e:
            print(f"    ⚠ Intento {intento} sin respuesta completa: {str(e)[:100]}")
            time.sleep(3)
    return False


def obtener_dias_publicados_en_vista(page, anio_objetivo):
    datos_dom = page.evaluate("""() => {
        const mesesValid = [
            'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
            'julio', 'agosto', 'septiembre', 'setiembre', 'octubre', 'noviembre', 'diciembre'
        ];
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
                const tok = lineas[i];
                if (/^(0?[1-9]|[12][0-9]|3[01])$/.test(tok)) {
                    dias.push(tok);
                }
            }
            if (dias.length > 0) {
                hallados.push({ mes_original: lineas[0], mes_lower: posibleMes, dias: dias });
            }
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
                    resultado.append((f_obj, item["mes_original"], d_str))
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
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        for sala_info in SALAS_OBJETIVO:
            sala_nombre = sala_info["nombre_bd"]
            menu_texto = sala_info["menu_texto"]
            codigo_sala = sala_info["codigo"]
            url_sala = sala_info["url"]

            print(f"\n🏛️  Consultando {sala_nombre} ({url_sala})...")
            cargo = cargar_pagina_tsj_con_reintentos(page, url_sala, menu_texto)
            if not cargo:
                print(f"    ❌ No fue posible conectar con {sala_nombre} tras 3 intentos.")
                continue

            portal_accesible = True

            for anio in anios_necesarios:
                try:
                    select_anio = page.locator("select").first
                    if select_anio.count() > 0:
                        valor_actual = select_anio.input_value()
                        if str(anio) not in str(valor_actual):
                            select_anio.select_option(label=str(anio))
                            page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"    ⚠ Aviso seleccionando año {anio}: {e}")

                dias_disponibles = obtener_dias_publicados_en_vista(page, anio)
                dias_en_rango = [
                    (f_obj, mes_txt, dia_txt)
                    for (f_obj, mes_txt, dia_txt) in dias_disponibles
                    if fecha_inicio <= f_obj <= fecha_fin
                ]

                print(f"    📆 Año {anio}: {len(dias_disponibles)} día(s) con publicaciones totales | {len(dias_en_rango)} dentro del rango buscado.")

                for f_obj, mes_txt, dia_txt in dias_en_rango:
                    fecha_ddmmyyyy = f_obj.strftime("%d/%m/%Y")
                    resultados_por_fecha.setdefault(f_obj, [])

                    try:
                        hrefs_antes = set(
                            page.evaluate(
                                f"() => Array.from(document.querySelectorAll('a[href*=\"/decisiones/{codigo_sala}/\"]')).map(a => a.href)"
                            )
                        )

                        clic_realizado = page.evaluate(
                            """([mesTarget, diaTarget]) => {
                                const contenedores = Array.from(document.querySelectorAll('tr, div, li'));
                                for (const cont of contenedores) {
                                    const txt = (cont.innerText || '').trim();
                                    if (!txt || txt.length > 250) continue;
                                    const lineas = txt.split(/\\n+/).map(s => s.trim()).filter(Boolean);
                                    if (lineas.length < 2) continue;
                                    if (lineas[0].toLowerCase() !== mesTarget.toLowerCase()) continue;

                                    const clickables = Array.from(cont.querySelectorAll('a, button, span, div'));
                                    for (const el of clickables) {
                                        if ((el.innerText || '').trim() === diaTarget && el.children.length === 0) {
                                            el.click();
                                            return true;
                                        }
                                    }
                                }
                                return false;
                            }""",
                            [mes_txt, dia_txt],
                        )

                        if not clic_realizado:
                            print(f"      ⚠ No se pudo hacer clic en {mes_txt} {dia_txt}")
                            continue

                        enlaces_dia = {}
                        for _ in range(12):
                            page.wait_for_timeout(1000)
                            items_dom = page.evaluate(
                                f"""() => {{
                                    return Array.from(document.querySelectorAll('a[href*="/decisiones/{codigo_sala}/"]')).map(a => {{
                                        const box = a.closest('tr, li, div');
                                        return {{
                                            href: a.href,
                                            resumen: box ? box.innerText.replace(/\\s+/g, ' ').trim() : a.innerText.trim()
                                        }};
                                    }});
                                }}"""
                            )
                            nuevos = [it for it in items_dom if it["href"] not in hrefs_antes]
                            if nuevos or (items_dom and not hrefs_antes):
                                lista_usar = nuevos if nuevos else items_dom
                                for it in lista_usar:
                                    full_url = urljoin("https://historico.tsj.gob.ve", it["href"])
                                    enlaces_dia[full_url] = {
                                        "url": full_url,
                                        "sala": sala_nombre,
                                        "fecha": f_obj,
                                        "resumen": it["resumen"],
                                    }
                                break

                        print(f"      📅 {fecha_ddmmyyyy} ({mes_txt} {dia_txt}): {len(enlaces_dia)} sentencia(s) encontrada(s)")
                        resultados_por_fecha[f_obj].extend(enlaces_dia.values())

                    except Exception as e:
                        print(f"      ⚠ Error leyendo día {fecha_ddmmyyyy}: {e}")

        browser.close()

    return resultados_por_fecha, portal_accesible


# ─── Prompt Exclusivo para Vicios de Nulidad y su Lesividad ───────────────────
EXTRACTION_PROMPT_WEB = """Actúa como un Magistrado Auxiliar experto en Derecho Procesal Penal y Constitucional Penal venezolano.
Analiza la siguiente sentencia del Tribunal Supremo de Justicia (TSJ) de Venezuela.

METADATOS DE LA SENTENCIA ANALIZADA:
- Sala Emisora: {sala}
- Número de Sentencia detectado: {numero_sugerido}
- Fecha de Publicación: {fecha}
- Origen: {url}

REGLAS DE FILTRADO Y EXTRACCIÓN ESTRICTA (FUNDAMENTAL):

1. EXCLUSIVO PARA SENTENCIAS QUE DECRETEN NULIDADES EN MATERIA PENAL:
   - Si la Sala Emisora es la "Sala Constitucional", verifica primero que el caso sea de MATERIA PENAL. Si no es materia penal, devuelve inmediatamente una lista vacía: []
   - Verifica si en esta sentencia la Sala EFECTIVAMENTE DECRETA UNA NULIDAD (ya sea nulidad de oficio, nulidad absoluta, anulación del fallo de la Corte de Apelaciones o de Primera Instancia, reposición de la causa, o declaratoria con lugar de recurso/amparo/revisión que anule actuaciones o sentencias).
   - Si la sentencia NO decreta ninguna nulidad (por ejemplo, si únicamente declara el recurso inadmisible, desestimado por manifiestamente infundado, sin lugar, o es un auto de trámite/radicación/avocamiento sin decretar nulidad), devuelve inmediatamente una lista vacía: []

2. QUÉ DEBES EXTRAER (LOS 2 O 3 EXTRACTOS MÁS RELEVANTES SOBRE EL VICIO Y SU LESIVIDAD):
   - Extrae ÚNICAMENTE los DOS (2) o TRES (3) extractos más relevantes de la sentencia.
   - Se entiende por "más relevantes" aquellos pasajes que contengan la explicación más concisa, directa y contundente acerca de:
     a) CUÁL ES EL VICIO ESPECÍFICO que generó la nulidad (ej. inmotivación o motivación aparente en alzada, incongruencia omisiva, silencio de pruebas o falta de pronunciamiento sobre pruebas promovidas, violación al principio de inmediación, subversión del procedimiento, falta de notificación personal al imputado, usurpación de funciones, violación a la cosa juzgada, etc.).
     b) POR QUÉ DICHO VICIO ES CONSIDERADO LESIVO POR LA SALA (es decir, el agravio constitucional o procesal: cómo ese vicio vulnera el derecho a la defensa, el debido proceso, la tutela judicial efectiva, la presunción de inocencia o la igualdad de armas, haciendo insalvable el acto y obligando a su anulación).

3. SOLO REDACCIÓN PROPIA DE LA SALA (PROHIBIDO EXTRAER CITAS TEXTUALES):
   - Toma los extractos EXCLUSIVAMENTE de los párrafos redactados con VOZ PROPIA por la Sala en su sección motiva ("Motivación para decidir" / "Consideraciones para decidir" / "De la Nulidad de Oficio").
   - QUEDA TERMINANTEMENTE PROHIBIDO extraer textos que estén entre comillas ("...", “...”, «...»).
   - NO extraigas citas textuales de sentencias anteriores, ni transcripciones de artículos del COPP o la Constitución, ni citas de autores/doctrina, ni los alegatos de las partes o transcripciones del fallo recurrido.
   - NO extraigas la parte dispositiva ("PRIMERO: Se declara...").

ESTRUCTURA DEL JSON A DEVOLVER:
Devuelve una lista JSON (de 2 a 3 objetos máximo) con estas claves exactas:
- "numero": El número entero de ESTA sentencia sin puntos ni ceros a la izquierda (ej. "{numero_sugerido}").
- "sala": Exactamente "{sala}".
- "fecha": "{fecha}".
- "tema": Título conciso (de 2 a 6 palabras) que identifique el vicio de nulidad o su efecto lesivo (ej. "Incongruencia omisiva probatoria", "Nulidad por inmotivación en alzada", "Efecto extensivo de la nulidad").
- "extracto": Síntesis concisa (de 2 a 4 líneas) redactada como máxima jurisprudencial que explique cuál es el vicio que generó la nulidad y por qué la Sala lo considera lesivo de garantías fundamentales.
- "contenido_citado": El párrafo exacto de REDACCIÓN PROPIA DE LA SALA (mínimo 180 caracteres, sin comillas ni citas de otros fallos) donde la Sala explica de la forma más concisa el vicio constatado y por qué dicho vicio lesiona el ordenamiento jurídico o los derechos de las partes provocando la nulidad.
- "ratificaciones": Si dentro de ese razonamiento propio la Sala menciona como referencia algún fallo previo, indícalo brevemente; de lo contrario, deja cadena vacía ("").

Devuelve ÚNICAMENTE el JSON puro ([{{...}}, {{...}}]), sin bloques markdown ni texto adicional.

TEXTO DE LA SENTENCIA A ANALIZAR:
"""

JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def extraer_maximas_con_ia(client, primary_model, fallback_model, texto_sentencia, meta, max_retries=4):
    prompt = EXTRACTION_PROMPT_WEB.format(
        sala=meta["sala"],
        numero_sugerido=meta["numero"] or "extraer del encabezado del texto",
        fecha=meta["fecha"],
        url=meta["url"],
    ) + "\n" + texto_sentencia

    current_model = primary_model
    last_error = ""

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=current_model,
                contents=prompt,
            )
            raw = (response.text or "").strip()
            if not raw:
                raise ValueError("Respuesta vacía del modelo.")

            sentencias = None
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    sentencias = data
            except json.JSONDecodeError:
                match = JSON_ARRAY_RE.search(raw)
                if match:
                    data = json.loads(match.group())
                    if isinstance(data, list):
                        sentencias = data

            if sentencias is not None:
                return sentencias[:MAX_MAXIMAS_POR_SENTENCIA], True, ""
            last_error = "Respuesta no es un JSON válido."
        except Exception as e:
            last_error = str(e)
            is_busy = any(k in last_error for k in ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"])
            if is_busy and fallback_model and current_model != fallback_model:
                current_model = fallback_model
            time.sleep(4 * attempt)

    return [], False, last_error


# ─── Exportación CSV y Resumen ────────────────────────────────────────────────
def count_sentencias(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), COUNT(DISTINCT numero || '_' || sala) FROM sentencias")
    return cursor.fetchone()


def export_csv(conn):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, numero, sala, fecha, COALESCE(tema, ''), extracto, contenido_citado, COALESCE(ratificaciones, ''), archivo_origen "
        "FROM sentencias ORDER BY id"
    )
    rows = cursor.fetchall()
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "ID", "Número", "Sala", "Fecha", "Tema / Punto Jurídico",
            "Extracto (Resumen)", "Contenido Citado (Textual)",
            "Ratificaciones / Precedentes", "Archivo Origen"
        ])
        writer.writerows(rows)
    print(f"📊 CSV actualizado: {CSV_PATH} ({len(rows)} registros)")


# ─── Flujo Principal ──────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("  EXTRACTOR WEB TSJ - EXCLUSIVO SENTENCIAS DE NULIDAD PENAL")
    print("  Objetivo: 2 a 3 extractos sobre el vicio y su lesividad")
    print("=" * 65)

    conn = init_db()
    fecha_inicio, fecha_fin = obtener_rango_fechas(conn)

    print(f"🕒 Fecha actual (Venezuela): {fecha_fin.strftime('%d/%m/%Y')}")
    if fecha_inicio > fecha_fin:
        print(f"✨ La base de datos ya está al día (última revisión: {(fecha_inicio - timedelta(days=1)).strftime('%d/%m/%Y')}).")
        export_csv(conn)
        conn.close()
        return

    print(f"📅 Rango de revisión programado: {fecha_inicio.strftime('%d/%m/%Y')} -> {fecha_fin.strftime('%d/%m/%Y')}")

    # 1) Buscar decisiones publicadas en el calendario de círculos verdes del TSJ
    decisiones_por_dia, portal_accesible = buscar_decisiones_en_portal_tsj(fecha_inicio, fecha_fin)

    if not portal_accesible:
        print("\n⚠️  El portal del TSJ no respondió. No se modificará la fecha de control.")
        export_csv(conn)
        conn.close()
        return

    total_halladas = sum(len(lista) for lista in decisiones_por_dia.values())

    if total_halladas == 0:
        print("\nℹ️  No se hallaron sentencias publicadas en los días del rango consultado.")
        actualizar_ultima_fecha_revisada(conn, fecha_fin)
        print(f"✔  Fecha de control actualizada a: {fecha_fin.strftime('%Y-%m-%d')}")
        export_csv(conn)
        conn.close()
        return

    # 2) Inicializar cliente Gemini
    print("\n🤖 Inicializando conexión con Google AI Studio (Gemini)...")
    client = init_client()
    primary_model, fallback_model = select_model(client)
    print(f"✅ Modelo Primario: {primary_model} | Respaldo: {fallback_model}")

    total_inserted = 0
    total_updated = 0
    total_duplicated = 0

    # 3) Procesar día por día en orden cronológico
    todo_sin_errores_ia = True
    for dia_actual in sorted(decisiones_por_dia.keys()):
        items_dia = decisiones_por_dia[dia_actual]
        if not items_dia:
            continue

        print(f"\n{'─' * 65}")
        print(f"📆 Procesando publicaciones del día {dia_actual.strftime('%d/%m/%Y')} ({len(items_dia)} sentencia(s))")

        candidatas_dia = []
        for item in items_dia:
            url = item["url"]
            if url_ya_procesada(conn, url):
                print(f"  ⏭️  Ya procesada previamente: {url}")
                continue

            texto_completo = descargar_texto_sentencia(url)
            if not texto_completo or len(texto_completo) < 500:
                continue

            es_penal, tiene_nulidad, score_nulidad = evaluar_pertinencia_y_nulidad(
                texto_completo, item.get("resumen", ""), item["sala"]
            )
            if not es_penal:
                print(f"  ⏭️  Descartada (Sala Constitucional - No es materia penal): {url}")
                guardar_estado_bloque(conn, url, "EXITOSO", 0, "Descartada: No es materia penal")
                continue

            if FILTRAR_SOLO_CON_INDICADORES_NULIDAD and not tiene_nulidad:
                print(f"  ⏭️  Omitida (Sin indicadores de nulidad): {url}")
                guardar_estado_bloque(conn, url, "EXITOSO", 0, "Omitida: Sin indicadores de nulidad")
                continue

            meta = extraer_metadatos_cabecera(texto_completo, url, item["sala"], dia_actual)
            meta["url"] = url
            meta["tiene_nulidad"] = tiene_nulidad
            meta["score_nulidad"] = score_nulidad
            meta["texto_recortado"] = recortar_seccion_relevante(texto_completo)
            candidatas_dia.append(meta)

        candidatas_dia.sort(key=lambda x: x["score_nulidad"], reverse=True)

        for meta in candidatas_dia:
            print(f"  🔥 [CANDIDATA NULIDAD] Analizando {meta['sala']} | Sentencia N° {meta['numero'] or '?'} ({meta['url']})...", end=" ")

            maximas, exito, error_msg = extraer_maximas_con_ia(
                client=client,
                primary_model=primary_model,
                fallback_model=fallback_model,
                texto_sentencia=meta["texto_recortado"],
                meta=meta,
            )

            if exito:
                guardar_estado_bloque(conn, meta["url"], "EXITOSO", len(maximas))
                if maximas:
                    print(f"✔ ({len(maximas)} extracto(s) de nulidad extraído(s))")
                    ins, upd, dup = insert_sentencias(conn, maximas, meta["url"])
                    total_inserted += ins
                    total_updated += upd
                    total_duplicated += dup
                else:
                    print("✔ (Descartada tras lectura IA: no decreta nulidad)")
            else:
                todo_sin_errores_ia = False
                guardar_estado_bloque(conn, meta["url"], "ERROR", 0, error_msg)
                print(f"✘ Error IA: {error_msg[:60]}")

            time.sleep(3)

    if todo_sin_errores_ia:
        actualizar_ultima_fecha_revisada(conn, fecha_fin)
        print(f"\n✔ Fecha de control actualizada a: {fecha_fin.strftime('%Y-%m-%d')}")

    # 4) Exportar CSV y mostrar resumen
    total_ext, sent_unicas = count_sentencias(conn)
    export_csv(conn)
    conn.close()

    print(f"\n{'=' * 65}")
    print(f"  RESUMEN DE EJECUCIÓN AUTOMÁTICA")
    print(f"{'=' * 65}")
    print(f"  ✅ Extractos de nulidad nuevos: {total_inserted}")
    print(f"  📝 Extractos enriquecidos:      {total_updated}")
    print(f"  🔄 Duplicados omitidos:         {total_duplicated}")
    print(f"  📋 Total extractos en BD:       {total_ext} ({sent_unicas} sentencias únicas)")
    print(f"{'=' * 65}")


if __name__ == "__main__":
    main()
