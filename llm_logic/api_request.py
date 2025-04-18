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


import requests
import argparse

URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


def make_api_request_2(iam_token, folder_id, user_text):
    data = {}
    data["modelUri"] = f"gpt://{folder_id}/yandexgpt"

    # Настраиваем опции
    data["completionOptions"] = {"temperature": 0.3, "maxTokens": 1000}
    # Указываем контекст для модели
    data["messages"] = [
        {"role": "system", "text": "Исправь ошибки в тексте."},
        {"role": "user", "text": f"{user_text}"},
    ]

    # Отправляем запрос
    response = requests.post(
        URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {iam_token}"
        },
        json=data,
    ).json()

    # Распечатываем результат
    print(response)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--iam_token", required=True, help="IAM token")
    parser.add_argument("--folder_id", required=True, help="Folder id")
    parser.add_argument("--user_text", required=True, help="User text")
    args = parser.parse_args()
    run(args.iam_token, args.folder_id, args.user_text)
