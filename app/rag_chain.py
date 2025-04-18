from langchain.chains import ConversationalRetrievalChain
from langchain.chat_models import ChatOpenAI
from langchain.memory import ConversationBufferMemory
from langchain_community.embeddings import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS

# Загрузка векторного хранилища
def load_vector_store():
    embeddings = OpenAIEmbeddings(model="text-embedding-ada-002")
    vectorstore = FAISS.load_local("faiss_index", embeddings)
    return vectorstore


# Создание RAG-цепочки
def create_rag_chain(vectorstore):
    # Инициализация модели ChatGPT-4
    llm = ChatOpenAI(model="gpt-4o", temperature=0)

    # Инициализация памяти для хранения истории диалога
    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

    # Создание RAG-цепочки
    qa_chain = ConversationalRetrievalChain.from_llm(
        llm=llm,
        retriever=vectorstore.as_retriever(),  # Используем векторное хранилище для поиска
        memory=memory,  # Передаем память для хранения истории
    )
    return qa_chain


if __name__ == "__main__":
    vectorstore = load_vector_store()
    qa_chain = create_rag_chain(vectorstore)
    print("RAG Created - Victory.")
x`