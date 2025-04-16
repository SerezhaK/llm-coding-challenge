import streamlit as st


def init_page():
    st.set_page_config(
        page_title="Code Review Assistant",
        page_icon="🤖",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    st.header("Code Review Assistant")


def init_openai_client():
    try:
        return OpenAI(api_key=st.session_state["OPENAI_API_KEY"])
    except Exception as e:
        st.error(f"Ошибка инициализации OpenAI: {str(e)}")
        st.stop()
