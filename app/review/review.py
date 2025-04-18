import streamlit as st


def process_code_review(client, query: str):
    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system",
                 "content": "Вы - ассистент по code review. Анализируйте код и предлагайте улучшения."},
                {"role": "user", "content": query}
            ]
        )
        return response.choices[0].message.content
    except Exception as e:
        st.error(f"Ошибка при обработке запроса: {str(e)}")
        return None
    