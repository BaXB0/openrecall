import numpy as np
from sentence_transformers import SentenceTransformer
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
MODEL_NAME: str = "all-MiniLM-L6-v2"
EMBEDDING_DIM: int = 384  # Dimension for all-MiniLM-L6-v2

# Load the model globally to avoid reloading it on every call
try:
    model = SentenceTransformer(MODEL_NAME)
    logger.info(f"SentenceTransformer model '{MODEL_NAME}' loaded successfully.")
except Exception as e:
    logger.error(f"Failed to load SentenceTransformer model '{MODEL_NAME}': {e}")
    model = None


def get_embedding(text: str) -> np.ndarray:
    """
    Generates a sentence embedding for the given text.

    Splits the text into lines, encodes each line using the pre-loaded
    SentenceTransformer model, and returns the mean of the embeddings.
    Handles empty input text by returning a zero vector.

    Args:
        text: The input string to embed.

    Returns:
        A numpy array representing the mean embedding of the text lines,
        or a zero vector if the input is empty, whitespace only, or the
        model failed to load. The array type is float32.
    """
    if model is None:
        logger.error("SentenceTransformer model is not loaded. Returning zero vector.")
        return np.zeros(EMBEDDING_DIM, dtype=np.float32)

    if not text or text.isspace():
        logger.warning("Input text is empty or whitespace. Returning zero vector.")
        return np.zeros(EMBEDDING_DIM, dtype=np.float32)

    # Split text into non-empty lines
    sentences = [line for line in text.split("\n") if line.strip()]

    if not sentences:
        logger.warning("No non-empty lines found after splitting. Returning zero vector.")
        return np.zeros(EMBEDDING_DIM, dtype=np.float32)

    try:
        sentence_embeddings = model.encode(sentences)
        # Calculate the mean embedding
        mean_embedding = np.mean(sentence_embeddings, axis=0, dtype=np.float32)
        return mean_embedding
    except Exception as e:
        logger.error(f"Error generating embedding: {e}")
        return np.zeros(EMBEDDING_DIM, dtype=np.float32)


def get_embeddings(texts: list) -> np.ndarray:
    """
    Batched version of :func:`get_embedding` for many documents at once.

    Each text is embedded on its own (newlines are collapsed to spaces) and the
    rows come back in input order, so callers can align them with their source
    list. Returns a (len(texts), EMBEDDING_DIM) float32 array; rows for empty
    text or a failed model are zero vectors.

    Args:
        texts: The input strings to embed.

    Returns:
        A 2D numpy array of shape (len(texts), EMBEDDING_DIM).
    """
    if model is None:
        logger.error("SentenceTransformer model is not loaded. Returning zero vectors.")
        return np.zeros((len(texts), EMBEDDING_DIM), dtype=np.float32)

    prepared = [" ".join((t or "").split()) for t in texts]
    try:
        return np.asarray(
            model.encode(prepared, batch_size=32, show_progress_bar=False),
            dtype=np.float32,
        )
    except Exception as e:
        logger.error(f"Error generating batch embeddings: {e}")
        return np.zeros((len(texts), EMBEDDING_DIM), dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Calculates the cosine similarity between two numpy vectors.

    Args:
        a: The first numpy array.
        b: The second numpy array.

    Returns:
        The cosine similarity score (float between -1 and 1),
        or 0.0 if either vector has zero magnitude.
    """
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)

    if norm_a == 0 or norm_b == 0:
        logger.warning("One or both vectors have zero magnitude. Returning 0 similarity.")
        return 0.0

    similarity = np.dot(a, b) / (norm_a * norm_b)
    # Clip values to handle potential floating-point inaccuracies slightly outside [-1, 1]
    return float(np.clip(similarity, -1.0, 1.0))
