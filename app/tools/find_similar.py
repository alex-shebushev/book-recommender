"""find_similar — retrieve books similar to a seed, from the Bedrock KB.

Pure retrieval (no LLM). Queries the Bedrock Knowledge Base `Retrieve` API with
the seed book's description/themes, resolves each hit to a book via chunk
metadata, and returns a deduped, thresholded, ranked list.

Auth/region come from the standard AWS chain (your AWS CLI profile in ~/.aws);
only BEDROCK_KB_ID must be set. See DESIGN.md §5–6 and OPEN_QUESTIONS.md.

Metadata dependency: each hit resolves to a book only if the KB was ingested
per-book with `isbn`/`title` in chunk metadata. When that metadata is absent the
hit is still returned (isbn/title = None) so the tool degrades instead of
failing — but recommendations can't be verified against the catalog without it.

Direct test:
    python -m tools.find_similar "a lonely astronaut saves earth with humor"
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from isbn import to_isbn10, to_isbn13
from models import SimilarBook

logger = logging.getLogger(__name__)


class FindSimilarError(RuntimeError):
    """Raised when the KB retrieval could not be completed."""


def find_similar(
    query: str,
    *,
    knowledge_base_id: str | None = None,
    region: str | None = None,
    top_k: int = 5,
    min_score: float = 0.0,
    exclude_isbns: Iterable[str] | None = None,
    flag_isbns: Iterable[str] | None = None,
    metadata_filter: dict | None = None,
    client: Any | None = None,
) -> list[SimilarBook]:
    """Return up to `top_k` books most similar to `query`.

    Args:
        query: The seed book's description / themes (the agent supplies this).
        knowledge_base_id: Bedrock KB id; defaults to env BEDROCK_KB_ID.
        region: AWS region; None lets boto3 resolve it from the AWS CLI profile.
        top_k: Max books to return after dedup.
        min_score: Drop hits below this relevance score (0 = off, the default —
            scores aren't normalized across vector stores, so calibrate per store).
        exclude_isbns: ISBNs to omit client-side (seed book + already-read). Prefer
            `metadata_filter` for server-side exclusion when the KB supports it.
        flag_isbns: ISBNs to *keep but mark* `excluded=True` (the reader's
            Exclusion shelf). Unlike `exclude_isbns` these still take a slot and
            are returned, so the caller can hide or gray them. Any ISBN form.
        metadata_filter: Bedrock retrieval `filter` dict pushed to the Retrieve
            call (e.g. exclude ISBNs server-side). Requires the field to be
            filterable in the KB; rejected by Bedrock otherwise.
        client: Injectable boto3 'bedrock-agent-runtime' client (for tests).

    Returns:
        Ranked, deduped list of SimilarBook. Empty when nothing clears the
        threshold — the caller treats that as "no strong matches".
    """
    kb_id = knowledge_base_id or os.getenv("BEDROCK_KB_ID")
    if not kb_id:
        raise FindSimilarError("BEDROCK_KB_ID is not set.")
    if not query or not query.strip():
        return []

    region = region or os.getenv("AWS_REGION") or None
    client = client or boto3.client("bedrock-agent-runtime", region_name=region)
    # Match exclusion regardless of ISBN form. The KB stores each book's `isbn`
    # as ISBN-10 while the app shares ISBN-13, so comparing raw strings excluded
    # nothing (Q13). Expand every excluded value to all its forms.
    exclude: set[str] = set()
    for item in exclude_isbns or ():
        exclude |= _isbn_forms(item)
    # Soft exclusion: kept and tagged, not dropped.
    flag: set[str] = set()
    for item in flag_isbns or ():
        flag |= _isbn_forms(item)

    # Over-fetch: several chunks can map to one book, and (absent a server-side
    # filter) we drop excluded ISBNs client-side. Clamp to Bedrock's per-call max.
    num_results = min(max(top_k * 3, 10), 100)
    search_config: dict = {"numberOfResults": num_results}
    if metadata_filter:
        search_config["filter"] = metadata_filter
    # The project's KB is a *managed* knowledge base: Bedrock rejects
    # `vectorSearchConfiguration` for it and requires this key instead. A KB
    # backed by your own vector store would need `vectorSearchConfiguration`.
    try:
        response = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"managedSearchConfiguration": search_config},
        )
    except (ClientError, BotoCoreError) as e:
        raise FindSimilarError(f"Bedrock retrieve failed: {e}") from e

    # Aggregate chunks -> one best-scored entry per book.
    best: dict[str, SimilarBook] = {}
    for result in response.get("retrievalResults", []):
        score = float(result.get("score") or 0.0)
        if score < min_score:
            continue

        metadata = result.get("metadata") or {}
        isbn = _clean(metadata.get("isbn"))
        title = _clean(metadata.get("title"))
        forms = _isbn_forms(isbn) if isbn else set()
        if forms & exclude:
            continue

        snippet = ((result.get("content") or {}).get("text") or "").strip()
        source = _location_uri(result.get("location") or {})

        # Dedup key: ISBN when known, else title, else the snippet head.
        key = isbn or title or f"__chunk__{snippet[:48]}"
        existing = best.get(key)
        if existing is None or score > existing.score:
            best[key] = SimilarBook(
                isbn=isbn, title=title, score=score, snippet=snippet, source=source,
                excluded=bool(forms & flag),
            )

    if not any(b.isbn for b in best.values()) and best:
        logger.warning(
            "find_similar: no hits carried an `isbn` — the KB may lack per-book "
            "metadata; results can't be resolved against the catalog."
        )

    ranked = sorted(best.values(), key=lambda b: b.score, reverse=True)
    return ranked[:top_k]


def _isbn_forms(value: Any) -> set[str]:
    """Every comparable form of an ISBN: the cleaned raw value plus its ISBN-13
    and ISBN-10. Lets exclusion match whichever form the KB stored (ISBN-10)
    against whichever the caller passed (ISBN-13), and vice versa."""
    forms: set[str] = set()
    raw = _clean(value)
    if not raw:
        return forms
    forms.add(raw)
    for convert in (to_isbn13, to_isbn10):
        form = convert(raw)
        if form:
            forms.add(form)
    return forms


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _location_uri(location: dict) -> str | None:
    """Pull a human-readable source URI out of a Bedrock `location` object."""
    if not location:
        return None
    # Common shape: {"type": "S3", "s3Location": {"uri": "s3://..."}}.
    for sub in location.values():
        if isinstance(sub, dict) and sub.get("uri"):
            return sub["uri"]
    return _clean(location.get("type"))


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    seed = " ".join(sys.argv[1:]).strip() or (
        "a hopeful science-fiction novel about first contact and found family"
    )
    print(f"Query: {seed}\n")
    try:
        books = find_similar(seed)
    except FindSimilarError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if not books:
        print("No strong matches.")
    for book in books:
        print(f"{book.score:.3f}  {book.title or '?'}  [{book.isbn or 'no-isbn'}]")
        if book.snippet:
            print(f"        {book.snippet[:140]}")
