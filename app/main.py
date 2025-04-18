import logging
import streamlit as st
from openai import OpenAI
from pathlib import Path
from components.sidebar import sidebar
from components.page import init_page, init_openai_client
from review.review import process_code_review

# Отключаем предупреждения
logging.getLogger().setLevel(logging.ERROR)


def main():
    init_page()
    sidebar()

    if not st.session_state.get("open_api_key_configured"):
        st.warning("☝️ Добавьте API ключ в боковой панели")
        st.stop()

    client = init_openai_client()

    st.subheader("Запрос на анализ кода")
    user_query = st.text_area(
        "Введите код или опишите, что нужно проанализировать:",
        height=150
    )

    if st.button("Выполнить code review"):
        if user_query:
            with st.spinner("Анализирую код..."):
                response = process_code_review(client, user_query)
                if response:
                    st.markdown("### Результат анализа")
                    st.markdown(response)
        else:
            st.warning("Введите код или запрос для анализа")


if __name__ == "__main__":
    main()
