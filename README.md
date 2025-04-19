# GitHub Code Review Assistant

A Streamlit-based application that uses LLM technology + RAG to analyze GitHub repositories and provide code reviews based on commit history.

## Description

This application allows users to:
- Input a GitHub repository URL
- Specify a GitHub username for review
- Select a date range for analysis
- Optionally provide additional query specifications
- Receive an AI-generated code review that evaluates the developer's growth, code quality, and provides recommendations

The review analyzes:
- Clarity of commit messages
- Frequency and regularity of commits
- Complexity of tasks solved
- Code quality
- Project involvement

## Installation

### Prerequisites
- Python 3.8+
- Git

### Setup
1. Clone this repository:
   ```
   git clone https://github.com/yourusername/llm-coding-challenge.git
   cd llm-coding-challenge
   ```

2. Create and activate a virtual environment:
   ```
   python -m venv venv
   venv\Scripts\activate
   ```

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Create a `.env` file with your API keys:
   ```
   OPENAI_API_KEY=your_openai_api_key
   GITHUB_TOKEN=your_github_token
   ```

## Usage

1. Start the Streamlit application:
   ```
   streamlit run app\main.py
   ```

2. In the web interface:
   - Enter API keys for Yandex GPT
   - Enter GitHub PAT
   - Enter the GitHub repository URL
   - Enter the GitHub username for review
   - Select the date range for analysis
   - Optionally add specific review requests
   - Click "Выполнить code review" (Perform code review)

3. View the generated code review with analysis, rating, and recommendations.

## Project Structure

```
llm-coding-challenge\
├── app\                      # Streamlit application
│   ├── components\           # UI components
│   │   ├── hello_world.py    # Welcome component
│   │   ├── page.py           # Page initialization
│   │   ├── sidebar.py        # Sidebar component
│   │   └── user_date_validation.py # Date validation
│   └── main.py               # Main application entry point
├── llm_logic\                # LLM integration logic
│   ├── parsing\              # Repository parsing
│   │   ├── models.py         # Database models
│   │   └── repo_parsing.py   # GitHub repository parser
│   ├── api_request.py        # API request handling
│   └── prompt.py             # LLM prompt templates
├── .env                      # Environment variables (not in repo)
├── Dockerfile                # Docker configuration
├── requirements.txt          # Python dependencies
└── README.md                 # This file
```

## Dependencies

Key dependencies include:
- streamlit - Web application framework
- openai - OpenAI API client
- git/pygit2 - Git repository interaction
- sqlalchemy - Database ORM
- requests - HTTP requests

For a complete list, see `requirements.txt`.

## Docker Support

You can also run the application using Docker:

```
docker build -t github-code-review .
docker run -p 8501:8501 github-code-review
```

Then access the application at http://localhost:8501
