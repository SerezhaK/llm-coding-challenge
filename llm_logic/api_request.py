import requests


def make_api_request(url, headers, params=None):
    """
    Helper function to make GET requests to the GitHub API and handle common errors.
    Includes basic error handling for timeouts, HTTP errors, and request exceptions.
    Args:
        url (str): The API endpoint URL.
        headers (dict): Request headers, including Authorization and Accept.
        params (dict, optional): URL parameters. Defaults to None.
    Returns:
        requests.Response: The response object if successful, None otherwise.
    """
    # Ensure the correct API version is specified in headers
    headers['X-GitHub-Api-Version'] = API_VERSION
    try:
        # time.sleep(REQUEST_DELAY) # Uncomment and adjust for rate limiting
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)

        # Optional: Basic rate limit check (prints remaining requests)
        # if 'X-RateLimit-Remaining' in response.headers:
        #     print(f"      API Rate Limit Remaining: {response.headers['X-RateLimit-Remaining']}")

        response.raise_for_status() # Raises HTTPError for bad responses (4xx or 5xx)
        return response
    except requests.exceptions.Timeout:
        print(f"Timeout error making API request to {url}")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error making API request to {url}: {e}")
        print(f"Response Status: {e.response.status_code}")
        print(f"Response Body: {e.response.text}")
        # Depending on needs, you might want to raise the exception or handle it differently
        # raise e
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error making API request to {url}: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred during API request to {url}: {e}")
        return None
