import streamlit as st
import os
import re
from urllib.parse import urlparse


def configure_session_state():
    if "YANDEX_API_KEY" not in st.session_state:
        st.session_state["YANDEX_API_KEY"] = ""
    if "FOLDER_ID" not in st.session_state:
        st.session_state["FOLDER_ID"] = ""
    if "GITHUB_BOT_ACCESS_TOKEN" not in st.session_state:
        st.session_state["GITHUB_BOT_ACCESS_TOKEN"] = ""


def set_yandex_api_key(api_key: str):
    st.session_state["YANDEX_API_KEY"] = api_key
    os.environ["YANDEX_API_KEY"] = api_key


def set_git_ATP(atp: str):
    st.session_state["GITHUB_BOT_ACCESS_TOKEN"] = atp
    os.environ["GITHUB_BOT_ACCESS_TOKEN"] = atp


def set_yandex_folder_id(folder_id: str):
    st.session_state["FOLDER_ID"] = folder_id
    os.environ["FOLDER_ID"] = folder_id


def set_repo_url(github_url: str):
    try:
        cleaned_url = github_url.rstrip('/')
        parsed_url = urlparse(cleaned_url)
        if parsed_url.netloc not in ('github.com', 'www.github.com'):
            return False

        path_parts = parsed_url.path.split('/')
        path_parts = [part for part in path_parts if part]
        if len(path_parts) < 2:
            return False

    except (AttributeError, ValueError):
        return False

    st.session_state["GIT_URL"] = github_url
    os.environ["GIT_URL"] = github_url

    match = re.match(r'https?://github\.com/([^/]+)/([^/]+)/?', github_url)

    st.session_state["OWNER"] = match.group(1)
    os.environ["OWNER"] = match.group(1)

    st.session_state["REPO"] = match.group(2)
    os.environ["REPO"] = match.group(2)

    return True


def url_validations(github_url: str):
    try:
        cleaned_url = github_url.rstrip('/')
        parsed_url = urlparse(cleaned_url)
        if parsed_url.netloc not in ('github.com', 'www.github.com'):
            return False

        path_parts = parsed_url.path.split('/')
        path_parts = [part for part in path_parts if part]
        if len(path_parts) < 2:
            return False

    except (AttributeError, ValueError):
        return False

    # st.session_state["GIT_URL"] = github_url
    # os.environ["GIT_URL"] = github_url

    match = re.match(r'https?://github\.com/([^/]+)/([^/]+)/?', github_url)

    # st.session_state["OWNER"] = match.group(1)
    # os.environ["OWNER"] = match.group(1)
    #
    # st.session_state["REPO"] = match.group(2)
    # os.environ["REPO"] = match.group(2)

    return match.group(1), match.group(2)
