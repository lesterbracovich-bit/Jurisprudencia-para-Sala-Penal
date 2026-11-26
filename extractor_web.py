#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Extractor Web de Jurisprudencia – Sala de Casación Penal del TSJ
=================================================================
Descarga sentencias publicadas en el sitio web del Tribunal Supremo de Justicia
(https://www.tsj.gob.ve/es/web/tsj/decisiones#5) y extrae MÁXIMAS JURÍDICAS
(extractos relevantes de redacción original de la Sala) mediante Google Gemini.

Características:
  - Extracción directa vía API JSON del portal TSJ (Liferay) y descarga HTML
    desde historico.tsj.gob.ve.
  - Extracción exclusiva de MÁXIMAS doctrinales y ratio decidendi redactadas
    originariamente por la Sala en cada sentencia (omite citas textuales ajenas).
  - Seguimiento incremental automático: recuerda la última fecha procesada y
    en ejecuciones posteriores solo revisa fechas nuevas a partir de ella.
  - Por defecto (primera ejecución): procesa todas las sentencias de OCTUBRE 2026.
  - Tabla de seguimiento de bloques (estado_bloques) para pausar y reanudar.
  - Compatible con la base de datos común 'jurisprudencia_ia.db' y el visor_web.py.

Uso:
    python extractor_web.py                    # Modo automático (Octubre la 1ª vez; luego fechas nuevas)
    python extractor_web.py --mes 10           # Procesa un mes específico (ej: 10 u octubre)
    python extractor_web.py --fecha 23/10/2026 # Procesa un solo día específico
    python extractor_web.py --desde 01/10/2026 # Procesa desde una fecha en adelante
    python extractor_web.py --todas            # Procesa todas las fechas publicadas del año
    python extractor_web.py --historico        # Ejecuta revisión histórica (nulidades 2000-09/2026)
    python extractor_web.py --reset-fecha      # Reinicia el registro de última fecha actual a cero
    python extractor_web.py --reset-historico  # Reinicia el registro de fecha histórica a cero
    python extractor_web.py --reprocesar-todo  # Reprocesa bloques previamente completados
"""

import json
import os
import re
import csv
import sqlite3
import sys
import time
import functools
import difflib
import unicodedata
from datetime import datetime, date
from urllib.parse import quote

# Forzar flush en todos los print para ver progreso en tiempo real en consola
print = functools.partial(print, flush=True)

try:
    import requests
    from bs4 import BeautifulSoup
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("=" * 60)
    print("⚠  Faltan dependencias. Instálalas con:")
    print("   pip install requests beautifulsoup4 urllib3")
    print("=" * 60)
    sys.exit(1)

from llm_client import (
    LimiteDiarioExcedidoError,
    PREFERRED_GEMINI_MODELS,
    PREFERRED_NVIDIA_MODELS,
    PREFERRED_OPENROUTER_MODELS,
    init_client as _init_llm_client,
    select_model,
)

# ─── Configuración General ───────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "jurisprudencia_ia.db")
CSV_PATH = os.path.join(BASE_DIR, "reporte_jurisprudencia.csv")

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "").strip()

# Modelos en orden de preferencia
PREFERRED_MODELS = list(PREFERRED_GEMINI_MODELS)
OPENROUTER_MODELS = list(PREFERRED_OPENROUTER_MODELS)
NVIDIA_MODELS = list(PREFERRED_NVIDIA_MODELS)

CHUNK_SIZE = 25_000
CHUNK_OVERLAP = 3_000

# ─── Configuración de Sala TSJ ────────────────────────────────────────────────
SALA_ID = "003"           # Sala de Casación Penal en Liferay
SALA_DIR = "scp"          # Subdirectorio en historico.tsj.gob.ve
SALA_NOMBRE = "Sala de Casación Penal"
ANO_DEFAULT = "2026"

# ─── Modo Actual vs. Modo Histórico (Nulidades 2000–09/2026) ─────────────────
FECHA_HIST_INICIO = date(2000, 1, 19)
FECHA_HIST_FIN = date(2026, 9, 30)
FECHA_ACTUAL_INICIO = date(2026, 10, 1)
CLAVE_CONTROL_ACTUAL = f"ultima_fecha_procesada_{SALA_ID}"
CLAVE_CONTROL_HIST = f"ultima_fecha_historica_{SALA_ID}"
CLAVE_HIST_COMPLETADO = f"historico_completado_{SALA_ID}"
HIST_MAX_DIAS_POR_RUN = 100
HIST_MAX_MINUTOS_POR_RUN = 140

# ─── Endpoints API TSJ (Liferay) ─────────────────────────────────────────────
TSJ_BASE = "https://www.tsj.gob.ve/es/decisiones"
TSJ_HISTORICO_BASE = "https://historico.tsj.gob.ve/decisiones"

# Endpoint para listar días con sentencias publicadas de un año y sala
API_LIST_DAYS = (
    "{base}?p_p_id=displaySentencias_WAR_NoticiasTsjPorlet612"
    "&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
    "&p_p_cacheability=cacheLevelPage"
    "&server%5Bendpoint%5D=%2Fservices%2FWSDecision.HTTPEndpoint"
    "&server%5Bmethod%5D=%2FlistDayByAnoSala"
    "&SALA={sala_id}&ANO={ano}"
)

# Endpoint para listar las sentencias de una fecha específica
API_LIST_DECISIONS = (
    "{base}?p_p_id=displayListaDecision_WAR_NoticiasTsjPorlet612"
    "&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
    "&p_p_cacheability=cacheLevelPage"
    "&server%5Bendpoint%5D=%2Fservices%2FWSDecision.HTTPEndpoint"
    "&server%5Bmethod%5D=%2FlistDecisionByFechaSala"
    "&FECHA={fecha_encoded}&SALA={sala_id}"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "es-VE,es;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.tsj.gob.ve/es/web/tsj/decisiones",
}

MESES_MAP = {
    "1": 1, "01": 1, "enero": 1,
    "2": 2, "02": 2, "febrero": 2,
    "3": 3, "03": 3, "marzo": 3,
    "4": 4, "04": 4, "abril": 4,
    "5": 5, "05": 5, "mayo": 5,
    "6": 6, "06": 6, "junio": 6,
    "7": 7, "07": 7, "julio": 7,
    "8": 8, "08": 8, "agosto": 8,
    "9": 9, "09": 9, "septiembre": 9, "setiembre": 9,
    "10": 10, "octubre": 10, "oct": 10,
    "11": 11, "noviembre": 11, "nov": 11,
    "12": 12, "diciembre": 12, "dic": 12,
}


# ─── Inicialización de Proveedores IA ────────────────────────────────────────
def init_client():
    """Crea e inicializa el cliente multi-proveedor (Gemini + OpenRouter + NVIDIA NIM)."""
    return _init_llm_client(
        gemini_models=PREFERRED_MODELS,
        openrouter_models=OPENROUTER_MODELS,
        nvidia_models=NVIDIA_MODELS,
        inline_gemini_key=API_KEY,
        inline_openrouter_key=OPENROUTER_API_KEY,
        inline_nvidia_key=NVIDIA_API_KEY,
        timeout_ms=180000,
    )


# ─── Consultas a la API del TSJ ──────────────────────────────────────────────
def parse_fecha(f_str):
    """Convierte cadena de fecha a objeto date."""
    if not f_str:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(f_str.strip(), fmt).date()
        except Exception:
            pass
    return None


def obtener_dias_publicados(session, sala_id=SALA_ID, ano=ANO_DEFAULT):
    """
    Consulta la API del TSJ para obtener todos los días con sentencias publicadas
    para la sala y año indicados.
    Retorna lista ordenada cronológicamente de dicts.
    """
    url = API_LIST_DAYS.format(base=TSJ_BASE, sala_id=sala_id, ano=ano)
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ⚠ Error consultando días publicados del TSJ: {e}")
        return []

    dias_raw = data.get("coleccion", {}).get("DIA", [])
    if isinstance(dias_raw, dict):
        dias_raw = [dias_raw]

    dias = []
    for d in dias_raw:
        fecha_str = d.get("FECHA", "").strip()
        f_dt = parse_fecha(fecha_str)
        if not f_dt:
            continue
        try:
            cuantas = int(d.get("CUANTAS", "0"))
        except ValueError:
            cuantas = 0

        dias.append({
            "fecha": fecha_str,
            "fecha_dt": f_dt,
            "cuantas": cuantas,
            "mes_nombre": d.get("NOMBREMES", "").strip(),
            "mes_num": int(d.get("NUMEROMES", f_dt.month)),
            "dia": d.get("DIA", "").strip(),
        })

    # Ordenar cronológicamente
    dias.sort(key=lambda x: x["fecha_dt"])
    return dias


def listar_sentencias_por_fecha(session, fecha_str, sala_id=SALA_ID, return_ok=False):
    """
    Consulta la API JSON del portal Liferay para obtener la lista de
    sentencias publicadas en una fecha y sala específicas.
    Si return_ok=True, devuelve (sentencias, consulta_exitosa).
    """
    fecha_encoded = quote(fecha_str, safe="")
    url = API_LIST_DECISIONS.format(
        base=TSJ_BASE, fecha_encoded=fecha_encoded, sala_id=sala_id
    )

    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"    ⚠ Error consultando API del TSJ para {fecha_str}: {e}")
        return ([], False) if return_ok else []
    except json.JSONDecodeError:
        print(f"    ⚠ Respuesta no es JSON válido para {fecha_str}")
        return ([], False) if return_ok else []

    coleccion = data.get("coleccion") or {}
    if not isinstance(coleccion, dict):
        coleccion = {}
    sentencias_raw = coleccion.get("SENTENCIA", [])
    if isinstance(sentencias_raw, dict):
        sentencias_raw = [sentencias_raw]

    sentencias = []
    for s in sentencias_raw:
        nombre_doc = (s.get("SSENTNOMBREDOC") or "").strip()
        if not nombre_doc or nombre_doc.lower() in ("null", "none", "-"):
            continue

        match = re.match(
            r"(\d+)-(\d+)-(\d+)-(\d{4})-(.+?)\.HTML?",
            nombre_doc, re.IGNORECASE
        )
        raw_num_api = (s.get("SSENTNUMERO") or "").strip()
        if match:
            numero = match.group(2)
            expediente = match.group(5)
        elif raw_num_api and raw_num_api.lower() not in ("null", "none", "-"):
            numero = raw_num_api
            expediente = (s.get("SSENTEXPEDIENTE") or "?").strip()
        else:
            match_old = re.match(r"^([A-Za-z]?-?\d+)-", nombre_doc)
            numero = match_old.group(1) if match_old else str(s.get("SSENTID") or "?")
            expediente = (s.get("SSENTEXPEDIENTE") or "?").strip()

        if numero.isdigit():
            numero = numero.lstrip("0") or "0"

        nombre_mes = (s.get("NOMBREMES") or "").strip().lower()
        sala_dir = (s.get("SSALADIR") or SALA_DIR).strip().lower()
        doc_url = f"{TSJ_HISTORICO_BASE}/{sala_dir}/{quote(nombre_mes, safe='')}/{quote(nombre_doc, safe='().-_~')}"

        sentencias.append({
            "numero": numero,
            "fecha": fecha_str,
            "url": doc_url,
            "expediente": expediente,
            "ponente": (s.get("SPONENOMBRE") or "").strip(),
            "procedimiento": (s.get("SPROCDESCRIPCION") or "").strip(),
            "partes": (s.get("SSENTPARTES") or "").strip(),
            "decision_resumen": (s.get("SSENTDECISION") or "").strip(),
            "archivo": nombre_doc,
        })

    return (sentencias, True) if return_ok else sentencias


def descargar_texto_sentencia(session, url, return_status=False):
    """
    Descarga y extrae el texto completo de una sentencia desde historico.tsj.gob.ve.
    Si return_status=True, devuelve (texto, status_code).
    """
    try:
        resp = session.get(url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,*/*",
        }, timeout=(10, 30))
        status_code = resp.status_code
        resp.raise_for_status()

        resp.encoding = resp.apparent_encoding or "latin-1"
        soup = BeautifulSoup(resp.text, "html.parser")

        # Eliminar elementos no textuales
        for tag in soup(["script", "style", "meta", "link"]):
            tag.decompose()

        # Priorizar contenedor de texto de Word si existe
        word_section = soup.find("div", class_="WordSection1")
        if word_section:
            text = word_section.get_text(separator="\n")
        else:
            body = soup.find("body")
            text = body.get_text(separator="\n") if body else soup.get_text(separator="\n")

        # Limpiar espacios en blanco excesivos
        lines = [line.strip() for line in text.splitlines()]
        text = "\n".join(line for line in lines if line)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)

        cleaned = text.strip()
        return (cleaned, status_code) if return_status else cleaned

    except requests.RequestException as e:
        status_code = getattr(getattr(e, "response", None), "status_code", 0) or 0
        print(f"    ⚠ Error descargando sentencia: {e}")
        return ("", status_code) if return_status else ""


def extraer_ponente_de_texto(texto, ponente_api=""):
    """Extrae el ponente del texto de la sentencia si la API no lo entregó."""
    if ponente_api:
        return ponente_api
    match = re.search(
        r"[Pp]onente[:\s]+(?:Magistrad[oa]\s+)?(.+?)(?:\n|$)", texto
    )
    if match:
        return re.sub(r"\s+", " ", match.group(1).strip().strip("."))
    return ""


# ─── Aislamiento del Capítulo Decisorio ──────────────────────────────────────
def aislar_capitulo_decisorio(texto):
    """
    Aísla el capítulo decisorio de la sentencia, que corresponde al capítulo
    inmediatamente anterior a la 'DECISIÓN', 'DISPOSITIVO' o 'DISPOSITIVA'.

    En la estructura procesal de la Sala de Casación Penal del TSJ venezolano,
    este capítulo es aquel donde los Magistrados analizan el fondo, resuelven
    sobre la admisibilidad o inadmisibilidad, desestiman, declaran con o sin
    lugar, o decretan la nulidad del fallo, formulando las razones y máximas
    jurídicas originales de la Sala.

    Oリング omite los antecedentes fácticos, las transcripciones de escritos de las
    partes o jueces de instancia (capítulos iniciales) y el dispositivo resolutivo
    final.

    Retorna una tupla: (texto_capitulo, nombre_capitulo).
    """
    if not texto or len(texto) < 800:
        return texto, "TEXTO_COMPLETO"

    lines = [l.strip() for l in texto.splitlines() if l.strip()]
    total_l = len(lines)

    # 1. Localizar el inicio del capítulo final (DECISIÓN / DISPOSITIVO / DISPOSITIVA)
    # Se busca desde el final hacia arriba, dentro del último 65% del documento
    idx_final = -1
    min_search_idx = max(int(total_l * 0.35), 0)

    for i in range(total_l - 1, min_search_idx, -1):
        l_u = lines[i].upper()
        if re.search(r"^(?:[IVXLCDM]+\s*[-.:]?\s*)?(?:DECISI[OÓ]N|DISPOSITIV[OA]|PARTE\s+DISPOSITIV[OA])$", l_u):
            if i > 0 and re.match(r"^[IVXLCDM]+$", lines[i - 1]):
                idx_final = i - 1
            else:
                idx_final = i
            break

    # Si no se encontró por título explícito, buscar la fórmula sacramental de cierre
    if idx_final == -1:
        for i in range(total_l - 1, min_search_idx, -1):
            l_u = lines[i].upper()
            if any(l_u.startswith(p) for p in [
                "POR LAS RAZONES EXPUESTAS",
                "POR LAS RAZONES PRECEDENTEMENTE",
                "POR LOS MOTIVOS EXPUESTOS",
                "POR LOS MOTIVOS ANTES EXPUESTOS",
                "ADMINISTRANDO JUSTICIA EN NOMBRE DE LA REPÚBLICA"
            ]):
                idx_final = i
                break

    if idx_final == -1:
        # No se pudo ubicar el capítulo final con certeza
        return texto, "TEXTO_COMPLETO"

    # 2. Localizar el inicio del capítulo inmediatamente anterior (Ratio Decidendi de la Sala)
    # Se busca hacia atrás desde idx_final
    PATRON_TITULO = re.compile(
        r"^(?:[IVXLCDM]+\s*[-.:]\s*)?"
        r"(?:"
        r"CONSIDERACIONES(?:\s+PARA\s+DECIDIR|\s+DE\s+LA\s+SALA|\s+PREVIAS)?"
        r"|MOTIVACI[OÓ]N(?:\s+PARA\s+DECIDIR)?"
        r"|MOTIVOS?\s+PARA\s+DECIDIR"
        r"|FUNDAMENTOS?\s+PARA\s+DECIDIR"
        r"|FUNDAMENTOS?\s+DE\s+LA\s+SALA"
        r"|DE\s+LA\s+FUNDAMENTACI[OÓ]N(?:\s+DEL\s+RECURSO)?"
        r"|(?:DE\s+LA|SOBRE\s+LA)\s+ADMISIBILIDAD"
        r"|(?:DE\s+LA|SOBRE\s+LA)\s+PROCEDENCIA(?:\s+DEL\s+AVOCAMIENTO)?"
        r"|NULIDAD(?:\s+DE\s+OFICIO)?"
        r"|DEL\s+FONDO(?:\s+DEL\s+ASUNTO|\s+DE\s+LA\s+CONTROVERSIA)?"
        r"|JUICIO\s+DE\s+LA\s+SALA"
        r"|CRITERIO\s+DE\s+LA\s+SALA"
        r")$",
        re.IGNORECASE
    )

    RE_EXCLUIR = re.compile(
        r"(?:SOLICITUD|RECURRENTE|SOLICITANTE|DEFENSA|QUERELLA|ACUSACI[OÓ]N|DENUNCIA|ANTECEDENTES|HECHOS|COMPETENCIA)",
        re.IGNORECASE
    )

    idx_inicio = -1
    titulo = ""

    # Estrategia 1: Buscar hacia atrás un encabezado temático explícito del análisis de la Sala
    for i in range(idx_final - 1, 0, -1):
        l = lines[i]
        if len(l) > 80:
            continue
        if re.match(r"^V\s*-\s*\d+", l, re.IGNORECASE):
            continue

        sig = lines[i + 1] if i + 1 < idx_final and len(lines[i + 1]) < 60 else ""
        combinada = f"{l} {sig}".strip()

        # Si l es un número romano solo (ej: "VI") y la siguiente es el título
        if re.match(r"^[IVXLCDM]+$", l):
            if PATRON_TITULO.match(sig) and not RE_EXCLUIR.search(sig):
                idx_inicio = i
                titulo = f"{l} - {sig}"
                break
            sig2 = f"{sig} {lines[i + 2]}".strip() if i + 2 < idx_final and len(lines[i + 2]) < 60 else ""
            if PATRON_TITULO.match(sig2) and not RE_EXCLUIR.search(sig2):
                idx_inicio = i
                titulo = f"{l} - {sig2}"
                break

        # Título directo en una línea o combinado en 2 líneas
        if PATRON_TITULO.match(combinada) and not RE_EXCLUIR.search(combinada):
            idx_inicio = i
            titulo = combinada
            if i > 0 and re.match(r"^[IVXLCDM]+$", lines[i - 1]):
                idx_inicio = i - 1
                titulo = f"{lines[i - 1]} - {combinada}"
            break
        elif PATRON_TITULO.match(l) and not RE_EXCLUIR.search(l):
            idx_inicio = i
            titulo = f"{l} {sig}" if sig and any(sig.upper().startswith(x) for x in ["PARA DECIDIR", "DE LA SALA", "PREVIAS", "DE OFICIO"]) else l
            if i > 0 and re.match(r"^[IVXLCDM]+$", lines[i - 1]):
                idx_inicio = i - 1
                titulo = f"{lines[i - 1]} - {titulo}"
            break

    # Estrategia 2: Si no hubo coincidencia temática explícita, buscar el número romano inmediatamente anterior
    if idx_inicio == -1:
        for i in range(idx_final - 1, 0, -1):
            l = lines[i]
            if len(l) > 80:
                continue
            if re.match(r"^V\s*-\s*\d+", l, re.IGNORECASE):
                continue

            # Romano solo
            if re.match(r"^[IVXLCDM]+$", l):
                sig = lines[i + 1] if i + 1 < idx_final and len(lines[i + 1]) < 80 else ""
                if not RE_EXCLUIR.search(sig):
                    idx_inicio = i
                    titulo = f"{l} - {sig}" if sig else l
                    break

            # Romano con título
            m_romano = re.match(r"^([IVXLCDM]+)\s*[-.:]\s*(.+)$", l)
            if m_romano:
                tit_posible = m_romano.group(2)
                if not RE_EXCLUIR.search(tit_posible):
                    idx_inicio = i
                    titulo = l
                    break

    # 3. Validar y construir el texto aislado
    if idx_inicio != -1 and idx_inicio < idx_final:
        cap_lineas = lines[idx_inicio:idx_final]
        texto_cap = "\n".join(cap_lineas).strip()
        if len(texto_cap) >= 400:
            return texto_cap, titulo

    # Fallback seguro: si no se detectó el encabezado del capítulo previo,
    # pero sí el de la DECISIÓN, tomamos el bloque inmediatamente anterior a la decisión
    if idx_final > 0:
        texto_previo = "\n".join(lines[:idx_final]).strip()
        if len(texto_previo) >= 400:
            return texto_previo, "SECCIÓN_PREVIA_A_DECISIÓN"

    return texto, "TEXTO_COMPLETO"


# ─── Chunking ────────────────────────────────────────────────────────────────
def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Divide texto en fragmentos con solapamiento."""
    if len(text) <= size:
        return [text]
    chunks = []
    step = size - overlap
    for i in range(0, len(text), step):
        chunks.append(text[i : i + size])
        if i + size >= len(text):
            break
    return chunks


# ─── Clasificación de Tipo de Decisión y Pauta de Máximas ────────────────────
def clasificar_pauta_decision(info, texto=""):
    """
    Determina la categoría de la decisión y la pauta de extracción de máximas
    según las reglas establecidas:
      1. Extradiciones: No tomar nada (0 máximas).
      2. Inadmisible o Desestimado: Tomar como máximo una (1) máxima.
      3. Con Lugar o Nulidad (incluye nulidad de oficio): Tomar entre 2 y 4 máximas.
      4. Otros: Pauta estándar (máximo 2 máximas).

    Retorna: (categoria, min_maximas, max_maximas, instruccion_prompt)
    """
    proc = (info.get("procedimiento") or "").lower()
    resumen = (info.get("decision_resumen") or "").lower()
    exp = (info.get("expediente") or "").upper()

    # 1. Extradición (omisión completa: 0 máximas)
    if (
        "extradici" in proc
        or exp.startswith("E")
        or "extradici" in resumen
        or "procedimiento de extradición" in (texto[:1200].lower() if texto else "")
    ):
        return (
            "EXTRADICION",
            0,
            0,
            "EXTRADICIÓN: No tomar ninguna máxima jurídica (0 máximas)."
        )

    # Texto complementario para análisis de fondo y dispositivo resolutivo
    corpus = resumen + " " + (texto[-4000:].lower() if texto else "")

    # 2. Con Lugar o Nulidad (incluye nulidad de oficio)
    patrones_con_lugar_nulidad = [
        "con lugar",
        "ha lugar",
        "de oficio la nulidad",
        "nulidad de oficio",
        "decreta de oficio",
        "decreta la nulidad",
        "anula de oficio",
        "declarar la nulidad",
        "se anula",
    ]
    es_con_lugar_o_nulidad = any(p in corpus for p in patrones_con_lugar_nulidad)

    # 3. Inadmisible o Desestimado
    patrones_inadmisible_desestimado = [
        "inadmisible",
        "inadmisibilidad",
        "desestima",
        "desestimado",
        "desestimar",
        "desestimación",
        "manifiestamente infundad",
    ]
    es_inadmisible_o_desestimado = any(p in corpus for p in patrones_inadmisible_desestimado)

    # Prevalencia: si la decisión decreta una nulidad o declara con lugar (incluso al desestimar
    # otra denuncia concurrente), se jerarquiza el pronunciamiento de fondo
    if es_con_lugar_o_nulidad:
        instruccion = (
            "PAUTA OBLIGATORIA DE CANTIDAD (DECISIÓN CON LUGAR O NULIDAD DE OFICIO):\n"
            "Esta sentencia declara CON LUGAR un recurso/solicitud o decreta de oficio una NULIDAD. "
            "Debes extraer ENTRE 2 Y 4 MÁXIMAS JURÍDICAS que desarrollen y motiven los criterios "
            "jurídicos de fondo sentados por la Sala."
        )
        return "CON_LUGAR_O_NULIDAD", 2, 4, instruccion

    if es_inadmisible_o_desestimado:
        instruccion = (
            "PAUTA OBLIGATORIA DE CANTIDAD (DECISIÓN INADMISIBLE O DESESTIMADA):\n"
            "Esta sentencia declara INADMISIBLE o DESESTIMA por infundado un recurso o solicitud. "
            "Debes tomar COMO MÁXIMO UNA (1) MÁXIMA JURÍDICA (únicamente el criterio doctrinal más "
            "relevante sobre la causal de inadmisibilidad o desestimación). Si no sienta doctrina "
            "relevante o solo aplica requisitos formales obvios, devuelve lista vacía []. "
            "BAJO NINGUNA CIRCUNSTANCIA extraigas más de una (1) máxima."
        )
        return "INADMISIBLE_O_DESESTIMADO", 0, 1, instruccion

    # Otros procedimientos ordinarios
    instruccion = (
        "PAUTA DE CANTIDAD:\n"
        "Extrae únicamente las máximas jurídicas necesarias y relevantes (máximo 2)."
    )
    return "ORDINARIO", 1, 2, instruccion


RE_NEG_NULIDAD = re.compile(
    r"\b(?:sin lugar|inadmisible|improcedente|niega|desestima|no procede|no ha lugar)\b[^.]{0,80}\b(?:nulidad|anulaci[oó]n|casaci[oó]n\s+de\s+oficio)\b",
    re.IGNORECASE,
)
RE_POS_DISPOSITIVO_NULIDAD = re.compile(
    r"\b(?:anula\w*|nulidad\w*|anulaci[oó]n|de\s+oficio\s+con\s+lugar|casaci[oó]n\s+de\s+oficio)\b",
    re.IGNORECASE,
)
RE_POS_CIERRE_NULIDAD = re.compile(
    r"(?:esta\s+sala\s+anula|de\s+oficio\s+la\s+nulidad|decreta(?:\s+de\s+oficio)?\s+la\s+nulidad|"
    r"declara(?:\s+de\s+oficio)?\s+la\s+nulidad|anula\s+de\s+oficio|de\s+oficio\s+anula|"
    r"se\s+anula|acarre[oó]\s+(?:la|su)\s+nulidad|produce\s+la\s+nulidad|casaci[oó]n\s+de\s+oficio)",
    re.IGNORECASE,
)


def _extraer_dispositivo_y_cierre(texto):
    """Aísla el bloque dispositivo de la sentencia y las líneas finales de motivación previas."""
    if not texto:
        return "", ""
    lines = [l.strip() for l in texto.splitlines() if l.strip()]
    total_l = len(lines)
    idx_final = -1
    min_idx = max(int(total_l * 0.35), 0)

    for i in range(total_l - 1, min_idx, -1):
        lu = lines[i].upper()
        if re.search(r"^(?:[IVXLCDM]+\s*[-.:]?\s*)?(?:DECISI[OÓ]N|DISPOSITIV[OA]|PARTE\s+DISPOSITIV[OA])$", lu):
            idx_final = i
            break

    if idx_final == -1:
        for i in range(total_l - 1, min_idx, -1):
            lu = lines[i].upper()
            if any(lu.startswith(p) for p in (
                "POR LAS RAZONES EXPUESTAS",
                "POR LAS RAZONES PRECEDENTEMENTE",
                "POR LOS MOTIVOS EXPUESTOS",
                "POR LOS MOTIVOS ANTES EXPUESTOS",
                "ADMINISTRANDO JUSTICIA EN NOMBRE DE LA REPÚBLICA",
            )):
                idx_final = i
                break

    if idx_final == -1:
        return "", texto[-2500:]

    idx_fin_disp = len(lines)
    for j in range(idx_final + 1, len(lines)):
        lu = lines[j].upper()
        if any(p in lu for p in ("PUBLÍQUESE", "PUBLIQUESE", "DADA, FIRMADA", "DADA FIRMADA", "VOTO SALVADO")):
            idx_fin_disp = j
            break

    disp = " ".join(lines[idx_final:idx_fin_disp])
    cierre_prev = " ".join(lines[max(0, idx_final - 15):idx_final])
    return disp, cierre_prev


def es_sentencia_de_nulidad(decision_resumen, texto=""):
    """Determina si una sentencia declara o decreta efectivamente una nulidad."""
    dec = (decision_resumen or "").strip()
    disp, cierre_prev = _extraer_dispositivo_y_cierre(texto)
    dec_clean = RE_NEG_NULIDAD.sub("", dec)
    disp_clean = RE_NEG_NULIDAD.sub("", disp)
    cierre_clean = RE_NEG_NULIDAD.sub("", cierre_prev)

    if RE_POS_DISPOSITIVO_NULIDAD.search(dec_clean) or RE_POS_DISPOSITIVO_NULIDAD.search(disp_clean):
        return True
    if RE_POS_CIERRE_NULIDAD.search(cierre_clean):
        return True
    return False


def clasificar_pauta_historica(info, texto=""):
    """
    Pauta exclusiva del modo histórico (19/01/2000 – 30/09/2026):
      - Solo se procesan sentencias de nulidad.
      - Se extrae como máximo UNA (1) máxima jurídica por sentencia.
    """
    proc = (info.get("procedimiento") or "").lower()
    resumen = (info.get("decision_resumen") or "").lower()
    exp = (info.get("expediente") or "").upper()

    if (
        "extradici" in proc
        or exp.startswith("E")
        or "extradici" in resumen
        or "procedimiento de extradición" in (texto[:1200].lower() if texto else "")
    ):
        return ("EXTRADICION", 0, 0, "EXTRADICIÓN: No tomar ninguna máxima jurídica (0 máximas).")

    if not texto:
        return ("SIN_TEXTO", 0, 0, "")

    if not es_sentencia_de_nulidad(info.get("decision_resumen", ""), texto):
        return ("OMITIDA_NO_NULIDAD", 0, 0, "")

    instruccion = (
        "PAUTA OBLIGATORIA DE CANTIDAD (MODO HISTÓRICO - SENTENCIA DE NULIDAD):\n"
        "Esta sentencia declara o decreta una NULIDAD. Debes extraer COMO MÁXIMO UNA (1) MÁXIMA JURÍDICA: "
        "únicamente el criterio doctrinario o ratio decidendi más relevante que fundamenta la nulidad. "
        "Si no sienta doctrina relevante o es puramente formal, devuelve lista vacía []. "
        "BAJO NINGUNA CIRCUNSTANCIA extraigas más de una (1) máxima."
    )
    return ("NULIDAD_HISTORICA", 0, 1, instruccion)


# ─── Prompt de Extracción de Máximas ─────────────────────────────────────────
MAXIMAS_PROMPT = """Actúa como un experto en Derecho Procesal Penal venezolano.
Estás analizando el CAPÍTULO DECISORIO de una sentencia de la Sala de Casación Penal
del Tribunal Supremo de Justicia de Venezuela (la sección donde la Sala motiva y toma
la decisión de desestimar el recurso, declararlo inadmisible o admitirlo, decretar
una nulidad, etc., ubicada inmediatamente antes de la parte resolutiva/dispositiva).

Tu tarea es extraer las MÁXIMAS JURÍDICAS (extractos doctrinarios originales relevantes)
contenidas en este capítulo de decisión.

DEFINICIÓN DE MÁXIMA JURÍDICA:
Una máxima es un párrafo o conjunto de párrafos de la propia redacción de la
sentencia donde la Sala sienta un criterio jurídico relevante, una interpretación
normativa, un principio de derecho procesal/penal, ratio decidendi o doctrina.
Son textos ORIGINALES formulados por la Sala en esta decisión (no citas de
otras sentencias ni transcripciones de alegatos de las partes).

DATOS DE ESTA SENTENCIA:
- Número: {numero}
- Sala: Sala de Casación Penal
- Fecha: {fecha}
- Ponente: {ponente}

PAUTA OBLIGATORIA PARA ESTA SENTENCIA:
{instruccion_pauta}

Para cada máxima encontrada, devuelve un objeto JSON con estas claves:

- "numero": "{numero}" (siempre el número de esta sentencia)
- "sala": "Sala de Casación Penal"
- "fecha": "{fecha}"
- "tema": Materia o punto jurídico específico. Frase concisa de 2 a 8 palabras
  (ej: "Inadmisibilidad del recurso de casación", "Desestimación por manifiestamente infundado",
  "Falta de fundamentación fáctica", "Principio de intervención mínima", "Nulidad absoluta de oficio", etc.).
- "extracto": Resumen claro (3-5 líneas) del criterio jurídico expuesto por la Sala.
- "contenido_citado": Texto COMPLETO Y SUSTANTIVO de la máxima tal como la
  redactó la Sala. Debe ser un párrafo o conjunto de párrafos con contenido
  doctrinario suficientemente extenso y autónomo (mínimo 150 caracteres).
  NO incluyas encabezados procedimentales, citas textuales de terceros ni el dispositivo resolutivo.
- "ratificaciones": Si en esta máxima la Sala ratifica o reitera un criterio
  de sentencia(s) previa(s), indicar aquí (ej: "Ratifica a Sentencia N° 348
  del 25/06/2007"). Si no, dejar cadena vacía "".

REGLAS DE EXCLUSIÓN (MUY IMPORTANTE):
1. NO extraigas transcripciones de escritos del recurrente, denuncias, alegatos
   de las partes, ni actos de los tribunales de instancia.
2. NO extraigas la parte dispositiva del fallo (PRIMERO: ..., SEGUNDO: ...).
3. NO extraigas encabezados ni datos de identificación del expediente.
4. NO extraigas citas textuales de OTRAS sentencias (esas son citas ajenas, no máximas
   originarias de esta sentencia). Si la Sala transcribe párrafos entrecomillados o
   textuales de una sentencia anterior, eso NO es una máxima de esta decisión.
5. SÍ extrae párrafos donde la Sala establece CRITERIOS PROPIOS, interpreta
   normas, fija doctrina o razona jurídicamente el caso concreto.
6. Cada máxima debe tener al menos 150 caracteres de contenido sustantivo.

Devuelve ÚNICAMENTE un JSON válido: una lista de objetos.
NO incluyas texto adicional, explicaciones, markdown ni bloques de código.
Si no encuentras máximas válidas, devuelve: []

TEXTO DEL CAPÍTULO DECISORIO:
"""

def extract_maximas(client, primary_model, fallback_model, text_chunk,
                    numero, fecha, ponente, instruccion_pauta="", max_permitidas=0,
                    max_retries=4):
    """
    Envía el texto de una sentencia al cliente multi-proveedor para extraer máximas originales.
    Retorna (maximas_list, exito, error_msg).
    """
    prompt = MAXIMAS_PROMPT.format(
        numero=numero, fecha=fecha, ponente=ponente,
        instruccion_pauta=instruccion_pauta
    ) + text_chunk

    sentencias, ok, err = client.extract_json_list(
        prompt=prompt,
        preferred_label=primary_model,
        max_retries=max_retries,
    )
    if ok:
        if max_permitidas > 0 and len(sentencias) > max_permitidas:
            sentencias = sentencias[:max_permitidas]
        return sentencias, True, ""
    return [], False, err


# ─── Base de Datos y Seguimiento ─────────────────────────────────────────────
def asegurar_columnas_enlace(conn):
    """Agrega columnas de enlace oficial TSJ a `sentencias` si aún no existen."""
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(sentencias)")
    cols = {r[1] for r in cur.fetchall()}
    added = False
    if "enlace" not in cols:
        cur.execute("ALTER TABLE sentencias ADD COLUMN enlace TEXT DEFAULT ''")
        added = True
    if "enlace_estado" not in cols:
        cur.execute("ALTER TABLE sentencias ADD COLUMN enlace_estado TEXT DEFAULT ''")
        added = True
    if added:
        conn.commit()


def init_db():
    """Inicializa tablas en SQLite."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sentencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT NOT NULL,
            sala TEXT NOT NULL,
            fecha TEXT,
            tema TEXT,
            extracto TEXT,
            contenido_citado TEXT,
            ratificaciones TEXT,
            archivo_origen TEXT,
            enlace TEXT DEFAULT '',
            enlace_estado TEXT DEFAULT ''
        )
    """)
    asegurar_columnas_enlace(conn)
    c.execute("CREATE INDEX IF NOT EXISTS idx_sentencias_num_sala ON sentencias(numero, sala)")
    c.execute("""
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS control_web (
            clave TEXT PRIMARY KEY,
            valor TEXT,
            fecha_actualizacion TEXT
        )
    """)
    conn.commit()
    return conn


def obtener_control(conn, clave, default=None):
    """Lee un valor de la tabla de control."""
    cur = conn.cursor()
    cur.execute("SELECT valor FROM control_web WHERE clave = ?", (clave,))
    r = cur.fetchone()
    return r[0] if r else default


def guardar_control(conn, clave, valor):
    """Guarda o actualiza un valor de control."""
    conn.cursor().execute(
        """
        INSERT INTO control_web (clave, valor, fecha_actualizacion)
        VALUES (?, ?, datetime('now', 'localtime'))
        ON CONFLICT(clave) DO UPDATE SET
            valor = excluded.valor,
            fecha_actualizacion = datetime('now', 'localtime')
        """,
        (clave, str(valor)),
    )
    conn.commit()


def normalizar_texto(texto):
    """Normaliza texto para comparación semántica."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode("utf-8").lower()
    return re.sub(r"[^\w\s]", " ", texto).strip()


def son_mismo_tema(tema1, extracto1, tema2, extracto2, citado1="", citado2=""):
    """Evalúa si dos extractos corresponden al mismo tema."""
    c1 = normalizar_texto(citado1)
    c2 = normalizar_texto(citado2)
    if c1 and c2:
        if c1 in c2 or c2 in c1 or difflib.SequenceMatcher(None, c1, c2, autojunk=False).ratio() >= 0.65:
            return True

    t1 = normalizar_texto(tema1)
    t2 = normalizar_texto(tema2)
    e1 = normalizar_texto(extracto1)
    e2 = normalizar_texto(extracto2)

    if t1 and t2:
        sw = {"de","la","el","los","las","en","por","del","un","una","y","o","a","al","para","con","sobre"}
        p1 = set(t1.split()) - sw
        p2 = set(t2.split()) - sw
        rt = difflib.SequenceMatcher(None, t1, t2, autojunk=False).ratio()
        if rt >= 0.65 or (p1 and p2 and len(p1 & p2) >= min(len(p1), len(p2))):
            return True
        if rt < 0.45 and (not p1 or not p2 or not (p1 & p2)):
            return False

    if difflib.SequenceMatcher(None, e1, e2, autojunk=False).ratio() >= 0.55:
        return True

    pe1 = set(w for w in e1.split() if len(w) > 4)
    pe2 = set(w for w in e2.split() if len(w) > 4)
    if pe1 and pe2:
        j = len(pe1 & pe2) / len(pe1 | pe2)
        if j >= 0.45:
            return True
    return False


VALORES_VACIOS = {
    "", "sin cita", "sin cita textual", "sin cita textual expresa",
    "no aplica", "ninguno", "sin máxima", "sin contenido",
}

FRASES_INVALIDAS = [
    "sin cita", "omite la transcripcion", "omite la transcripción",
    "primero: competente",
]


def _extraer_anio(fecha_str):
    """Extrae el año de 4 dígitos desde una fecha en texto."""
    m = re.search(r"\b(19\d{2}|20\d{2})\b", str(fecha_str or ""))
    return m.group(1) if m else ""


def _son_misma_sentencia(num1, fecha1, num2, fecha2):
    """Compara identidad de sentencia por número y año (si ambos años son conocidos)."""
    n1 = str(num1 or "").lstrip("0") or "0"
    n2 = str(num2 or "").lstrip("0") or "0"
    if n1 != n2:
        return False
    a1 = _extraer_anio(fecha1)
    a2 = _extraer_anio(fecha2)
    if a1 and a2:
        return a1 == a2
    return True


def limpiar_y_validar_maxima(s, numero_esperado=""):
    """Normaliza y valida una máxima jurídica."""
    if not isinstance(s, dict):
        return None

    raw = str(s.get("numero", "")).replace(".", "").strip()
    num = raw.lstrip("0") or "0" if raw.isdigit() else raw
    if not num or num == "0" or not any(ch.isdigit() for ch in num):
        return None
    if numero_esperado and num != numero_esperado:
        num = numero_esperado

    sala = str(s.get("sala", SALA_NOMBRE)).strip() or SALA_NOMBRE
    citado = str(s.get("contenido_citado", "")).strip()

    if citado.lower() in VALORES_VACIOS or len(citado) < 150:
        return None
    if citado.endswith(":") or citado.endswith("[...]"):
        return None
    if any(f in citado.lower() for f in FRASES_INVALIDAS):
        return None

    # Ratificaciones
    rr = s.get("ratificaciones", "")
    if isinstance(rr, dict):
        pp = []
        if rr.get("ratifica_a"):
            pp.append(f"Ratifica a: {rr['ratifica_a']}")
        if rr.get("ratificada_por"):
            pp.append(f"Ratificada por: {rr['ratificada_por']}")
        rs = " | ".join(pp)
    else:
        rs = str(rr or "").strip()

    # Filtrar autorratificaciones
    if rs:
        sin_fechas = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", rs)
        nums = re.findall(r"\b\d+\b", sin_fechas)
        if nums and set(nums) == {num}:
            rs = ""

    return {
        "numero": num,
        "sala": sala,
        "fecha": str(s.get("fecha", "")).strip(),
        "tema": str(s.get("tema", "")).strip(),
        "extracto": str(s.get("extracto", "")).strip(),
        "contenido_citado": citado,
        "ratificaciones": rs,
    }


def actualizar_ratificaciones_ratificada_por(texto_actual, nuevo_num, nueva_fecha):
    """
    Actualiza la cadena de ratificaciones para reflejar que el criterio fue ratificado
    por una nueva sentencia ('nuevo_num', 'nueva_fecha').

    Regla estricta:
      - Coloca como máximo DOS (2) sentencias ratificadoras.
      - La última ratificación SIEMPRE debe quedar en el segundo apartado.
    """
    nuevo_num_clean = str(nuevo_num).lstrip("0") or "0"

    parte_ratifica_a = ""
    m_a = re.search(r"(Ratifica\s+(?:a|la)\s+[^|]+)", texto_actual or "", re.I)
    if m_a:
        parte_ratifica_a = m_a.group(1).strip()

    ratifs_existentes = []
    m_por = re.search(r"Ratificada\s+(?:por|en)\s+(.+?)(?:$|\|)", texto_actual or "", re.I)
    if m_por:
        bloque = m_por.group(1)
        encontrados = re.findall(
            r"(?:Sentencias?|número|N[°ºo.]?|\b)\s*(\d+)(?:\s+(?:del?|de fecha)\s+([0-9/a-záéíóú\s]+?))?(?=\s*(?:y\s+|,\s*|$))",
            bloque, re.I
        )
        for num, f in encontrados:
            num_c = num.lstrip("0") or "0"
            f_c = f.strip() if f else ""
            if num_c != nuevo_num_clean and not any(r[0] == num_c for r in ratifs_existentes):
                ratifs_existentes.append((num_c, f_c))

    nueva_fecha_clean = str(nueva_fecha).strip() if nueva_fecha else ""
    ratifs_existentes.append((nuevo_num_clean, nueva_fecha_clean))

    # Mantener como máximo 2, quedando siempre la última en el segundo apartado
    if len(ratifs_existentes) > 2:
        ratifs_existentes = [ratifs_existentes[0], ratifs_existentes[-1]]

    if len(ratifs_existentes) == 1:
        n, f = ratifs_existentes[0]
        ftxt = f" del {f}" if f else ""
        parte_por = f"Ratificada por Sentencia N° {n}{ftxt}"
    else:
        n1, f1 = ratifs_existentes[0]
        n2, f2 = ratifs_existentes[1]
        f1txt = f" del {f1}" if f1 else ""
        f2txt = f" del {f2}" if f2 else ""
        parte_por = f"Ratificada por Sentencias N° {n1}{f1txt} y N° {n2}{f2txt}"

    if parte_ratifica_a:
        return f"{parte_ratifica_a} | {parte_por}"
    return parte_por


def es_misma_maxima_inter_sentencia(r_nuevo, fila_existente):
    """
    Determina si una máxima extraída de una nueva sentencia corresponde a un criterio/máxima
    que ya fue sentado en una sentencia previa existente en la base de datos.
    """
    ex_id, ex_num, ex_sala, ex_fecha, ex_tema, ex_ext, ex_cit, ex_ratif = fila_existente

    if _son_misma_sentencia(r_nuevo.get("numero", ""), r_nuevo.get("fecha", ""), ex_num, ex_fecha):
        return False

    num_ex = str(ex_num).lstrip("0") or "0"
    anio_ex = _extraer_anio(ex_fecha)

    # 1. Mención expresa en ratificaciones: "Ratifica a Sentencia N° X del ..."
    ratif_nueva = str(r_nuevo.get("ratificaciones", "") or "")
    if ratif_nueva:
        ratif_sin_fechas = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", ratif_nueva)
        nums_ratif = [n.lstrip("0") for n in re.findall(r"\b\d+\b", ratif_sin_fechas)]
        anios_ratif = re.findall(r"\b(?:19\d{2}|20\d{2})\b", ratif_nueva)
        if num_ex in nums_ratif and (not anios_ratif or not anio_ex or anio_ex in anios_ratif):
            return True

    # 2. Contenido citado idéntico o muy similar
    c_nuevo = normalizar_texto(r_nuevo.get("contenido_citado", ""))
    c_ex = normalizar_texto(ex_cit)
    if c_nuevo and c_ex and len(c_nuevo) >= 120 and len(c_ex) >= 120:
        if c_nuevo in c_ex or c_ex in c_nuevo:
            return True
        ratio_citado = difflib.SequenceMatcher(None, c_nuevo, c_ex, autojunk=False).ratio()
        if ratio_citado >= 0.65:
            return True

    # 3. Tema idéntico y extracto con alta similitud
    t_nuevo = normalizar_texto(r_nuevo.get("tema", ""))
    t_ex = normalizar_texto(ex_tema)
    e_nuevo = normalizar_texto(r_nuevo.get("extracto", ""))
    e_ex = normalizar_texto(ex_ext)

    if t_nuevo and t_ex:
        ratio_tema = difflib.SequenceMatcher(None, t_nuevo, t_ex, autojunk=False).ratio()
        if ratio_tema >= 0.75:
            ratio_ext = difflib.SequenceMatcher(None, e_nuevo, e_ex, autojunk=False).ratio()
            if ratio_ext >= 0.60:
                return True

    return False


def insert_maximas(conn, maximas, archivo_origen, modo_historico=False, enlace=""):
    """
    Inserta o enriquece máximas en la base de datos.
    Aplica regla anti-duplicados entre sentencias distintas (diferenciando año):
    si la máxima ya existe en otra sentencia, no se inserta como fila nueva;
    si es posterior, actualiza la ratificación en el registro original, y si
    es un criterio duplicado en revisión histórica anterior, se ignora.
    """
    asegurar_columnas_enlace(conn)
    cur = conn.cursor()
    ins = upd = dup = 0
    enlace_limpio = str(enlace or "").strip()
    estado_enlace = "ENCONTRADO" if enlace_limpio else ""

    for s in maximas:
        r = limpiar_y_validar_maxima(s)
        if r is None:
            continue

        # ── 1) Buscar si coincide con la MISMA sentencia (mismo número, sala y año) ──
        cur.execute(
            "SELECT id, COALESCE(tema,''), extracto, contenido_citado, fecha, "
            "COALESCE(ratificaciones,''), COALESCE(enlace,'') FROM sentencias WHERE numero=? AND sala=?",
            (r["numero"], r["sala"]),
        )
        existentes_misma_sentencia = [
            fila for fila in cur.fetchall()
            if _son_misma_sentencia(r["numero"], r["fecha"], r["numero"], fila[4])
        ]

        coincidente = None
        for fila in existentes_misma_sentencia:
            fid, et, ee, ec, ef, er, _enl = fila
            if son_mismo_tema(r["tema"], r["extracto"], et, ee, r["contenido_citado"], ec):
                coincidente = fila
                break

        if coincidente is not None:
            # Enriquecer registro existente de la misma sentencia
            rid, xt, xe, xc, xf, xr, xenl = coincidente
            xc = xc or ""; xe = xe or ""; xf = xf or ""; xr = xr or ""; xenl = xenl or ""
            act = False
            nt, nc, ne, nf, nr, nenl = xt, xc, xe, xf, xr, xenl

            if r["tema"] and not xt:
                nt = r["tema"]; act = True
            cvalid = r["contenido_citado"] and r["contenido_citado"].lower() not in VALORES_VACIOS
            cvacio = not xc or xc.lower() in VALORES_VACIOS
            if cvalid and (cvacio or len(r["contenido_citado"]) > len(xc)):
                nc = r["contenido_citado"]; act = True
            if r["extracto"] and (len(r["extracto"]) > len(xe) or ("ratifica" in r["extracto"].lower() and "ratifica" not in xe.lower())):
                ne = r["extracto"]; act = True
            if r["fecha"] and ("sin fecha" in xf.lower() or not xf):
                nf = r["fecha"]; act = True
            if r["ratificaciones"] and len(r["ratificaciones"]) > len(xr):
                nr = r["ratificaciones"]; act = True
            if enlace_limpio and not xenl:
                nenl = enlace_limpio; act = True

            if act:
                cur.execute(
                    "UPDATE sentencias SET tema=?,contenido_citado=?,extracto=?,"
                    "fecha=?,ratificaciones=?,archivo_origen=?,enlace=?,"
                    "enlace_estado=CASE WHEN ? != '' THEN 'ENCONTRADO' ELSE COALESCE(enlace_estado,'') END "
                    "WHERE id=?",
                    (nt, nc, ne, nf, nr, archivo_origen, nenl, nenl, rid),
                )
                upd += 1
            else:
                dup += 1
            continue

        # ── 2) Buscar si esta máxima ya fue sentada en OTRA sentencia previa ──
        cur.execute(
            "SELECT id, numero, sala, fecha, COALESCE(tema,''), extracto, contenido_citado, "
            "COALESCE(ratificaciones,'') FROM sentencias WHERE sala = ?",
            (r["sala"],),
        )
        otras_sentencias = [
            fila for fila in cur.fetchall()
            if not _son_misma_sentencia(r["numero"], r["fecha"], fila[1], fila[3])
        ]

        precedente_coincidente = None
        for fila_otra in otras_sentencias:
            if es_misma_maxima_inter_sentencia(r, fila_otra):
                precedente_coincidente = fila_otra
                break

        if precedente_coincidente is not None:
            p_id, p_num, p_sala, p_fecha, p_tema, p_ext, p_cit, p_ratif = precedente_coincidente
            f_nueva_dt = parse_fecha(r["fecha"])
            f_ex_dt = parse_fecha(p_fecha)

            # Si el registro existente es más reciente (típico al revisar histórico 2000-2026)
            # o en modo histórico ya existe el criterio, ignorar el duplicado sin alterar cronología
            if (f_nueva_dt and f_ex_dt and f_nueva_dt < f_ex_dt) or modo_historico:
                print(f"\n      ⏭️ Criterio duplicado (ya existe en Sentencia N° {p_num} del {p_fecha}). Se ignora.")
                dup += 1
                continue

            nuevas_ratif = actualizar_ratificaciones_ratificada_por(
                p_ratif, r["numero"], r["fecha"]
            )
            cur.execute(
                "UPDATE sentencias SET ratificaciones = ? WHERE id = ?",
                (nuevas_ratif, p_id),
            )
            print(f"\n      🔗 Criterio preexistente en Sentencia N° {p_num}. Se actualizó ratificación: '{nuevas_ratif}'")
            upd += 1
            continue

        # ── 3) Es una máxima nueva original: Insertar nuevo registro ──
        cur.execute(
            "INSERT INTO sentencias (numero,sala,fecha,tema,extracto,"
            "contenido_citado,ratificaciones,archivo_origen,enlace,enlace_estado) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["numero"], r["sala"], r["fecha"], r["tema"], r["extracto"],
             r["contenido_citado"], r["ratificaciones"], archivo_origen,
             enlace_limpio, estado_enlace),
        )
        ins += 1

    conn.commit()
    return ins, upd, dup


def guardar_estado_bloque(conn, archivo, bloque_idx, total, estado, n=0, err=""):
    conn.cursor().execute(
        "INSERT INTO estado_bloques (archivo,bloque_idx,total_bloques,estado,"
        "sentencias_detectadas,error_detalle,fecha_actualizacion) "
        "VALUES (?,?,?,?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(archivo,bloque_idx) DO UPDATE SET "
        "estado=excluded.estado,sentencias_detectadas=excluded.sentencias_detectadas,"
        "error_detalle=excluded.error_detalle,fecha_actualizacion=datetime('now','localtime')",
        (archivo, bloque_idx, total, estado, n, err),
    )
    conn.commit()


def obtener_bloques_a_procesar(conn, archivo, total, reprocess=False):
    cur = conn.cursor()
    cur.execute("SELECT total_bloques FROM estado_bloques WHERE archivo=? LIMIT 1", (archivo,))
    prev = cur.fetchone()
    if reprocess or (prev and prev[0] != total):
        cur.execute("DELETE FROM estado_bloques WHERE archivo=?", (archivo,))
        conn.commit()
        return list(range(1, total + 1))
    cur.execute("SELECT bloque_idx FROM estado_bloques WHERE archivo=? AND estado='EXITOSO'", (archivo,))
    ok = {r[0] for r in cur.fetchall()}
    return [i for i in range(1, total + 1) if i not in ok]


def count_sentencias(conn):
    c = conn.cursor()
    c.execute("SELECT COUNT(*), COUNT(DISTINCT numero||'_'||sala||'_'||COALESCE(fecha,'')) FROM sentencias")
    return c.fetchone()


def export_csv(conn):
    """Exporta la tabla sentencias a CSV compatible con Excel."""
    asegurar_columnas_enlace(conn)
    cur = conn.cursor()
    cur.execute(
        "SELECT id,numero,sala,fecha,COALESCE(tema,''),extracto,contenido_citado,"
        "COALESCE(ratificaciones,''),archivo_origen,COALESCE(enlace,'') FROM sentencias ORDER BY id"
    )
    rows = cur.fetchall()
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["ID","Número","Sala","Fecha","Tema / Punto Jurídico",
                     "Extracto (Resumen)","Contenido Citado (Textual)",
                     "Ratificaciones / Precedentes","Archivo Origen","Enlace TSJ"])
        w.writerows(rows)
    print(f"\n📊 CSV exportado: {CSV_PATH}  ({len(rows)} registros)")


# ─── Determinación Inteligente de Fechas ─────────────────────────────────────
def _reactivar_404_actuales_pendientes(conn):
    """
    Reactiva sentencias recientes (>= 01/10/2026) que pudieron quedar marcadas como
    'Omitida: HTML 404' antes de que el TSJ publicara sus HTML definitivos.
    """
    c = conn.cursor()
    c.execute(
        "SELECT id, archivo FROM estado_bloques "
        "WHERE estado = 'EXITOSO' AND error_detalle = 'Omitida: HTML 404'"
    )
    ids_reactivar = []
    for row_id, arch in c.fetchall():
        m = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", arch or "")
        if not m:
            continue
        f_dt = parse_fecha(f"{m.group(1)}/{m.group(2)}/{m.group(3)}")
        if f_dt and f_dt >= FECHA_ACTUAL_INICIO:
            ids_reactivar.append(row_id)

    if ids_reactivar:
        placeholders = ",".join("?" for _ in ids_reactivar)
        c.execute(
            f"UPDATE estado_bloques SET estado = 'ERROR', "
            f"error_detalle = 'HTML 404 pendiente (esperando publicación en TSJ)' "
            f"WHERE id IN ({placeholders})",
            ids_reactivar,
        )
        conn.commit()
        print(f"🔄 Se reactivaron {len(ids_reactivar)} sentencia(s) reciente(s) con HTML 404 previo para reintentar.")


def _obtener_fechas_estado_bloques(conn, fecha_min=None, fecha_max=None):
    """
    Lee de estado_bloques las fechas con errores activos y las completadas,
    filtradas opcionalmente al rango [fecha_min, fecha_max] para no mezclar
    el seguimiento actual con el histórico.
    """
    c = conn.cursor()
    c.execute("SELECT DISTINCT archivo FROM estado_bloques WHERE estado = 'ERROR'")
    fechas_con_error = set()
    for (arch,) in c.fetchall():
        m = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", arch or "")
        if not m:
            continue
        f_str = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
        f_dt = parse_fecha(f_str)
        if not f_dt:
            continue
        if fecha_min and f_dt < fecha_min:
            continue
        if fecha_max and f_dt > fecha_max:
            continue
        fechas_con_error.add(f_str)

    c.execute("SELECT archivo, min(estado) FROM estado_bloques GROUP BY archivo")
    completadas_por_fecha = {}
    for arch, min_est in c.fetchall():
        if min_est != "EXITOSO":
            continue
        m = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", arch or "")
        if not m:
            continue
        f_str = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
        f_dt = parse_fecha(f_str)
        if not f_dt:
            continue
        if fecha_min and f_dt < fecha_min:
            continue
        if fecha_max and f_dt > fecha_max:
            continue
        completadas_por_fecha[f_str] = completadas_por_fecha.get(f_str, 0) + 1

    return fechas_con_error, completadas_por_fecha


def determinar_dias_historicos(conn, session):
    """
    Determina los días pendientes del modo histórico (19/01/2000 – 30/09/2026)
    usando su propio cursor paralelo (`ultima_fecha_historica_003`).
    Retorna (dias_a_procesar, hay_mas_despues).
    """
    if obtener_control(conn, CLAVE_HIST_COMPLETADO) == "1":
        print("\n✨ La revisión histórica de nulidades (19/01/2000 – 30/09/2026) ya fue completada.")
        return [], False

    ultima_hist_str = obtener_control(conn, CLAVE_CONTROL_HIST)
    ultima_hist_dt = parse_fecha(ultima_hist_str) if ultima_hist_str else None
    fechas_con_error_hist, _ = _obtener_fechas_estado_bloques(conn, FECHA_HIST_INICIO, FECHA_HIST_FIN)

    anio_inicio = FECHA_HIST_INICIO.year
    if ultima_hist_dt:
        anio_inicio = max(FECHA_HIST_INICIO.year, ultima_hist_dt.year)
    if fechas_con_error_hist:
        min_err_year = min(parse_fecha(f).year for f in fechas_con_error_hist if parse_fecha(f))
        anio_inicio = min(anio_inicio, max(FECHA_HIST_INICIO.year, min_err_year))

    print("\n" + "=" * 60)
    print("🕰️  MODO HISTÓRICO DE RESPALDO (SOLO NULIDADES)")
    print(f"   Rango histórico: 19/01/2000 al 30/09/2026")
    print(f"   Cursor histórico actual: {ultima_hist_str or 'Inicio (19/01/2000)'}")
    print("   Regla: Solo sentencias de nulidad · Máximo 1 máxima/sentencia")
    if fechas_con_error_hist:
        print(f"   🔄 Fechas históricas con error previo: {', '.join(sorted(fechas_con_error_hist))}")
    print("=" * 60)

    dias_hist_dict = {}
    hay_mas_despues = False

    for ano in range(anio_inicio, FECHA_HIST_FIN.year + 1):
        dias_ano = obtener_dias_publicados(session, SALA_ID, str(ano))
        for d in dias_ano:
            f_dt = d["fecha_dt"]
            if f_dt < FECHA_HIST_INICIO or f_dt > FECHA_HIST_FIN:
                continue
            f = d["fecha"]
            if f in fechas_con_error_hist or ultima_hist_dt is None or f_dt > ultima_hist_dt:
                dias_hist_dict[f] = d

        if len(dias_hist_dict) >= HIST_MAX_DIAS_POR_RUN:
            if ano < FECHA_HIST_FIN.year:
                hay_mas_despues = True
            break

    dias_finales = sorted(dias_hist_dict.values(), key=lambda x: x["fecha_dt"])
    if len(dias_finales) > HIST_MAX_DIAS_POR_RUN:
        dias_finales = dias_finales[:HIST_MAX_DIAS_POR_RUN]
        hay_mas_despues = True

    if not dias_finales:
        print("\n✨ Revisión histórica (19/01/2000 – 30/09/2026) completada sin fechas pendientes.")
        return [], False

    print(f"\n📋 Días históricos en cola para esta ejecución ({len(dias_finales)} fecha(s)):")
    for d in dias_finales[:10]:
        tag = " (🔄 reintentando)" if d["fecha"] in fechas_con_error_hist else ""
        print(f"   • {d['fecha']} ({d['cuantas']} sentencias){tag}")
    if len(dias_finales) > 10:
        print(f"   ... y {len(dias_finales) - 10} fecha(s) más (hasta {dias_finales[-1]['fecha']}).")

    return dias_finales, hay_mas_despues


def determinar_dias_a_procesar(conn, session, args):
    """
    Determina qué fechas se deben procesar según argumentos o seguimiento dual:
      - Modo actual (desde 01/10/2026 en adelante): usa `ultima_fecha_procesada_003`.
      - Si no hay nuevas sentencias ni errores pendientes en el modo actual,
        activa automáticamente el modo histórico (19/01/2000 – 30/09/2026)
        usando su propio cursor `ultima_fecha_historica_003`.
    Retorna: (dias_a_procesar, modo_historico, hay_mas_historico)
    """
    # 1) Manejo de reinicios de control
    if "--reset-fecha" in args:
        conn.cursor().execute("DELETE FROM control_web WHERE clave = ?", (CLAVE_CONTROL_ACTUAL,))
        conn.commit()
        print("🔄 Se ha reiniciado el registro de última fecha actual.")

    if "--reset-historico" in args:
        conn.cursor().execute(
            "DELETE FROM control_web WHERE clave IN (?, ?)",
            (CLAVE_CONTROL_HIST, CLAVE_HIST_COMPLETADO),
        )
        conn.commit()
        print("🔄 Se ha reiniciado el registro de fecha histórica.")

    # Modo histórico forzado manualmente
    if "--historico" in args:
        dias_hist, hay_mas_hist = determinar_dias_historicos(conn, session)
        return dias_hist, True, hay_mas_hist

    anio_tope = max(int(ANO_DEFAULT), date.today().year)
    dias_publicados = []
    print("\n🌐 Consultando calendario de sentencias publicadas en el TSJ...")
    for ano in range(int(ANO_DEFAULT), anio_tope + 1):
        dias_ano = obtener_dias_publicados(session, SALA_ID, str(ano))
        dias_publicados.extend(dias_ano)

    dias_publicados.sort(key=lambda x: x["fecha_dt"])

    if not dias_publicados:
        print("⚠  No se pudieron obtener las fechas publicadas desde la API del TSJ.")
        return [], False, False

    print(f"📅 El portal del TSJ registra {len(dias_publicados)} días con sentencias desde {ANO_DEFAULT} para la {SALA_NOMBRE}.")

    # 2) Argumento explícito: --fecha DD/MM/AAAA
    if "--fecha" in args:
        idx = args.index("--fecha")
        if idx + 1 < len(args):
            f_target = args[idx + 1].strip()
            f_dt = parse_fecha(f_target)
            if f_dt and str(f_dt.year) != ANO_DEFAULT:
                dias_target_ano = obtener_dias_publicados(session, SALA_ID, str(f_dt.year))
                dias = [d for d in dias_target_ano if d["fecha"] == f_target]
            else:
                dias = [d for d in dias_publicados if d["fecha"] == f_target]
            if dias:
                print(f"🎯 Modo fecha específica: {f_target}")
                return dias, False, False
            print(f"⚠ La fecha {f_target} no tiene sentencias registradas en la API del TSJ.")
            if f_dt:
                return ([{"fecha": f_target, "fecha_dt": f_dt, "cuantas": 0, "mes_nombre": "", "mes_num": f_dt.month, "dia": str(f_dt.day)}], False, False)
            return [], False, False

    # 3) Argumento explícito: --mes MM / nombre
    if "--mes" in args:
        idx = args.index("--mes")
        if idx + 1 < len(args):
            m_arg = args[idx + 1].strip().lower()
            m_num = MESES_MAP.get(m_arg)
            if m_num:
                dias = [d for d in dias_publicados if d["mes_num"] == m_num]
                print(f"🎯 Modo mes específico: Mes {m_num} ({len(dias)} días encontrados)")
                return dias, False, False
            print(f"⚠ Mes no reconocido: {m_arg}")
            return [], False, False

    # 4) Argumento explícito: --desde DD/MM/AAAA
    if "--desde" in args:
        idx = args.index("--desde")
        if idx + 1 < len(args):
            d_str = args[idx + 1].strip()
            d_dt = parse_fecha(d_str)
            if d_dt:
                dias = [d for d in dias_publicados if d["fecha_dt"] >= d_dt]
                print(f"🎯 Modo rango: Desde {d_str} en adelante ({len(dias)} días a procesar)")
                return dias, False, False

    # 5) Argumento explícito: --todas
    if "--todas" in args or "--todo-el-ano" in args:
        print(f"🎯 Modo completo: Todas las fechas desde {ANO_DEFAULT} ({len(dias_publicados)} días)")
        return dias_publicados, False, False

    # 6) MODO AUTOMÁTICO (Actual >= Oct 2026) con transición a Histórico si está al día
    _reactivar_404_actuales_pendientes(conn)
    ultima_fecha_str = obtener_control(conn, CLAVE_CONTROL_ACTUAL)
    fechas_con_error, completadas_por_fecha = _obtener_fechas_estado_bloques(
        conn, fecha_min=FECHA_ACTUAL_INICIO
    )

    dias_a_procesar_dict = {}

    # A. Fechas actuales (>= Oct 2026) con errores activos
    for d in dias_publicados:
        if d["fecha_dt"] >= FECHA_ACTUAL_INICIO and d["fecha"] in fechas_con_error:
            dias_a_procesar_dict[d["fecha"]] = d

    # B. Fechas nuevas o iniciales (>= Oct 2026)
    if not ultima_fecha_str:
        dias_octubre = [d for d in dias_publicados if d["fecha_dt"] >= FECHA_ACTUAL_INICIO and d["mes_num"] == 10]
        for d in dias_octubre:
            dias_a_procesar_dict[d["fecha"]] = d
        print("\n" + "=" * 60)
        print("📌 PRIMERA EJECUCIÓN DETECTADA (MODO ACTUAL)")
        print(f"   Objetivo inicial: Todas las sentencias de OCTUBRE {ANO_DEFAULT}")
        print(f"   Días encontrados: {len(dias_a_procesar_dict)} fechas")
        print("=" * 60)
    else:
        ultima_dt = parse_fecha(ultima_fecha_str)
        for d in dias_publicados:
            if d["fecha_dt"] < FECHA_ACTUAL_INICIO:
                continue
            f = d["fecha"]
            if d["fecha_dt"] > ultima_dt:
                dias_a_procesar_dict[f] = d
            elif d["fecha_dt"] == ultima_dt and f in completadas_por_fecha and completadas_por_fecha[f] < d.get("cuantas", 0):
                dias_a_procesar_dict[f] = d

        print("\n" + "=" * 60)
        print("📌 MODO INCREMENTAL ACTUAL (DESDE OCTUBRE 2026)")
        print(f"   Última fecha actual completada: {ultima_fecha_str}")
        if fechas_con_error:
            print(f"   🔄 Días actuales pendientes por reintentar: {', '.join(sorted(fechas_con_error))}")
        print("=" * 60)

    dias_finales = sorted(dias_a_procesar_dict.values(), key=lambda x: x["fecha_dt"])

    if dias_finales:
        print(f"\n📋 Días actuales a procesar en esta ejecución ({len(dias_finales)} fecha(s)):")
        for d in dias_finales:
            extra_info = " (🔄 reintentando pendientes)" if (d["fecha"] in fechas_con_error or (d["fecha"] in completadas_por_fecha and completadas_por_fecha[d["fecha"]] < d.get("cuantas", 0))) else " (🆕 nueva fecha)"
            print(f"   • {d['fecha']} ({d['cuantas']} sentencias){extra_info}")
        return dias_finales, False, False

    print(f"\n✨ ¡Estás al día en sentencias nuevas! No hay publicaciones posteriores al {ultima_fecha_str}.")
    print("   Pasando automáticamente a la revisión histórica de nulidades (19/01/2000 – 30/09/2026)...")
    dias_hist, hay_mas_hist = determinar_dias_historicos(conn, session)
    return dias_hist, True, hay_mas_hist


# ─── Main ─────────────────────────────────────────────────────────────────────
def _procesar_lote_dias(
    conn,
    session,
    client,
    primary,
    fallback,
    dias_a_procesar,
    modo_historico,
    hay_mas_historico,
    reprocess_all,
    start_time,
):
    """Procesa una lista de días (en modo actual o en modo histórico de nulidades)."""
    ins_total = 0
    upd_total = 0
    dup_total = 0
    sentencias_total = 0
    dias_completados = 0
    textos_descargados = 0

    clave_control = CLAVE_CONTROL_HIST if modo_historico else CLAVE_CONTROL_ACTUAL
    limite_diario_alcanzado = False
    tiempo_limite_alcanzado = False
    bloqueo_avance_control = False

    for dia_idx, dia_info in enumerate(dias_a_procesar, 1):
        if limite_diario_alcanzado or tiempo_limite_alcanzado:
            break
        if modo_historico and (time.monotonic() - start_time) / 60.0 >= HIST_MAX_MINUTOS_POR_RUN:
            print(f"\n⏱️  Tope preventivo de tiempo ({HIST_MAX_MINUTOS_POR_RUN} min) alcanzado. Guardando avance histórico...")
            tiempo_limite_alcanzado = True
            break

        fecha_str = dia_info["fecha"]
        modo_etiqueta = "HISTÓRICO NULIDADES" if modo_historico else "ACTUAL"
        print(f"\n{'━' * 60}")
        print(f"📅 [{dia_idx}/{len(dias_a_procesar)}] ({modo_etiqueta}) PROCESANDO DÍA: {fecha_str}")
        print(f"   Decisiones esperadas en portal: {dia_info['cuantas']}")
        print(f"{'━' * 60}")

        sentencias, api_ok = listar_sentencias_por_fecha(session, fecha_str, SALA_ID, return_ok=True)
        if not api_ok:
            print(f"   ⚠ No se pudo consultar la lista de sentencias para {fecha_str}.")
            bloqueo_avance_control = True
            continue
        if not sentencias:
            print(f"   ℹ Sin documentos descargables válidos para {fecha_str}.")
            if not bloqueo_avance_control:
                ultima_guardada = obtener_control(conn, clave_control)
                ultima_guardada_dt = parse_fecha(ultima_guardada) if ultima_guardada else None
                if not ultima_guardada_dt or dia_info["fecha_dt"] > ultima_guardada_dt:
                    guardar_control(conn, clave_control, fecha_str)
                    print(f"   📌 Control ({clave_control}) actualizado = {fecha_str}")
            dias_completados += 1
            continue

        print(f"   📥 Sentencias listadas para este día: {len(sentencias)}")

        hubo_errores_graves_en_dia = False

        for idx, info in enumerate(sentencias, 1):
            if limite_diario_alcanzado or tiempo_limite_alcanzado:
                break
            if modo_historico and (time.monotonic() - start_time) / 60.0 >= HIST_MAX_MINUTOS_POR_RUN:
                print(f"\n⏱️  Tope preventivo de tiempo ({HIST_MAX_MINUTOS_POR_RUN} min) alcanzado a mitad del día.")
                tiempo_limite_alcanzado = True
                hubo_errores_graves_en_dia = True
                break

            numero = info["numero"]
            url = info["url"]
            ponente = info["ponente"]
            archivo_id = f"TSJ_Web_SCP_{numero}_{fecha_str.replace('/', '-')}"

            print(f"\n  📖 [{idx}/{len(sentencias)}] Sentencia N° {numero} del {fecha_str}")
            if ponente:
                print(f"     👤 Ponente: {ponente}")
            if info.get("procedimiento"):
                print(f"     📋 Procedimiento: {info['procedimiento']}")

            # 1. Filtro previo por procedimiento: Extradición
            cat_pre, _, _, _ = clasificar_pauta_decision(info)
            if cat_pre == "EXTRADICION":
                print("     🚫 Decisión de Extradición: se omite la extracción según directriz (0 máximas).")
                guardar_estado_bloque(conn, archivo_id, 1, 1, "EXITOSO", 0, "Omitida: Extradición")
                sentencias_total += 1
                continue

            # Descargar texto
            print("     📥 Descargando texto...", end=" ", flush=True)
            texto, status_code = descargar_texto_sentencia(session, url, return_status=True)
            if not texto:
                if status_code == 404:
                    if modo_historico:
                        print("⚠ HTML no disponible (404), se omite.", flush=True)
                        guardar_estado_bloque(conn, archivo_id, 1, 1, "EXITOSO", 0, "Omitida: HTML 404")
                        sentencias_total += 1
                    else:
                        print("⏳ HTML aún no publicado (404) en TSJ; queda pendiente para reintento.", flush=True)
                        guardar_estado_bloque(
                            conn, archivo_id, 1, 1, "ERROR", 0,
                            "HTML 404 pendiente (esperando publicación en TSJ)",
                        )
                        hubo_errores_graves_en_dia = True
                else:
                    print("✘ No se pudo descargar", flush=True)
                    hubo_errores_graves_en_dia = True
                continue
            textos_descargados += 1
            print(f"✔ ({len(texto):,} caracteres)", flush=True)

            if not ponente:
                ponente = extraer_ponente_de_texto(texto)
                if ponente:
                    print(f"     👤 Ponente detectado: {ponente}")

            # 2. Clasificar tipo de decisión y pauta de máximas según modo
            if modo_historico:
                categoria, min_m, max_m, instruccion_pauta = clasificar_pauta_historica(info, texto)
                if categoria == "EXTRADICION":
                    print("     🚫 Decisión de Extradición: se omite (0 máximas).")
                    guardar_estado_bloque(conn, archivo_id, 1, 1, "EXITOSO", 0, "Omitida: Extradición")
                    sentencias_total += 1
                    continue
                if categoria == "OMITIDA_NO_NULIDAD":
                    print("     ⏭️  No es sentencia de nulidad: se omite (modo histórico).")
                    guardar_estado_bloque(conn, archivo_id, 1, 1, "EXITOSO", 0, "Omitida: No es nulidad")
                    sentencias_total += 1
                    continue
            else:
                categoria, min_m, max_m, instruccion_pauta = clasificar_pauta_decision(info, texto)
                if categoria == "EXTRADICION":
                    print("     🚫 Decisión de Extradición: se omite la extracción según directriz (0 máximas).")
                    guardar_estado_bloque(conn, archivo_id, 1, 1, "EXITOSO", 0, "Omitida: Extradición")
                    sentencias_total += 1
                    continue

            print(f"     ⚖️  Tipo de fallo: {categoria} (Pauta: {min_m} a {max_m} máximas)")

            # Aislar el capítulo decisorio previo a la DECISIÓN / DISPOSITIVO
            texto_decisorio, nombre_cap = aislar_capitulo_decisorio(texto)
            print(f"     🎯 Capítulo decisorio: '{nombre_cap}' ({len(texto_decisorio):,} de {len(texto):,} caracteres)", flush=True)

            chunks = chunk_text(texto_decisorio)
            total_chunks = len(chunks)
            pendientes = obtener_bloques_a_procesar(conn, archivo_id, total_chunks, reprocess_all)

            print(f"     📄 Bloques: {total_chunks} total, {len(pendientes)} pendiente(s)", flush=True)

            if not pendientes:
                print("     ✨ Esta sentencia ya estaba procesada exitosamente.")
                sentencias_total += 1
                continue

            maximas_sentencia = []
            bloques_error = []
            for count, c_num in enumerate(pendientes, 1):
                if max_m > 0 and len(maximas_sentencia) >= max_m:
                    print(f"     ⏩ Cuota alcanzada ({len(maximas_sentencia)}/{max_m} máximas). Omitiendo bloque {c_num}.", flush=True)
                    guardar_estado_bloque(conn, archivo_id, c_num, total_chunks, "EXITOSO", 0, "Cuota alcanzada")
                    continue

                chunk = chunks[c_num - 1]
                print(f"     🔄 [{count}/{len(pendientes)}] Analizando bloque {c_num}/{total_chunks}...", end=" ", flush=True)

                cupo_disponible = max_m - len(maximas_sentencia) if max_m > 0 else 0
                try:
                    maximas, ok, err = extract_maximas(
                        client, primary, fallback, chunk, numero, fecha_str, ponente,
                        instruccion_pauta=instruccion_pauta, max_permitidas=cupo_disponible,
                    )
                except LimiteDiarioExcedidoError as e:
                    print(f"\n     🛑 {e}", flush=True)
                    limite_diario_alcanzado = True
                    hubo_errores_graves_en_dia = True
                    guardar_estado_bloque(conn, archivo_id, c_num, total_chunks, "ERROR", 0, str(e))
                    break

                if ok:
                    guardar_estado_bloque(conn, archivo_id, c_num, total_chunks, "EXITOSO", len(maximas))
                    print(f"✔ ({len(maximas)} máximas)", flush=True)
                    if maximas:
                        for m in maximas:
                            m["numero"] = numero
                            m["fecha"] = fecha_str
                        i, u, d = insert_maximas(conn, maximas, archivo_id, modo_historico=modo_historico, enlace=url)
                        ins_total += i
                        upd_total += u
                        dup_total += d
                        maximas_sentencia.extend(maximas)
                else:
                    guardar_estado_bloque(conn, archivo_id, c_num, total_chunks, "ERROR", 0, err)
                    bloques_error.append(c_num)
                    print(f"✘ {err[:60]}...", flush=True)

                time.sleep(2)

            if limite_diario_alcanzado or tiempo_limite_alcanzado:
                sentencias_total += 1
                break

            # Pase de recuperación para bloques con error
            if bloques_error and (max_m == 0 or len(maximas_sentencia) < max_m):
                print(f"\n     ⚠️  {len(bloques_error)} bloque(s) con error. Iniciando recuperación...", flush=True)
                time.sleep(6)
                for r in list(bloques_error):
                    if max_m > 0 and len(maximas_sentencia) >= max_m:
                        break
                    print(f"     🔁 Reintentando bloque {r}...", end=" ", flush=True)
                    cupo_disponible = max_m - len(maximas_sentencia) if max_m > 0 else 0
                    try:
                        maximas, ok, err = extract_maximas(
                            client, fallback, primary, chunks[r - 1], numero, fecha_str, ponente,
                            instruccion_pauta=instruccion_pauta, max_permitidas=cupo_disponible,
                            max_retries=5,
                        )
                    except LimiteDiarioExcedidoError as e:
                        print(f"\n     🛑 {e}", flush=True)
                        limite_diario_alcanzado = True
                        hubo_errores_graves_en_dia = True
                        guardar_estado_bloque(conn, archivo_id, r, total_chunks, "ERROR", 0, str(e))
                        break

                    if ok:
                        guardar_estado_bloque(conn, archivo_id, r, total_chunks, "EXITOSO", len(maximas))
                        print(f"✔ RECUPERADO ({len(maximas)} máximas)", flush=True)
                        bloques_error.remove(r)
                        if maximas:
                            for m in maximas:
                                m["numero"] = numero
                                m["fecha"] = fecha_str
                            i, u, d = insert_maximas(conn, maximas, archivo_id, modo_historico=modo_historico, enlace=url)
                            ins_total += i
                            upd_total += u
                            dup_total += d
                            maximas_sentencia.extend(maximas)
                    else:
                        guardar_estado_bloque(conn, archivo_id, r, total_chunks, "ERROR", 0, err)
                        print("✘ Fallido.", flush=True)
                        hubo_errores_graves_en_dia = True
                    time.sleep(3)

            sentencias_total += 1

        # Al completar el día, verificar si quedan errores pendientes en este día
        fecha_sufijo_guion = fecha_str.replace("/", "-")
        c = conn.cursor()
        c.execute(
            "SELECT count(*) FROM estado_bloques WHERE (archivo LIKE ? OR archivo LIKE ?) AND estado = 'ERROR'",
            (f"%{fecha_str}%", f"%{fecha_sufijo_guion}%")
        )
        errores_restantes = c.fetchone()[0]

        if errores_restantes == 0 and not hubo_errores_graves_en_dia and not limite_diario_alcanzado and not tiempo_limite_alcanzado:
            if not bloqueo_avance_control:
                ultima_guardada = obtener_control(conn, clave_control)
                ultima_guardada_dt = parse_fecha(ultima_guardada) if ultima_guardada else None
                fecha_actual_dt = dia_info["fecha_dt"]

                if not ultima_guardada_dt or fecha_actual_dt > ultima_guardada_dt:
                    guardar_control(conn, clave_control, fecha_str)
                    print(f"\n  📌 Control ({clave_control}) actualizado: última fecha registrada = {fecha_str}")
            else:
                print(f"\n  ⏸️  Control ({clave_control}) retenido porque un día previo en esta ejecución falló al consultar la API.")
            dias_completados += 1
        else:
            bloqueo_avance_control = True
            print(f"\n  ⚠️  El día {fecha_str} tiene pendientes o se detuvo antes de concluir.")
            print(f"     El control ({clave_control}) NO avanza para retomarlo en la próxima ejecución.")

    if (
        modo_historico
        and not hay_mas_historico
        and not limite_diario_alcanzado
        and not tiempo_limite_alcanzado
        and not bloqueo_avance_control
        and dias_completados == len(dias_a_procesar)
    ):
        guardar_control(conn, CLAVE_HIST_COMPLETADO, "1")
        print("\n🏁 Revisión histórica (19/01/2000 – 30/09/2026) marcada como completada.")

    return {
        "ins": ins_total,
        "upd": upd_total,
        "dup": dup_total,
        "sentencias": sentencias_total,
        "dias_completados": dias_completados,
        "dias_total": len(dias_a_procesar),
        "textos_descargados": textos_descargados,
        "limite_diario": limite_diario_alcanzado,
        "tiempo_limite": tiempo_limite_alcanzado,
    }


def main():
    print("=" * 60)
    print("  EXTRACTOR WEB DE JURISPRUDENCIA – SALA DE CASACIÓN PENAL")
    print("  Tribunal Supremo de Justicia de Venezuela")
    print("  Extracción de Máximas Jurídicas Originales")
    print("=" * 60)

    args = sys.argv[1:]
    reprocess_all = "--reprocesar-todo" in args or "--todos" in args
    es_modo_explicito = any(
        a in args for a in ("--fecha", "--mes", "--desde", "--todas", "--todo-el-ano", "--historico")
    )

    conn = init_db()
    session = requests.Session()
    session.verify = False

    # 1) Determinar días a procesar (modo actual o respaldo histórico de nulidades)
    dias_a_procesar, modo_historico, hay_mas_historico = determinar_dias_a_procesar(conn, session, args)

    if not dias_a_procesar:
        if modo_historico and not hay_mas_historico and obtener_control(conn, CLAVE_HIST_COMPLETADO) != "1":
            guardar_control(conn, CLAVE_HIST_COMPLETADO, "1")
        conn.close()
        return

    # 2) Inicializar cliente multi-proveedor y seleccionar modelos
    print("\n🤖 Inicializando conexión con los proveedores de IA...")
    client = init_client()
    print("\n🔍 Verificando modelos disponibles (primario y respaldo)...")
    primary, fallback = select_model(client)
    print(f"\n✅ Modelo Primario: {primary}")
    print(f"🛡️  Modelo de Respaldo: {fallback}")
    or_backup = client.describe_openrouter_backup()
    if or_backup and not primary.startswith("openrouter:"):
        print(f"🔀 Respaldo OpenRouter: {or_backup}")
    nv_backup = client.describe_nvidia_backup()
    if nv_backup and not primary.startswith("nvidia:"):
        print(f"🟢 Respaldo NVIDIA NIM: {nv_backup}")

    # 3) Procesar día a día
    start_time = time.monotonic()
    res = _procesar_lote_dias(
        conn, session, client, primary, fallback,
        dias_a_procesar, modo_historico, hay_mas_historico, reprocess_all, start_time,
    )

    total_general_ins = res["ins"]
    total_general_upd = res["upd"]
    total_general_dup = res["dup"]
    total_sentencias_procesadas = res["sentencias"]
    dias_completados_en_run = res["dias_completados"]
    dias_totales_en_run = res["dias_total"]
    limite_diario_alcanzado = res["limite_diario"]
    tiempo_limite_alcanzado = res["tiempo_limite"]
    modo_resumen = "HISTÓRICO NULIDADES" if modo_historico else "ACTUAL"

    # Si en modo automático actual todas las sentencias nuevas devolvieron 404 (aún sin HTML),
    # quedan marcadas como pendientes y aprovechamos la cuota de hoy en el modo histórico.
    if (
        not modo_historico
        and not es_modo_explicito
        and res["textos_descargados"] == 0
        and not limite_diario_alcanzado
    ):
        print("\n⏳ Las sentencias actuales aún no tienen HTML disponible en el TSJ; quedan pendientes para la próxima corrida.")
        print("   Pasando al modo histórico de nulidades (19/01/2000 – 30/09/2026) para aprovechar la ejecución...")
        dias_hist, hay_mas_hist = determinar_dias_historicos(conn, session)
        if dias_hist:
            res_hist = _procesar_lote_dias(
                conn, session, client, primary, fallback,
                dias_hist, True, hay_mas_hist, reprocess_all, start_time,
            )
            total_general_ins += res_hist["ins"]
            total_general_upd += res_hist["upd"]
            total_general_dup += res_hist["dup"]
            total_sentencias_procesadas += res_hist["sentencias"]
            dias_completados_en_run += res_hist["dias_completados"]
            dias_totales_en_run += res_hist["dias_total"]
            limite_diario_alcanzado = res_hist["limite_diario"]
            tiempo_limite_alcanzado = res_hist["tiempo_limite"]
            modo_resumen = "ACTUAL + HISTÓRICO NULIDADES"
        elif not hay_mas_hist and obtener_control(conn, CLAVE_HIST_COMPLETADO) != "1":
            guardar_control(conn, CLAVE_HIST_COMPLETADO, "1")

    if limite_diario_alcanzado:
        print("\n" + "=" * 60)
        print("🛑 LÍMITE DE CUOTA ALCANZADO EN LOS PROVEEDORES CONFIGURADOS")
        print("   Se agotó la cuota disponible en Gemini, OpenRouter y/o NVIDIA NIM por el día de hoy.")
        print("   Todas las máximas y sentencias procesadas con éxito están aseguradas en la BD.")
        print("   Las sentencias restantes quedaron registradas y se retomarán automáticamente")
        print("   en la próxima ejecución programada (a las 8:15 PM).")
        print("=" * 60)
        conn.commit()
    elif tiempo_limite_alcanzado:
        print("\n" + "=" * 60)
        print("⏱️  CIERRE PREVENTIVO POR TIEMPO DE EJECUCIÓN")
        print("   Todo el progreso histórico completado hasta aquí quedó guardado.")
        print("   Se continuará automáticamente desde la siguiente fecha en la próxima ejecución.")
        print("=" * 60)
        conn.commit()

    # 4) Resumen final
    total_ext, sent_unicas = count_sentencias(conn)
    print(f"\n{'=' * 60}")
    print(f"  RESUMEN DE EJECUCIÓN WEB ({modo_resumen})")
    print(f"{'=' * 60}")
    print(f"  📅 Días procesados:            {dias_completados_en_run}/{dias_totales_en_run}")
    print(f"  📄 Sentencias analizadas:      {total_sentencias_procesadas}")
    print(f"  ✅ Máximas nuevas añadidas:    {total_general_ins}")
    print(f"  📝 Máximas enriquecidas:       {total_general_upd}")
    print(f"  🔄 Duplicadas sin cambios:     {total_general_dup}")
    print(f"  📋 Total extractos en BD:      {total_ext}")
    print(f"  ⚖️  Sentencias únicas en BD:    {sent_unicas}")
    print(f"  🗄  Base de datos:              {DB_PATH}")
    client.print_usage_summary()

    # 5) Exportar CSV
    export_csv(conn)
    conn.close()
    print(f"\n✅ Proceso finalizado.")


if __name__ == "__main__":
    main()
