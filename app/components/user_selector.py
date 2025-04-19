import streamlit as st
from github import Github

import requests
import logging

logger = logging.getLogger(__name__)


def user_selector(usernames):
    selected_users = []
    st.write("Выберите пользователей:")

    for username in usernames:
        if st.checkbox(username, key=username):
            selected_users.append(username)

    return selected_users


if __name__ == "__main__":
    repo_url = "https://github.com/streamlit/streamlit"
    github_token = None
    contributors = get_contributors(repo_url, github_token)
    print(contributors)