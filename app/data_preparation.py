import pandas as pd
from langchain_community.document_loaders import DataFrameLoader
from langchain_community.embeddings import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS




def load_dataset(file_path):
    df = pd.read_csv(file_path)
    return df


# Создание векторного хранилища
def create_vector_store(df, embedding_column="description"):
    loader = DataFrameLoader(df, page_content_column=embedding_column)
    documents = loader.load()

    embeddings = OpenAIEmbeddings(model="text-embedding-3-large")

    vectorstore = FAISS.from_documents(documents, embeddings)

    vectorstore.save_local("faiss_index")

    print("Vector storage is created and preserved.")


# for manual tests
if __name__ == "__main__":
    dataset_path = "dataset/data/data.csv"
    df = load_dataset(dataset_path)

    create_vector_store(df)
