#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Extractor de Jurisprudencia Venezolana
=======================================
Lee documentos PDF y DOCX de la carpeta 'Documentos_Jurisprudencia',
extrae sentencias citadas usando Google Gemini y las almacena en SQLite
sin duplicados.

Incluye sistema de seguimiento por bloques (estado_bloques) y recuperación
automática de bloques fallidos por congestión o saturación de la IA (error 503/429).
Al finalizar, exporta un CSV compatible con Excel.
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

# Forzar flush en todos los print para ver progreso en tiempo real
print = functools.partial(print, flush=True)

import pdfplumber
from docx import Document

from drive_sync import sync_csv_to_drive
from llm_client import (
    LimiteDiarioExcedidoError,
    PREFERRED_NVIDIA_MODELS,
    PREFERRED_OPENROUTER_MODELS,
    init_client as _init_llm_client,
    select_model,
)

# ─── Configuración ────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(BASE_DIR, "Documentos_Jurisprudencia")
DB_PATH = os.path.join(BASE_DIR, "jurisprudencia_ia.db")
CSV_PATH = os.path.join(BASE_DIR, "reporte_jurisprudencia.csv")
CHUNK_SIZE = 25_000     # caracteres por bloque
CHUNK_OVERLAP = 3_000  # solapamiento entre bloques para no perder contexto en bordes

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "").strip()

# Modelos a probar en orden de preferencia
PREFERRED_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-4-hydrogen",
]
OPENROUTER_MODELS = list(PREFERRED_OPENROUTER_MODELS)
NVIDIA_MODELS = list(PREFERRED_NVIDIA_MODELS)


# ─── Inicialización del cliente IA ────────────────────────────────────────────
def init_client():
    """Crea e inicializa el cliente multi-proveedor (Gemini + OpenRouter + NVIDIA NIM)."""
    return _init_llm_client(
        gemini_models=PREFERRED_MODELS,
        openrouter_models=OPENROUTER_MODELS,
        nvidia_models=NVIDIA_MODELS,
        inline_gemini_key=API_KEY,
        inline_openrouter_key=OPENROUTER_API_KEY,
        inline_nvidia_key=NVIDIA_API_KEY,
        timeout_ms=None,
    )


# ─── Lectura de documentos ────────────────────────────────────────────────────
def read_pdf(filepath):
    """Extrae texto completo de un PDF usando pdfplumber."""
    text_parts = []
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
    except Exception as e:
        print(f"    ⚠ Error leyendo PDF '{os.path.basename(filepath)}': {e}")
    return "\n".join(text_parts)


def read_docx(filepath):
    """Extrae texto de párrafos y tablas de un DOCX."""
    text_parts = []
    try:
        doc = Document(filepath)
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    text_parts.append(row_text)
    except Exception as e:
        print(f"    ⚠ Error leyendo DOCX '{os.path.basename(filepath)}': {e}")
    return "\n".join(text_parts)


def read_document(filepath):
    """Lee un documento según su extensión."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".pdf":
        return read_pdf(filepath)
    elif ext == ".docx":
        return read_docx(filepath)
    else:
        return ""


# ─── Chunking con Solapamiento (Overlap) ──────────────────────────────────────
def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """
    Divide el texto en fragmentos con solapamiento (overlap) para asegurar que ninguna
    cita jurisprudencial o su argumentación precedente/posterior quede cortada en bordes.
    """
    if len(text) <= size:
        return [text]
    chunks = []
    step = size - overlap
    for i in range(0, len(text), step):
        chunks.append(text[i : i + size])
        if i + size >= len(text):
            break
    return chunks


# ─── Prompt y extracción con IA ──────────────────────────────────────────────
EXTRACTION_PROMPT = """Actúa como un experto en Derecho Procesal Penal venezolano.
Analiza el siguiente texto jurídico y extrae todas las sentencias, fallos y criterios jurisprudenciales
del Tribunal Supremo de Justicia (TSJ) de Venezuela contenidos o invocados en él como jurisprudencia o doctrina aplicable.

Para cada sentencia o criterio encontrado, devuelve un objeto JSON con estas claves exactas:
- "numero": ÚNICAMENTE el número entero de la sentencia o fallo sin puntos de miles ni ceros a la izquierda (ej. "123", "456", "495", "1282"). PROHIBIDO extraer decisiones si el texto no indica su número de sentencia expreso, o si solo menciona el número de expediente (ej. "AA30-P-...", "10-1326"), un auto, una resolución administrativa o solo la fecha.
- "sala": Sala del TSJ normalizada. Usa exactamente una de estas:
    "Sala Constitucional", "Sala de Casación Penal", "Sala de Casación Civil",
    "Sala de Casación Social", "Sala Político-Administrativa",
    "Sala Electoral", "Sala Plena".
- "fecha": Fecha de la sentencia en formato DD/MM/AAAA si está disponible, o lo más aproximado posible.
- "tema": Materia o punto jurídico específico sobre el cual versa este criterio o extracto (ejemplo: "Inmotivación del fallo", "Violación de ley por falta de aplicación", "Requisitos de admisibilidad del recurso de casación", "Presunción iuris tantum en estafa", "Principio de intervención mínima penal", etc.). Frase concisa de 2 a 6 palabras que resuma el tema central.
- "extracto": Resumen claro y conciso (de 3 a 4 líneas) sobre el criterio jurídico o ratio decidendi aplicado para este tema.
  * REGLA DE RATIFICACIÓN DE CRITERIOS: Si la sentencia ratifica o reitera el criterio de una sentencia previa (o es ratificada por una posterior), DEBES MENCIONAR EXPRESAMENTE en el extracto el número y fecha de la decisión que está ratificando o por la cual fue ratificada (ejemplo: "Ratifica el criterio sentado en la Sentencia N° 348 del 25/06/2007, que dispone que...").
- "contenido_citado": Texto o extracto sustantivo vinculado a la sentencia en el documento, siguiendo estas reglas:
    1. CITA TEXTUAL DIRECTA (entre comillas o extracto transcrito): Si la sentencia viene acompañada de párrafos transcritos expresamente (con comillas o fórmulas como "establece:", "indicó:", "(…)"), copia fielmente ese texto literal. (Si una sentencia previa fue ratificada por una posterior y luego se transcribe la cita, AMBAS sentencias comparten dicha transcripción).
    2. CRITERIO PRECEDENTE / HACIA ATRÁS: Si la sentencia es citada como respaldo de un criterio o tesis que fue explicado en los párrafos ANTERIORES, extrae el párrafo doctrinario expuesto INMEDIATAMENTE ANTES.
    3. CRITERIO SUBSIGUIENTE / HACIA ADELANTE: Si la mención de la sentencia introduce una regla que se desarrolla en los párrafos INMEDIATAMENTE POSTERIORES, extrae dicho desarrollo.
- "ratificaciones": Detalle de la vinculación jurisprudencial si el texto indica ratificación (ejemplo: "Ratifica a Sentencia N° 348 del 25/06/2007" o "Ratificada por Sentencia N° 445 del 13/08/2024"). Si no menciona ratificación, dejar cadena vacía ("").

REGLAS PARA COMPENDIOS DE EXTRACTOS O MÁXIMAS DE UNA SENTENCIA (FUNDAMENTAL):
- Si el documento analizado es una recopilación, compendio de extractos o máximas de una sentencia principal (por ejemplo, encabezado por títulos como "COMPENDIO DE EXTRACTOS JURÍDICOS RELEVANTES... SENTENCIA N.° 495 DEL 23 DE OCTUBRE DE 2026", o donde las distintas secciones indican repetidamente "La sentencia número 495 del 23 de octubre de 2026 establece:"):
  * La sentencia a la cual PERTENECEN TODOS los extractos es ESA SENTENCIA PRINCIPAL (en este caso, "numero": "495", "sala": "Sala de Casación Penal", "fecha": "23/10/2026").
  * CADA EXTRACTO numerado o titulado del compendio DEBE SER EXTRAÍDO con el número "495", la sala "Sala de Casación Penal", su fecha "23/10/2026", y su respectivo "tema" tomado del título de ese extracto.
  * Si dentro de la cita de un extracto la Sentencia 495 menciona, acoge o ratifica a otra sentencia previa (ej. Sentencia N° 73, N° 268 o N° 544), el registro sigue siendo de la Sentencia N° 495, y la sentencia previa se coloca en la clave "ratificaciones" (ej. "Ratifica a Sentencia N° 73 del 06/02/2024"). NO debes poner como número principal de la sentencia el de la decisión citada, sino el de la sentencia emisora del compendio (495).
  * NO extraigas como sentencias principales aquellas que solo se mencionen de paso dentro del texto de los extractos como pruebas documentales o antecedentes históricos del caso. Todos los extractos pertenecen a la sentencia que los pronuncia.

REGLAS DE MÚLTIPLES EXTRACTOS POR SENTENCIA (MUY IMPORTANTE):
- Si una misma sentencia del TSJ es citada o sienta doctrina sobre DOS O MÁS TEMAS o puntos jurídicos diferentes en el texto analizado (por ejemplo, en un pasaje se cita sobre "Inmotivación" y en otro punto sobre "Falta de aplicación de norma"), DEBES GENERAR UN OBJETO SEPARADO PARA CADA TEMA DISTINTO. Cada objeto debe tener su respectivo "tema", "extracto" y "contenido_citado".

REGLAS DE EXCLUSIÓN Y CONTROL DE CALIDAD (MUY IMPORTANTE):
1. REGLA ANTI-CITAS ANIDADAS (NO EXTRAER CITAS DENTRO DE CITAS AJENAS):
   - NO extraigas sentencias cuyo número o fecha aparezcan DENTRO de un texto que está entre comillas ("...", “...”) o entre paréntesis (...), cuando dicho texto sea la transcripción de la decisión de un tribunal de instancia (Corte de Apelaciones o Tribunal de Juicio/Control), los alegatos del recurrente, la denuncia del fiscal o los antecedentes del caso.
   - Ejemplo a IGNORAR:
     '...la Corte de Apelaciones dictó decisión señalando: “...delata esta Corte el yerro cometido... en desconocimiento de la sentencia 383 ratificada en fallo 540...”'.
     En ese caso, la 383 y la 540 NO deben ser extraídas, pues son citas de la Corte de Apelaciones dentro de su texto transcrito.

2. REGLA DE EXCLUSIÓN DE CITAS EN CURSIVA / TRANSCRIPCIÓN DE DENUNCIAS O ALEGATOS:
   - En las sentencias, las transcripciones de las denuncias de los recurrentes, escritos de defensa o acusaciones a menudo se formatean en LETRA CURSIVA (itálica) o en bloques de transcripción directa SIN comillas, precedidas de frases como:
     * "En el escrito recursivo se constató que la [primera/segunda/tercera...] denuncia refiere lo siguiente:"
     * "PRIMERA DENUNCIA:", "SEGUNDA DENUNCIA:", "TERCERA DENUNCIA:", etc.
     * "El recurrente alegó / adujo / denunció:"
     * "La defensa sostuvo / alegó:"
   - Si una sentencia o decisión es citada DENTRO de esos párrafos de denuncia o alegatos del recurrente (por ejemplo: "...en el escrito recursivo se constató que la tercera denuncia refiere lo siguiente: TERCERA DENUNCIA: ...siendo pertinente indicar la decisión 195..."), NO LA EXTRAIGAS. Esa sentencia es un alegato de la parte impugnante, NO jurisprudencia sentada por el tribunal en este fallo.
   - Solo extrae sentencias que el ponente o Sala del TSJ cite en sus propias secciones de decisión de fondo ("Motivación para decidir", "Consideraciones de la Sala", "Esta Sala observa / considera").

3. REGLA DE EXCLUSIÓN DE PARTES DISPOSITIVAS Y RESOLUCIONES:
   - NO extraigas partes dispositivas de fallos (fórmulas como "PRIMERO: COMPETENTE...", "DESESTIMA POR MANIFIESTAMENTE INFUNDADO...", "DECLARA SIN LUGAR...") ni autos de mera sustanciación o resoluciones administrativas; extrae ÚNICAMENTE criterios doctrinarios, interpretaciones normativas o ratio decidendi sustantivos y completos.

4. NO confundas los antecedentes fácticos, las decisiones recurridas de primera/segunda instancia o los alegatos de las partes con jurisprudencia vinculante del TSJ.
5. Devuelve ÚNICAMENTE un JSON válido: una lista de objetos [{"numero": ..., "sala": ..., "fecha": ..., "tema": ..., "extracto": ..., "contenido_citado": ..., "ratificaciones": ...}, ...]
6. NO incluyas texto adicional, explicaciones, markdown ni bloques de código. Solo el JSON puro.
7. Si no encuentras ninguna sentencia válida según estas reglas, devuelve una lista vacía: []
8. No inventes sentencias que no estén en el texto.

TEXTO A ANALIZAR:
"""

def extract_sentencias(client, primary_model, fallback_model, text_chunk, max_retries=4):
    """
    Envía un bloque de texto al cliente multi-proveedor y extrae las sentencias.
    Devuelve (sentencias, exito, error_msg).
    """
    prompt = EXTRACTION_PROMPT + text_chunk
    return client.extract_json_list(
        prompt=prompt,
        preferred_label=primary_model,
        max_retries=max_retries,
    )


# ─── Base de Datos ────────────────────────────────────────────────────────────
def init_db():
    """
    Crea la base de datos, tabla sentencias (permitiendo múltiples extractos por sentencia
    cuando traten de temas diferentes) y tabla de seguimiento de bloques.
    Aplica migración automática segura si la tabla previa poseía restricción UNIQUE(numero, sala).
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Verificar si la tabla 'sentencias' ya existe y cuál es su definición
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sentencias'")
    row_table = cursor.fetchone()

    if row_table is None:
        # Creación limpia con la nueva estructura
        cursor.execute("""
            CREATE TABLE sentencias (
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
    else:
        sql_def = row_table[0] or ""
        # Si tiene la restricción UNIQUE(numero, sala), migramos para admitir múltiples extractos por sentencia
        if "UNIQUE" in sql_def.upper() and "NUMERO" in sql_def.upper() and "SALA" in sql_def.upper():
            print("  🔄 Migrando esquema de base de datos para permitir múltiples extractos por sentencia...")
            cursor.execute("PRAGMA table_info(sentencias)")
            cols_existentes = [col[1] for col in cursor.fetchall()]

            cursor.execute("""
                CREATE TABLE sentencias_migracion (
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

            tema_expr = "tema" if "tema" in cols_existentes else "'' AS tema"
            ratif_expr = "ratificaciones" if "ratificaciones" in cols_existentes else "'' AS ratificaciones"
            citado_expr = "contenido_citado" if "contenido_citado" in cols_existentes else "'' AS contenido_citado"

            cursor.execute(f"""
                INSERT INTO sentencias_migracion (id, numero, sala, fecha, tema, extracto, contenido_citado, ratificaciones, archivo_origen)
                SELECT id, numero, sala, fecha, {tema_expr}, extracto, {citado_expr}, {ratif_expr}, archivo_origen
                FROM sentencias
            """)
            cursor.execute("DROP TABLE sentencias")
            cursor.execute("ALTER TABLE sentencias_migracion RENAME TO sentencias")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sentencias_num_sala ON sentencias(numero, sala)")
            conn.commit()
            print("  ✔ Migración completada con éxito. Datos históricos conservados.")
        else:
            # Asegurar que todas las columnas nuevas existan
            cursor.execute("PRAGMA table_info(sentencias)")
            columns = [col[1] for col in cursor.fetchall()]
            if "tema" not in columns:
                cursor.execute("ALTER TABLE sentencias ADD COLUMN tema TEXT")
            if "contenido_citado" not in columns:
                cursor.execute("ALTER TABLE sentencias ADD COLUMN contenido_citado TEXT")
            if "ratificaciones" not in columns:
                cursor.execute("ALTER TABLE sentencias ADD COLUMN ratificaciones TEXT")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sentencias_num_sala ON sentencias(numero, sala)")

    # Tabla para rastrear qué bloques se procesaron con éxito y cuáles tuvieron error
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
    conn.commit()
    return conn


def normalizar_texto(texto):
    """Limpia y normaliza texto para comparación semántica insensible a tildes y mayúsculas."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode("utf-8").lower()
    return re.sub(r"[^\w\s]", " ", texto).strip()


def son_mismo_tema(tema1, extracto1, tema2, extracto2, citado1="", citado2=""):
    """
    Evalúa si dos extractos de la misma sentencia corresponden al mismo tema o punto jurídico.
    Si son temas distintos, retorna False (permitiendo que ambos coexistan como bloques separados).
    Si son el mismo tema o criterio sustancialmente idéntico, retorna True (para enriquecer/actualizar).
    """
    c1_norm = normalizar_texto(citado1)
    c2_norm = normalizar_texto(citado2)
    if c1_norm and c2_norm:
        if c1_norm in c2_norm or c2_norm in c1_norm or difflib.SequenceMatcher(None, c1_norm, c2_norm, autojunk=False).ratio() >= 0.65:
            return True

    t1_norm = normalizar_texto(tema1)
    t2_norm = normalizar_texto(tema2)
    e1_norm = normalizar_texto(extracto1)
    e2_norm = normalizar_texto(extracto2)

    # 1. Si ambos tienen tema explícito definido
    if t1_norm and t2_norm:
        stopwords = {"de", "la", "el", "los", "las", "en", "por", "del", "un", "una", "y", "o", "a", "al", "para", "con", "sobre"}
        palabras1 = set(t1_norm.split()) - stopwords
        palabras2 = set(t2_norm.split()) - stopwords

        # Similitud directa de cadena en el tema
        ratio_tema = difflib.SequenceMatcher(None, t1_norm, t2_norm, autojunk=False).ratio()

        # Si comparten palabras clave significativas del tema o alta similitud
        if ratio_tema >= 0.65 or (palabras1 and palabras2 and len(palabras1 & palabras2) >= min(len(palabras1), len(palabras2))):
            return True

        # Si los temas explícitos son claramente distintos (ej: "inmotivacion" vs "falta de aplicacion")
        if ratio_tema < 0.45 and (not palabras1 or not palabras2 or not (palabras1 & palabras2)):
            return False

    # 2. Si uno de los temas está vacío, recurrimos a la similitud del texto del extracto
    ratio_extracto = difflib.SequenceMatcher(None, e1_norm, e2_norm, autojunk=False).ratio()
    if ratio_extracto >= 0.55:
        return True

    # 3. Comparación por palabras clave de los extractos
    palabras_e1 = set(w for w in e1_norm.split() if len(w) > 4)
    palabras_e2 = set(w for w in e2_norm.split() if len(w) > 4)
    if palabras_e1 and palabras_e2:
        interseccion = len(palabras_e1 & palabras_e2)
        union = len(palabras_e1 | palabras_e2)
        jaccard = interseccion / union if union else 0
        if jaccard >= 0.45:
            return True

    return False


def guardar_estado_bloque(conn, archivo, bloque_idx, total_bloques, estado, sentencias_detectadas=0, error_detalle=""):
    """Registra o actualiza el estado de lectura de un bloque específico."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO estado_bloques (archivo, bloque_idx, total_bloques, estado, sentencias_detectadas, error_detalle, fecha_actualizacion)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
        ON CONFLICT(archivo, bloque_idx) DO UPDATE SET
            estado = excluded.estado,
            sentencias_detectadas = excluded.sentencias_detectadas,
            error_detalle = excluded.error_detalle,
            fecha_actualizacion = datetime('now', 'localtime')
    """, (archivo, bloque_idx, total_bloques, estado, sentencias_detectadas, error_detalle))
    conn.commit()


def obtener_bloques_a_procesar(conn, archivo, total_bloques, reprocess_all=False):
    """
    Retorna los índices (1-based) de bloques que deben ser procesados.
    Si total_bloques cambió (por activar solapamiento) o reprocess_all es True,
    reinicia el seguimiento para ese archivo y procesa todos los bloques.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT total_bloques FROM estado_bloques WHERE archivo = ? LIMIT 1", (archivo,))
    row_prev = cursor.fetchone()

    if reprocess_all or (row_prev and row_prev[0] != total_bloques):
        cursor.execute("DELETE FROM estado_bloques WHERE archivo = ?", (archivo,))
        conn.commit()
        return list(range(1, total_bloques + 1))

    cursor.execute("SELECT bloque_idx FROM estado_bloques WHERE archivo = ? AND estado = 'EXITOSO'", (archivo,))
    exitosos = {row[0] for row in cursor.fetchall()}
    return [i for i in range(1, total_bloques + 1) if i not in exitosos]


def obtener_resumen_bloques(conn, archivo):
    """Devuelve un conteo de bloques exitosos y con error para un archivo."""
    cursor = conn.cursor()
    cursor.execute("SELECT estado, COUNT(*) FROM estado_bloques WHERE archivo = ? GROUP BY estado", (archivo,))
    return dict(cursor.fetchall())


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


def limpiar_y_validar_registro(s):
    """
    Normaliza y valida estrictamente una sentencia extraída.
    Retorna un diccionario saneado o None si el registro debe ser descartado.
    Filtra tanto antes de INSERT como antes de UPDATE.
    """
    if not isinstance(s, dict):
        return None

    raw_numero = str(s.get("numero", "")).replace(".", "").strip()
    # Eliminar ceros a la izquierda
    num_limpio = raw_numero.lstrip("0")
    if not num_limpio:
        num_limpio = "0"

    # Si no es estrictamente numérico, descartar (bloquea fechas, expedientes con guiones, autos, resoluciones, "S/N")
    if not num_limpio.isdigit() or num_limpio == "0":
        return None

    sala = str(s.get("sala", "")).strip()
    if not sala:
        return None

    contenido_citado = str(s.get("contenido_citado", "")).strip()
    citado_lower = contenido_citado.lower()

    # Validar contenido citado:
    # 1. No debe estar en valores vacíos
    if citado_lower in VALORES_VACIOS:
        return None

    # 2. No debe tener menos de 150 caracteres
    if len(contenido_citado) < 150:
        return None

    # 3. No debe terminar en ":" o "[...]"
    if contenido_citado.endswith(":") or contenido_citado.endswith("[...]") or contenido_citado.endswith("...:"):
        return None

    # 4. No debe contener frases inválidas
    if any(frase in citado_lower for frase in FRASES_INVALIDAS):
        return None

    # Parsear ratificaciones
    ratificaciones_raw = s.get("ratificaciones", "")
    if isinstance(ratificaciones_raw, dict):
        partes = []
        if ratificaciones_raw.get("ratifica_a"):
            partes.append(f"Ratifica a: {ratificaciones_raw['ratifica_a']}")
        if ratificaciones_raw.get("ratificada_por"):
            partes.append(f"Ratificada por: {ratificaciones_raw['ratificada_por']}")
        ratificaciones_str = " | ".join(partes)
    else:
        ratificaciones_str = str(ratificaciones_raw or "").strip()

    # Si la única sentencia mencionada en ratificaciones_str es el propio num_limpio, limpiar autorratificación
    if ratificaciones_str:
        ratif_sin_fechas = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", ratificaciones_str)
        nums_mencionados = re.findall(r"\b\d+\b", ratif_sin_fechas)
        if nums_mencionados and set(nums_mencionados) == {num_limpio}:
            ratificaciones_str = ""

    fecha = str(s.get("fecha", "")).strip()
    tema = str(s.get("tema", "")).strip()
    extracto = str(s.get("extracto", "")).strip()

    return {
        "numero": num_limpio,
        "sala": sala,
        "fecha": fecha,
        "tema": tema,
        "extracto": extracto,
        "contenido_citado": contenido_citado,
        "ratificaciones": ratificaciones_str,
    }


def insert_sentencias(conn, sentencias, archivo_origen):
    """
    Inserta o enriquece sentencias en la base de datos.
    Aplica limpieza y validación rigurosa a cada registro antes de insertar o actualizar.
    Permite que una misma sentencia (mismo número, sala y fecha) tenga MÚLTIPLES EXTRACTOS
    en bloques separados si tratan sobre temas o puntos jurídicos diferentes.
    Si el extracto trata sobre el mismo tema ya registrado, lo enriquece si aporta mayor detalle.
    """
    cursor = conn.cursor()
    inserted = 0
    updated = 0
    duplicated = 0

    for s in sentencias:
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

        # Consultar todos los registros ya existentes para este número y sala
        cursor.execute(
            "SELECT id, COALESCE(tema, ''), extracto, contenido_citado, fecha, COALESCE(ratificaciones, '') "
            "FROM sentencias WHERE numero = ? AND sala = ?",
            (numero, sala),
        )
        filas_existentes = cursor.fetchall()

        # Buscar si alguno de los registros existentes corresponde al MISMO tema/punto jurídico
        fila_coincidente = None
        for fila in filas_existentes:
            f_id, ex_tema, ex_extracto, ex_citado, ex_fecha, ex_ratif = fila
            if son_mismo_tema(tema, extracto, ex_tema, ex_extracto, contenido_citado, ex_citado):
                fila_coincidente = fila
                break

        if fila_coincidente is None:
            # Es un nuevo criterio / nuevo tema para esta sentencia (o primera vez que aparece):
            # Se inserta como un bloque/extracto independiente
            cursor.execute(
                """
                INSERT INTO sentencias (numero, sala, fecha, tema, extracto, contenido_citado, ratificaciones, archivo_origen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (numero, sala, fecha, tema, extracto, contenido_citado, ratificaciones_str, archivo_origen),
            )
            inserted += 1
        else:
            # Corresponde al mismo tema/criterio ya guardado: evaluar si se enriquece
            row_id, existing_tema, existing_extracto, existing_citado, existing_fecha, existing_ratificaciones = fila_coincidente
            existing_citado = existing_citado or ""
            existing_extracto = existing_extracto or ""
            existing_fecha = existing_fecha or ""
            existing_ratificaciones = existing_ratificaciones or ""

            debe_actualizar = False
            nuevo_tema = existing_tema
            nuevo_citado = existing_citado
            nuevo_extracto = existing_extracto
            nueva_fecha = existing_fecha
            nuevo_ratificaciones = existing_ratificaciones

            # Asignar tema si antes no tenía
            if tema and not existing_tema:
                nuevo_tema = tema
                debe_actualizar = True

            # Mejorar contenido citado si el nuevo aporta texto o es más completo
            es_nuevo_valido = contenido_citado and contenido_citado.lower() not in VALORES_VACIOS
            es_viejo_vacio = not existing_citado or existing_citado.lower() in VALORES_VACIOS

            if es_nuevo_valido:
                if es_viejo_vacio or len(contenido_citado) > len(existing_citado):
                    nuevo_citado = contenido_citado
                    debe_actualizar = True

            # Mejorar extracto si el nuevo es más completo o menciona ratificación
            if extracto and (len(extracto) > len(existing_extracto) or ("ratifica" in extracto.lower() and "ratifica" not in existing_extracto.lower())):
                nuevo_extracto = extracto
                debe_actualizar = True

            # Mejorar fecha si antes no había fecha exacta
            if fecha and ("sin fecha" in existing_fecha.lower() or not existing_fecha) and "sin fecha" not in fecha.lower():
                nueva_fecha = fecha
                debe_actualizar = True

            # Añadir o enriquecer ratificaciones
            if ratificaciones_str and len(ratificaciones_str) > len(existing_ratificaciones):
                nuevo_ratificaciones = ratificaciones_str
                debe_actualizar = True

            if debe_actualizar:
                cursor.execute(
                    """
                    UPDATE sentencias 
                    SET tema = ?, contenido_citado = ?, extracto = ?, fecha = ?, ratificaciones = ?, archivo_origen = ? 
                    WHERE id = ?
                    """,
                    (nuevo_tema, nuevo_citado, nuevo_extracto, nueva_fecha, nuevo_ratificaciones, archivo_origen, row_id),
                )
                updated += 1
            else:
                duplicated += 1

    conn.commit()
    return inserted, updated, duplicated


def count_sentencias(conn):
    """Cuenta el total de extractos y sentencias únicas en la BD."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), COUNT(DISTINCT numero || '_' || sala) FROM sentencias")
    return cursor.fetchone()


# ─── Exportación CSV ─────────────────────────────────────────────────────────
def export_csv(conn):
    """Exporta toda la tabla sentencias a CSV compatible con Excel en español."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(sentencias)")
    cols = {r[1] for r in cursor.fetchall()}
    if "enlace" not in cols:
        cursor.execute("ALTER TABLE sentencias ADD COLUMN enlace TEXT DEFAULT ''")
        conn.commit()
    cursor.execute(
        "SELECT id, numero, sala, fecha, COALESCE(tema, ''), extracto, contenido_citado, "
        "COALESCE(ratificaciones, ''), archivo_origen, COALESCE(enlace, '') FROM sentencias ORDER BY id"
    )
    rows = cursor.fetchall()

    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "ID", "Número", "Sala", "Fecha", "Tema / Punto Jurídico",
            "Extracto (Resumen)", "Contenido Citado (Textual)",
            "Ratificaciones / Precedentes", "Archivo Origen", "Enlace TSJ",
        ])
        writer.writerows(rows)

    print(f"\n📊 CSV exportado: {CSV_PATH}  ({len(rows)} registros)")
    sync_csv_to_drive(CSV_PATH, quiet_if_unconfigured=True)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  EXTRACTOR DE JURISPRUDENCIA VENEZOLANA")
    print("  Con Sistema de Resiliencia y Recuperación de Bloques")
    print("=" * 60)

    # Parámetros por línea de comandos
    reprocess_all = "--reprocesar-todo" in sys.argv or "--todos" in sys.argv

    # 1) Verificar carpeta de documentos
    if not os.path.isdir(DOCS_DIR):
        os.makedirs(DOCS_DIR, exist_ok=True)
        print(f"\n📁 Se creó la carpeta: {DOCS_DIR}")

    supported_ext = {".pdf", ".docx"}
    all_files = [
        f for f in os.listdir(DOCS_DIR)
        if os.path.splitext(f)[1].lower() in supported_ext
    ]
    # Si existe tanto <nombre>.docx como <nombre>.docx.pdf, excluir el .docx.pdf para no duplicar
    files = [
        f for f in all_files
        if not (f.lower().endswith(".docx.pdf") and f[:-4] in all_files)
    ]

    conn = init_db()

    if not files:
        print(f"\n⚠  No se encontraron archivos PDF o DOCX en: {DOCS_DIR}")
        total_ext, sent_unicas = count_sentencias(conn)
        print(f"📋 Total extractos en base de datos: {total_ext} ({sent_unicas} sentencias únicas)")
        export_csv(conn)
        conn.close()
        return

    print(f"\n📁 Carpeta: {DOCS_DIR}")
    print(f"📄 Archivos encontrados: {len(files)}")
    for f in files:
        print(f"   • {f}")

    # 2) Inicializar cliente multi-proveedor y seleccionar modelos
    print("\n🤖 Inicializando conexión con los proveedores de IA...")
    client = init_client()

    print("\n🔍 Verificando modelos disponibles (primario y respaldo)...")
    primary_model, fallback_model = select_model(client)
    print(f"\n✅ Modelo Primario: {primary_model}")
    print(f"🛡️  Modelo de Respaldo: {fallback_model}")
    or_backup = client.describe_openrouter_backup()
    if or_backup and not primary_model.startswith("openrouter:"):
        print(f"🔀 Respaldo OpenRouter: {or_backup}")
    nv_backup = client.describe_nvidia_backup()
    if nv_backup and not primary_model.startswith("nvidia:"):
        print(f"🟢 Respaldo NVIDIA NIM: {nv_backup}")

    total_inserted = 0
    total_updated = 0
    total_duplicated = 0
    limite_diario_alcanzado = False

    for idx, filename in enumerate(files, 1):
        if limite_diario_alcanzado:
            break
        filepath = os.path.join(DOCS_DIR, filename)
        print(f"\n{'─' * 60}")
        print(f"📖 [{idx}/{len(files)}] Archivo: {filename}")

        # Leer documento
        text = read_document(filepath)
        if not text.strip():
            print("    ⚠ No se pudo extraer texto de este archivo.")
            continue

        print(f"    Texto extraído: {len(text):,} caracteres")
        chunks = chunk_text(text)
        total_chunks = len(chunks)

        # Determinar qué bloques procesar
        bloques_pendientes = obtener_bloques_a_procesar(conn, filename, total_chunks, reprocess_all)
        resumen_previo = obtener_resumen_bloques(conn, filename)

        completados_previos = resumen_previo.get("EXITOSO", 0)
        errores_previos = resumen_previo.get("ERROR", 0)

        print(f"    Bloques totales: {total_chunks}")
        if not reprocess_all and completados_previos > 0:
            print(f"    ✅ Previamente completados: {completados_previos}")
            if errores_previos > 0:
                print(f"    ⚠️ Con error previo pendiente: {errores_previos}")
            print(f"    🔄 Bloques a procesar en esta ejecución: {len(bloques_pendientes)}")

        if not bloques_pendientes:
            print("    ✨ Todos los bloques de este documento ya están completados exitosamente.")
            continue

        # Procesar bloques pendientes
        bloques_con_error = []
        for count, c_num in enumerate(bloques_pendientes, 1):
            chunk_data = chunks[c_num - 1]
            print(f"    🔄 [{count}/{len(bloques_pendientes)}] Analizando bloque {c_num}/{total_chunks}...", end=" ")

            try:
                sentencias, exito, error_msg = extract_sentencias(
                    client=client,
                    primary_model=primary_model,
                    fallback_model=fallback_model,
                    text_chunk=chunk_data,
                )
            except LimiteDiarioExcedidoError as e:
                print(f"\n    🛑 {e}")
                guardar_estado_bloque(conn, filename, c_num, total_chunks, "ERROR", 0, str(e))
                limite_diario_alcanzado = True
                break

            if exito:
                guardar_estado_bloque(conn, filename, c_num, total_chunks, "EXITOSO", len(sentencias))
                print(f"✔ ({len(sentencias)} sentencias encontradas)")

                if sentencias:
                    ins, upd, dup = insert_sentencias(conn, sentencias, filename)
                    total_inserted += ins
                    total_updated += upd
                    total_duplicated += dup
            else:
                guardar_estado_bloque(conn, filename, c_num, total_chunks, "ERROR", 0, error_msg)
                bloques_con_error.append(c_num)
                print(f"✘ Error: {error_msg[:60]}...")

            # Pausa breve entre llamadas
            time.sleep(2)

        if limite_diario_alcanzado:
            break

        # ─── PASE DE RECUPERACIÓN AUTOMÁTICA ──────────────────────────────────
        if bloques_con_error:
            print(f"\n    ⚠️  Se detectaron {len(bloques_con_error)} bloque(s) con error temporal por saturación.")
            print(f"    🔄 Iniciando PASE DE RECUPERACIÓN para bloques: {bloques_con_error}")
            print("    ⏳ Esperando 6 segundos para descongestionar la API...")
            time.sleep(6)

            recuperados = 0
            for r_num in list(bloques_con_error):
                chunk_data = chunks[r_num - 1]
                print(f"    🔁 Reintentando bloque {r_num}/{total_chunks} con modelo de respaldo...", end=" ")

                try:
                    sentencias, exito, error_msg = extract_sentencias(
                        client=client,
                        primary_model=fallback_model,
                        fallback_model=primary_model,
                        text_chunk=chunk_data,
                        max_retries=5,
                    )
                except LimiteDiarioExcedidoError as e:
                    print(f"\n    🛑 {e}")
                    guardar_estado_bloque(conn, filename, r_num, total_chunks, "ERROR", 0, str(e))
                    limite_diario_alcanzado = True
                    break

                if exito:
                    guardar_estado_bloque(conn, filename, r_num, total_chunks, "EXITOSO", len(sentencias))
                    print(f"✔ RECUPERADO ({len(sentencias)} sentencias)")
                    recuperados += 1
                    bloques_con_error.remove(r_num)

                    if sentencias:
                        ins, upd, dup = insert_sentencias(conn, sentencias, filename)
                        total_inserted += ins
                        total_updated += upd
                        total_duplicated += dup
                else:
                    guardar_estado_bloque(conn, filename, r_num, total_chunks, "ERROR", 0, error_msg)
                    print(f"✘ Reintento fallido.")

                time.sleep(3)

            print(f"    📊 Recuperación completada: {recuperados} bloque(s) rescatados.")

    if limite_diario_alcanzado:
        print("\n" + "=" * 60)
        print("🛑 LÍMITE DE CUOTA ALCANZADO EN TODOS LOS PROVEEDORES")
        print("   Los bloques completados quedaron guardados y podrás reanudar luego.")
        print("=" * 60)
        conn.commit()

    # 5) Resumen final
    total_ext, sent_unicas = count_sentencias(conn)
    print(f"\n{'=' * 60}")
    print(f"  RESUMEN FINAL")
    print(f"{'=' * 60}")
    print(f"  📄 Archivos analizados:        {len(files)}")
    print(f"  ✅ Extractos nuevos añadidos:  {total_inserted}")
    print(f"  📝 Extractos enriquecidos:     {total_updated}")
    print(f"  🔄 Duplicados sin cambios:     {total_duplicated}")
    print(f"  📋 Total extractos en BD:      {total_ext}")
    print(f"  ⚖️  Sentencias únicas en BD:    {sent_unicas}")
    print(f"  🗄  Base de datos:              {DB_PATH}")
    client.print_usage_summary()

    # 6) Exportar CSV actualizado
    export_csv(conn)

    conn.close()
    print(f"\n✅ Proceso completado exitosamente.")


if __name__ == "__main__":
    main()
