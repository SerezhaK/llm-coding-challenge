import streamlit as st
import os
from .user_date_validation import (configure_session_state, set_yandex_api_key, set_git_ATP,
                                   set_yandex_folder_id, set_yandex_iam_token)
from urllib.parse import urlparse


def sidebar():
    configure_session_state()

    with st.sidebar:
        st.markdown("## Настройки")

        api_key = st.text_input(
            "Yandex API Key",
            type="password",
            placeholder="Введите ваш API ключ",
            value=st.session_state["YANDEX_API_KEY"]
        )

        folder_id = st.text_input(
            "Yandex folder id",
            placeholder="Идентификатор каталога",
            value=st.session_state["FOLDER_ID"]
        )

        git_pat = st.text_input(
            "GitHub PAT",
            type="password",
            placeholder="Ваш GitHub PAT",
            value=st.session_state["GITHUB_BOT_ACCESS_TOKEN"]
        )

        yandex_iam_token = st.text_input(
            "Yandex IAM Token",
            type="password",
            placeholder="Ваш YAM token из yandex",
            value=st.session_state["YAM_TOKEN"]
        )

        if api_key:
            set_yandex_api_key(api_key)
        if folder_id:
            set_yandex_folder_id(folder_id)
        if git_pat:
            set_git_ATP(git_pat)
        if yandex_iam_token:
            set_yandex_iam_token(yandex_iam_token)

        if not (api_key and folder_id and git_pat and yandex_iam_token):
            st.warning("⚠️ Пожалуйста введите все данные для работы. \nPAT можно получить: "
                       "GitHub->settings->Developer Settings->Personal access tokens")
            st.stop()
