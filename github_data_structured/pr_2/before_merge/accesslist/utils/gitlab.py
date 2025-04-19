import requests
from django.conf import settings

HEADERS = {"PRIVATE-TOKEN": settings.GIT_ACCESS_TOKEN}
PROJECT_ID = 6219


def get_templates_files_from_gitlab(branch_name: str = "master"):
    url = f"https://gitlab.alfastrah.ru/api/v4/projects/{PROJECT_ID}/repository/tree"
    try:
        response = requests.get(url, headers=HEADERS, params={"ref": branch_name})
        if response.status_code == 200:
            file_list = [
                {"id": file["id"], "name": file["name"].split(".")[0], "path": file["path"]}
                for file in response.json()
                if file["name"].endswith(".json")
            ]
            return file_list
        else:
            return []
    except Exception:
        return []


def get_templates_from_gitlab(file_id: int, branch_name: str = "master"):
    url = f"https://gitlab.alfastrah.ru/api/v4/projects/{PROJECT_ID}/repository/blobs/{file_id}/raw"

    try:
        response = requests.get(url, headers=HEADERS, params={"ref": branch_name})
        if response.status_code == 200:
            return response.json()
        return None
    except Exception:
        return None
