import streamlit as st
from .hello_world import hello_world


def init_page():
    st.set_page_config(
        page_title="Code Review Assistant",
        page_icon="🤖",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    st.header("Code Review Assistant")
    hello_world()
