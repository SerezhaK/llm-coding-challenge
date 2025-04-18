import os
import json
import tempfile  # Import tempfile module
import shutil  # Import shutil for cleaning up temporary directories
from langchain.text_splitter import Language, RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline
from langchain_community.llms import HuggingFacePipeline
from tqdm import tqdm  # For progress bars


class PRSpecificRAG:
    """
    A Retrieval-Augmented Generation system for analyzing Pull Request data.
    Uses Chroma as the vector store and a HuggingFace model for the LLM.
    """

    def __init__(self, data_path="pull_request_data_structured"):
        """
        Initializes the RAG system with data path, embeddings, and text splitter.
        Args:
            data_path (str, optional): The base directory containing structured PR data.
                                       Defaults to "pull_request_data_structured".
        """
        self.data_path = data_path
        # Initialize embeddings model (CodeBERT for code understanding)
        print("Initializing embeddings model...")
        try:
            self.embeddings = HuggingFaceEmbeddings(
                model_name="microsoft/graphcodebert-base",
                model_kwargs={"trust_remote_code": True}  # Needed for some models
            )
            print("Embeddings model initialized.")
        except Exception as e:
            print(f"Error initializing embeddings model: {str(e)}")
            self.embeddings = None  # Ensure embeddings is None if initialization fails
            # Depending on criticality, you might want to raise an exception here
            # raise e

        # Initialize text splitter for Python code
        self.splitter = RecursiveCharacterTextSplitter.from_language(
            language=Language.PYTHON,
            chunk_size=2048,  # Size of text chunks
            chunk_overlap=50  # Overlap between chunks to maintain context
        )
        self.pr_databases = {}  # Dictionary to store Chroma DBs for each PR (in-memory)
        self.llm = None  # LLM will be initialized separately

        # Keep track of temporary directories created for Chroma
        self._temp_chroma_dirs = {}

    def __del__(self):
        """
        Destructor to clean up temporary Chroma directories when the object is deleted.
        """
        print("Cleaning up temporary Chroma directories...")
        for pr_number, temp_dir in self._temp_chroma_dirs.items():
            try:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                    print(f"Cleaned up temporary directory for PR {pr_number}: {temp_dir}")
            except Exception as e:
                print(f"Error cleaning up temporary directory {temp_dir} for PR {pr_number}: {e}")

    def _load_pr_metadata(self, pr_dir):
        """
        Loads the metadata.json file for a single PR directory.
        Returns the metadata dictionary.
        Args:
            pr_dir (str): The path to the specific PR directory.
        Returns:
            dict: The metadata dictionary.
        Raises:
            FileNotFoundError: If metadata file is not found.
            json.JSONDecodeError: If metadata file is invalid JSON.
        """
        metadata_path = os.path.join(pr_dir, "metadata.json")
        if not os.path.exists(metadata_path):
            raise FileNotFoundError(f"Metadata file not found for PR in {pr_dir}")
        with open(metadata_path, "r", encoding='utf-8') as f:
            return json.load(f)

    def _process_single_pr(self, pr_dir_name):
        """
        Processes data for a single PR directory, creates text chunks,
        and builds a Chroma vector database using a temporary directory for persistence.
        Args:
            pr_dir_name (str): The name of the specific PR directory (e.g., "pr_123").
        Returns:
            Chroma: The Chroma vector database instance, or None on failure.
        """
        pr_number_str = pr_dir_name.split("_")[-1]  # Get PR number from directory name
        try:
            pr_number = int(pr_number_str)
        except ValueError:
            print(f"Warning: Could not parse PR number from directory name: {pr_dir_name}. Skipping.")
            return None  # Skip if PR number cannot be parsed

        full_path = os.path.join(self.data_path, pr_dir_name)
        if not os.path.isdir(full_path):
            print(f"Warning: PR directory not found or is not a directory: {full_path}. Skipping.")
            return None  # Skip if directory doesn't exist

        print(f"Processing data for PR #{pr_number_str}...")  # Use string for logging

        try:
            # Load metadata even if not explicitly stored in Chroma, as it's used for context creation
            metadata = self._load_pr_metadata(full_path)
        except FileNotFoundError as e:
            print(f"Error loading metadata for PR #{pr_number_str}: {e}. Skipping.")
            return None
        except json.JSONDecodeError as e:
            print(f"Error decoding metadata JSON for PR #{pr_number_str}: {e}. Skipping.")
            return None
        except Exception as e:
            print(f"An unexpected error occurred loading metadata for PR #{pr_number_str}: {e}. Skipping.")
            return None

        if self.embeddings is None:
            print(f"Skipping vector DB creation for PR #{pr_number_str}: Embeddings model not initialized.")
            return None

        chunks = []
        # Process changed files
        changed_files = metadata.get("changed_files_manifest", [])
        if not changed_files:
            print(f"No changed files found in metadata for PR #{pr_number_str}.")
            pass  # Continue to process other data

        for file_meta in tqdm(changed_files, desc=f"Processing files for PR #{pr_number_str}"):
            # Ensure file_meta is a dictionary and has a filename
            if not isinstance(file_meta, dict) or 'filename' not in file_meta:
                print(f"Warning: Skipping invalid file metadata entry for PR #{pr_number_str}: {file_meta}")
                continue

            filename = file_meta["filename"]
            try:
                # Load code and patch content
                before_code = self._read_code_file(full_path, "before_merge", filename)
                after_code = self._read_code_file(full_path, "after_merge", filename)
                patch = self._read_patch_file(full_path, filename)

                # Create context string for the file
                context = self._create_context(metadata, filename, before_code, after_code, patch)

                # Split the context into smaller chunks
                file_chunks = self.splitter.split_text(context)

                # Add chunks.
                chunks.extend(file_chunks)

            except Exception as e:
                print(f"Error processing file {filename} in PR {pr_number_str}: {str(e)}")

        # Optionally, add PR body and comments as separate documents if needed
        # This data will also be chunked and added to Chroma
        pr_body = metadata.get("body")
        if pr_body:
            body_chunks = self.splitter.split_text(f"PR Body:\n{pr_body}")
            chunks.extend(body_chunks)

        issue_comments = metadata.get("issue_comments", [])
        for comment in issue_comments:
            if isinstance(comment, dict) and comment.get("body"):
                comment_chunks = self.splitter.split_text(
                    f"Issue Comment by {comment.get('user', 'N/A')}:\n{comment.get('body')}")
                chunks.extend(comment_chunks)

        review_comments = metadata.get("review_comments", [])
        for comment in review_comments:
            if isinstance(comment, dict) and comment.get("body"):
                comment_chunks = self.splitter.split_text(
                    f"Review Comment by {comment.get('user', 'N/A')} on {comment.get('path', 'N/A')}:\n{comment.get('body')}")
                chunks.extend(comment_chunks)

        if not chunks:
            print(f"No processable content found for PR #{pr_number_str}. Skipping vector DB creation.")
            return None  # Skip if no chunks were created

        # Create a temporary directory for Chroma persistence for this PR
        # This helps avoid conflicts and ensures a clean DB each time.
        try:
            temp_dir = tempfile.mkdtemp(prefix=f"chroma_db_pr_{pr_number_str}_")
            self._temp_chroma_dirs[pr_number_str] = temp_dir  # Store for cleanup

            vector_db = Chroma.from_texts(
                texts=chunks,
                embedding=self.embeddings,
                persist_directory=temp_dir  # Explicitly use the temporary directory
            )
            print(f"Successfully created Chroma DB for PR #{pr_number_str} in temporary directory: {temp_dir}")
        except Exception as e:
            print(f"Error creating Chroma DB for PR #{pr_number_str}: {str(e)}")
            # Clean up the temporary directory if creation failed
            if pr_number_str in self._temp_chroma_dirs:
                try:
                    shutil.rmtree(self._temp_chroma_dirs[pr_number_str])
                    del self._temp_chroma_dirs[pr_number_str]
                except Exception as cleanup_e:
                    print(f"Error during cleanup of temp dir {temp_dir}: {cleanup_e}")

            return None  # Return None if DB creation fails

        # Store the created vector database in the dictionary
        self.pr_databases[pr_number_str] = vector_db  # Store with string key
        return vector_db

    def _read_code_file(self, pr_path, dir_name, filename):
        """
        Reads content from a code file within a PR directory.
        Returns the file content as a string or an empty string if not found/error.
        Args:
            pr_path (str): The full path to the specific PR directory.
            dir_name (str): The subdirectory name ('before_merge' or 'after_merge').
            filename (str): The name of the file.
        Returns:
            str: The file content as a string, or empty string if not found/error.
        """
        file_path = os.path.join(pr_path, dir_name, filename)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                print(f"Error reading file {file_path}: {str(e)}")
                return ""  # Return empty string on error
        return ""  # Return empty string if file doesn't exist

    def _read_patch_file(self, pr_path, filename):
        """
        Reads content from a patch file within a PR directory.
        Returns the patch content as a string or an empty string if not found/error.
         Args:
            pr_path (str): The full path to the specific PR directory.
            filename (str): The name of the original file (used to construct patch filename).
        Returns:
            str: The patch content as a string, or empty string if not found/error.
        """
        patch_path = os.path.join(pr_path, "changed_files", filename + ".patch")
        if os.path.exists(patch_path):
            try:
                with open(patch_path, "r", encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                print(f"Error reading patch file {patch_path}: {str(e)}")
                return ""  # Return empty string on error
        return ""  # Return empty string if file doesn't exist

    def _create_context(self, metadata, filename, before_code, after_code, patch):
        """
        Creates a combined context string for a specific file within a PR,
        including PR details, code changes, and relevant comments.
        Args:
            metadata (dict): The full PR metadata dictionary.
            filename (str): The name of the file being processed.
            before_code (str): The code content before the change.
            after_code (str): The code content after the change.
            patch (str): The patch/diff content.
        Returns:
            str: A formatted string containing the context for the RAG model.
        """
        # Filter review comments relevant to this specific file
        file_review_comments = [
            c.get('body') for c in metadata.get('review_comments', [])
            if isinstance(c, dict) and c.get('path') == filename and c.get('body')
            # Ensure valid comment dict and body/path exist
        ]
        comments_text = "\n".join(
            file_review_comments) if file_review_comments else "No specific review comments for this file."

        # Safely get CI check names
        ci_checks_list = [c.get('name', 'N/A') for c in metadata.get('check_runs', []) if isinstance(c, dict)]
        ci_checks_str = ", ".join(ci_checks_list) if ci_checks_list else "No CI checks found."

        context = (
            f"--- Pull Request #{metadata.get('pr_number', 'N/A')} - {metadata.get('title', 'N/A')} ---\n"
            f"Author: {metadata.get('author_login', 'N/A')}\n"
            f"File: {filename}\n"
            f"Status: {metadata.get('state', 'N/A')}\n"
            f"CI Checks for head commit: {ci_checks_str}\n\n"
            f"BEFORE CODE:\n{before_code}\n\n"
            f"AFTER CODE:\n{after_code}\n\n"
            f"DIFF:\n{patch}\n\n"
            f"REVIEW COMMENTS on this file:\n{comments_text}\n"
        )
        return context

    def initialize_llm(self):
        """
        Initializes the HuggingFace Language Model pipeline.
        Loads the tokenizer and model, sets up the text generation pipeline.
        """
        if self.llm is not None:
            print("LLM already initialized.")
            return

        # model_name = "HuggingFaceH4/zephyr-7b-beta" # Larger model example
        model_name = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"  # Smaller model for faster local testing
        print(f"Initializing LLM: {model_name}...")
        try:
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            model = AutoModelForCausalLM.from_pretrained(model_name)

            text_gen_pipeline = pipeline(
                "text-generation",
                model=model,
                tokenizer=tokenizer,
                temperature=0.1,  # Controls randomness (lower is more deterministic)
                max_new_tokens=512,  # Max tokens to generate in the response
                repetition_penalty=1.1  # Penalize repeating tokens
                # Add other pipeline arguments as needed (e.g., device='cuda' or device='cpu')
            )

            self.llm = HuggingFacePipeline(pipeline=text_gen_pipeline)
            print("LLM initialized successfully.")
        except Exception as e:
            print(f"Error initializing LLM: {str(e)}")
            self.llm = None  # Ensure LLM is None if initialization fails
            # Depending on criticality, you might want to raise an exception here
            # raise e

    def load_pr(self, pr_number):
        """
        Loads data for a specific PR number into an in-memory vector database.
        Processes the raw data files saved by get_all_pull_requests_structured.
        Returns the created Chroma DB instance or None on failure.
        Args:
            pr_number (int or str): The pull request number.
        Returns:
            Chroma: The Chroma vector database instance, or None on failure.
        """
        pr_number_str = str(pr_number)  # Ensure key is string
        pr_dir_name = f"pr_{pr_number_str}"  # Ensure directory name uses string PR number
        pr_dir_path = os.path.join(self.data_path, pr_dir_name)

        if not os.path.exists(pr_dir_path):
            print(f"Error: Data directory for PR #{pr_number_str} not found at {pr_dir_path}")
            return None

        # Process the PR data and create/store the vector DB
        # _process_single_pr now handles creating/storing the DB and using a temp dir
        vector_db = self._process_single_pr(pr_dir_name)
        return vector_db  # Returns the DB instance or None

    def get_review(self, pr_number, question):
        """
        Gets a review/answer for a specific question about a PR.
        Loads PR data if necessary, retrieves relevant context, formats the prompt,
        and invokes the LLM directly.
        Args:
            pr_number (int or str): The pull request number.
            question (str): The question to ask about the PR.
        Returns:
            dict: A dictionary containing the PR number, question, answer, and sources.
        """
        pr_number_str = str(pr_number)  # Ensure key is string

        try:
            if self.llm is None:
                raise ValueError("LLM is not initialized. Call initialize_llm() first.")

            # Load PR data if not already in memory or if the stored DB is None
            if pr_number_str not in self.pr_databases or self.pr_databases[pr_number_str] is None:
                print(f"PR #{pr_number_str} data not loaded. Attempting to load...")
                loaded_db = self.load_pr(pr_number_str)  # Pass as string
                if loaded_db is None:
                    raise ValueError(f"Failed to load data for PR #{pr_number_str}. Cannot perform RAG analysis.")

            # Get the retriever for the specific PR's database
            retriever = self.pr_databases[pr_number_str].as_retriever(search_kwargs={"k": 2})

            # Retrieve relevant documents based on the question
            source_documents = retriever.get_relevant_documents(question)

            # Format the retrieved documents into a context string
            context_text = "\n\n---\n\n".join([doc.page_content for doc in source_documents])

            # Get CI checks for the prompt (assuming they are in metadata)
            pr_dir_name = f"pr_{pr_number_str}"
            metadata = self._load_pr_metadata(os.path.join(self.data_path, pr_dir_name))
            ci_checks_list = [c.get('name', 'N/A') for c in metadata.get('check_runs', []) if isinstance(c, dict)]
            ci_checks_str = ", ".join(ci_checks_list) if ci_checks_list else "No CI checks found."

            # Define the prompt template manually
            prompt_template = """<|system|>
            You are a helpful assistant specializing in code review analysis.
            You are analyzing Pull Request #{pr_number}. Relevant context from the PR is provided below:

            {context}

            Consider the following aspects from the PR data:
            1. Code changes (diff)
            2. Developer comments (issue and review comments)
            3. Results of CI checks: {ci_checks}
            4. Commit history (summarized in context)

            Based on the provided context, answer the user's question about the Pull Request.
            If the context does not contain enough information to answer the question,
            state that you cannot answer based on the available information.
            </s>
            <|user|>
            {question}
            </s>
            <|assistant|>
            """

            # --- Debugging Print Statements (can be removed later) ---
            print(f"--- Debugging Prompt Variables for PR #{pr_number_str} ---")
            print(f"pr_number_str: {pr_number_str}")
            print(f"question: {question}")
            print(f"ci_checks_str: {ci_checks_str}")
            print(f"context_text (first 200 chars): {context_text[:200]}...")
            print("--- End Debugging Print Statements ---")

            # Manually format the prompt string
            formatted_prompt = prompt_template.format(
                pr_number=pr_number_str,
                context=context_text,
                ci_checks=ci_checks_str,
                question=question
            )

            # Invoke the LLM directly with the formatted prompt
            # The LLM pipeline expects a single string input
            llm_response = self.llm.invoke(formatted_prompt)

            # The LLM response might contain the original prompt + the generated answer.
            # We need to extract just the generated answer part.
            # This extraction depends on the specific LLM's output format.
            # For chat models, the assistant's response usually follows the <|assistant|> tag.
            answer = llm_response.split("<|assistant|>")[-1].strip()

            # Format the source documents for output
            formatted_sources = self._format_sources(source_documents)

            return {
                "pr": pr_number_str,  # Return as string for consistency
                "question": question,
                "answer": answer if answer else "Could not generate an answer based on the available information.",
                "sources": formatted_sources
            }
        except ValueError as e:
            # Handles errors from LLM not initialized or data loading failed
            print(f"Error getting review for PR #{pr_number_str}: {e}")
            return {
                "pr": pr_number_str,
                "question": question,
                "answer": f"Error processing PR #{pr_number_str}: {e}",
                "sources": []
            }
        except Exception as e:
            print(f"An unexpected error occurred getting review for PR #{pr_number_str}: {str(e)}")
            return {
                "pr": pr_number_str,
                "question": question,
                "answer": f"An unexpected error occurred during review generation: {str(e)}",
                "sources": []
            }

    def _format_sources(self, docs):
        """
        Formats source documents returned by the retriever for output.
        Since metadata is not stored in Chroma, this will only show basic info
        like the document content itself if the retriever returns Document objects.
        Args:
            docs (list): A list of source documents (expected to be Document objects without metadata).
        Returns:
            list: A list of formatted source dictionaries (will have limited info).
        """
        formatted_sources = []
        for doc in docs:
            # When metadata is not stored, the retrieved 'doc' is typically a Document object
            # with only 'page_content'.
            # We can't get file, checks, or author from metadata if it wasn't stored.
            source_info = {
                "content_snippet": str(doc.page_content)[:200] + "..." if doc and hasattr(doc,
                                                                                          'page_content') else "N/A",
                # Show a snippet of the content
                "file": "Unknown (metadata not stored)",  # Indicate metadata is missing
                "checks": "Unknown (metadata not stored)",
                "author": "Unknown (metadata not stored)"
            }
            formatted_sources.append(source_info)
        return formatted_sources
