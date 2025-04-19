import os
import time
import streamlit as st
from datetime import datetime
from dateutil.parser import isoparse  # Import for parsing ISO 8601 dates
import logging
from .core import fetch_github_data, CodeChangeRAG
from datetime import datetime, timezone
logger = logging.getLogger(__name__)

OUTPUT_DIR_BASE = "github_data_structured"
CODER_ANALYSIS_OUTPUT_DIR = "coder_analysis"  # New directory for saving coder analysis results
pr_state_to_fetch = 'closed'  # Fetch closed PRs for analysis


def code_review(
        github_owner,
        github_repo,
        coder_to_analyze_login,
        branch_for_merge_history='master',
        analysis_start_date_str="2025-01-01T00:00:00Z",
        analysis_end_date_str="2025-04-18T23:59:59Z"
):
    # --- Date Range for Filtering Changes for Analysis ---
    # Set the start and end dates for filtering changes for coder analysis
    # The format should be ISO 8601:YYYY-MM-DDTHH:MM:SSZ
    # Example: "2024-01-01T00:00:00Z"
    # try:
    #     analysis_start_date = isoparse(analysis_start_date_str) if analysis_start_date_str else None
    #     analysis_end_date = isoparse(analysis_end_date_str) if analysis_end_date_str else None
    #     if analysis_start_date and analysis_end_date and analysis_start_date > analysis_end_date:
    #         print("Warning: analysis_start_date is after analysis_end_date. No changes will be analyzed.")
    #         analysis_start_date = analysis_end_date = None  # Invalidate date range if illogical
    # except ValueError as e:
    #     print(
    #         f"Error parsing date strings: {e}. Please ensure dates are in ISO 8601 format (YYYY-MM-DDTHH:MM:SSZ). Skipping date filtering for analysis.")
    #     analysis_start_date = None
    #     analysis_end_date = None

    # --- Data Fetching ---

    # Выводим информацию в виде markdown в Streamlit
    st.markdown(f"""
    ### Начинаем сбор данных для репозитория:  
    **Репозиторий:** `{github_owner}/{github_repo}`  
    **Состояние PR для выборки:** `{pr_state_to_fetch}`  
    **Ветка для истории слияний:** `{branch_for_merge_history}`  
    **Диапазон дат анализа:** `{analysis_start_date_str}` до `{analysis_end_date_str}`   

    ⚠️ **Внимание:**  
    - Процесс сбора данных может занять много времени  
    - Может потребоваться значительное место на диске и API-запросы  
    """)

    # Pass the date range to fetch_github_data for filtering merge commits during fetch
    # Note: PR fetching is NOT filtered by date here, only merge history is.
    # PRs will be filtered by date AFTER fetching.
    fetched_pr_data, fetched_merge_history = fetch_github_data(
        github_owner,
        github_repo,
        pr_state=pr_state_to_fetch,
        branch_for_merge_history=branch_for_merge_history,
        merge_history_since=analysis_start_date_str,  # Use analysis date range for fetching merge commits
        merge_history_until=analysis_end_date_str  # Use analysis date range for fetching merge commits
    )

    start_time = time.time()
    end_time = time.time()

    execution_time = end_time - start_time

    if fetched_pr_data or fetched_merge_history:
        # Успешный сценарий с данными
        st.success("✅ Успешное завершение обработки данных!")

        col1, col2 = st.columns(2)
        with col1:
            st.metric(label="Загружено PR", value=len(fetched_pr_data))
        with col2:
            st.metric(label="Загружено коммитов", value=len(fetched_merge_history))

        st.info(f"**Время выполнения:** {execution_time:.2f} секунд")

        with st.expander("Детали сохранения"):
            st.write(f"📁 **Папка с данными:** `{OUTPUT_DIR_BASE}`")
            st.write(f"⏱ **Начало:** `{datetime.fromtimestamp(start_time)}`")
            st.write(f"⏱ **Окончание:** `{datetime.fromtimestamp(end_time)}`")

    else:

        st.warning("⚠️ Данные не были обработаны")
        st.info(f"**Время выполнения:** {execution_time:.2f} секунд")

        with st.expander("Детали"):
            st.write(f"⏱ **Начало:** `{datetime.fromtimestamp(start_time)}`")
            st.write(f"⏱ **Окончание:** `{datetime.fromtimestamp(end_time)}`")
            st.write("ℹ️ Возможные причины: нет данных в указанном диапазоне или проблемы с доступом")

    # --- Filter Fetched Changes by Date and Coder for Analysis ---
    changes_for_coder_analysis = []
    all_fetched_changes = fetched_pr_data + fetched_merge_history

    logging.info(
        f"\nFiltering fetched changes by date ({analysis_start_date_str} to {analysis_end_date_str}) and coder ({coder_to_analyze_login}) for analysis...")
    for change in all_fetched_changes:
        change_type = change.get('request_type')
        change_id = change.get('request_id')
        change_date_str = None
        author_login = None
        committer_login = None
        merged_by_login = None

        # Determine the relevant date and author/committer based on change type
        if change_type == 'pr':
            change_date_str = change.get('merged_at') or change.get('closed_at') or change.get('updated_at')
            author_login = change.get('author_login')
            merged_by_login = change.get('merged_by_login')
        elif change_type == 'merge_commit':
            change_date_str = change.get('committer_date') or change.get('author_date')
            author_login = change.get('api_author_login')
            committer_login = change.get('api_committer_login')

        if change_date_str:
            try:
                start_date = datetime.fromisoformat(analysis_start_date_str).replace(tzinfo=timezone.utc)
                end_date = datetime.fromisoformat(analysis_end_date_str).replace(tzinfo=timezone.utc)
                change_date = isoparse(change_date_str).astimezone(timezone.utc)  # Convert to UTC

                # Check if the change is within the date range AND the coder is the author, committer, or merger
                if start_date <= change_date <= end_date:
                    if author_login in coder_to_analyze_login or \
                            committer_login in coder_to_analyze_login or \
                            merged_by_login in coder_to_analyze_login:
                        changes_for_coder_analysis.append(change)
            except ValueError:
                logging.warning(
                    f"Warning: Could not parse date '{change_date_str}' for {change_type.upper()} {change_id}. Skipping filtering for this change.")
                pass  # Skip if date parsing fails
    logging.info(
        f"Found {len(changes_for_coder_analysis)} changes (PRs and Merge Commits) by {coder_to_analyze_login} within the specified date range for analysis.")

    # --- Coder Activity Analysis ---
    if not changes_for_coder_analysis:
        print(f"No changes found for coder {coder_to_analyze_login} within the specified date range.")
    else:
        # Initialize RAG system
        rag_system = CodeChangeRAG(data_path=OUTPUT_DIR_BASE)
        rag_system.initialize_llm()  # Initialize the LLM once

        if rag_system.llm is None:
            print("LLM was not initialized. Cannot perform RAG analysis for coder activity.")
        else:
            # Perform the coder activity analysis using the RAG system
            coder_analysis_results = rag_system.analyze_coder_activity(
                coder_to_analyze_login,
                changes_for_coder_analysis
            )

            # Create directory to save coder analysis results
            os.makedirs(CODER_ANALYSIS_OUTPUT_DIR, exist_ok=True)
            print(f"\nSaving coder analysis results to directory: {CODER_ANALYSIS_OUTPUT_DIR}")

    # Создаем красивый заголовок анализа
    st.header(f"📊 Анализ активности разработчика: {coder_to_analyze_login}")
    st.caption(
        f"Репозиторий: {github_owner}/{github_repo} • Период: {analysis_start_date_str} — {analysis_end_date_str}")

    # Раздел с количественными метриками
    with st.container(border=True):
        st.subheader("📈 Количественные метрики")

        cols = st.columns(4)
        cols[0].metric("Всего изменений", coder_analysis_results.get('total_changes_analyzed', 0))
        cols[1].metric("Всего коммитов", coder_analysis_results.get('total_commits', 0))
        cols[2].metric("Добавлено строк", coder_analysis_results.get('total_additions', 0))
        cols[3].metric("Удалено строк", coder_analysis_results.get('total_deletions', 0))

    # Раздел с качественным анализом
    st.subheader("🧠 Качественный анализ (RAG)")
    rag_analysis = coder_analysis_results.get('analysis_results')

    if isinstance(rag_analysis, dict):
        tabs = st.tabs([f"Вопрос {i + 1}" for i in range(len(rag_analysis))])

        for i, (question, result) in enumerate(rag_analysis.items()):
            with tabs[i]:
                st.markdown(f"**❓ Вопрос:** {question}")
                st.markdown(f"**💡 Ответ:** {result.get('answer', 'N/A')}")

                # Дополнительно можно показать источники
                with st.expander("🔍 Показать источники"):
                    if result.get('sources'):
                        for j, source in enumerate(result['sources']):
                            st.markdown(f"**Источник {j + 1}:**\n{source.get('content_snippet', 'N/A')}")
                    else:
                        st.info("Нет релевантных источников")
    else:
        st.warning(str(rag_analysis))

    # Добавляем кнопку для экспорта (если все же нужно)
    if st.button("📥 Экспортировать анализ в PDF", help="Сохранить результаты анализа в файл"):
        # Здесь можно реализовать экспорт в PDF
        st.toast("Экспорт в PDF пока не реализован", icon="⚠️")

    # Уведомление об успешном завершении
    st.toast(f"Анализ для {coder_to_analyze_login} успешно завершен!", icon="✅")

    print("\n--- Script Finished ---")
