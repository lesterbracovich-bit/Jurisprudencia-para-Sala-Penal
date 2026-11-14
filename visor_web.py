#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Visor Web de Jurisprudencia Venezolana
=====================================
Interfaz interactiva desarrollada en Streamlit para consultar,
filtrar y leer sentencias y extractos jurisprudenciales del TSJ
almacenados en SQLite (jurisprudencia_ia.db).
"""

import os
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
            COALESCE(archivo_origen, '') AS archivo_origen
        FROM sentencias
        ORDER BY id DESC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


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


# ─── Aplicación Principal ─────────────────────────────────────────────────────
def main():
    # Encabezado formal adaptable a modo claro y oscuro
    st.markdown('<div class="main-title">⚖️ Sistema de Jurisprudencia del Tribunal Supremo de Justicia</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-title">Consulta, búsqueda temática y lectura especializada de sentencias y criterios vinculantes</div>', unsafe_allow_html=True)
    st.markdown('<div class="credit-line">📖 <em>Compilado por Lester Bracovich</em></div>', unsafe_allow_html=True)

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
        busqueda = st.text_input(
            "Buscar en extractos o citas:",
            placeholder="Ej: casación, sobreseimiento, acusación...",
            help="Busca coincidencias en el resumen jurídico, la cita textual o el número de sentencia.",
        )

        # ─── Aplicación Inmediata de Filtros ─────────────────────────────────
        df_filtrado = df.copy()

        # Filtro por sala
        if sala_seleccionada != "Todas las Salas":
            df_filtrado = df_filtrado[df_filtrado["sala"] == sala_seleccionada]

        # Filtro por palabra clave
        if busqueda.strip():
            termino = busqueda.strip().lower()
            condicion = (
                df_filtrado["tema"].str.lower().str.contains(termino, na=False)
                | df_filtrado["extracto"].str.lower().str.contains(termino, na=False)
                | df_filtrado["contenido_citado"].str.lower().str.contains(termino, na=False)
                | df_filtrado["ratificaciones"].str.lower().str.contains(termino, na=False)
                | df_filtrado["numero"].astype(str).str.lower().str.contains(termino, na=False)
            )
            df_filtrado = df_filtrado[condicion]

        # ─── Panel de Resultados en la Barra Lateral ──────────────────────────
        hay_filtro = bool(busqueda.strip()) or (sala_seleccionada != "Todas las Salas")
        cant_hallazgos = len(df_filtrado)

        if hay_filtro:
            if cant_hallazgos > 0:
                st.success(f"🎯 **{cant_hallazgos}** resultado(s) hallado(s)")
                with st.expander(f"📋 Lista rápida ({cant_hallazgos})", expanded=True):
                    for _, r in df_filtrado.iterrows():
                        num = r["numero"]
                        s_corta = "Penal" if "Penal" in r["sala"] else ("Const." if "Const" in r["sala"] else r["sala"][:12])
                        tema_c = f" - *{r['tema'][:26]}...*" if r.get("tema") else ""
                        st.markdown(f"• **Sent. N° {num}** ({s_corta}){tema_c}")
            else:
                st.error("❌ No se hallaron resultados para esta búsqueda.")
        else:
            st.caption(f"ℹ️ Mostrando los {cant_hallazgos} extractos registrados.")

        st.divider()

        # Métricas de la Base de Datos
        st.subheader("📊 Estadísticas")
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.metric("Total Extractos", len(df))
        with col_m2:
            sentencias_unicas = len(df.drop_duplicates(subset=["numero", "sala"]))
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

    # ─── Barra informativa de resultados (Cabecera principal) ─────────────────
    col_info, col_export = st.columns([3, 1])
    with col_info:
        st.markdown(f"**Resultados encontrados:** `{len(df_filtrado)}` extracto(s)")
    with col_export:
        csv_data = df_filtrado.to_csv(index=False, sep=";", encoding="utf-8-sig")
        st.download_button(
            label="📥 Exportar vista (CSV)",
            data=csv_data,
            file_name="jurisprudencia_filtrada.csv",
            mime="text/csv",
        )

    st.write("")

    # ─── Renderizado de Tarjetas de Sentencias ────────────────────────────────
    if df_filtrado.empty:
        st.info("No se encontraron sentencias con los criterios seleccionados.")
        return

    for _, row in df_filtrado.iterrows():
        numero = row["numero"]
        sala = row["sala"]
        fecha = row["fecha"]
        tema = row.get("tema", "") or ""
        extracto = row["extracto"]
        contenido_citado = row["contenido_citado"]
        ratificaciones = row.get("ratificaciones", "") or ""
        archivo = row["archivo_origen"]

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


if __name__ == "__main__":
    main()
