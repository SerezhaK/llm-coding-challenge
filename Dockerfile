FROM python:3.11.9

LABEL authors="Serezhaaaaa"

RUN pip install --upgrade pip

COPY ./requirements.txt .
RUN pip install -r requirements.txt

COPY . /app
WORKDIR /app

CMD ["streamlit", "run", "app/main.py", "--server.port", "8080"]