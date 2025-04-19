import logging
import streamlit as st
from components.sidebar import sidebar
from components.page import init_page
from datetime import datetime, timedelta
from components.user_selector import user_selector
import sys
from pathlib import Path
import os
from components.user_date_validation import url_validations

sys.path.append(str(Path(__file__).parent.parent))

from llm_logic.api_request import make_api_request
from llm_logic.code_reviewer import code_review


def main():
    init_page()
    sidebar()

    github_url = st.text_input(
        "Ссылка на GitHub репозиторий",
        placeholder="https://github.com/username/repo",
        help="Введите ссылку на конкретный репозиторий"
    )

    if github_url:
        owner, repo = url_validations(github_url)
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
    # if st.button("фывф"):
    #     st.text([start_date, end_date])
    if st.button("Выполнить code review"):
        if github_url and (len(github_username) != 0) and (len(branch_for_merge_history) != 0):
            with st.spinner("Анализирую код..."):

                # big analysis
                code_review(
                    github_owner=owner,
                    github_repo=repo,
                    coder_to_analyze_login=github_username,
                    branch_for_merge_history=branch_for_merge_history,
                    analysis_start_date_str=start_date,
                    analysis_end_date_str=end_date,
                )
        else:
            st.warning("Пожалуйста заполните данные")


if __name__ == "__main__":
    main()
