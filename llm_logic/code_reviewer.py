import os
import os
import requests
import json
import base64
import time
import re
import tempfile
import streamlit as st
import shutil
import csv
from datetime import datetime
from dateutil.parser import isoparse  # Import for parsing ISO 8601 dates
from tqdm import tqdm
# Imports for the RAG system
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter, Language
from langchain.vectorstores import Chroma
# Import the YandexGPT LLM
from langchain_community.llms import YandexGPT
# Removed HuggingFacePipeline, AutoTokenizer, AutoModelForCausalLM as they are no longer needed for the LLM
# from transformers import pipeline, AutoTokenizer, AutoModelForCausalLM
# from langchain.llms import HuggingFacePipeline

import logging

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
        analysis_end_date_str="2025-04-18T23:59:59Z",
        github_secret=os.environ.get('GITHUB_BOT_ACCESS_TOKEN'),
):
    # --- Date Range for Filtering Changes for Analysis ---
    # Set the start and end dates for filtering changes for coder analysis
    # The format should be ISO 8601:YYYY-MM-DDTHH:MM:SSZ
    # Example: "2024-01-01T00:00:00Z"

    try:
        analysis_start_date = isoparse(analysis_start_date_str) if analysis_start_date_str else None
        analysis_end_date = isoparse(analysis_end_date_str) if analysis_end_date_str else None
        if analysis_start_date and analysis_end_date and analysis_start_date > analysis_end_date:
            print("Warning: analysis_start_date is after analysis_end_date. No changes will be analyzed.")
            analysis_start_date = analysis_end_date = None  # Invalidate date range if illogical
    except ValueError as e:
        print(
            f"Error parsing date strings: {e}. Please ensure dates are in ISO 8601 format (YYYY-MM-DDTHH:MM:SSZ). Skipping date filtering for analysis.")
        analysis_start_date = None
        analysis_end_date = None

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

    if all_fetched_changes and analysis_start_date and analysis_end_date and coder_to_analyze_login != 'your_coder_login':
        print(
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
                    change_date = isoparse(change_date_str)
                    # Check if the change is within the date range AND the coder is the author, committer, or merger
                    if analysis_start_date <= change_date <= analysis_end_date:
                        if author_login == coder_to_analyze_login or \
                                committer_login == coder_to_analyze_login or \
                                merged_by_login == coder_to_analyze_login:
                            changes_for_coder_analysis.append(change)
                except ValueError:
                    logging.warning(
                        f"Warning: Could not parse date '{change_date_str}' for {change_type.upper()} {change_id}. Skipping filtering for this change.")
                    pass  # Skip if date parsing fails
        logging.info(
            f"Found {len(changes_for_coder_analysis)} changes (PRs and Merge Commits) by {coder_to_analyze_login} within the specified date range for analysis.")
    elif all_fetched_changes and (analysis_start_date is None or analysis_end_date is None):
        logging.info("\nDate range for filtering changes is not valid. Skipping coder analysis date filtering.")
        # If date range is invalid, don't perform analysis
        changes_for_coder_analysis = []
    elif coder_to_analyze_login == 'your_coder_login':
        logging.info("\nCoder login for analysis is not set. Skipping coder analysis.")
        changes_for_coder_analysis = []
    else:
        logging.info("\nNo change data fetched or available for filtering.")

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

            # Save the analysis results to a file
            analysis_filename = os.path.join(CODER_ANALYSIS_OUTPUT_DIR,
                                             f"coder_analysis_{coder_to_analyze_login}_{datetime.now().strftime('%Y%m%d')}.txt")
            with open(analysis_filename, 'w', encoding='utf-8') as analysis_file:
                analysis_file.write(f"Coder Activity Analysis for {coder_to_analyze_login}\n")
                analysis_file.write(f"Period: {analysis_start_date_str} to {analysis_end_date_str}\n")
                analysis_file.write(f"Repository: {github_owner}/{github_repo}\n")
                analysis_file.write("-" * 40 + "\n\n")

                analysis_file.write("Quantitative Metrics:\n")
                analysis_file.write(
                    f"Total Changes Analyzed (PRs + Merge Commits): {coder_analysis_results.get('total_changes_analyzed', 0)}\n")
                analysis_file.write(
                    f"Total Commits (including those in PRs): {coder_analysis_results.get('total_commits', 0)}\n")
                analysis_file.write(f"Total Lines Added: {coder_analysis_results.get('total_additions', 0)}\n")
                analysis_file.write(f"Total Lines Deleted: {coder_analysis_results.get('total_deletions', 0)}\n")
                analysis_file.write("-" * 40 + "\n\n")

                analysis_file.write("Qualitative Analysis (Generated by RAG):\n")
                rag_analysis = coder_analysis_results.get('analysis_results')
                if isinstance(rag_analysis, dict):
                    for question, result in rag_analysis.items():
                        analysis_file.write(f"Question: {question}\n")
                        analysis_file.write(f"Answer: {result.get('answer', 'N/A')}\n")
                        # Optionally include sources in the output file
                        # analysis_file.write("Sources:\n")
                        # if result.get('sources'):
                        #     for i, source in enumerate(result['sources']):
                        #         analysis_file.write(f"  Source {i+1}: {source['content_snippet']}\n")
                        # else:
                        #     analysis_file.write("  No relevant sources found.\n")
                        analysis_file.write("---\n\n")
                else:
                    analysis_file.write(str(rag_analysis))  # Print error message or other non-dict result

            print(f"\nSuccessfully saved coder analysis for {coder_to_analyze_login} to {analysis_filename}")

    print("\n--- Script Finished ---")
