import logging
import streamlit as st
from components.sidebar import sidebar
from components.page import init_page
from datetime import datetime, timedelta
from components.user_selector import user_selector, get_contributors
import sys
from pathlib import Path
import os

sys.path.append(str(Path(__file__).parent.parent))

from llm_logic.api_request import make_api_request


def main():
    init_page()
    sidebar()

    github_url = st.text_input(
        "Ссылка на GitHub репозиторий",
        placeholder="https://github.com/username/repo",
        help="Введите ссылку на конкретный репозиторий"
    )

    # if github_url:
    # st.markdown( get_contributors(github_url, token=os.environ.get("GITHUB_TOKEN")))
    # github_username = user_selector(contributors)

    github_username = st.text_input("Username для ревью", placeholder="Введите логин пользователя GitHub")

    branch_for_merge_history = st.text_input("Ветка для анализа коммитов",
                                             placeholder="master/main ")

    selected_dates = st.date_input(
        "Выберите диапазон дат для ревью",
        value=[datetime.now() - timedelta(days=7), datetime.now()],  # По умолчанию: последние 7 дней
        format="YYYY-MM-DD",  # Формат даты
        help="Выберите начальную и конечную дату",
    )

    # Проверяем, что выбрано 2 даты (диапазон)
    if len(selected_dates) == 2:
        start_date, end_date = selected_dates
        st.success(f"Выбран период с {start_date} по {end_date}")
    elif len(selected_dates) == 1:
        st.warning("Выберите диапазон дат (дважды кликните на календаре)")
        start_date = end_date = selected_dates[0]
    else:
        st.error("Ошибка при выборе дат")

    st.subheader("Запрос на анализ кода")
    user_query = st.text_area(
        "Если хотите уточнить запрос к review, то можете написать тут:",
        height=150
    )

    if st.button("Выполнить code review"):
        if github_url and (len(github_username) != 0) and (len(branch_for_merge_history) != 0):
            with st.spinner("Анализирую код..."):

                # big analysis
                response = make_api_request(user_query)
                if response:
                    st.markdown("### Результат анализа")
                    st.markdown(response)
        else:
            st.warning("Пожалуйста заполните данные")


if __name__ == "__main__":
    main()
