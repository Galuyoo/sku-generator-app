import base64
import streamlit as st

def logo_to_base64(path):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except FileNotFoundError:
        return None

def render_logo(path="logo.png", width=200):
    logo_base64 = logo_to_base64(path)
    if logo_base64:
        st.markdown(
            f"""
            <div style="text-align: center;">
                <img src="data:image/png;base64,{logo_base64}" width="{width}"/>
            </div>
            """,
            unsafe_allow_html=True
        )
    else:
        st.warning("⚠️ Logo couldn't be loaded.")
