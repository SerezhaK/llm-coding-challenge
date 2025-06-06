import streamlit as st


def hello_world():
    st.markdown(
        """
            Привет! Я — бот для ревью кода на основе Yandex GPT. 🚀
            
            Чтобы начать, заполни поля слева:  
            * 1️⃣ [PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) для доступа к GitHub
            * 2️⃣ Folder ID и API key для доступпа к Yandex GPT 
            
            После этого отправь мне:
            * 1️⃣ Ссылку на GitHub-репозиторий и ветку для анализа
            * 2️⃣ GitHub-юзернейм (для упоминания в комментариях)
            * 3️⃣ Период за который нужно провести ревью 
            * 4️⃣ Можете добавить контекст, на котором хотите сосредоточится, например "Хорошо ли разработчик понимает ООП?"  
                    
            Я проанализирую код, проверю стиль, найду потенциальные уязвимости и дам рекомендации. Начнем?
        """
    )
