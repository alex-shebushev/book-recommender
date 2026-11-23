"""Offline tests for parse_bookshelf — no Anthropic/AWS/DB.

The vision call and the catalog loader are injected, so these check the matcher
(language cascade, fuzzy title, author boost, threshold) and the JSON parsing.
Run from the app folder:

    python -m unittest tests.test_parse_bookshelf -v
"""
from __future__ import annotations

import json
import unittest

from tools.parse_bookshelf import (
    ExtractedBook,
    _language_tiers,
    _match_one,
    _parse_extracted,
    parse_bookshelf,
)

CATALOG = [
    {"book_id": 1, "isbn": "111", "title": "Dune", "authors": ["Frank Herbert"], "language": "en", "cover_url": None},
    {"book_id": 2, "isbn": "222", "title": "Neuromancer", "authors": ["William Gibson"], "language": "en", "cover_url": None},
    {"book_id": 3, "isbn": "333", "title": "Dune", "authors": ["Frank Herbert"], "language": "de", "cover_url": None},
    {"book_id": 4, "isbn": "444", "title": "Hyperion", "authors": ["Dan Simmons"], "language": "en", "cover_url": None},
]


def eb(title, title_en=None, authors=(), language=None):
    return ExtractedBook(title=title, title_en=title_en, authors=list(authors), language=language)


class LanguageTiersTest(unittest.TestCase):
    def test_order(self):
        self.assertEqual(_language_tiers("de"), ["de", "en", None])
        self.assertEqual(_language_tiers("en"), ["en", None])
        self.assertEqual(_language_tiers(None), ["en", None])


class MatchTest(unittest.TestCase):
    def test_exact_prefers_english_edition(self):
        row = _match_one(eb("Dune", "Dune", ["Frank Herbert"], "en"), CATALOG)
        self.assertEqual(row["book_id"], 1)

    def test_language_cascade_prefers_detected_language(self):
        # A German spine (canonical title Dune) resolves to the German edition.
        row = _match_one(eb("Der Wüstenplanet", "Dune", ["Frank Herbert"], "de"), CATALOG)
        self.assertEqual(row["book_id"], 3)

    def test_author_boost_accepts_loose_title(self):
        # "Hyperion Cantos" vs "Hyperion" is ~0.70 — below the title-only bar, but
        # the matching author pulls it over.
        row = _match_one(eb("Hyperion Cantos", "Hyperion Cantos", ["Dan Simmons"], "en"), CATALOG)
        self.assertIsNotNone(row)
        self.assertEqual(row["book_id"], 4)

    def test_loose_title_without_author_is_rejected(self):
        row = _match_one(eb("Hyperion Cantos", "Hyperion Cantos", ["Someone Else"], "en"), CATALOG)
        self.assertIsNone(row)

    def test_unrelated_title_is_rejected(self):
        self.assertIsNone(_match_one(eb("A Totally Different Book", authors=["Nobody"]), CATALOG))

    def test_matches_on_canonical_title_not_spine(self):
        # The spine title is foreign; matching must use title_en.
        row = _match_one(eb("Neuromante", "Neuromancer", ["William Gibson"], "es"), CATALOG)
        self.assertEqual(row["book_id"], 2)


class ParseExtractedTest(unittest.TestCase):
    def test_books_object(self):
        reply = json.dumps({"books": [{"title": "Dune", "title_en": "Dune", "authors": ["Frank Herbert"], "language": "EN"}]})
        out = _parse_extracted(reply)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].language, "en")  # lowercased

    def test_bare_array_and_fence(self):
        reply = "```json\n[{\"title\": \"Neuromancer\"}]\n```"
        out = _parse_extracted(reply)
        self.assertEqual(out[0].title, "Neuromancer")
        self.assertEqual(out[0].title_en, "Neuromancer")  # falls back to title

    def test_skips_items_without_title(self):
        out = _parse_extracted(json.dumps({"books": [{"authors": ["x"]}, "nope", {"title": "Dune"}]}))
        self.assertEqual([b.title for b in out], ["Dune"])


class ParseBookshelfTest(unittest.TestCase):
    def test_end_to_end_with_fakes(self):
        reply = json.dumps({"books": [
            {"title": "Dune", "title_en": "Dune", "authors": ["Frank Herbert"], "language": "en"},
            {"title": "Neuromancer", "title_en": "Neuromancer", "authors": ["William Gibson"], "language": "en"},
            {"title": "Unknown Spine", "title_en": "Unknown Spine", "authors": [], "language": "en"},
        ]})
        result = parse_bookshelf(
            b"fake-bytes", "image/jpeg",
            shelf_book_ids={2},
            vision=lambda b64, mt: reply,
            load_catalog=lambda: CATALOG,
        )
        ids = {m.book_id for m in result.matches}
        self.assertEqual(ids, {1, 2})  # the unknown spine is omitted
        by_id = {m.book_id: m for m in result.matches}
        self.assertTrue(by_id[2].already_on_shelf)
        self.assertFalse(by_id[1].already_on_shelf)
        self.assertEqual(by_id[1].spine_title, "Dune")
        self.assertTrue(any("matched 2" in n for n in result.notes))


if __name__ == "__main__":
    unittest.main()
