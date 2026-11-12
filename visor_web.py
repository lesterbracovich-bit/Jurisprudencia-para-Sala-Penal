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

# ─── Estilos CSS Personalizados ───────────────────────────────────────────────
st.markdown(
    """
    <style>
    /* Estilos generales adaptados para fondo oscuro y lectura clara */
    .main-title {
        font-family: 'Georgia', serif;
        font-size: 2.3rem;
        color: #ffffff !important;
        font-weight: 700;
        margin-bottom: 0.2rem;
    }
    .sub-title {
        color: #cbd5e1 !important;
        font-size: 1.1rem;
        margin-bottom: 1.5rem;
    }
    .sentencia-header {
        font-size: 1.35rem;
        font-weight: 800;
        color: #ffffff !important;
        letter-spacing: 0.5px;
    }
    .fecha-label {
        text-align: right;
        color: #e2e8f0 !important;
        font-size: 1.05rem;
        font-weight: 600;
    }
    .badge-sala {
        padding: 4px 12px;
        border-radius: 12px;
        font-size: 0.9rem;
        font-weight: 700;
        display: inline-block;
    }
    .badge-penal {
        background-color: #7f1d1d !important;
        color: #fee2e2 !important;
        border: 1px solid #b91c1c;
    }
    .badge-const {
        background-color: #1e3a8a !important;
        color: #dbeafe !important;
        border: 1px solid #2563eb;
    }
    .badge-plena {
        background-color: #78350f !important;
        color: #fef3c7 !important;
        border: 1px solid #d97706;
    }
    .badge-sala-default {
        background-color: #334155 !important;
        color: #f8fafc !important;
        border: 1px solid #475569;
    }
    .extracto-text {
        font-size: 1.08rem;
        line-height: 1.7;
        color: #f8fafc !important;
        text-align: justify;
        margin-top: 0.5rem;
        margin-bottom: 0.7rem;
    }
    .cita-box {
        background-color: rgba(30, 41, 59, 0.7) !important;
        border-left: 4px solid #60a5fa !important;
        padding: 14px 18px;
        margin-top: 10px;
        border-radius: 0 8px 8px 0;
        font-style: italic;
        color: #f1f5f9 !important;
        line-height: 1.65;
        font-size: 1.02rem;
        text-align: justify;
    }
    .metadata-row {
        font-size: 1.05rem;
        color: #ffffff !important;
        margin-bottom: 0.4rem;
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
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
    query = """
        SELECT 
            id,
            numero,
            sala,
            COALESCE(fecha, 'Sin fecha') AS fecha,
            COALESCE(extracto, '') AS extracto,
            COALESCE(contenido_citado, '') AS contenido_citado,
            COALESCE(archivo_origen, '') AS archivo_origen
        FROM sentencias
        ORDER BY id DESC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def get_sala_badge_class(sala: str) -> str:
    """Devuelve clase CSS acorde a la sala para destacar visualmente."""
    sala_lower = sala.lower()
    if "penal" in sala_lower:
        return "badge-penal"
    elif "constitucional" in sala_lower:
        return "badge-const"
    elif "plena" in sala_lower:
        return "badge-plena"
    return "badge-sala-default"


# ─── Aplicación Principal ─────────────────────────────────────────────────────
def main():
    # Encabezado formal
    st.markdown('<div class="main-title">⚖️ Sistema de Jurisprudencia del Tribunal Supremo de Justicia</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-title">Consulta, búsqueda temática y lectura especializada de sentencias y criterios vinculantes</div>', unsafe_allow_html=True)

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

        st.divider()

        # Métricas de la Base de Datos
        st.subheader("📊 Estadísticas")
        st.metric("Total sentencias registradas", len(df))
        
        st.markdown("**Distribución por Sala:**")
        distribucion = df["sala"].value_counts()
        for s_nombre, s_cant in distribucion.items():
            st.markdown(f"- **{s_nombre}:** {s_cant}")

        st.divider()

        # Botón para recargar datos
        if st.button("🔄 Actualizar datos"):
            st.cache_data.clear()
            st.rerun()

    # ─── Aplicación de Filtros ────────────────────────────────────────────────
    df_filtrado = df.copy()

    # Filtro por sala
    if sala_seleccionada != "Todas las Salas":
        df_filtrado = df_filtrado[df_filtrado["sala"] == sala_seleccionada]

    # Filtro por palabra clave
    if busqueda.strip():
        termino = busqueda.strip().lower()
        condicion = (
            df_filtrado["extracto"].str.lower().str.contains(termino, na=False)
            | df_filtrado["contenido_citado"].str.lower().str.contains(termino, na=False)
            | df_filtrado["numero"].astype(str).str.lower().str.contains(termino, na=False)
        )
        df_filtrado = df_filtrado[condicion]

    # Barra informativa de resultados
    col_info, col_export = st.columns([3, 1])
    with col_info:
        st.markdown(f"**Resultados encontrados:** `{len(df_filtrado)}` sentencia(s)")
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
        extracto = row["extracto"]
        contenido_citado = row["contenido_citado"]
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

            st.markdown("<hr style='margin: 8px 0 14px 0; border: none; border-top: 1px solid #334155;'/>", unsafe_allow_html=True)

            # Ratio Decidendi / Extracto Jurídico
            if extracto.strip():
                st.markdown("**📌 Criterio Jurídico / Ratio Decidendi:**")
                st.markdown(f'<div class="extracto-text">{extracto}</div>', unsafe_allow_html=True)

            # Contenido Citado Textual (si existe y no es vacío)
            if contenido_citado.strip() and contenido_citado.strip() != "Sin cita textual expresa":
                st.markdown("**📜 Texto Literal Citado en el Documento:**")
                st.markdown(f'<div class="cita-box">"{contenido_citado}"</div>', unsafe_allow_html=True)

            # Pie de tarjeta con archivo de origen
            if archivo:
                st.caption(f"📁 Documento de origen: `{archivo}`")


if __name__ == "__main__":
    main()
