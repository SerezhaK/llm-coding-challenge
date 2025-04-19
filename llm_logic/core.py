import os
import requests
import json
import base64
import time
import re
import tempfile
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
from .api_request import make_api_request

GITHUB_PER_PAGE = 100
GITHUB_REQUEST_TIMEOUT = 100
OUTPUT_DIR_BASE = "github_data_structured"
GITHUB_API_VERSION = '2022-11-28'
SUMMARY_CSV_FILE = "github_changes_summary.csv"


def fetch_paginated_data(url, headers, params=None, per_page=GITHUB_PER_PAGE):
    """Fetches all pages for a given paginated GitHub API endpoint."""
    if params is None:
        params = {}
    params['per_page'] = per_page
    all_items = []
    current_url = url

    while current_url:
        response = make_api_request(current_url, headers=headers, params=params if '?' not in current_url else None,
                                    timeout=GITHUB_REQUEST_TIMEOUT)

        if not response:
            print("Failed to fetch paginated data page. Stopping pagination.")
            break

        if response and response.status_code == 200:
            try:
                items_page = response.json()
                if not items_page or not isinstance(items_page, list):
                    break
                all_items.extend(items_page)

                if 'next' in response.links:
                    current_url = response.links['next']['url']
                    params = None  # Reset params for subsequent requests using next link
                else:
                    current_url = None
            except json.JSONDecodeError as e:
                print(f"Error decoding JSON from {current_url}: {e}")
                break
            except Exception as e:
                print(f"An unexpected error occurred processing page data: {e}")
                break

        else:
            print(f"Stopping pagination. Received status {response.status_code} for a page.")
            break

    return all_items


# --- File Saving Helper ---

def save_file(content, base_dir, relative_path):
    """
    Saves content to a file, creating necessary subdirectories. Returns True on success, False on failure.
    """
    if content is None or content == "":
        return False
    try:
        full_path = os.path.join(base_dir, relative_path)
        parent_dir = os.path.dirname(full_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except IOError as e:
        print(f"          Error writing file {full_path}: {e}")
        return False
    except Exception as e:
        print(f"          Unexpected error saving file {full_path}: {e}")
        return False


# --- Linked Issue Parsing Helper ---

def parse_linked_issues(text):
    """
    Rudimentary parsing for linked issue references (GitHub, Jira style) in text.
    Returns a sorted list of unique issue references found.
    """
    if not text: return []
    github_refs_keyword = re.findall(r'(?:close(?:s|d)?|resolve(?:s|d)?|fix(?:es|ed)?)\s+#(\d+)', text, re.IGNORECASE)
    github_refs_simple = re.findall(r'(?<![a-zA-Z0-9])#(\d+)\b', text)
    jira_refs = re.findall(r'\b([A-Z][A-Z0-9_]+-\d+)\b', text)
    issues = set()
    for ref in github_refs_keyword: issues.add(f"GH-{ref}")
    for ref in github_refs_simple:
        if f"GH-{ref}" not in issues: issues.add(f"GH-{ref}")
    for ref in jira_refs: issues.add(ref)
    return sorted(list(issues))


# --- GitHub Data Fetching Functions ---

def github_get_file_content(owner, repo, file_path, commit_sha, headers):
    """Get decoded content of a file from GitHub."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={commit_sha}"
    response = make_api_request(api_url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
    if response and response.status_code == 200:
        try:
            content_data = response.json()
            if isinstance(content_data, dict) and content_data.get('type') == 'file' and 'content' in content_data:
                if content_data.get('encoding') == 'base64':
                    return base64.b64decode(content_data['content'].replace('\n', '')).decode('utf-8', errors='replace')
                else:
                    return content_data['content']
            elif isinstance(content_data, dict) and content_data.get('type') in ['dir', 'submodule', 'symlink']:
                return ""  # Return empty string for non-file types
            else:
                print(
                    f"        Warning: Could not get file content for {file_path} @ {commit_sha[:7]}. Unexpected format.")
                return ""
        except json.JSONDecodeError as e:
            print(f"        Error decoding JSON response for file content {file_path} @ {commit_sha[:7]}: {e}")
            return ""
    elif response and response.status_code == 404:
        # File not found at this commit, which is expected for added/removed files
        return ""
    else:
        print(
            f"        Warning: Failed to fetch file content for {file_path} @ {commit_sha[:7]}. Status: {response.status_code if response else 'N/A'}")
        return ""


def github_process_commit_files_list(owner, repo, commit_sha, headers):
    """Fetches the list of changed files for a specific commit."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{commit_sha}"
    response = make_api_request(api_url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
    if not response or response.status_code != 200:
        print(f"    Failed to fetch commit details for {commit_sha[:7]}. Skipping file list processing.")
        return [], 0, 0, 0

    try:
        commit_details = response.json()
        if not isinstance(commit_details, dict):
            print(f"    ERROR: Commit details for {commit_sha[:7]} is not a dictionary. Skipping file list processing.")
            return [], 0, 0, 0

        files_list = commit_details.get('files', [])
        if not files_list:
            return [], 0, 0, 0

        processed_files_metadata = []
        total_additions = 0
        total_deletions = 0
        total_changes = 0

        for f in files_list:
            if not isinstance(f, dict) or 'filename' not in f or 'status' not in f:
                print(f"Warning: Skipping invalid file entry for commit {commit_sha[:7]}: {f}")
                continue

            additions = f.get('additions', 0)
            deletions = f.get('deletions', 0)
            changes = f.get('changes', 0)

            total_additions += additions
            total_deletions += deletions
            total_changes += changes

            processed_files_metadata.append({
                'filename': f['filename'],
                'status': f['status'],
                'additions': additions,
                'deletions': deletions,
                'changes': changes,
                'sha': f.get('sha'),  # Blob SHA
                'blob_url': f.get('blob_url'),
                'raw_url': f.get('raw_url'),
                'patch': f.get('patch'),  # Patch content is sometimes included here
                'patch_saved': False,  # Flag to track if patch was saved
                'content_base_saved': False,  # Flag to track if base content was saved
                'content_head_saved': False,  # Flag to track if head content was saved
                'previous_filename': f.get('previous_filename')  # For renamed files
            })
        return processed_files_metadata, total_additions, total_deletions, total_changes

    except json.JSONDecodeError as e:
        print(
            f"    Error decoding JSON response for commit details {commit_sha[:7]}: {e}. Skipping file list processing.")
        return [], 0, 0, 0
    except Exception as e:
        print(
            f"    Unexpected error processing commit files list for {commit_sha[:7]}: {e}. Skipping file list processing.")
        return [], 0, 0, 0


def github_process_pr_files(owner, repo, pr_number, base_sha, head_sha, headers, pr_output_dir):
    """Fetches and saves changed files (before/after content, patch) for a GitHub PR."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    files_list = fetch_paginated_data(api_url, headers=headers, per_page=100)
    if not files_list:
        print(f"    No files found or error fetching files for GitHub PR #{pr_number}.")
        return []

    print(f"    Processing {len(files_list)} files for GitHub PR #{pr_number}...")

    before_dir = os.path.join(pr_output_dir, "before_merge")
    after_dir = os.path.join(pr_output_dir, "after_merge")
    patch_dir = os.path.join(pr_output_dir, "changed_files")
    os.makedirs(before_dir, exist_ok=True)
    os.makedirs(after_dir, exist_ok=True)
    os.makedirs(patch_dir, exist_ok=True)

    processed_files_metadata = []
    for f in tqdm(files_list, desc=f"Processing files for GitHub PR #{pr_number}"):
        if not isinstance(f, dict) or 'filename' not in f or 'status' not in f:
            print(f"Warning: Skipping invalid file entry for GitHub PR #{pr_number}: {f}")
            continue

        filename = f['filename']
        status = f['status']

        content_base = ""
        # Fetch base content only if the file wasn't added and base_sha is available
        if status != 'added' and base_sha:
            content_base = github_get_file_content(owner, repo, filename, base_sha, headers)
            if content_base:
                save_file(content_base, before_dir, filename)

        content_head = ""
        # Fetch head content only if the file wasn't removed/deleted and head_sha is available
        if status not in ['removed', 'deleted'] and head_sha:
            content_head = github_get_file_content(owner, repo, filename, head_sha, headers)
            if content_head:
                save_file(content_head, after_dir, filename)

        patch_content = f.get('patch')
        if patch_content:
            # Use original filename for patch file name
            patch_filename = os.path.basename(filename) + ".patch"
            save_file(patch_content, patch_dir, patch_filename)

        processed_files_metadata.append({
            'filename': filename,
            'status': status,
            'additions': f.get('additions', 0),
            'deletions': f.get('deletions', 0),
            'changes': f.get('changes', 0),
            'sha': f.get('sha'),  # Blob SHA
            'blob_url': f.get('blob_url'),
            'raw_url': f.get('raw_url'),
            'patch_saved': bool(patch_content),
            'content_base_saved': bool(content_base),
            'content_head_saved': bool(content_head),
            'previous_filename': f.get('previous_filename')  # For renamed files
        })
    return processed_files_metadata


def github_get_pr_reviews(owner, repo, pr_number, headers):
    """Fetches reviews for a GitHub PR."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    reviews = fetch_paginated_data(api_url, headers=headers, per_page=GITHUB_PER_PAGE)
    if not reviews: return []
    return [{'id': r.get('id'), 'user': r.get('user', {}).get('login', 'ghost'),
             'state': r.get('state'), 'submitted_at': r.get('submitted_at'),
             'body': r.get('body'), 'commit_id': r.get('commit_id')}
            for r in reviews if isinstance(r, dict)]


def github_get_pr_review_comments(owner, repo, pr_number, headers):
    """Fetches review comments (inline code comments) for a GitHub PR."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments"
    comments = fetch_paginated_data(api_url, headers=headers, per_page=GITHUB_PER_PAGE)
    if not comments: return []
    return [{'id': c.get('id'), 'user': c.get('user', {}).get('login', 'ghost'),
             'body': c.get('body'), 'path': c.get('path'), 'position': c.get('position'),
             'original_position': c.get('original_position'),
             'commit_id': c.get('commit_id'), 'original_commit_id': c.get('original_commit_id'),
             'created_at': c.get('created_at'), 'updated_at': c.get('updated_at'),
             'in_reply_to_id': c.get('in_reply_to_id')}
            for c in comments if isinstance(c, dict)]


def github_get_pr_issue_comments(owner, repo, pr_number, headers):
    """Fetches issue comments (comments on the PR itself) for a GitHub PR."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    comments = fetch_paginated_data(api_url, headers=headers, per_page=GITHUB_PER_PAGE)
    if not comments: return []
    return [{'id': c.get('id'), 'user': c.get('user', {}).get('login', 'ghost'),
             'body': c.get('body'), 'created_at': c.get('created_at'), 'updated_at': c.get('updated_at')}
            for c in comments if isinstance(c, dict)]


def github_get_pr_commits(owner, repo, pr_number, headers):
    """Fetches commits associated with a GitHub PR."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits"
    commits = fetch_paginated_data(api_url, headers=headers, per_page=GITHUB_PER_PAGE)
    if not commits: return []
    return [{'sha': c.get('sha'), 'message': c.get('commit', {}).get('message'),
             'author': c.get('commit', {}).get('author'), 'committer': c.get('commit', {}).get('committer'),
             'api_author_login': c.get('author').get('login') if c.get('author') else None,
             'api_committer_login': c.get('committer').get('login') if c.get('committer') else None,
             'parents': [p.get('sha') for p in c.get('parents', []) if isinstance(p, dict) and p.get('sha')]}
            for c in commits if isinstance(c, dict)]


def github_get_commit_check_runs(owner, repo, ref_sha, headers):
    """Fetches check runs (newer Checks API) for a specific GitHub commit SHA."""
    if not ref_sha: return []
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref_sha}/check-runs"
    response = make_api_request(api_url, headers=headers, params={'per_page': GITHUB_PER_PAGE},
                                timeout=GITHUB_REQUEST_TIMEOUT)
    if not response or response.status_code != 200: return []
    try:
        data = response.json()
        check_runs_list = data.get('check_runs', []) if isinstance(data, dict) else data
        if not isinstance(check_runs_list, list): return []
        return [{'name': cr.get('name'), 'status': cr.get('status'), 'conclusion': cr.get('conclusion'),
                 'started_at': cr.get('started_at'), 'completed_at': cr.get('completed_at'),
                 'app_owner': cr.get('app', {}).get('owner', {}).get('login'),
                 'app_name': cr.get('app', {}).get('name')}
                for cr in check_runs_list if isinstance(cr, dict)]
    except json.JSONDecodeError:
        return []
    except Exception as e:
        print(f"Error fetching check runs for {ref_sha[:7]}: {e}")
        return []


def github_get_commit_statuses(owner, repo, ref_sha, headers):
    """Fetches statuses (older Status API) for a specific GitHub commit SHA."""
    if not ref_sha: return []
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref_sha}/statuses"
    statuses = fetch_paginated_data(api_url, headers=headers, per_page=GITHUB_PER_PAGE)
    if not statuses: return []
    return [{'context': s.get('context'), 'state': s.get('state'), 'description': s.get('description'),
             'target_url': s.get('target_url'), 'creator_login': s.get('creator', {}).get('login'),
             'created_at': s.get('created_at'), 'updated_at': s.get('updated_at')}
            for s in statuses if isinstance(s, dict)]


# --- GitHub Merge Commit History Analysis Function ---

def github_analyze_merge_commits_history(owner, repo, branch='master', since=None, until=None, headers=None):
    """
    Fetches commits for a repository branch and identifies merge commits from history.
    Includes fetching limited commit details initially.
    """
    if not headers:
        print("Skipping GitHub merge commit history analysis: Headers are missing.")
        return []

    print(f"--- Fetching commits for {owner}/{repo} on branch '{branch}' to find merge commits ---")

    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    params = {
        'sha': branch,
        'per_page': GITHUB_PER_PAGE,
        'since': since,
        'until': until,
    }

    # Filter out None values from params
    params = {k: v for k, v in params.items() if v is not None}

    all_commits = fetch_paginated_data(api_url, headers=headers, params=params, per_page=GITHUB_PER_PAGE)

    if not all_commits:
        print("No commits found or error fetching commits history.")
        return []

    print(f"Found {len(all_commits)} commits in history. Identifying merge commits...")

    merge_commits = []
    for commit_data in all_commits:
        if not isinstance(commit_data, dict):
            continue

        commit = commit_data.get('commit')
        parents = commit_data.get('parents', [])

        # A merge commit typically has more than one parent
        if commit and isinstance(commit, dict) and isinstance(parents, list) and len(parents) > 1:
            merge_commit_info = {
                'platform': 'github',
                'request_type': 'merge_commit',  # Indicate this is a merge commit
                'request_id': commit_data.get('sha'),  # Use full SHA as ID
                'sha': commit_data.get('sha'),
                'node_id': commit_data.get('node_id'),
                'commit_url': commit_data.get('html_url'),
                'message': commit.get('message'),
                'author_name': commit.get('author', {}).get('name'),
                'author_email': commit.get('author', {}).get('email'),  # Include author email
                'author_date': commit.get('author', {}).get('date'),
                'committer_name': commit.get('committer', {}).get('name'),
                'committer_email': commit.get('committer', {}).get('email'),  # Include committer email
                'committer_date': commit.get('committer', {}).get('date'),
                'parent_shas': [p.get('sha') for p in parents if isinstance(p, dict) and p.get('sha')],
                'api_author_login': commit_data.get('author').get('login') if commit_data.get('author') else None,
                'api_committer_login': commit_data.get('committer').get('login') if commit_data.get(
                    'committer') else None,
                'changed_files_manifest': [],  # To be populated later
                'changed_files_count': 0,
                'total_additions': 0,
                'total_deletions': 0,
                'total_changes': 0,
                'check_runs': [],  # To be populated later
                'statuses': [],  # To be populated later
                'linked_issues_parsed': []  # To be populated later
            }
            merge_commits.append(merge_commit_info)

    print(f"Identified {len(merge_commits)} merge commits in history.")
    return merge_commits


# --- Main Data Fetching Function ---

def fetch_github_data(owner, repo, pr_state='all', branch_for_merge_history='master', merge_history_since=None,
                      merge_history_until=None):
    """
    Fetches data for GitHub PRs and identifies and enriches merge commits from history.
    """
    # Check for the token early
    GITHUB_BOT_ACCESS_TOKEN = os.environ.get('GITHUB_BOT_ACCESS_TOKEN')
    if not GITHUB_BOT_ACCESS_TOKEN or GITHUB_BOT_ACCESS_TOKEN == 'YOUR_GITHUB_TOKEN':
        print("Skipping GitHub data fetching: GITHUB_BOT_ACCESS_TOKEN is not set or is the default placeholder.")
        return [], []

    print(f"--- Starting GitHub data fetch for {owner}/{repo} ---")
    print(f"--- Output base directory: {OUTPUT_DIR_BASE} ---")

    os.makedirs(OUTPUT_DIR_BASE, exist_ok=True)

    headers = {
        'Authorization': f'token {GITHUB_BOT_ACCESS_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': GITHUB_API_VERSION
    }

    # --- Fetch and Process Pull Requests ---
    print(f"\n--- Fetching Pull Requests (state: {pr_state}) ---")
    pr_api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    pr_params = {
        'state': pr_state,
        'per_page': GITHUB_PER_PAGE,
        'sort': 'updated',
        'direction': 'desc',
    }
    pull_requests_list = fetch_paginated_data(pr_api_url, headers=headers, params=pr_params, per_page=GITHUB_PER_PAGE)

    processed_prs_metadata = []
    if not pull_requests_list:
        print("No Pull Requests found or error fetching PR list.")
    else:
        print(f"Found {len(pull_requests_list)} Pull Requests. Processing details...")
        for pr_summary in tqdm(pull_requests_list, desc="Processing Pull Requests"):
            if not isinstance(pr_summary, dict) or 'number' not in pr_summary: continue
            pr_number = pr_summary['number']

            pr_output_dir = os.path.join(OUTPUT_DIR_BASE, f"pr_{pr_number}")
            os.makedirs(pr_output_dir, exist_ok=True)

            pr_detail_url = pr_summary.get('url')
            if not pr_detail_url:
                print(f"\n    ERROR: Missing 'url' in GitHub PR summary for #{pr_number}. Skipping.")
                continue
            pr_detail_response = make_api_request(pr_detail_url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
            if not pr_detail_response or pr_detail_response.status_code != 200:
                print(f"\n    ERROR: Failed to fetch full details for GitHub PR #{pr_number}. Skipping.")
                continue

            try:
                pr = pr_detail_response.json()
                if not isinstance(pr, dict):
                    print(f"\n    ERROR: Full GitHub PR details for #{pr_number} is not a dictionary. Skipping.")
                    continue
            except json.JSONDecodeError as e:
                print(f"\n    ERROR: Failed to decode JSON for full GitHub PR #{pr_number} details: {e}. Skipping.")
                continue

            base_sha = pr.get('base', {}).get('sha')
            head_sha = pr.get('head', {}).get('sha')
            pr_body = pr.get('body')

            if not base_sha or not head_sha:
                print(
                    f"\n    Warning: Missing base_sha ('{base_sha}') or head_sha ('{head_sha}') for PR #{pr_number}. File content fetching might be incomplete.")

            files_metadata = github_process_pr_files(owner, repo, pr_number, base_sha, head_sha, headers, pr_output_dir)

            reviews = github_get_pr_reviews(owner, repo, pr_number, headers)
            review_comments = github_get_pr_review_comments(owner, repo, pr_number, headers)
            issue_comments = github_get_pr_issue_comments(owner, repo, pr_number, headers)
            commits_list = github_get_pr_commits(owner, repo, pr_number, headers)
            check_runs = github_get_commit_check_runs(owner, repo, head_sha, headers) if head_sha else []
            statuses = github_get_commit_statuses(owner, repo, head_sha, headers) if head_sha else []

            linked_issues = set()
            if pr_body: linked_issues.update(parse_linked_issues(pr_body))
            for c in commits_list:
                if isinstance(c, dict) and c.get('message'):
                    linked_issues.update(parse_linked_issues(c.get('message')))
            for ic in issue_comments:
                if isinstance(ic, dict) and ic.get('body'):
                    linked_issues.update(parse_linked_issues(ic.get('body')))
            for r in reviews:
                if isinstance(r, dict) and r.get('body'):
                    linked_issues.update(parse_linked_issues(r.get('body')))
            for rc in review_comments:
                if isinstance(rc, dict) and rc.get('body'):
                    linked_issues.update(parse_linked_issues(rc.get('body')))

            metadata = {
                'platform': 'github',
                'request_type': 'pr',  # Indicate this is a PR
                'request_id': pr_number,
                'api_url': pr.get('url'),
                'html_url': pr.get('html_url'),
                'state': pr.get('state'),
                'title': pr.get('title'),
                'author_login': pr.get('user', {}).get('login', 'ghost'),
                'author_association': pr.get('author_association'),
                'body': pr_body,
                'created_at': pr.get('created_at'),
                'updated_at': pr.get('updated_at'),
                'closed_at': pr.get('closed_at'),
                'merged_at': pr.get('merged_at'),
                'merged_by_login': pr.get('merged_by', {}).get('login') if pr.get('merged_by') is not None else None,
                'base_branch': pr.get('base', {}).get('ref'),
                'base_commit_sha': base_sha,
                'head_branch': pr.get('head', {}).get('ref'),
                'head_repo_full_name': pr.get('head', {}).get('repo', {}).get('full_name'),
                'head_commit_sha': head_sha,
                'reviews': reviews,  # PR reviews
                'review_comments': review_comments,  # PR inline comments
                'issue_comments': issue_comments,  # PR issue comments
                'commits_list': commits_list,  # Commits included in the PR
                'commits_count': len(commits_list),
                'check_runs': check_runs,  # Check runs for the head commit
                'statuses': statuses,  # Statuses for the head commit
                'linked_issues_parsed': sorted(list(linked_issues)),
                'changed_files_count': len(files_metadata),
                'total_additions': sum(f.get('additions', 0) for f in files_metadata),
                'total_deletions': sum(f.get('deletions', 0) for f in files_metadata),
                'total_changes': sum(f.get('changes', 0) for f in files_metadata),
                'changed_files_manifest': files_metadata  # Files changed in the PR
            }

            metadata_filename = os.path.join(pr_output_dir, "metadata.json")
            try:
                with open(metadata_filename, 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, indent=2, ensure_ascii=False)
                processed_prs_metadata.append(metadata)
            except IOError as e:
                print(f"\n    Error writing metadata JSON file {metadata_filename}: {e}")
            except TypeError as e:
                print(f"\n    Error serializing metadata JSON for GitHub PR #{pr_number}: {e}")
            except Exception as e:
                print(f"\n    Unexpected error saving metadata JSON for GitHub PR #{pr_number}: {e}")

    # --- Fetch and Process Merge Commits from History ---
    merge_commits_history_list = github_analyze_merge_commits_history(
        owner,
        repo,
        branch=branch_for_merge_history,
        since=merge_history_since,
        until=merge_history_until,
        headers=headers
    )

    enriched_merge_commits = []
    if merge_commits_history_list:
        print(f"\n--- Enriching {len(merge_commits_history_list)} Merge Commits with detailed data ---")
        for commit_info in tqdm(merge_commits_history_list, desc="Enriching merge commits"):
            commit_sha = commit_info.get('sha')
            parent_shas = commit_info.get('parent_shas', [])
            # For merge commits, the base commit for diff is typically the first parent
            base_sha_for_files = parent_shas[0] if parent_shas else None

            if not commit_sha:
                print(f"Warning: Skipping merge commit with no SHA: {commit_info}")
                continue

            # Use short SHA for directory name for brevity
            commit_output_dir = os.path.join(OUTPUT_DIR_BASE, f"commit_{commit_sha[:7]}")
            os.makedirs(commit_output_dir, exist_ok=True)

            # Get the list of files changed in this specific merge commit
            files_metadata, total_additions, total_deletions, total_changes = github_process_commit_files_list(
                owner, repo, commit_sha, headers
            )

            before_dir = os.path.join(commit_output_dir, "before_merge")
            after_dir = os.path.join(commit_output_dir, "after_merge")
            patch_dir = os.path.join(commit_output_dir, "changed_files")
            os.makedirs(before_dir, exist_ok=True)
            os.makedirs(after_dir, exist_ok=True)
            os.makedirs(patch_dir, exist_ok=True)

            updated_files_metadata = []
            for file_info in files_metadata:
                filename = file_info.get('filename')
                if not filename: continue

                # Save patch if available
                patch_content = file_info.pop('patch', None)  # Remove patch from file_info dict after getting it
                if patch_content:
                    patch_filename = os.path.basename(filename) + ".patch"
                    file_info['patch_saved'] = save_file(patch_content, patch_dir, patch_filename)
                else:
                    file_info['patch_saved'] = False

                # Fetch and save base content
                content_base = ""
                if base_sha_for_files and file_info.get('status') != 'added':
                    content_base = github_get_file_content(owner, repo, filename, base_sha_for_files, headers)
                    if content_base:
                        file_info['content_base_saved'] = save_file(content_base, before_dir, filename)
                    else:
                        file_info['content_base_saved'] = False
                else:
                    file_info['content_base_saved'] = False  # No base content for added files or if base_sha is missing

                # Fetch and save head content (content at the merge commit SHA)
                content_head = ""
                if file_info.get('status') not in ['removed', 'deleted']:
                    content_head = github_get_file_content(owner, repo, filename, commit_sha, headers)
                    if content_head:
                        file_info['content_head_saved'] = save_file(content_head, after_dir, filename)
                    else:
                        file_info['content_head_saved'] = False
                else:
                    file_info['content_head_saved'] = False  # No head content for removed/deleted files

                updated_files_metadata.append(file_info)

            # Update commit_info with detailed file data and counts
            commit_info['changed_files_manifest'] = updated_files_metadata
            commit_info['changed_files_count'] = len(updated_files_metadata)
            commit_info['total_additions'] = sum(f.get('additions', 0) for f in updated_files_metadata)
            commit_info['total_deletions'] = sum(f.get('deletions', 0) for f in updated_files_metadata)
            commit_info['total_changes'] = sum(f.get('changes', 0) for f in updated_files_metadata)

            # Fetch and add check runs and statuses for the merge commit SHA
            check_runs = github_get_commit_check_runs(owner, repo, commit_sha, headers)
            statuses = github_get_commit_statuses(owner, repo, commit_sha, headers)
            commit_info['check_runs'] = check_runs
            commit_info['statuses'] = statuses

            # Parse linked issues from the merge commit message
            if commit_info.get('message'):
                commit_info['linked_issues_parsed'] = sorted(list(parse_linked_issues(commit_info.get('message'))))
            else:
                commit_info['linked_issues_parsed'] = []

            # Save the enriched metadata for the merge commit
            metadata_filename = os.path.join(commit_output_dir, "metadata.json")
            try:
                with open(metadata_filename, 'w', encoding='utf-8') as f:
                    json.dump(commit_info, f, indent=2, ensure_ascii=False)
            except IOError as e:
                print(f"    Error writing metadata JSON file {metadata_filename}: {e}")
            except TypeError as e:
                print(f"    Error serializing metadata JSON for commit {commit_sha[:7]}: {e}")
            except Exception as e:
                print(f"    Unexpected error saving metadata JSON for commit {commit_sha[:7]}: {e}")

            enriched_merge_commits.append(commit_info)

        merge_commits_history_list = enriched_merge_commits  # Update the list with enriched data

    all_changes_summary = []

    # Add PR summaries to the overall summary
    for pr_data in processed_prs_metadata:
        change_type = 'PR'
        change_id = pr_data.get('request_id')
        pr_author_login = pr_data.get('author_login', 'N/A')
        pr_author_name = pr_author_login  # Using login as name for simplicity in summary
        # Attempt to get email from the first commit in the PR's commit list
        pr_author_email = 'N/A'
        if pr_data.get('commits_list'):
            first_commit = pr_data['commits_list'][0]
            if isinstance(first_commit, dict) and first_commit.get('author'):
                pr_author_email = first_commit['author'].get('email', 'N/A')

        change_date_str = pr_data.get('merged_at') or pr_data.get('closed_at') or pr_data.get('updated_at')
        change_date = change_date_str  # Keep as string for CSV
        directory = os.path.join(OUTPUT_DIR_BASE, f"pr_{change_id}")
        # Add email to the list
        all_changes_summary.append(
            [pr_author_login, pr_author_name, pr_author_email, change_type, change_id, change_date, directory])

    # Add Merge Commit summaries to the overall summary
    for mc_data in merge_commits_history_list:
        change_type = 'Merge Commit'
        change_id = mc_data.get('sha')[:7]  # Use short SHA for summary ID
        mc_author_login = mc_data.get('api_author_login') or 'N/A'
        mc_author_name = mc_data.get('author_name') or 'N/A'
        mc_author_email = mc_data.get('author_email') or 'N/A'  # Get author email for merge commit

        change_date = mc_data.get('committer_date') or mc_data.get('author_date')  # Use committer or author date
        directory = os.path.join(OUTPUT_DIR_BASE, f"commit_{change_id}")
        # Add email to the list
        all_changes_summary.append(
            [mc_author_login, mc_author_name, mc_author_email, change_type, change_id, change_date, directory])

    summary_filepath = os.path.join(OUTPUT_DIR_BASE, SUMMARY_CSV_FILE)
    try:
        with open(summary_filepath, 'w', newline='', encoding='utf-8') as csvfile:
            csv_writer = csv.writer(csvfile)
            # Update header row to include 'Author Email'
            csv_writer.writerow(
                ['Author Login', 'Author Name', 'Author Email', 'What (Type)', 'ID (PR#/Commit SHA)', 'When (Date)',
                 'Directory'])
            csv_writer.writerows(all_changes_summary)
        print(f"Successfully saved summary to {summary_filepath}")
    except IOError as e:
        print(f"Error writing summary CSV file {summary_filepath}: {e}")
    except Exception as e:
        print(f"Unexpected error saving summary CSV: {e}")

    print(f"\n--- Finished GitHub data fetch and summary generation for {owner}/{repo}. ---")
    print(f"--- Data saved in subdirectories within: {OUTPUT_DIR_BASE} ---")
    print(f"--- Summary saved to: {summary_filepath} ---")

    return processed_prs_metadata, merge_commits_history_list


# --- RAG System ---
class CodeChangeRAG:
    def __init__(self, data_path="github_data_structured"):
        self.data_path = data_path
        print("Initializing embeddings model...")
        try:
            ################################################################################################################################
            self.embeddings = HuggingFaceEmbeddings(
                model_name="microsoft/graphcodebert-base",
                model_kwargs={"trust_remote_code": True}
            )
            ################################################################################################################################
            print("Embeddings model initialized.")
        except Exception as e:
            print(f"Error initializing embeddings model: {str(e)}")
            self.embeddings = None

        print("Initializing text splitter...")
        try:
            # Using a text splitter suitable for code
            ################################################################################################################################
            self.splitter = RecursiveCharacterTextSplitter.from_language(
                language=Language.PYTHON,  # Assuming Python, adjust if needed
                chunk_size=2048,  # Increased chunk size
                chunk_overlap=200  # Increased overlap
            )
            ################################################################################################################################
            print("Text splitter initialized.")
        except Exception as e:
            print(f"Error initializing text splitter: {str(e)}")
            self.splitter = None

        # Dictionary to hold vector databases for each PR or Merge Commit
        self.change_databases = {}
        # Dictionary to hold vector databases for coder analysis (aggregated changes)
        self.coder_databases = {}
        self.llm = None
        self._temp_chroma_dirs = {}  # To keep track of temporary directories for cleanup

    def __del__(self):
        """
        Destructor to clean up temporary Chroma directories.
        """
        print("Cleaning up temporary Chroma directories...")
        # Iterate over a copy of keys because items might be deleted during iteration
        for change_id, temp_dir in list(self._temp_chroma_dirs.items()):
            try:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                    print(f"Cleaned up temporary directory for {change_id}: {temp_dir}")
                    del self._temp_chroma_dirs[change_id]
            except Exception as e:
                print(f"Error cleaning up temporary directory {temp_dir} for {change_id}: {e}")

    def _load_change_metadata(self, change_dir):
        """Loads the metadata.json file for a single change (PR or Merge Commit) directory."""
        metadata_path = os.path.join(change_dir, "metadata.json")
        if not os.path.exists(metadata_path):
            raise FileNotFoundError(f"Metadata file not found in {change_dir}")
        with open(metadata_path, "r", encoding='utf-8') as f:
            return json.load(f)

    def _process_single_change(self, change_dir_name):
        """
        Processes data for a single change directory (PR or Merge Commit),
        creates text chunks, and builds a Chroma vector database.
        """
        # Extract change identifier (PR number or short commit SHA) from directory name
        parts = change_dir_name.split("_")
        if len(parts) < 2:
            print(f"Warning: Could not parse change identifier from directory name: {change_dir_name}. Skipping.")
            return None
        change_type_prefix = parts[0]  # 'pr' or 'commit'
        change_id = "_".join(parts[1:])  # PR number or short SHA

        full_path = os.path.join(self.data_path, change_dir_name)
        if not os.path.isdir(full_path):
            print(f"Warning: Change directory not found or is not a directory: {full_path}. Skipping.")
            return None

        print(f"Processing data for {change_type_prefix.upper()} {change_id}...")

        try:
            metadata = self._load_change_metadata(full_path)
            # Use the full request_id from metadata for consistency
            full_change_id = str(metadata.get('request_id', change_id))
            change_type = metadata.get('request_type', change_type_prefix)
        except FileNotFoundError as e:
            print(f"Error loading metadata for {change_type_prefix.upper()} {change_id}: {e}. Skipping.")
            return None
        except json.JSONDecodeError as e:
            print(f"Error decoding metadata JSON for {change_type_prefix.upper()} {change_id}: {e}. Skipping.")
            return None
        except Exception as e:
            print(
                f"An unexpected error occurred loading metadata for {change_type_prefix.upper()} {change_id}: {e}. Skipping.")
            return None

        if self.embeddings is None or self.splitter is None:
            print(
                f"Skipping vector DB creation for {change_type.upper()} {full_change_id}: Embeddings model or splitter not initialized.")
            return None

        chunks = []
        changed_files = metadata.get("changed_files_manifest", [])
        if not changed_files:
            print(f"No changed files found in metadata for {change_type.upper()} {full_change_id}.")
            pass

        for file_meta in tqdm(changed_files, desc=f"Processing files for {change_type.upper()} {full_change_id}"):
            if not isinstance(file_meta, dict) or 'filename' not in file_meta:
                print(
                    f"Warning: Skipping invalid file metadata entry for {change_type.upper()} {full_change_id}: {file_meta}")
                continue

            filename = file_meta["filename"]
            try:
                # Read code content saved during the fetch process
                before_code = self._read_code_file(full_path, "before_merge", filename)
                after_code = self._read_code_file(full_path, "after_merge", filename)
                patch = self._read_patch_file(full_path, filename)

                # Create context string for the file
                context = self._create_context(metadata, filename, before_code, after_code, patch)

                # Split the context into chunks
                file_chunks = self.splitter.split_text(context)

                chunks.extend(file_chunks)

            except Exception as e:
                print(f"Error processing file {filename} in {change_type.upper()} {full_change_id}: {str(e)}")

        # Add other relevant metadata fields as chunks
        # Only add PR-specific fields if it's a PR
        if change_type == 'pr':
            pr_body = metadata.get("body")
            if pr_body:
                body_chunks = self.splitter.split_text(f"Pull Request Body:\n{pr_body}")
                chunks.extend(body_chunks)

            issue_comments = metadata.get("issue_comments", [])
            for comment in issue_comments:
                if isinstance(comment, dict) and comment.get("body"):
                    comment_chunks = self.splitter.split_text(
                        f"Pull Request Issue Comment by {comment.get('user', 'N/A')}:\n{comment.get('body')}")
                    chunks.extend(comment_chunks)

            review_comments = metadata.get("review_comments", [])
            for comment in review_comments:
                if isinstance(comment, dict) and comment.get("body"):
                    comment_chunks = self.splitter.split_text(
                        f"Pull Request Review Comment by {comment.get('user', 'N/A')} on {comment.get('path', 'N/A')}:\n{comment.get('body')}")
                    chunks.extend(comment_chunks)
        elif change_type == 'merge_commit':
            # Add merge commit message as context
            commit_message = metadata.get("message")
            if commit_message:
                message_chunks = self.splitter.split_text(f"Merge Commit Message:\n{commit_message}")
                chunks.extend(message_chunks)
            # Add linked issues parsed from commit message
            linked_issues = metadata.get("linked_issues_parsed", [])
            if linked_issues:
                issues_text = "Linked Issues: " + ", ".join(linked_issues)
                issues_chunks = self.splitter.split_text(issues_text)
                chunks.extend(issues_chunks)

        if not chunks:
            print(
                f"No processable content found for {change_type.upper()} {full_change_id}. Skipping vector DB creation.")
            return None

        try:
            # Use the full change ID for the temporary directory name
            temp_dir = tempfile.mkdtemp(prefix=f"chroma_db_{change_type}_{full_change_id}_")
            self._temp_chroma_dirs[full_change_id] = temp_dir

            # Create and persist the Chroma vector database
            ################################################################################################################################
            vector_db = Chroma.from_texts(
                texts=chunks,
                embedding=self.embeddings,
                persist_directory=temp_dir
            )
            ################################################################################################################################
            print(
                f"Successfully created Chroma DB for {change_type.upper()} {full_change_id} in temporary directory: {temp_dir}")
        except Exception as e:
            print(f"Error creating Chroma DB for {change_type.upper()} {full_change_id}: {str(e)}")
            # Clean up the temporary directory if DB creation fails
            if full_change_id in self._temp_chroma_dirs:
                try:
                    shutil.rmtree(self._temp_chroma_dirs[full_change_id])
                    del self._temp_chroma_dirs[full_change_id]
                except Exception as cleanup_e:
                    print(f"Error during cleanup of temp dir {temp_dir}: {cleanup_e}")

            return None

        # Store the vector database using the full change ID
        self.change_databases[full_change_id] = vector_db
        return vector_db

    def _read_code_file(self, change_path, dir_name, filename):
        """Reads content from a code file within a change directory."""
        file_path = os.path.join(change_path, dir_name, filename)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                print(f"Error reading file {file_path}: {str(e)}")
                return ""
        return ""

    def _read_patch_file(self, change_path, filename):
        """Reads content from a patch file within a change directory."""
        # Patch files are named after the base filename
        patch_path = os.path.join(change_path, "changed_files", os.path.basename(filename) + ".patch")
        if os.path.exists(patch_path):
            try:
                with open(patch_path, "r", encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                print(f"Error reading patch file {patch_path}: {str(e)}")
                return ""
        return ""

    def _create_context(self, metadata, filename, before_code, after_code, patch):
        """
        Creates a combined context string for a specific file within a change (PR or Merge Commit),
        including change details, code changes, and relevant comments/checks.
        """
        change_type = metadata.get('request_type', 'change')
        change_id = metadata.get('request_id', 'N/A')
        title = metadata.get('title',
                             metadata.get('message', 'No Title/Message'))  # Use title for PR, message for commit
        author_login = metadata.get('author_login',
                                    metadata.get('api_author_login', 'N/A'))  # Use PR author or commit author login

        # Get CI check names for the head commit of the change
        ci_checks_list = [c.get('name', 'N/A') for c in metadata.get('check_runs', []) if isinstance(c, dict)]
        ci_checks_str = ", ".join(ci_checks_list) if ci_checks_list else "No CI checks found."

        #########################################################################################################################
        context_parts = [
            f"--- {change_type.upper()} {change_id} - {title} ---",
            f"Author: {author_login}",
            f"File: {filename}",
            f"Status: {metadata.get('state', 'N/A') if change_type == 'pr' else 'N/A'}",  # State is PR specific
            f"CI Checks for head commit: {ci_checks_str}",
            f"BEFORE CODE:\n{before_code}",
            f"AFTER CODE:\n{after_code}",
            f"DIFF:\n{patch}"
        ]
        #########################################################################################################################

        # Add comments specific to PRs
        if change_type == 'pr':
            file_review_comments = [
                c.get('body') for c in metadata.get('review_comments', [])
                if isinstance(c, dict) and c.get('path') == filename and c.get('body')
            ]
            comments_text = "\n".join(
                file_review_comments) if file_review_comments else "No specific review comments for this file."
            context_parts.append(f"REVIEW COMMENTS on this file:\n{comments_text}")

        return "\n\n".join(context_parts)

    def initialize_llm(self):
        """Initializes the YandexGPT Language Model."""
        if self.llm is not None:
            print("LLM already initialized.")
            return

        print("Initializing YandexGPT LLM...")
        # Check if API Key and Folder ID are set
        YANDEX_API_KEY = os.environ.get('YANDEX_API_KEY')
        if not YANDEX_API_KEY or YANDEX_API_KEY == 'YOUR_YANDEX_API_KEY':
            print("YANDEX_API_KEY is not set or is the default placeholder. Cannot initialize YandexGPT.")
            self.llm = None
            return

        YANDEX_FOLDER_ID = os.environ.get('YANDEX_FOLDER_ID')
        if not YANDEX_FOLDER_ID or YANDEX_FOLDER_ID == 'YOUR_YANDEX_FOLDER_ID':
            print("YANDEX_FOLDER_ID is not set or is the default placeholder. Cannot initialize YandexGPT.")
            self.llm = None
            return

        try:
            # Initialize YandexGPT with API Key and Folder ID
            #########################################################################################################################
            self.llm = YandexGPT(api_key=YANDEX_API_KEY, folder_id=YANDEX_FOLDER_ID)
            #########################################################################################################################
            print("YandexGPT LLM initialized successfully.")
        except Exception as e:
            print(f"Error initializing YandexGPT LLM: {e}")  # Print the specific error message
            self.llm = None
            print("LLM initialization failed. Qualitative analysis will not be performed.")

    def load_change(self, change_identifier, change_type):
        """
        Loads data for a specific change (PR number or Commit SHA) into an in-memory vector database.
        change_identifier: The PR number (int or str) or the full commit SHA (str).
        change_type: 'pr' or 'merge_commit'.
        """
        if change_type == 'pr':
            change_dir_name = f"pr_{change_identifier}"
            # Use the string representation of the PR number as the internal ID
            internal_change_id = str(change_identifier)
        elif change_type == 'merge_commit':
            # Use the full commit SHA as the internal ID
            internal_change_id = str(change_identifier)
            # Use the short SHA for the directory name lookup
            change_dir_name = f"commit_{str(change_identifier)[:7]}"
        else:
            print(f"Error: Invalid change_type '{change_type}'. Must be 'pr' or 'merge_commit'.")
            return None

        change_dir_path = os.path.join(self.data_path, change_dir_name)

        if not os.path.exists(change_dir_path):
            print(f"Error: Data directory for {change_type.upper()} {change_identifier} not found at {change_dir_path}")
            return None

        # Check if the database is already loaded
        if internal_change_id in self.change_databases and self.change_databases[internal_change_id] is not None:
            print(f"Data for {change_type.upper()} {change_identifier} already loaded.")
            return self.change_databases[internal_change_id]

        vector_db = self._process_single_change(change_dir_name)

        # Store the DB using the full change ID (PR number or full SHA)
        if vector_db:
            self.change_databases[internal_change_id] = vector_db

        return vector_db

    def _format_sources(self, docs):
        """Formats source documents returned by the retriever for output."""
        formatted_sources = []
        for doc in docs:
            source_info = {
                # Truncate content snippet for display
                "content_snippet": str(doc.page_content)[:500] + "..." if doc and hasattr(doc,
                                                                                          'page_content') else "N/A",
                # Note: Metadata like file, checks, author are not automatically attached
                # to document objects by Chroma in this configuration.
                # To include them, you would need to add them to the metadata dictionary
                # when creating the Chroma DB: Chroma.from_texts(..., metadatas=[{...}])
                "file": "Unknown (metadata not stored)",
                "checks": "Unknown (metadata not stored)",
                "author": "Unknown (metadata not stored)"
            }
            formatted_sources.append(source_info)
        return formatted_sources

    def analyze_coder_activity(self, coder_login, changes_list):
        """
        Analyzes the activity of a specific coder based on a list of their changes.
        Loads all relevant changes into a single vector database for analysis.
        """
        if not changes_list:
            print(f"No changes provided for coder analysis for {coder_login}.")
            return {
                "coder_login": coder_login,
                "total_changes_analyzed": 0,
                "total_commits": 0,
                "total_additions": 0,
                "total_deletions": 0,
                "analysis_results": "No changes to analyze for this coder in the specified period."
            }

        print(f"\n--- Starting RAG Analysis for Coder: {coder_login} ---")
        print(f"Analyzing {len(changes_list)} changes for {coder_login}...")

        # Create a unique identifier for the coder analysis database
        coder_db_id = f"coder_{coder_login}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        # Check if LLM is initialized BEFORE proceeding with RAG analysis
        if self.llm is None:
            print(f"Skipping coder analysis for {coder_login}: LLM is not initialized.")
            return {
                "coder_login": coder_login,
                "total_changes_analyzed": len(changes_list),
                "total_commits": sum(
                    c.get('commits_count', 1) if c.get('request_type') == 'pr' else 1 for c in changes_list),
                # Count commits in PRs + individual merge commits
                "total_additions": sum(c.get('total_additions', 0) for c in changes_list),
                "total_deletions": sum(c.get('total_deletions', 0) for c in changes_list),
                "analysis_results": "RAG system not initialized. Cannot perform qualitative analysis."
            }

        if self.embeddings is None or self.splitter is None:
            print(f"Skipping coder analysis for {coder_login}: Embeddings model or splitter not initialized.")
            return {
                "coder_login": coder_login,
                "total_changes_analyzed": len(changes_list),
                "total_commits": sum(
                    c.get('commits_count', 1) if c.get('request_type') == 'pr' else 1 for c in changes_list),
                # Count commits in PRs + individual merge commits
                "total_additions": sum(c.get('total_additions', 0) for c in changes_list),
                "total_deletions": sum(c.get('deletions', 0) for c in changes_list),
                "analysis_results": "Embeddings model or text splitter not initialized. Cannot perform qualitative analysis."
            }

        all_coder_chunks = []
        total_commits = 0
        total_additions = 0
        total_deletions = 0

        # Process each change and collect chunks
        for change_data in tqdm(changes_list, desc=f"Processing changes for {coder_login}"):
            change_type = change_data.get('request_type')
            change_id = change_data.get('request_id')

            if not change_type or not change_id:
                print(f"Warning: Skipping change with missing type or ID during coder analysis: {change_data}")
                continue

            # Construct the expected directory name
            if change_type == 'pr':
                change_dir_name = f"pr_{change_id}"
                total_commits += change_data.get('commits_count', 1)  # Count commits within the PR
            elif change_type == 'merge_commit':
                change_dir_name = f"commit_{str(change_id)[:7]}"
                total_commits += 1  # Count the merge commit itself
            else:
                continue  # Skip unknown change types

            full_path = os.path.join(self.data_path, change_dir_name)
            if not os.path.isdir(full_path):
                print(
                    f"Warning: Data directory not found for {change_type.upper()} {change_id} at {full_path}. Skipping.")
                continue

            try:
                metadata = self._load_change_metadata(full_path)

                changed_files = metadata.get("changed_files_manifest", [])
                total_additions += metadata.get('total_additions', 0)
                total_deletions += metadata.get('total_deletions', 0)

                for file_meta in changed_files:
                    if not isinstance(file_meta, dict) or 'filename' not in file_meta:
                        continue

                    filename = file_meta["filename"]
                    before_code = self._read_code_file(full_path, "before_merge", filename)
                    after_code = self._read_code_file(full_path, "after_merge", filename)
                    patch = self._read_patch_file(full_path, filename)

                    # Create context string for the file, including coder info
                    context = self._create_context(metadata, filename, before_code, after_code, patch)

                    # Add coder-specific context
                    coder_context = f"Coder: {coder_login}\nChange Type: {change_type.upper()}\nChange ID: {change_id}\n"
                    context = coder_context + context

                    file_chunks = self.splitter.split_text(context)
                    all_coder_chunks.extend(file_chunks)

                # Add other relevant metadata fields as chunks (PR body, comments, etc.)
                if change_type == 'pr':
                    pr_body = metadata.get("body")
                    if pr_body:
                        all_coder_chunks.extend(self.splitter.split_text(
                            f"Pull Request Body by {metadata.get('author_login', 'N/A')}:\n{pr_body}"))

                    issue_comments = metadata.get("issue_comments", [])
                    for comment in issue_comments:
                        if isinstance(comment, dict) and comment.get("body"):
                            all_coder_chunks.extend(self.splitter.split_text(
                                f"Issue Comment by {comment.get('user', 'N/A')}:\n{comment.get('body')}"))

                    review_comments = metadata.get("review_comments", [])
                    for comment in review_comments:
                        if isinstance(comment, dict) and comment.get("body"):
                            all_coder_chunks.extend(self.splitter.split_text(
                                f"Review Comment by {comment.get('user', 'N/A')} on {comment.get('path', 'N/A')}:\n{comment.get('body')}"))
                elif change_type == 'merge_commit':
                    commit_message = metadata.get("message")
                    if commit_message:
                        all_coder_chunks.extend(self.splitter.split_text(
                            f"Merge Commit Message by {metadata.get('api_author_login', 'N/A')}:\n{commit_message}"))
                    linked_issues = metadata.get("linked_issues_parsed", [])
                    if linked_issues:
                        issues_text = "Linked Issues: " + ", ".join(linked_issues)
                        all_coder_chunks.extend(self.splitter.split_text(issues_text))

            except Exception as e:
                print(f"Error processing data for {change_type.upper()} {change_id} during coder analysis: {str(e)}")

        if not all_coder_chunks:
            print(f"No processable content found for coder analysis for {coder_login}. Skipping RAG analysis.")
            return {
                "coder_login": coder_login,
                "total_changes_analyzed": len(changes_list),
                "total_commits": total_commits,
                "total_additions": total_additions,
                "total_deletions": total_deletions,
                "analysis_results": "No content generated for RAG analysis."
            }

        try:
            # Create a temporary directory for the coder-specific database
            temp_dir = tempfile.mkdtemp(prefix=f"chroma_db_coder_{coder_login}_")
            self._temp_chroma_dirs[coder_db_id] = temp_dir  # Store using the unique ID

            # Create and persist the Chroma vector database for the coder's activity
            coder_vector_db = Chroma.from_texts(
                texts=all_coder_chunks,
                embedding=self.embeddings,
                persist_directory=temp_dir
            )
            print(f"Successfully created Chroma DB for coder {coder_login} in temporary directory: {temp_dir}")

            # Store the coder database
            self.coder_databases[coder_db_id] = coder_vector_db

        except Exception as e:
            print(f"Error creating Chroma DB for coder {coder_login}: {str(e)}")
            # Clean up the temporary directory if DB creation fails
            if coder_db_id in self._temp_chroma_dirs:
                try:
                    shutil.rmtree(self._temp_chroma_dirs[coder_db_id])
                    del self._temp_chroma_dirs[coder_db_id]
                except Exception as cleanup_e:
                    print(f"Error during cleanup of temp dir {temp_dir}: {cleanup_e}")
            return {
                "coder_login": coder_login,
                "total_changes_analyzed": len(changes_list),
                "total_commits": total_commits,
                "total_additions": total_additions,
                "total_deletions": total_deletions,
                "analysis_results": f"Error creating RAG database: {str(e)}"
            }

        # Define questions for coder analysis
        #########################################################################################################################
        coder_analysis_questions = [
            f"Summarize the main types of code changes made by {coder_login} during this period.",
            f"Based on the code changes and comments, what can be said about {coder_login}'s coding style or practices?",
            f"Are there any recurring issues or patterns of errors in the code contributed by {coder_login}?",
            f"Based on the complexity of changes and discussions, how might {coder_login}'s skills have developed during this period?",
            f"Identify any notable contributions or challenging tasks undertaken by {coder_login}.",
            f"Summarize the feedback received on {coder_login}'s code (from reviews or comments).",
            # Add more specific questions as needed
        ]
        #########################################################################################################################

        analysis_results = {}
        retriever = coder_vector_db.as_retriever(search_kwargs={"k": 5})  # Use a higher k for broader context

        # Query the coder-specific database
        for q in tqdm(coder_analysis_questions, desc=f"Generating analysis for {coder_login}"):
            print(f"Question for {coder_login}: {q}")
            try:
                source_documents = retriever.get_relevant_documents(q)
                context_text = "\n\n---\n\n".join([doc.page_content for doc in source_documents])

                if not source_documents:
                    analysis_results[q] = {
                        "answer": "Could not find relevant information to answer this question.",
                        "sources": []
                    }
                    continue

                # Use a general prompt for coder analys
                #########################################################################################################################is
                prompt_template = """<|system|>
                You are a helpful assistant analyzing the code contributions of a developer over a specific period.
                Relevant context from the developer's code changes (Pull Requests and Merge Commits), messages, and comments is provided below:

                Coder: {coder_login}
                Context:
                {context}

                Based on the provided context, answer the following question about the developer's activity and potential skill development.
                Focus on identifying patterns, types of work, and feedback received.
                If the context does not contain enough information to answer the question,
                state that you cannot answer based on the available information.

                UNSWER ON RUSSIAN LANGUAGE, DONT ANSWER DONT KNOW THERE ANYWAY SHOULD BE FULL ANSWER ON THE QUESTION
                </s>
                <|user|>
                {question}
                </s>
                <|assistant|>
                """
                formatted_prompt = prompt_template.format(
                    coder_login=coder_login,
                    context=context_text,
                    question=q
                )

                llm_response = self.llm.invoke(formatted_prompt)
                answer = llm_response.strip()  # Assuming YandexGPT returns a simple string
                formatted_sources = self._format_sources(source_documents)
                #########################################################################################################################
                analysis_results[q] = {
                    "answer": answer if answer else "Could not generate an answer based on the available information.",
                    "sources": formatted_sources
                }
            except Exception as e:
                print(f"Error generating RAG answer for question '{q[:50]}...' for {coder_login}: {str(e)}")
                analysis_results[q] = {
                    "answer": f"An error occurred during analysis: {str(e)}",
                    "sources": []
                }

        print(f"--- RAG Analysis finished for Coder: {coder_login} ---")

        return {
            "coder_login": coder_login,
            "total_changes_analyzed": len(changes_list),
            "total_commits": total_commits,
            "total_additions": total_additions,
            "total_deletions": total_deletions,
            "analysis_results": analysis_results
        }
