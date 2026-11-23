"""get_reviews_and_guides — the Knowledge Base's own text about one book.

Pure retrieval (no LLM). Looks one book up in the Bedrock Knowledge Base by
ISBN and returns its passages, each with a `source_id` the agent can cite. This
is the grounding tool: what the agent says about a book should come from here.

What the KB holds today (checked against the live KB): one document per book,
`doc_type` "book", made of title, author, genres, editorial review quotes and
an "About the Author" section, split into one or two passages. There are no
separate reading guides yet; they would come back through this same tool once
ingested, told apart by `doc_type`. The review quotes are mostly publisher
praise, so treat them as marketing copy rather than independent criticism.

The book is pinned by a metadata filter, so every passage returned belongs to
it. Bedrock still needs a query text: `question` ranks the passages, it does
not select them. An empty list means the KB has nothing on this book.

Auth/region come from the standard AWS chain (your AWS CLI profile in ~/.aws);
BEDROCK_KB_ID is read from the environment or the app's `.env`.

Direct test:
    python -m tools.get_reviews_and_guides 9780553283686
    python -m tools.get_reviews_and_guides 0765348276 "is there romance in it"
"""
from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from config import KBConfig  # importing config also loads .env
from isbn import to_isbn10, to_isbn13
from models import ReviewDoc

logger = logging.getLogger(__name__)

# Ranks the passages when the caller has no specific question.
DEFAULT_QUESTION = "Reviews, critical reception and description of this book"

# Bedrock's per-call maximum for numberOfResults.
MAX_RESULTS = 100


class GetReviewsError(RuntimeError):
    """Raised when the lookup could not be completed (bad ISBN, AWS failure)."""


def get_reviews_and_guides(
    isbn: str,
    *,
    question: str | None = None,
    top_k: int = 5,
    knowledge_base_id: str | None = None,
    region: str | None = None,
    client: Any | None = None,
) -> list[ReviewDoc]:
    """Return up to `top_k` Knowledge Base passages about the book `isbn`.

    Args:
        isbn: The book, as ISBN-10 or ISBN-13, with or without hyphens.
        question: What the caller wants to know ("is the ending hopeful?").
            Passages are ordered by relevance to it. Optional.
        top_k: Max passages to return. A book has one or two today.
        knowledge_base_id: Bedrock KB id; defaults to BEDROCK_KB_ID.
        region: AWS region; None lets boto3 resolve it from the AWS CLI profile.
        client: Injectable boto3 'bedrock-agent-runtime' client (for tests).

    Returns:
        Passages ordered best match first. Empty when the KB has nothing filed
        under this ISBN — the caller reports that as "no source on this book"
        instead of answering from memory.

    Raises:
        GetReviewsError: `isbn` is malformed, the KB id is missing, or the
            Bedrock call failed.
    """
    isbn13 = to_isbn13(isbn)
    if isbn13 is None:
        raise GetReviewsError(f"Not a valid ISBN-10 or ISBN-13: {isbn!r}")
    if top_k < 1:
        raise GetReviewsError(f"top_k must be at least 1, got {top_k}")

    kb = KBConfig.from_env()
    kb_id = knowledge_base_id or kb.knowledge_base_id
    if not kb_id:
        raise GetReviewsError("BEDROCK_KB_ID is not set.")

    client = client or boto3.client(
        "bedrock-agent-runtime", region_name=region or kb.region
    )
    search_config = {
        "numberOfResults": min(top_k, MAX_RESULTS),
        "filter": _isbn_filter(isbn13),
        # The KB's default reranking keeps only the single best passage of a
        # book. Turn it off so every passage of the book comes back.
        "rerankingModelType": "NONE",
    }
    try:
        # `managedSearchConfiguration` because the project's KB is a managed
        # knowledge base; Bedrock rejects `vectorSearchConfiguration` for it.
        response = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": (question or "").strip() or DEFAULT_QUESTION},
            retrievalConfiguration={"managedSearchConfiguration": search_config},
        )
    except (ClientError, BotoCoreError) as e:
        raise GetReviewsError(f"Bedrock retrieve failed: {e}") from e

    docs: dict[str, ReviewDoc] = {}
    for result in response.get("retrievalResults", []):
        text = ((result.get("content") or {}).get("text") or "").strip()
        if not text:
            continue
        metadata = result.get("metadata") or {}
        source = _source_uri(result.get("location") or {}, metadata)
        source_id = _clean(metadata.get("_chunk_id")) or source
        doc = ReviewDoc(
            isbn=to_isbn13(metadata.get("isbn")) or _clean(metadata.get("isbn")),
            title=_clean(metadata.get("title")),
            author=_clean(metadata.get("author")),
            text=text,
            score=float(result.get("score") or 0.0),
            source=source,
            source_id=source_id,
            doc_type=_clean(metadata.get("doc_type")),
        )
        # The same passage should not come back twice; keep the first if it does.
        docs.setdefault(source_id or text, doc)

    if not docs:
        # An unknown ISBN and a renamed metadata field look the same from here:
        # Bedrock returns no hits and no error for a filter on a missing key.
        logger.info("get_reviews_and_guides: no KB passages for ISBN %s", isbn13)

    ranked = sorted(docs.values(), key=lambda d: d.score, reverse=True)
    return ranked[:top_k]


def _isbn_filter(isbn13: str) -> dict:
    """Match the book whichever ISBN field and form the KB filed it under.

    The KB keeps the ISBN-10 in `isbn` and an ISBN-13 in `isbn13`. The `isbn13`
    values are unreliable (some malformed, some another edition's), so `isbn`
    is the arm that normally matches. The last arm covers a KB re-ingested with
    ISBN-13 in `isbn`.
    """
    arms = [_equals("isbn13", isbn13), _equals("isbn", isbn13)]
    isbn10 = to_isbn10(isbn13)
    if isbn10:
        arms.insert(0, _equals("isbn", isbn10))
    return {"orAll": arms}


def _equals(key: str, value: str) -> dict:
    return {"equals": {"key": key, "value": value}}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _source_uri(location: dict, metadata: dict) -> str | None:
    """The file a passage came from: Bedrock's `location`, else the metadata."""
    # Common shape: {"type": "S3", "s3Location": {"uri": "s3://..."}}.
    for sub in location.values():
        if isinstance(sub, dict):
            uri = sub.get("uri") or sub.get("url")
            if uri:
                return uri
    return _clean(metadata.get("_source_uri"))


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print("usage: python -m tools.get_reviews_and_guides <isbn> [question]")
        raise SystemExit(2)

    try:
        found = get_reviews_and_guides(
            sys.argv[1], question=" ".join(sys.argv[2:]) or None
        )
    except GetReviewsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if not found:
        print("The Knowledge Base has nothing on this book.")
    for doc in found:
        print(f"{doc.score:.3f}  {doc.title or '?'}  [{doc.isbn or 'no-isbn'}]")
        print(f"        cite as: {doc.source_id}")
        print(f"        {doc.text}\n")
