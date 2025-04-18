import streamlit as st
import os


def configure_session_state():
    if "OPENAI_API_KEY" not in st.session_state:
        st.session_state["OPENAI_API_KEY"] = ""
    if "GIT_URL" not in st.session_state:
        st.session_state["GIT_URL"] = ""


def set_open_api_key(api_key: str):
    st.session_state["OPENAI_API_KEY"] = api_key
    os.environ["OPENAI_API_KEY"] = api_key


def set_git_url(git_url: str):
    st.session_state["GITHUB_BOT_ACCESS_TOKEN"] = git_url
    os.environ["GITHUB_BOT_ACCESS_TOKEN"] = git_url
    os.environ["github_secret"] = git_url

и
def set_git_ATP(git_url: str):
    st.session_state["GIT_URL"] = git_url
    os.environ["GIT_URL"] = git_url


def sidebar():
    configure_session_state()

    with st.sidebar:
        st.markdown("## Настройки")
        api_key = st.text_input(
            "OpenAI API Key",
            type="password",
            placeholder="Введите ваш API ключ (sk-...)",
            value=st.session_state["OPENAI_API_KEY"]
        )

        repo_url = st.text_input(
            "Repository URL",
            placeholder="Ссылка на репозиторий (https://github.com/...)",
            value=st.session_state["GIT_URL"]
        )

        if api_key:
            set_open_api_key(api_key)
        if repo_url:
            set_git_url(repo_url)

        if not (api_key and repo_url):
            st.warning("⚠️ Требуется API ключ и ссылка на репозиторий")
            st.stop()
