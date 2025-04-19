from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


class Repository(Base):
    __tablename__ = 'repositories'
    id = Column(Integer, primary_key=True)
    url = Column(String, unique=True)
    name = Column(String)


class Commit(Base):
    __tablename__ = 'commits'
    id = Column(Integer, primary_key=True)
    hash = Column(String, unique=True)
    author = Column(String)
    message = Column(String)
    date = Column(DateTime)
    repository_id = Column(Integer, ForeignKey('repositories.id'))

    repository = relationship("Repository", back_populates="commits")


class File(Base):
    __tablename__ = 'files'
    id = Column(Integer, primary_key=True)
    path = Column(String)
    repository_id = Column(Integer, ForeignKey('repositories.id'))

    repository = relationship("Repository", back_populates="files")


class LineChange(Base):
    __tablename__ = 'line_changes'
    id = Column(Integer, primary_key=True)
    file_id = Column(Integer, ForeignKey('files.id'))
    commit_id = Column(Integer, ForeignKey('commits.id'))
    line_number = Column(Integer)
    content = Column(String)

    file = relationship("File", back_populates="line_changes")
    commit = relationship("Commit", back_populates="line_changes")


Repository.commits = relationship("Commit", order_by=Commit.date, back_populates="repository")
Repository.files = relationship("File", order_by=File.path, back_populates="repository")
File.line_changes = relationship("LineChange", order_by=LineChange.line_number, back_populates="file")
Commit.line_changes = relationship("LineChange", back_populates="commit")
