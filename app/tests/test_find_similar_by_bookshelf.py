"""Offline tests for find_similar_by_bookshelf — no Anthropic or AWS.

The LLM and find_similar are replaced with fakes, so these check the graph
wiring, the per-cluster query, slot allocation, dedup, exclusion and
degradation. Run from the app folder:

    python -m unittest tests.test_find_similar_by_bookshelf -v
"""
from __future__ import annotations

import json
import unittest

from models import Book, SimilarBook
from tools.find_similar import FindSimilarError
from tools.find_similar_by_bookshelf import (
    CLUSTER_SYSTEM,
    MAX_DESCRIPTION_CHARS,
    MAX_QUERY_CHARS,
    OVERFETCH,
    WHOLE_SHELF_LABEL,
    FindSimilarByBookshelfError,
    _allocate,
    _cluster_query,
    _parse_clusters,
    _parse_json,
    find_similar_by_bookshelf_detailed,
)

F1, F2, S1 = "9780000000011", "9780000000028", "9780000000035"
SHELF = [
    Book(isbn=F1, title="Fantasy One", description="dragons", genres=["Fantasy"]),
    Book(isbn=F2, title="Fantasy Two", description="wizards", genres=["Fantasy", "Adventure"]),
    Book(isbn=S1, title="SciFi One", description="starships", genres=["Science Fiction"]),
]
CLUSTERS_JSON = json.dumps(
    {"clusters": [
        {"label": "fantasy", "isbns": [F1, F2]},
        {"label": "science fiction", "isbns": [S1]},
    ]}
)


def hit(isbn: str | None, title: str, score: float) -> SimilarBook:
    return SimilarBook(isbn=isbn, title=title, score=score, snippet=f"{title} text", source=None)


class Fakes:
    """Records calls. `hits` maps a word that appears in a cluster's query
    (e.g. one of its books' descriptions) to what the KB returns for it."""

    def __init__(self, hits: dict[str, list[SimilarBook] | Exception], cluster_reply: str = CLUSTERS_JSON):
        self.hits = hits
        self.cluster_reply = cluster_reply
        self.llm_calls: list[str] = []
        self.find_calls: list[dict] = []

    def llm(self, system: str, user: str) -> str:
        assert system == CLUSTER_SYSTEM, "the only LLM call is clustering"
        self.llm_calls.append(user)
        return self.cluster_reply

    def find(self, query, *, exclude_isbns, flag_isbns=None, top_k, min_score):
        flag = set(flag_isbns or ())
        self.find_calls.append(
            {"query": query, "exclude": list(exclude_isbns), "flag": list(flag), "top_k": top_k}
        )
        for word, result in self.hits.items():
            if word in query:
                if isinstance(result, Exception):
                    raise result
                # Mimic find_similar: keep flagged hits, tag them excluded.
                import dataclasses
                return [dataclasses.replace(h, excluded=h.isbn in flag) for h in result]
        return []

    def run(self, books, **kw):
        return find_similar_by_bookshelf_detailed(books, llm=self.llm, find=self.find, **kw)


class AllocateTests(unittest.TestCase):
    def test_floor_plus_proportional(self):
        # sci-fi 1, fantasy 2, thriller 2, romance 1, nonfiction 1 -> 6 slots
        self.assertEqual(_allocate([1, 2, 2, 1, 1], 6), [1, 2, 1, 1, 1])

    def test_majority_dominates(self):
        self.assertEqual(_allocate([2, 1], 5), [3, 2])
        self.assertEqual(_allocate([3, 1], 6), [4, 2])

    def test_equal_remainders_go_to_the_larger_cluster(self):
        # Regression: float remainders (4*3/9 - 1 vs 1*3/9) differed in the last
        # bit and gave the 1-book cluster the 4-book cluster's slot.
        self.assertEqual(_allocate([4, 1, 2, 1, 1], 8), [3, 1, 2, 1, 1])

    def test_more_clusters_than_slots(self):
        self.assertEqual(_allocate([1, 3, 2], 2), [0, 1, 1])

    def test_sums_to_budget(self):
        for sizes in ([1], [5, 1, 1], [2, 2, 2], [7, 3, 1, 1]):
            for k in range(1, 12):
                self.assertEqual(sum(_allocate(sizes, k)), k, (sizes, k))


class ClusterQueryTests(unittest.TestCase):
    def test_genres_then_descriptions(self):
        self.assertEqual(
            _cluster_query("epic fantasy", SHELF[:2]),
            "Genres: Fantasy, Adventure. dragons wizards",
        )

    def test_genres_deduped_case_insensitively(self):
        books = [Book(isbn=F1, genres=["Fantasy"], description="a"),
                 Book(isbn=F2, genres=["fantasy", "Heist"], description="b")]
        self.assertTrue(_cluster_query("x", books).startswith("Genres: Fantasy, Heist."))

    def test_fallbacks_subjects_label_and_title(self):
        no_genres = Book(isbn=F1, title="Dune", authors=["Frank Herbert"], subjects=["Deserts", "Ecology"])
        self.assertEqual(_cluster_query("sf", [no_genres]), "Genres: Deserts, Ecology. Dune by Frank Herbert")
        bare = Book(isbn=F1, title="Dune")
        self.assertEqual(_cluster_query("science fiction", [bare]), "Genres: science fiction. Dune")

    def test_lengths_are_capped(self):
        long = Book(isbn=F1, genres=["Fantasy"], description="word " * 500)
        q = _cluster_query("x", [long])
        self.assertLessEqual(len(q) - len("Genres: Fantasy. "), MAX_DESCRIPTION_CHARS + 1)
        self.assertTrue(q.endswith("…"))
        many = [Book(isbn=F1, genres=["Fantasy"], description="word " * 100) for _ in range(40)]
        self.assertLessEqual(len(_cluster_query("x", many)), MAX_QUERY_CHARS)


class ParseTests(unittest.TestCase):
    def test_unparseable_falls_back_to_whole_shelf(self):
        clusters, notes = _parse_clusters("not json at all", SHELF)
        self.assertEqual([c.label for c in clusters], [WHOLE_SHELF_LABEL])
        self.assertEqual(sorted(clusters[0].isbns), sorted([F1, F2, S1]))
        self.assertTrue(notes)

    def test_prose_around_json(self):
        reply = f"Here are the clusters:\n{CLUSTERS_JSON}\nLet me know if you need more."
        self.assertEqual(len(_parse_json(reply)["clusters"]), 2)

    def test_missing_book_gets_own_cluster_and_duplicates_keep_first(self):
        reply = json.dumps({"clusters": [
            {"label": "fantasy", "isbns": [F1, F2]},
            {"label": "also fantasy", "isbns": [F1]},  # duplicate -> ignored
        ]})
        clusters, notes = _parse_clusters(reply, SHELF)
        self.assertEqual([c.isbns for c in clusters], [[F1, F2], [S1]])
        self.assertEqual(clusters[1].label, "like SciFi One")
        self.assertTrue(notes)

    def test_json_in_code_fence_and_hyphenated_isbns(self):
        reply = "```json\n" + json.dumps({"clusters": [
            {"label": "all", "isbns": ["978-0-00-000001-1", F2, S1, "9999999999999"]}
        ]}) + "\n```"
        clusters, notes = _parse_clusters(reply, SHELF)
        self.assertEqual(clusters[0].isbns, [F1, F2, S1])
        self.assertEqual(notes, [])


class GraphTests(unittest.TestCase):
    def test_end_to_end_slots_dedup_exploratory_exclusion(self):
        fakes = Fakes({
            "dragons": [
                hit("9781000000001", "A", 0.90),
                hit("9781000000002", "B", 0.80),
                hit("9781000000003", "C", 0.70),
                hit("0553283685", "Dup", 0.50),                       # ISBN-10 form
                hit("9781000000099", "Fantasy One (Book 1)", 0.95),   # other edition of a shelf book
                hit("9781000000098", "Already Read: A Novel", 0.93),  # other edition of an excluded book
            ],
            "starships": [
                hit("9781000000004", "D", 0.85),
                hit("9780553283686", "Dup", 0.60),  # same book, ISBN-13, better score
                hit("9781000000005", "E", 0.40),
            ],
        })
        excluded = [Book(isbn="9789999999999", title="Already Read")]
        result = fakes.run(SHELF, top_k=5, exclude=excluded)

        self.assertEqual([b.title for b in result.books], ["A", "B", "C", "D", "Dup"])
        self.assertEqual({b.title: b.cluster for b in result.books}["Dup"], "science fiction")
        self.assertEqual([b.exploratory for b in result.books], [False, False, False, True, True])
        self.assertEqual([c.exploratory for c in result.clusters], [False, True])
        self.assertEqual(
            [c.query for c in result.clusters],
            ["Genres: Fantasy, Adventure. dragons wizards", "Genres: Science Fiction. starships"],
        )
        self.assertTrue(any("Dropped 2" in n for n in result.notes), result.notes)

        # One LLM call (clustering) and one KB search per cluster, over-fetching.
        self.assertEqual(len(fakes.llm_calls), 1)
        self.assertEqual(len(fakes.find_calls), 2)
        for call in fakes.find_calls:
            self.assertEqual(call["top_k"], 5 * OVERFETCH)
            for isbn in (F1, F2, S1, "9789999999999"):
                self.assertIn(isbn, call["exclude"])

    def test_catalog_isbn10_and_genres_feed_clustering(self):
        shelf = [
            Book(isbn="0553283685", title="Hyperion", genres=["Science Fiction"]),
            Book(isbn="0-441-01359-7", title="Dune", genres=["Science Fiction"]),
        ]
        reply = json.dumps({"clusters": [{"label": "sf", "isbns": ["9780553283686", "9780441013593"]}]})
        fakes = Fakes({"Hyperion": [hit("9781000000004", "D", 0.8)]}, cluster_reply=reply)
        result = fakes.run(shelf)
        self.assertIn("genres: Science Fiction", fakes.llm_calls[0])
        self.assertEqual(result.clusters[0].isbns, ["9780553283686", "9780441013593"])
        self.assertEqual(result.clusters[0].query, "Genres: Science Fiction. Hyperion Dune")
        self.assertIn("9780553283686", fakes.find_calls[0]["exclude"])
        self.assertEqual([b.title for b in result.books], ["D"])

    def test_soft_exclude_is_kept_and_tagged(self):
        # A soft-excluded book that matches is returned with excluded=True, not
        # dropped, and its ISBN goes to find() as a flag, never as a hard exclude.
        XISBN = "9781000000050"
        fakes = Fakes({
            "dragons": [hit("9781000000001", "A", 0.90), hit(XISBN, "Disliked", 0.80)],
            "starships": [hit("9781000000004", "D", 0.85)],
        })
        result = fakes.run(SHELF, top_k=6, soft_exclude=[Book(isbn=XISBN, title="Disliked")])
        titles = [b.title for b in result.books]
        self.assertIn("Disliked", titles)
        self.assertEqual([b.title for b in result.books if b.excluded], ["Disliked"])
        for call in fakes.find_calls:
            self.assertIn(XISBN, call["flag"])
            self.assertNotIn(XISBN, call["exclude"])

    def test_backfill_when_a_cluster_runs_short(self):
        fakes = Fakes({
            "dragons": [hit(f"978100000000{i}", t, s) for i, (t, s) in
                        enumerate([("A", 0.9), ("B", 0.8), ("C", 0.7), ("G", 0.65)], 1)],
            "starships": [hit("9781000000009", "D", 0.85)],
        })
        result = fakes.run(SHELF, top_k=5)
        self.assertEqual([b.title for b in result.books], ["A", "B", "C", "G", "D"])

    def test_one_failed_search_degrades(self):
        fakes = Fakes({
            "dragons": [hit("9781000000001", "A", 0.9)],
            "starships": FindSimilarError("throttled"),
        })
        result = fakes.run(SHELF, top_k=3)
        self.assertEqual([b.title for b in result.books], ["A"])
        self.assertTrue(any("failed" in n for n in result.notes))

    def test_every_search_failed_raises(self):
        err = FindSimilarError("no credentials")
        with self.assertRaises(FindSimilarByBookshelfError):
            Fakes({"dragons": err, "starships": err}).run(SHELF)

    def test_bad_and_duplicate_books_are_skipped(self):
        reply = json.dumps({"clusters": [{"label": "fantasy", "isbns": [F1, F2]}]})
        fakes = Fakes({"dragons": [hit("9781000000001", "A", 0.9)]}, cluster_reply=reply)
        shelf = SHELF[:2] + [Book(isbn="not-an-isbn", title="Broken"), SHELF[0]]
        result = fakes.run(shelf)
        self.assertEqual(result.skipped, ["Broken: no valid ISBN"])
        self.assertEqual(result.clusters[0].isbns, [F1, F2])

    def test_single_book_skips_the_llm(self):
        fakes = Fakes({"starships": [hit("9781000000004", "D", 0.8)]})
        result = fakes.run([SHELF[2]])
        self.assertEqual(fakes.llm_calls, [])
        self.assertEqual([c.label for c in result.clusters], ["like SciFi One"])
        self.assertEqual(result.clusters[0].query, "Genres: Science Fiction. starships")
        self.assertFalse(result.books[0].exploratory)

    def test_nothing_usable(self):
        fakes = Fakes({})
        result = fakes.run([Book(isbn="nope", title="Broken")])
        self.assertEqual(result.books, [])
        self.assertEqual(fakes.find_calls, [])

    def test_bad_arguments(self):
        with self.assertRaises(FindSimilarByBookshelfError):
            Fakes({}).run(SHELF, top_k=0)
        self.assertEqual(Fakes({}).run([]).books, [])


if __name__ == "__main__":
    unittest.main()
