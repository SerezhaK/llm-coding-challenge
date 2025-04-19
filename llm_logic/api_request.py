import requests
import os
from .prompt import typical_prompt
import logging

logger = logging.getLogger(__name__)


def make_api_request(
        url,
        headers,
        params=None,
        timeout=100):
    """Helper function for making GET requests to the GitHub API."""

    headers['X-GitHub-Api-Version'] = '2022-11-28'

    try:
        response = requests.get(url, headers=headers, params=params, timeout=timeout)
        response.raise_for_status()
        return response

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout error making API request to {url}")
        return None

    except requests.exceptions.HTTPError as e:
        logger.warning(f"HTTP error making API request to {url}: {e}")
        return None

    except requests.exceptions.RequestException as e:
        logger.warning(f"Error making API request to {url}: {e}")
        return None

    except Exception as e:
        logger.warning(f"An unexpected error occurred during API request to {url}: {e}")
        return None
