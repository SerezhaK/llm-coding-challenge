import requests
import os
from .prompt import typical_prompt


def make_api_request(
        user_text,
        system_text=typical_prompt,
        temperature=0.3,
        max_tokens=10000):
    folder_id = os.environ.get("FOLDER_ID")
    api_key = os.environ.get("YANDEX_API_KEY")

    data = {
        "modelUri": f"gpt://{folder_id}/yandexgpt",
        "completionOptions": {"temperature": temperature, "maxTokens": max_tokens},
        "messages": [
            {"role": "system",
             "text": f"{system_text}"},

            {"role": "user",
             "text": f"{user_text}"},
        ]}

    response = requests.post(
        "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}"
        },
        json=data,
    ).json()

    # only text
    try:
        return response['result']['alternatives'][0]['message']['text']
    except KeyError:
        return [response, api_key]