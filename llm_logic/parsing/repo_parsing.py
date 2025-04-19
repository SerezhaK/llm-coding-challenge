import git
import pygit2
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import os
from models import Repository, Commit, File, LineChange, Base
import tempfile
import shutil
import logging

logger = logging.getLogger(__name__)


class GitParser:
    def __init__(self, db_path='git_analysis.db'):
        self.engine = create_engine(f'sqlite:///{db_path}')
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def analyze_repository(self, repo_url, clone_path_repo=None):
        if clone_path_repo is None:
            clone_path_repo = repo_url.split('/')[-1].replace('.git', '')

        session = self.Session()

        # We check if there is already such a repository in the database
        repo = session.query(Repository).filter_by(url=repo_url).first()
        if repo:
            logger.info('The repository has already been processed')
            return
        try:
            logger.info('Startes cloning repository...', repo_url)
            git.Repo.clone_from(repo_url, clone_path_repo)
            repo_name = os.path.basename(clone_path_repo)
            logger.info('Cloning the repository is completed', repo_url)
        except git.exc.GitCommandError:
            print(f"We use the existing local copy in {clone_path_repo}")
            repo_name = os.path.basename(clone_path_repo)

        # Add the repository to the base
        repo = Repository(url=repo_url, name=repo_name)
        session.add(repo)
        session.commit()

        # We analyze the commits
        repo_pygit = pygit2.Repository(clone_path_repo)
        repo_git = git.Repo(clone_path_repo)

        logger.info('Start allocation of commits')

        for commit in repo_git.iter_commits():
            db_commit = Commit(
                hash=commit.hexsha,
                author=commit.author.name,
                message=commit.message.strip(),
                date=datetime.fromtimestamp(commit.authored_date),
                repository_id=repo.id
            )
            session.add(db_commit)

            # We get changes in the commite
            if commit.parents:
                parent = commit.parents[0]
                diff = parent.diff(commit, create_patch=True)

                for file_diff in diff:
                    file_path = file_diff.a_path if file_diff.a_path else file_diff.b_path

                    # Skip binary files
                    if file_diff.diff.startswith(b'Binary files'):
                        continue

                    # Check/add the file to the database
                    db_file = session.query(File).filter_by(path=file_path, repository_id=repo.id).first()
                    if not db_file:
                        db_file = File(path=file_path, repository_id=repo.id)
                        session.add(db_file)
                        session.commit()

                    # analyze the changes in the lines
                    diff_lines = file_diff.diff.decode('utf-8', errors='replace').split('\n')
                    current_line = None
                    line_content = []

                    for line in diff_lines:
                        if line.startswith('@@'):
                            parts = line.split(' ')
                            new_info = parts[2].split(',')
                            current_line = int(new_info[0][1:])
                            line_content = []
                        elif line.startswith('+') and not line.startswith('++'):
                            if current_line is not None:
                                content = line[1:]
                                line_content.append(content)
                                print(line_content)
                                # Сохраняем изменение строки
                                line_change = LineChange(
                                    file_id=db_file.id,
                                    commit_id=db_commit.id,
                                    line_number=current_line,
                                    content=content
                                )
                                session.add(line_change)

                                current_line += 1

                        elif line.startswith('-') and not line.startswith('--'):
                            # Удаленная строка
                            if current_line is not None:
                                current_line += 0  # Номера строк смещаются
                        else:
                            if current_line is not None:
                                current_line += 1

        session.commit()
        logger.info('All commits are allocated')


# Test
if __name__ == "__main__":
    analyzer = GitParser()
    while True:
        repo_url = input("Введите URL Git репозитория: ")
        clone_path = input("Введите путь для клонирования (оставьте пустым для автоматического выбора): ") or None
        analyzer.analyze_repository(repo_url, clone_path)
