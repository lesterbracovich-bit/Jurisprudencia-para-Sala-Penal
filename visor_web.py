#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Visor Web de Jurisprudencia Venezolana
=====================================
Interfaz interactiva desarrollada en Streamlit para consultar,
filtrar y leer sentencias y extractos jurisprudenciales del TSJ
almacenados en SQLite (jurisprudencia_ia.db).
"""

import base64
import html
import os
import re
import sqlite3
import pandas as pd
import streamlit as st

# ─── Configuración de Página ──────────────────────────────────────────────────
st.set_page_config(
    page_title="Jurisprudencia TSJ | Visor Jurídico",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "jurisprudencia_ia.db")

# ─── Estilos CSS Personalizados (Bimodal: Modo Claro y Modo Oscuro) ────────────
st.markdown(
    """
    <style>
    /* Tipografía y colores adaptativos usando variables nativas de Streamlit */
    .main-title {
        font-family: 'Georgia', serif;
        font-size: 2.3rem;
        color: var(--text-color) !important;
        font-weight: 700;
        margin-bottom: 0.2rem;
    }
    .sub-title {
        color: var(--text-color) !important;
        opacity: 0.82;
        font-size: 1.08rem;
        margin-bottom: 0.4rem;
    }
    .credit-line {
        color: var(--text-color) !important;
        opacity: 0.65;
        font-size: 0.95rem;
        margin-top: -0.2rem;
        margin-bottom: 1.4rem;
    }
    .sentencia-header {
        font-size: 1.35rem;
        font-weight: 800;
        color: var(--text-color) !important;
        letter-spacing: 0.5px;
    }
    .fecha-label {
        text-align: right;
        color: var(--text-color) !important;
        opacity: 0.85;
        font-size: 1.02rem;
        font-weight: 600;
    }
    .badge-sala {
        padding: 4px 12px;
        border-radius: 12px;
        font-size: 0.88rem;
        font-weight: 700;
        display: inline-block;
        color: #ffffff !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.12);
    }
    .badge-penal {
        background-color: #991b1b !important;
        border: 1px solid #7f1d1d;
    }
    .badge-const {
        background-color: #1d4ed8 !important;
        border: 1px solid #1e40af;
    }
    .badge-civil {
        background-color: #047857 !important;
        border: 1px solid #065f46;
    }
    .badge-plena {
        background-color: #b45309 !important;
        border: 1px solid #92400e;
    }
    .badge-sala-default {
        background-color: #475569 !important;
        border: 1px solid #334155;
    }
    .badge-tema {
        background-color: rgba(14, 165, 233, 0.13) !important;
        border: 1px solid #0284c7 !important;
        color: var(--text-color) !important;
        font-size: 0.92rem;
        font-weight: 700;
        padding: 3px 10px;
        border-radius: 6px;
        display: inline-block;
    }
    .ratif-box {
        background-color: rgba(59, 130, 246, 0.10) !important;
        border-left: 3px solid #2563eb !important;
        border-radius: 4px;
        padding: 8px 12px;
        margin-bottom: 12px;
        color: var(--text-color) !important;
        font-size: 0.95rem;
    }
    .extracto-text {
        font-size: 1.06rem;
        line-height: 1.7;
        color: var(--text-color) !important;
        text-align: justify;
        margin-top: 0.5rem;
        margin-bottom: 0.7rem;
    }
    .cita-box {
        background-color: var(--secondary-background-color) !important;
        border-left: 4px solid #2563eb !important;
        padding: 14px 18px;
        margin-top: 10px;
        border-radius: 0 8px 8px 0;
        font-style: italic;
        color: var(--text-color) !important;
        line-height: 1.65;
        font-size: 1.02rem;
        text-align: justify;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
    }
    .metadata-row {
        font-size: 1.05rem;
        color: var(--text-color) !important;
        margin-bottom: 0.4rem;
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
    }
    .tarjeta-divider {
        margin: 8px 0 14px 0;
        border: none;
        border-top: 1px solid rgba(148, 163, 184, 0.28);
    }
    .tsj-card-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 12px;
        padding-top: 10px;
        border-top: 1px dashed rgba(148, 163, 184, 0.30);
    }
    .tsj-copy-group {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
    }
    .tsj-copy-label {
        font-size: 0.88rem;
        font-weight: 600;
        color: var(--text-color) !important;
        opacity: 0.82;
        margin-right: 2px;
    }
    .tsj-copy-btn {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 5px 11px;
        border-radius: 8px;
        border: 1px solid rgba(148, 163, 184, 0.45);
        background-color: var(--secondary-background-color);
        color: var(--text-color);
        font-size: 0.85rem;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.16s ease;
        line-height: 1.3;
        font-family: inherit;
    }
    .tsj-copy-btn:hover:not(:disabled) {
        border-color: #2563eb;
        background-color: rgba(37, 99, 235, 0.13);
        transform: translateY(-1px);
    }
    .tsj-copy-btn:active:not(:disabled) {
        transform: translateY(0);
    }
    .tsj-copy-btn.copied {
        border-color: #047857 !important;
        background-color: #059669 !important;
        color: #ffffff !important;
    }
    .tsj-copy-btn:disabled {
        opacity: 0.45;
        cursor: not-allowed;
    }
    .tsj-link-original {
        font-weight: 600;
        font-size: 0.92rem;
        text-decoration: none;
        white-space: nowrap;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─── Conexión y Carga de Datos ────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_data():
    """Carga sentencias desde la base de datos local SQLite."""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(sentencias)")
    cols = [c[1] for c in cursor.fetchall()]
    if "ratificaciones" not in cols:
        cursor.execute("ALTER TABLE sentencias ADD COLUMN ratificaciones TEXT")
        conn.commit()
    if "tema" not in cols:
        cursor.execute("ALTER TABLE sentencias ADD COLUMN tema TEXT")
        conn.commit()
    if "enlace" not in cols:
        cursor.execute("ALTER TABLE sentencias ADD COLUMN enlace TEXT DEFAULT ''")
        conn.commit()

    query = """
        SELECT 
            id,
            numero,
            sala,
            COALESCE(fecha, 'Sin fecha') AS fecha,
            COALESCE(tema, '') AS tema,
            COALESCE(extracto, '') AS extracto,
            COALESCE(contenido_citado, '') AS contenido_citado,
            COALESCE(ratificaciones, '') AS ratificaciones,
            COALESCE(archivo_origen, '') AS archivo_origen,
            COALESCE(enlace, '') AS enlace
        FROM sentencias
        ORDER BY id DESC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def _parse_fecha_sort(valor: str):
    """Convierte la fecha en formato texto a Timestamp para orden cronológico."""
    if not valor:
        return pd.NaT
    texto = str(valor).strip()
    if texto.lower() in ("s/f", "sin fecha", "none", "null", ""):
        return pd.NaT
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        ts = pd.to_datetime(texto, format=fmt, errors="coerce")
        if pd.notna(ts):
            return ts
    return pd.to_datetime(texto, dayfirst=True, errors="coerce")


def _parse_numero_sort(valor: str) -> int:
    """Extrae la parte numérica del número de sentencia para desempate en la misma fecha."""
    m = re.search(r"\d+", str(valor or ""))
    return int(m.group()) if m else 0


def ordenar_cronologicamente(df: pd.DataFrame, ascendente: bool = False) -> pd.DataFrame:
    """Ordena el DataFrame cronológicamente por fecha y número de sentencia."""
    if df.empty:
        return df
    df_ord = df.copy()
    df_ord["_fecha_sort"] = df_ord["fecha"].map(_parse_fecha_sort)
    df_ord["_num_sort"] = df_ord["numero"].map(_parse_numero_sort)
    df_ord = df_ord.sort_values(
        by=["_fecha_sort", "_num_sort", "id"],
        ascending=[ascendente, ascendente, ascendente],
        na_position="last",
        kind="mergesort",
    )
    return df_ord.drop(columns=["_fecha_sort", "_num_sort"]).reset_index(drop=True)


def get_sala_badge_class(sala: str) -> str:
    """Devuelve clase CSS acorde a la sala para destacar visualmente con alto contraste."""
    sala_lower = sala.lower()
    if "penal" in sala_lower:
        return "badge-penal"
    elif "constitucional" in sala_lower:
        return "badge-const"
    elif "civil" in sala_lower:
        return "badge-civil"
    elif "plena" in sala_lower:
        return "badge-plena"
    return "badge-sala-default"


def _codificar_b64(texto: str) -> str:
    """Codifica texto UTF-8 en Base64 ASCII seguro para atributos HTML data-*."""
    return base64.b64encode(texto.encode("utf-8")).decode("ascii")


def formatear_cita_textual(contenido_citado: str) -> str:
    """Normaliza la cita textual para copiarla al portapapeles con comillas si no las tiene."""
    cita = (contenido_citado or "").strip()
    if not cita or cita == "Sin cita textual expresa":
        return ""
    if cita[0] in ('"', "“", "«"):
        return cita
    return f'"{cita}"'


def formatear_datos_sentencia(numero: str, sala: str, fecha: str) -> str:
    """Genera una referencia formal con número, sala y fecha de la sentencia."""
    num_clean = str(numero or "").strip() or "S/N"
    sala_clean = str(sala or "").strip()
    fecha_clean = str(fecha or "").strip()

    if "tribunal supremo" in sala_clean.lower():
        sala_texto = f"de la {sala_clean}" if sala_clean.lower().startswith("sala") else sala_clean
    elif sala_clean.lower().startswith("sala"):
        sala_texto = f"de la {sala_clean} del Tribunal Supremo de Justicia"
    elif sala_clean:
        sala_texto = f"({sala_clean}) del Tribunal Supremo de Justicia"
    else:
        sala_texto = "del Tribunal Supremo de Justicia"

    if not fecha_clean or fecha_clean.lower() in ("s/f", "sin fecha", "none", "null"):
        return f"Sentencia N° {num_clean} {sala_texto} (S/F)"
    return f"Sentencia N° {num_clean} {sala_texto}, de fecha {fecha_clean}"


def construir_textos_copia(numero: str, sala: str, fecha: str, contenido_citado: str) -> tuple[str, str, str]:
    """Devuelve la tupla (cita_textual, datos_sentencia, ambos) lista para copiar."""
    texto_cita = formatear_cita_textual(contenido_citado)
    texto_datos = formatear_datos_sentencia(numero, sala, fecha)
    texto_ambos = f"{texto_datos}:\n\n{texto_cita}" if texto_cita else ""
    return texto_cita, texto_datos, texto_ambos


def _render_boton_copia(etiqueta: str, texto: str, tooltip: str, tooltip_vacio: str) -> str:
    """Construye el HTML de un botón de copiado individual."""
    if texto:
        b64 = _codificar_b64(texto)
        return (
            f'<button type="button" class="tsj-copy-btn" '
            f'data-copy-b64="{b64}" title="{html.escape(tooltip, quote=True)}">'
            f'{html.escape(etiqueta)}</button>'
        )
    return (
        f'<button type="button" class="tsj-copy-btn" disabled '
        f'title="{html.escape(tooltip_vacio, quote=True)}">'
        f'{html.escape(etiqueta)}</button>'
    )


def render_footer_tarjeta(numero: str, sala: str, fecha: str, contenido_citado: str, enlace: str) -> None:
    """Renderiza los botones de copiado (cita, datos o ambos) y el enlace oficial TSJ."""
    texto_cita, texto_datos, texto_ambos = construir_textos_copia(numero, sala, fecha, contenido_citado)
    botones_html = "".join([
        _render_boton_copia(
            "💬 Cita textual",
            texto_cita,
            "Copiar únicamente la cita textual",
            "No hay cita textual disponible",
        ),
        _render_boton_copia(
            "📄 Datos de la sentencia",
            texto_datos,
            "Copiar número, sala y fecha de la sentencia",
            "Sin datos disponibles",
        ),
        _render_boton_copia(
            "📑 Ambos",
            texto_ambos,
            "Copiar los datos de la sentencia junto con la cita textual",
            "Se requiere cita textual para copiar ambos",
        ),
    ])

    link_html = ""
    if enlace:
        safe_url = html.escape(enlace, quote=True)
        link_html = (
            f'<a class="tsj-link-original" href="{safe_url}" '
            f'target="_blank" rel="noopener noreferrer">'
            f'🔗 Ver sentencia original en el TSJ</a>'
        )

    footer_html = (
        f'<div class="tsj-card-footer">'
        f'<div class="tsj-copy-group">'
        f'<span class="tsj-copy-label">📋 Copiar:</span>'
        f'{botones_html}'
        f'</div>'
        f'{link_html}'
        f'</div>'
    )
    st.html(footer_html, width="stretch")


_COPY_JS_SCRIPT = """
<script>
(function() {
    if (window.__tsjCopyBound) return;
    window.__tsjCopyBound = true;

    function decodeBase64Utf8(b64) {
        var bin = atob(b64);
        var bytes = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) {
            bytes[i] = bin.charCodeAt(i);
        }
        return new TextDecoder("utf-8").decode(bytes);
    }

    function fallbackCopy(text) {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "0";
        ta.style.left = "0";
        ta.style.width = "1px";
        ta.style.height = "1px";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        try {
            ta.focus({ preventScroll: true });
        } catch (_) {
            ta.focus();
        }
        ta.select();
        var ok = false;
        try {
            ok = document.execCommand("copy");
        } catch (e) {
            ok = false;
        }
        document.body.removeChild(ta);
        return ok;
    }

    function copyText(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text).catch(function() {
                if (!fallbackCopy(text)) {
                    throw new Error("Copy failed");
                }
            });
        }
        return new Promise(function(resolve, reject) {
            if (fallbackCopy(text)) {
                resolve();
            } else {
                reject(new Error("Copy failed"));
            }
        });
    }

    document.addEventListener("click", function(ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest(".tsj-copy-btn") : null;
        if (!btn || btn.disabled) return;
        var b64 = btn.getAttribute("data-copy-b64") || "";
        if (!b64) return;

        var text = "";
        try {
            text = decodeBase64Utf8(b64);
        } catch (e) {
            return;
        }

        if (!btn.dataset.origLabel) {
            btn.dataset.origLabel = btn.textContent;
        }

        copyText(text).then(function() {
            if (btn.__resetTimer) {
                clearTimeout(btn.__resetTimer);
            }
            btn.classList.add("copied");
            btn.textContent = "✅ ¡Copiado!";
            btn.__resetTimer = setTimeout(function() {
                btn.classList.remove("copied");
                if (btn.dataset.origLabel) {
                    btn.textContent = btn.dataset.origLabel;
                }
            }, 1800);
        }).catch(function() {
            if (btn.__resetTimer) {
                clearTimeout(btn.__resetTimer);
            }
            btn.textContent = "⚠️ No se pudo copiar";
            btn.__resetTimer = setTimeout(function() {
                if (btn.dataset.origLabel) {
                    btn.textContent = btn.dataset.origLabel;
                }
            }, 2000);
        });
    });
})();
</script>
"""


# ─── Aplicación Principal ─────────────────────────────────────────────────────
def main():
    # Encabezado formal adaptable a modo claro y oscuro + manejador global de copiado
    st.html(
        """
        <div class="main-title">⚖️ Sistema de Jurisprudencia del Tribunal Supremo de Justicia</div>
        <div class="sub-title">Consulta, búsqueda temática y lectura especializada de sentencias y criterios vinculantes</div>
        <div class="credit-line">📖 <em>Compilado por Lester Bracovich</em></div>
        """
        + _COPY_JS_SCRIPT,
        unsafe_allow_javascript=True,
    )

    df = load_data()

    if df.empty:
        st.warning("⚠️ No se encontró la base de datos `jurisprudencia_ia.db` o aún no contiene registros. Ejecuta `extractor.py` primero.")
        st.stop()

    # ─── Barra lateral de Filtros y Estadísticas ──────────────────────────────
    with st.sidebar:
        st.header("🔍 Filtros de Búsqueda")

        # Filtro por Sala
        salas_disponibles = sorted([s for s in df["sala"].dropna().unique() if s.strip()])
        opciones_sala = ["Todas las Salas"] + salas_disponibles
        sala_seleccionada = st.selectbox("Seleccione la Sala:", opciones_sala)

        # Filtro por Texto / Palabras Clave
        busqueda_lateral = st.text_input(
            "Buscar en extractos o citas:",
            placeholder="Ej: casación, acusación, 433...",
            help="Busca coincidencias en el tema, resumen jurídico, cita textual o número de sentencia.",
        )

        st.divider()

        # Selector de Límite de Visualización y Orden Cronológico
        st.subheader("📑 Visualización")
        orden_opcion = st.selectbox(
            "Orden cronológico:",
            options=["Más recientes primero", "Más antiguas primero"],
            index=0,
            help="Ordena las sentencias por su fecha de publicación (las entradas sin fecha se muestran al final).",
        )
        limite_opcion = st.selectbox(
            "Criterios en pantalla:",
            options=[20, 50, 100, "Todos"],
            index=0,
            help="Por defecto se muestran los últimos 20 criterios para maximizar la velocidad y ligereza de la página.",
        )

        # ─── Aplicación Inmediata de Filtros ─────────────────────────────────
        df_filtrado = df.copy()

        # Filtro por sala
        if sala_seleccionada != "Todas las Salas":
            df_filtrado = df_filtrado[df_filtrado["sala"] == sala_seleccionada]

        # Filtro por palabra clave (barra lateral)
        if busqueda_lateral.strip():
            termino = busqueda_lateral.strip().lower()
            condicion = (
                df_filtrado["tema"].str.lower().str.contains(termino, na=False)
                | df_filtrado["extracto"].str.lower().str.contains(termino, na=False)
                | df_filtrado["contenido_citado"].str.lower().str.contains(termino, na=False)
                | df_filtrado["ratificaciones"].str.lower().str.contains(termino, na=False)
                | df_filtrado["numero"].astype(str).str.lower().str.contains(termino, na=False)
            )
            df_filtrado = df_filtrado[condicion]

        # Ordenar cronológicamente antes de mostrar lista rápida y tarjetas
        df_filtrado = ordenar_cronologicamente(
            df_filtrado, ascendente=(orden_opcion == "Más antiguas primero")
        )

        # ─── Panel de Resultados en la Barra Lateral ──────────────────────────
        hay_filtro = bool(busqueda_lateral.strip()) or (sala_seleccionada != "Todas las Salas")
        cant_hallazgos = len(df_filtrado)

        if hay_filtro:
            if cant_hallazgos > 0:
                st.success(f"🎯 **{cant_hallazgos}** resultado(s) hallado(s)")
                with st.expander(f"📋 Lista rápida ({cant_hallazgos})", expanded=False):
                    for _, r in df_filtrado.iterrows():
                        num = r["numero"]
                        s_corta = "Penal" if "Penal" in r["sala"] else ("Const." if "Const" in r["sala"] else r["sala"][:12])
                        tema_c = f" - *{r['tema'][:26]}...*" if r.get("tema") else ""
                        st.markdown(f"• **Sent. N° {num}** ({s_corta}){tema_c}")
            else:
                st.error("❌ No se hallaron resultados para esta búsqueda.")
        else:
            st.caption(f"ℹ️ Base de datos: {len(df)} extractos registrados.")

        st.divider()

        # Métricas de la Base de Datos
        st.subheader("📊 Estadísticas")
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.metric("Total Extractos", len(df))
        with col_m2:
            sentencias_unicas = len(df.drop_duplicates(subset=["numero", "sala", "fecha"]))
            st.metric("Sentencias Únicas", sentencias_unicas)
        
        st.markdown("**Distribución por Sala:**")
        distribucion = df["sala"].value_counts()
        for s_nombre, s_cant in distribucion.items():
            st.markdown(f"- **{s_nombre}:** {s_cant}")

        st.divider()

        # Botón para recargar datos
        if st.button("🔄 Actualizar datos"):
            st.cache_data.clear()
            st.rerun()

        st.markdown(
            """
            <div style="text-align: center; color: #64748b; font-size: 0.85rem; margin-top: 1.5rem; border-top: 1px solid #334155; padding-top: 12px;">
                📚 Compilado por <strong>Lester Bracovich</strong>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ─── Buscador Rápido Principal ───────────────────────────────────────────
    busqueda_principal = st.text_input(
        "🔍 Buscar jurisprudencia (tema, palabra clave, expediente o N° de sentencia):",
        value=busqueda_lateral,
        placeholder="Ej: casación, inmotivación, dosimetría, 433, acusación, sobreseimiento...",
        help="Filtra en tiempo real sobre el tema jurídico, el criterio/resumen o la cita textual.",
    )

    # Si se escribió en la barra principal y es diferente al lateral, aplicar filtro
    if busqueda_principal.strip() and busqueda_principal.strip() != busqueda_lateral.strip():
        termino = busqueda_principal.strip().lower()
        condicion = (
            df_filtrado["tema"].str.lower().str.contains(termino, na=False)
            | df_filtrado["extracto"].str.lower().str.contains(termino, na=False)
            | df_filtrado["contenido_citado"].str.lower().str.contains(termino, na=False)
            | df_filtrado["ratificaciones"].str.lower().str.contains(termino, na=False)
            | df_filtrado["numero"].astype(str).str.lower().str.contains(termino, na=False)
        )
        df_filtrado = df_filtrado[condicion]
        hay_filtro = True
        cant_hallazgos = len(df_filtrado)

    # ─── Control de Paginación y Estado ──────────────────────────────────────
    if "pagina_actual" not in st.session_state:
        st.session_state["pagina_actual"] = 1

    # Resetear a página 1 si cambian los filtros, búsqueda u orden
    firma_filtro = (sala_seleccionada, busqueda_lateral.strip(), busqueda_principal.strip(), orden_opcion, str(limite_opcion))
    if st.session_state.get("ultima_firma_filtro") != firma_filtro:
        st.session_state["pagina_actual"] = 1
        st.session_state["ultima_firma_filtro"] = firma_filtro

    total_registros = len(df_filtrado)
    if limite_opcion == "Todos":
        tamano_pagina = max(1, total_registros)
        total_paginas = 1
    else:
        tamano_pagina = int(limite_opcion)
        total_paginas = max(1, (total_registros + tamano_pagina - 1) // tamano_pagina)

    # Asegurar que la página esté en rango
    if st.session_state["pagina_actual"] > total_paginas:
        st.session_state["pagina_actual"] = total_paginas
    if st.session_state["pagina_actual"] < 1:
        st.session_state["pagina_actual"] = 1

    pagina_actual = st.session_state["pagina_actual"]

    # Segmentar los registros correspondientes a la página activa
    inicio = (pagina_actual - 1) * tamano_pagina
    fin = min(inicio + tamano_pagina, total_registros)
    df_mostrar = df_filtrado.iloc[inicio:fin] if total_registros > 0 else df_filtrado

    # ─── Barra informativa de resultados (Cabecera principal) ─────────────────
    col_info, col_export = st.columns([3, 1])
    with col_info:
        if not hay_filtro:
            st.info(
                f"📌 **Mostrando criterios del {inicio + 1} al {fin}** *(de {total_registros} totales)*. "
                f"Página **{pagina_actual}** de **{total_paginas}**. Usa los botones de abajo o el buscador para explorar más."
            )
        else:
            st.markdown(
                f"🎯 **{total_registros}** resultado(s) encontrados. "
                f"Mostrando del **{inicio + 1} al {fin}** (Página **{pagina_actual}** de **{total_paginas}**)."
            )
    with col_export:
        csv_data = df_filtrado.to_csv(index=False, sep=";", encoding="utf-8-sig")
        st.download_button(
            label="📥 Exportar vista (CSV)",
            data=csv_data,
            file_name="jurisprudencia_filtrada.csv",
            mime="text/csv",
        )

    def render_paginacion(posicion="top"):
        """Renderiza los controles de cambio de página si hay más de 1 página."""
        if total_paginas <= 1:
            return
        c_prev, c_info_pag, c_next = st.columns([1, 2, 1])
        with c_prev:
            if st.button("⬅️ Anterior", key=f"btn_prev_{posicion}", disabled=(pagina_actual <= 1), use_container_width=True):
                st.session_state["pagina_actual"] -= 1
                st.rerun()
        with c_info_pag:
            st.markdown(
                f"<div style='text-align: center; padding-top: 6px; font-weight: 600; font-size: 0.98rem; color: var(--text-color);'>"
                f"Página {pagina_actual} de {total_paginas} &nbsp;•&nbsp; ({inicio + 1} - {fin} de {total_registros})"
                f"</div>",
                unsafe_allow_html=True,
            )
        with c_next:
            if st.button("Siguiente ➡️", key=f"btn_next_{posicion}", disabled=(pagina_actual >= total_paginas), use_container_width=True):
                st.session_state["pagina_actual"] += 1
                st.rerun()

    # Controles de paginación superiores
    render_paginacion("top")
    st.write("")

    # ─── Renderizado de Tarjetas de Sentencias ────────────────────────────────
    if df_mostrar.empty:
        st.warning("🔍 No se encontraron sentencias con los criterios de búsqueda seleccionados.")
        return

    for _, row in df_mostrar.iterrows():
        numero = row["numero"]
        sala = row["sala"]
        fecha = row["fecha"]
        tema = row.get("tema", "") or ""
        extracto = row["extracto"]
        contenido_citado = row["contenido_citado"]
        ratificaciones = row.get("ratificaciones", "") or ""
        archivo = row["archivo_origen"]
        enlace = str(row.get("enlace", "") or "").strip()

        # Tarjeta limpia con borde
        with st.container(border=True):
            badge_class = get_sala_badge_class(sala)

            # Fila de Encabezado destacando Número, Sala y Fecha en negritas y color blanco
            col_header, col_date = st.columns([3, 1])
            with col_header:
                st.markdown(
                    f"""
                    <div class="metadata-row">
                        <span class="sentencia-header">
                            ⚖️ Sentencia N° <strong>{numero}</strong>
                        </span>
                        &nbsp;&nbsp;
                        <span class="badge-sala {badge_class}"><strong>{sala}</strong></span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with col_date:
                st.markdown(
                    f"""
                    <div class="fecha-label">
                        📅 <strong>Fecha:</strong> <strong>{fecha}</strong>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            # Etiqueta de Tema / Punto Jurídico (si está definido)
            if tema.strip():
                st.markdown(
                    f"""
                    <div style="margin-top: 6px; margin-bottom: 8px;">
                        <span class="badge-tema">
                            📌 <strong>Punto Jurídico:</strong> {tema}
                        </span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            st.markdown('<hr class="tarjeta-divider"/>', unsafe_allow_html=True)

            # Línea de Precedente / Ratificación (si existe)
            if ratificaciones.strip():
                st.markdown(
                    f"""
                    <div class="ratif-box">
                        🔁 <strong>Línea de Precedente / Ratificación:</strong> {ratificaciones}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            # Ratio Decidendi / Extracto Jurídico
            if extracto.strip():
                st.markdown("**📌 Criterio Jurídico / Ratio Decidendi:**")
                st.markdown(f'<div class="extracto-text">{extracto}</div>', unsafe_allow_html=True)

            # Contenido Citado Textual (si existe y no es vacío)
            if contenido_citado.strip() and contenido_citado.strip() != "Sin cita textual expresa":
                st.markdown("**📜 Texto Literal Citado en el Documento:**")
                st.markdown(f'<div class="cita-box">"{contenido_citado}"</div>', unsafe_allow_html=True)

            # Barra de acciones de copiado + enlace oficial TSJ
            render_footer_tarjeta(numero, sala, fecha, contenido_citado, enlace)

    # Controles de paginación inferiores (al final de los 20 criterios)
    st.write("")
    render_paginacion("bottom")


if __name__ == "__main__":
    main()
