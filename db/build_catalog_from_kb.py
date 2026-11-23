#!/usr/bin/env python3
"""Build the catalog data file from the Bedrock Knowledge Base's source documents.

The KB is fed from S3 files `s3://<bucket>/book-data/<isbn10>.txt`, each with a
`.metadata.json` sidecar (isbn, isbn13, title, author, year, language, genres,
page_count, ...). This lists every ingested document, reads each sidecar for the
metadata and the `.txt` for a short description, and writes `db/kb_catalog.json`
— the committed source the seed/init use, so loading the catalog never needs AWS.

Re-run only when the KB changes. Needs AWS credentials (your CLI profile) and
BEDROCK_KB_ID; reads the app's `.env` for the KB id via the app's config.

Usage (from the repo's Book Recommender/ dir, with the app venv active):
    python db/build_catalog_from_kb.py
    python db/build_catalog_from_kb.py --limit 20    # a quick sample
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Reuse the app's KB config (model/region/KB id + .env loading).
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
from config import KBConfig  # noqa: E402

OUT = Path(__file__).resolve().parent / "kb_catalog.json"
MAX_DESCRIPTION_CHARS = 1200
WORKERS = 24

# Everything up to and including the "Genres: … ." header line; the blurb follows.
_HEADER = re.compile(r"^.*?Genres:[^.]*\.\s*", re.S)
# Labels the blurb often opens with; dropped for a cleaner description.
_LABEL = re.compile(r"^(From the Publisher|From Booklist|Review|Amazon\.com Review|"
                    r"From the Inside Flap|From the Back Cover|Product Description)\s*", re.I)


def _title_case_genres(genres) -> list[str]:
    out: list[str] = []
    for g in genres or []:
        label = " ".join(w.capitalize() for w in str(g).split())
        if label and label.lower() not in {x.lower() for x in out}:
            out.append(label)
    return out


def _description(body: str) -> str | None:
    """The publisher/review blurb: text after the Genres header, before the bio."""
    text = body.split("About the Author")[0]
    text = _HEADER.sub("", text, count=1)
    text = _LABEL.sub("", text.strip(), count=1)
    text = " ".join(text.split())
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"
    return text or None


def _language(code: str | None) -> str | None:
    if not code:
        return None
    return {"eng": "en", "spa": "es", "fre": "fr", "ger": "de"}.get(code.lower(), code.lower()[:2])


def _book_from(meta: dict, body: str) -> dict | None:
    isbn = str(meta.get("isbn") or "").strip()
    title = (meta.get("title") or "").strip()
    if not isbn or not title:
        return None
    author = (meta.get("author") or "").strip()
    year = meta.get("year")
    pages = meta.get("page_count")
    return {
        "isbn": isbn,
        "title": title,
        "authors": [author] if author else [],
        "description": _description(body),
        "pages": int(pages) if isinstance(pages, (int, float)) or (isinstance(pages, str) and pages.isdigit()) else None,
        "published_year": int(year) if isinstance(year, (int, float)) or (isinstance(year, str) and year.isdigit()) else None,
        "language": _language(meta.get("language")),
        "genres": _title_case_genres(meta.get("genres")),
        "cover_url": f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg",
        "source": "bedrock-kb",
    }


def _list_documents(ba, kb_id: str, ds_id: str) -> list[dict]:
    """Every ingested document's S3 identifier (bucket + key)."""
    docs: list[dict] = []
    for page in ba.get_paginator("list_knowledge_base_documents").paginate(
        knowledgeBaseId=kb_id, dataSourceId=ds_id
    ):
        for d in page.get("documentDetails", []):
            uri = (((d.get("identifier") or {}).get("s3") or {}).get("uri") or "")
            if uri.startswith("s3://") and uri.endswith(".txt"):
                bucket, _, key = uri[5:].partition("/")
                docs.append({"bucket": bucket, "key": key})
    return docs


def _data_source_id(ba, kb_id: str) -> str:
    sources = ba.list_data_sources(knowledgeBaseId=kb_id)["dataSourceSummaries"]
    if not sources:
        raise SystemExit("The knowledge base has no data source.")
    return sources[0]["dataSourceId"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=0, help="only the first N documents (sampling)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    kb = KBConfig.from_env()
    if not kb.knowledge_base_id:
        raise SystemExit("BEDROCK_KB_ID is not set (app/.env).")
    ba = boto3.client("bedrock-agent", region_name=kb.region)
    s3 = boto3.client("s3", region_name=kb.region)

    ds_id = _data_source_id(ba, kb.knowledge_base_id)
    docs = _list_documents(ba, kb.knowledge_base_id, ds_id)
    if args.limit:
        docs = docs[: args.limit]
    print(f"Knowledge base documents: {len(docs)}")

    def fetch(doc: dict):
        b, k = doc["bucket"], doc["key"]
        try:
            meta_raw = s3.get_object(Bucket=b, Key=k + ".metadata.json")["Body"].read()
            meta = json.loads(meta_raw).get("metadataAttributes", {})
            body = s3.get_object(Bucket=b, Key=k)["Body"].read().decode("utf-8", "replace")
        except (ClientError, BotoCoreError, ValueError) as e:
            return k, None, str(e)
        return k, _book_from(meta, body), None

    books: list[dict] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, (key, book, err) in enumerate(pool.map(fetch, docs), 1):
            if err or book is None:
                failures.append(f"{key}: {err or 'missing isbn/title'}")
            else:
                books.append(book)
            if i % 100 == 0:
                print(f"  {i}/{len(docs)} …")

    books.sort(key=lambda b: b["isbn"])
    OUT.write_text(
        json.dumps({"source": "bedrock-kb", "count": len(books), "books": books},
                   indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {len(books)} books to {OUT.name}")
    if failures:
        print(f"Skipped {len(failures)}:")
        for f in failures[:10]:
            print(f"   {f}")
    with_desc = sum(1 for b in books if b["description"])
    print(f"  with description: {with_desc}/{len(books)} | with pages: {sum(1 for b in books if b['pages'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
