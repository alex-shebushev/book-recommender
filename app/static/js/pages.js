// The views: Catalog (list), BookDetail, Shelf, Login, Signup.

import React from "react";
import { html, BookCard, Cover, Stars, Loading, ErrorState } from "./ui.js";
import { Link, navigate, useSearch } from "./router.js";
import {
  getBooks, getBook, getSimilar, getGenres, getShelf, putRating,
  getShelves, getShelfById, createShelf, renameShelf, deleteShelf,
  addBookToShelf, removeBookFromShelf, getLibraryRecommendations, getShelfRecommendations,
  sendAssistant, parseBookshelfPhoto, bulkAddToShelf,
} from "./api.js";
import { useAuth } from "./auth.js";

const PAGE_SIZE = 24;

const SHELVES_PER_PAGE = 5;

// Books shown in a shelf preview: 3 (narrow) / 5 (medium) / 10 (wide) by width.
function usePreviewCount() {
  const calc = () => (window.innerWidth < 768 ? 3 : window.innerWidth < 1200 ? 5 : 10);
  const [n, setN] = React.useState(calc);
  React.useEffect(() => {
    const onResize = () => setN(calc());
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return n;
}

// A compact fixed-height cover + title linking to the book (for preview strips).
function CoverThumb({ book }) {
  return html`
    <${Link} to=${`/books/${book.book_id}`} className="cover-thumb">
      <${Cover} book=${book} />
      <div className="book-title">${book.title}</div>
    <//>`;
}

// ---- Catalog ----------------------------------------------------------------

export function Catalog() {
  const search = useSearch();
  const q = new URLSearchParams(search).get("q") || "";

  const [genre, setGenre] = React.useState(null);
  const [genres, setGenres] = React.useState([]);
  const [books, setBooks] = React.useState([]);
  const [total, setTotal] = React.useState(0);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    getGenres().then(setGenres).catch(() => setGenres([]));
  }, []);

  React.useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    getBooks({ q, genre, limit: PAGE_SIZE, offset: 0 })
      .then((data) => { if (active) { setBooks(data.items); setTotal(data.total); } })
      .catch((e) => active && setError(e.message))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [q, genre]);

  const loadMore = () => {
    getBooks({ q, genre, limit: PAGE_SIZE, offset: books.length })
      .then((data) => setBooks((prev) => [...prev, ...data.items]))
      .catch((e) => setError(e.message));
  };

  return html`
    <div className="page">
      <header className="hero">
        <h1>Find your next book</h1>
        <p className="muted">
          Browse the catalog, open a book for the full story, and keep a shelf of your own.
        </p>
      </header>

      <div className="filters">
        <button className=${`chip ${!genre ? "active" : ""}`} onClick=${() => setGenre(null)}>All</button>
        ${genres.map(
          (g) => html`<button key=${g} className=${`chip ${genre === g ? "active" : ""}`}
                               onClick=${() => setGenre((cur) => (cur === g ? null : g))}>${g}</button>`
        )}
      </div>

      ${q && html`<p className="center muted" style=${{ marginBottom: "20px" }}>
        ${total} result${total === 1 ? "" : "s"} for “${q}”
      </p>`}

      ${error
        ? html`<${ErrorState} message=${error} />`
        : loading
        ? html`<${Loading} />`
        : books.length === 0
        ? html`<div className="state">No books match. Try another search or genre.</div>`
        : html`
            <div className="grid">
              ${books.map((b) => html`<${BookCard} key=${b.book_id} book=${b} />`)}
            </div>
            ${books.length < total &&
            html`<div className="load-more">
              <button className="btn btn-ghost" onClick=${loadMore}>
                Load more (${books.length} of ${total})
              </button>
            </div>`}
          `}
    </div>`;
}

// ---- Shelves + rating on a book detail --------------------------------------

function StarPicker({ value, onChange }) {
  return html`<div className="star-picker">
    ${[1, 2, 3, 4, 5].map(
      (n) => html`<span key=${n} className=${n <= value ? "on" : ""}
                        onClick=${() => onChange(n === value ? 0 : n)}>★</span>`
    )}
  </div>`;
}

// The signed-in user's own rating of this book — independent of shelves.
// Clicking a star saves at once; clicking the current star clears it.
function RatingControl({ book, onChange }) {
  const { user } = useAuth();
  const [rating, setRating] = React.useState(book.my_rating || 0);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState("");
  if (!user) return null;

  const save = async (next) => {
    if (busy) return;
    setRating(next); setBusy(true); setMsg("");
    try {
      const r = await putRating(book.book_id, { rating: next || null });
      setMsg(next ? "Rating saved." : "Rating cleared.");
      onChange && onChange({ my_rating: r.rating });
    } catch (e) { setMsg(e.message); setRating(book.my_rating || 0); } finally { setBusy(false); }
  };

  return html`
    <div className="shelf-control">
      <h3>Your rating</h3>
      <div className="row"><${StarPicker} value=${rating} onChange=${save} /></div>
      ${msg && html`<div className="save-msg">${msg}</div>`}
    </div>`;
}

// The book's shelves: a checklist of the user's shelves, the built-in Exclusion
// shelf first. A book is on Exclusion or on normal shelves, never both — the
// backend enforces that, and the checklist mirrors it.
function ShelfPicker({ book, onChange }) {
  const { user } = useAuth();
  const [shelves, setShelves] = React.useState(null);
  const [selected, setSelected] = React.useState(new Set(book.shelf_ids || []));
  const [newName, setNewName] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState("");

  React.useEffect(() => {
    if (!user) return;
    getShelves().then(setShelves).catch(() => setShelves([]));
  }, [user]);

  if (!user) {
    return html`<div className="shelf-badge">
      <${Link} to="/login" className="nav-link">Sign in<//>&nbsp;to put this book on your shelves.
    </div>`;
  }
  if (shelves === null) return null;

  const exclusion = shelves.find((s) => s.is_exclusion);
  const normal = shelves.filter((s) => !s.is_exclusion);
  const apply = (next) => { setSelected(next); onChange && onChange({ shelf_ids: [...next] }); };

  const toggle = async (shelf) => {
    setBusy(true); setErr("");
    try {
      const next = new Set(selected);
      if (next.has(shelf.shelf_id)) {
        await removeBookFromShelf(shelf.shelf_id, book.book_id);
        next.delete(shelf.shelf_id);
      } else {
        await addBookToShelf(shelf.shelf_id, book.book_id);
        for (const s of shelves) if (s.is_exclusion !== shelf.is_exclusion) next.delete(s.shelf_id);
        next.add(shelf.shelf_id);
      }
      apply(next);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const createAndAdd = async (e) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    setBusy(true); setErr("");
    try {
      const shelf = await createShelf(name);
      await addBookToShelf(shelf.shelf_id, book.book_id);
      setShelves((list) => [...list, { ...shelf, book_count: 1 }]);
      const next = new Set(selected);
      if (exclusion) next.delete(exclusion.shelf_id);
      next.add(shelf.shelf_id);
      apply(next);
      setNewName("");
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  return html`
    <div className="shelf-control">
      <h3>Shelves</h3>
      ${exclusion && html`
        <label className="shelf-check shelf-check-exclusion">
          <input type="checkbox" checked=${selected.has(exclusion.shelf_id)} disabled=${busy}
                 onChange=${() => toggle(exclusion)} />
          <span>
            ${exclusion.name}
            <span className="shelf-check-hint">
              Never recommended and never used as your taste. Takes the book off your other shelves.
            </span>
          </span>
        </label>`}
      <div className="shelf-checks">
        ${normal.map((s) => html`
          <label key=${s.shelf_id} className="shelf-check">
            <input type="checkbox" checked=${selected.has(s.shelf_id)} disabled=${busy}
                   onChange=${() => toggle(s)} />
            ${s.name}
          </label>`)}
      </div>
      ${normal.length === 0 &&
        html`<p className="muted" style=${{ fontSize: "14px", marginBottom: "12px" }}>
          No shelves yet — create one below.
        </p>`}
      <form className="new-shelf-form" onSubmit=${createAndAdd}>
        <input type="text" placeholder="New shelf name" value=${newName}
               onChange=${(e) => setNewName(e.target.value)} />
        <button className="btn btn-ghost btn-sm" type="submit" disabled=${busy || !newName.trim()}>
          Add shelf
        </button>
      </form>
      ${err && html`<div className="save-msg">${err}</div>`}
    </div>`;
}

// "Find similar" — calls the Bedrock KB find_similar tool for this book.
function SimilarRow({ r }) {
  const pct = Math.round((r.score || 0) * 100);
  const title = r.title || "Untitled";
  return html`
    <div className=${`similar-row ${r.excluded ? "similar-row-excluded" : ""}`}>
      <div className="similar-title">
        ${r.book_id
          ? html`<${Link} to=${`/books/${r.book_id}`}>${title}<//>`
          : html`<span>${title}</span>`}
        <span className="similar-score">${pct}% match</span>
        ${r.cluster && html`<span className="tag">${r.cluster}</span>`}
        ${r.excluded && html`<span className="tag tag-excluded">On your Exclusion shelf</span>`}
      </div>
      ${r.author && html`<div className="book-author">${r.author}</div>`}
      ${r.snippet && html`<p className="similar-snippet">${r.snippet}</p>`}
      ${r.book_id
        ? html`<${Link} to=${`/books/${r.book_id}`} className="see-all">View in catalog →<//>`
        : html`<span className="muted" style=${{ fontSize: "12px" }}>Not in this catalog</span>`}
    </div>`;
}

function SimilarBooks({ book }) {
  const [status, setStatus] = React.useState("idle"); // idle | loading | done | error
  const [results, setResults] = React.useState([]);
  const [err, setErr] = React.useState("");
  const [hide] = useHideExcluded();

  const run = async () => {
    setStatus("loading"); setErr("");
    try {
      setResults(await getSimilar(book.book_id));
      setStatus("done");
    } catch (e) { setErr(e.message); setStatus("error"); }
  };

  const { visible, anyExcluded, allExcluded } = splitExcluded(results, hide);

  return html`
    <div className="similar">
      <div className="rec-controls">
        <button className="btn btn-ghost" onClick=${run} disabled=${status === "loading"}>
          ${status === "loading" ? "Finding similar…" : "Find similar"}
        </button>
      </div>
      ${status === "error" && html`<p className="save-msg" style=${{ marginTop: "12px" }}>${err}</p>`}
      ${status === "done" && results.length === 0 &&
        html`<p className="muted" style=${{ marginTop: "16px" }}>No strong matches found.</p>`}
      ${status === "done" && results.length > 0 && html`
        <div className="similar-list">
          <p className="muted" style=${{ fontSize: "13px" }}>From the knowledge base — snippets are the grounding each match is based on.</p>
          ${allExcluded
            ? html`<p className="muted">All results are in your exclusions.</p>`
            : visible.map((r, i) => html`<${SimilarRow} key=${r.isbn || i} r=${r} />`)}
        </div>`}
    </div>`;
}

// ---- Exclusion-shelf display toggle (shared) --------------------------------

// Remembered in the browser so it behaves like a bookshelf setting, and shared
// across every result view: the toggle lives only on the bookshelf / Exclusion
// screens, so changing it there must update any open list (including the always-
// mounted assistant) live. Checked = hide Exclusion-shelf books; unchecked =
// show them grayed.
const _hideExcludedListeners = new Set();
function _readHideExcluded() {
  try { return localStorage.getItem("hideExcluded") !== "false"; } catch { return true; }
}
function useHideExcluded() {
  const [hide, setHide] = React.useState(_readHideExcluded);
  React.useEffect(() => {
    const onChange = () => setHide(_readHideExcluded());
    _hideExcludedListeners.add(onChange);
    return () => _hideExcludedListeners.delete(onChange);
  }, []);
  const update = (next) => {
    try { localStorage.setItem("hideExcluded", next ? "true" : "false"); } catch { /* ignore */ }
    _hideExcludedListeners.forEach((fn) => fn());
  };
  return [hide, update];
}

function ExclusionToggle({ hide, onChange }) {
  return html`<label className="exclusion-toggle">
    <input type="checkbox" checked=${hide} onChange=${(e) => onChange(e.target.checked)} />
    Hide books on your Exclusion shelf
  </label>`;
}

// Split results for display: with hide on, drop excluded; else keep them (grayed
// by SimilarRow). `allExcluded` is true when there were results but hiding left none.
function splitExcluded(items, hide) {
  const anyExcluded = items.some((r) => r.excluded);
  const visible = hide ? items.filter((r) => !r.excluded) : items;
  return { visible, anyExcluded, allExcluded: items.length > 0 && visible.length === 0 };
}

// ---- Bookshelf recommendations (find_similar_by_bookshelf) -------------------

// State for one "Get recommendations" button: idle | loading | done | error.
function useRecommendations(load) {
  const [state, setState] = React.useState({ status: "idle", data: null, err: "" });
  const run = async () => {
    setState({ status: "loading", data: null, err: "" });
    try { setState({ status: "done", data: await load(), err: "" }); }
    catch (e) { setState({ status: "error", data: null, err: e.message }); }
  };
  return [state, run];
}

function RecButton({ state, run, label }) {
  const busy = state.status === "loading";
  return html`<div className="rec-controls">
    <button className="btn btn-primary" onClick=${run} disabled=${busy}>
      ${busy ? "Finding recommendations…" : state.status === "done" ? "Refresh recommendations ↻" : label}
    </button>
  </div>`;
}

function RecommendationsPanel({ state, hide }) {
  if (state.status === "idle") return null;
  if (state.status === "loading") {
    return html`<section className="recs"><p className="muted">
      Grouping your books by genre and searching the knowledge base — this takes about half a minute.
    </p></section>`;
  }
  if (state.status === "error") {
    return html`<section className="recs"><p className="save-msg">${state.err}</p></section>`;
  }

  const { items, clusters, skipped, notes } = state.data;
  const remarks = [...skipped.map((s) => `Skipped ${s}`), ...notes];
  const { visible, allExcluded } = splitExcluded(items, hide);
  const core = visible.filter((r) => !r.exploratory);
  const extra = visible.filter((r) => r.exploratory);

  return html`
    <section className="recs">
      <h2>Recommendations</h2>
      ${clusters.length > 0 && html`
        <div className="recs-clusters">
          <span className="muted">Your shelf by genre:</span>
          ${clusters.map((c, i) => html`
            <span key=${i} className=${`tag ${c.exploratory ? "tag-exploratory" : ""}`} title=${c.query ? `Searched with: ${c.query}` : ""}>
              ${c.label} · ${c.size}
            </span>`)}
        </div>`}
      ${items.length === 0 &&
        html`<p className="muted" style=${{ marginTop: "16px" }}>No new books found for this shelf in the knowledge base.</p>`}
      ${allExcluded &&
        html`<p className="muted" style=${{ marginTop: "16px" }}>All results are in your exclusions.</p>`}
      ${core.length > 0 && html`
        <div className="similar-list">
          ${core.map((r, i) => html`<${SimilarRow} key=${r.isbn || i} r=${r} />`)}
        </div>`}
      ${extra.length > 0 && html`
        <div className="recs-group">
          <h3>You might also enjoy</h3>
          <p className="muted recs-note">Matches for the smaller parts of your shelf.</p>
          <div className="similar-list">
            ${extra.map((r, i) => html`<${SimilarRow} key=${r.isbn || i} r=${r} />`)}
          </div>
        </div>`}
      ${remarks.length > 0 && html`
        <ul className="recs-notes">${remarks.map((n, i) => html`<li key=${i}>${n}</li>`)}</ul>`}
    </section>`;
}

// ---- Book detail ------------------------------------------------------------

export function BookDetail({ id }) {
  const [book, setBook] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    getBook(id)
      .then((b) => active && setBook(b))
      .catch((e) => active && setError(e.message))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [id]);

  if (loading) return html`<div className="page"><${Loading} /></div>`;
  if (error) return html`<div className="page"><${ErrorState} message=${error} /></div>`;

  const authors = (book.authors || []).join(", ");
  const meta = [
    book.published_year,
    book.pages && `${book.pages} pages`,
    book.language && book.language.toUpperCase(),
  ].filter(Boolean);

  return html`
    <div className="page section">
      <${Link} to="/" className="back-link">← Back to catalog<//>
      <div className="detail">
        <${Cover} book=${book} />
        <div>
          <h1>${book.title}</h1>
          ${authors && html`<div className="authors">${authors}</div>`}
          <div className="meta-row">
            ${meta.map((m) => html`<span key=${m}>${m}</span>`)}
          </div>
          <div className="book-tags">
            ${(book.genres || []).map((g) => html`<span className="tag" key=${g}>${g}</span>`)}
          </div>
          ${book.description && html`<p className="description">${book.description}</p>`}
          <${ShelfPicker} book=${book} onChange=${(patch) => setBook((b) => ({ ...b, ...patch }))} />
          ${/* Rating input hidden for now — ratings aren't surfaced in the UI yet. */ ""}
          <${SimilarBooks} book=${book} />
          ${book.source &&
          html`<div style=${{ marginTop: "20px" }}>
            <a className="nav-link" href=${book.source} target="_blank" rel="noreferrer">
              View on Open Library ↗
            </a>
          </div>`}
        </div>
      </div>
    </div>`;
}

// ---- My bookshelf -------------------------------------------------------------

const EXCLUSION_NOTE = "Books here are never recommended and never used as your taste.";

function NewShelfForm({ onCreated }) {
  const [name, setName] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState("");
  const submit = async (e) => {
    e.preventDefault();
    const n = name.trim();
    if (!n) return;
    setBusy(true); setErr("");
    try { await createShelf(n); setName(""); onCreated && await onCreated(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  return html`
    <form className="new-shelf-form" onSubmit=${submit}>
      <input type="text" placeholder="New shelf name" value=${name}
             onChange=${(e) => setName(e.target.value)} />
      <button className="btn btn-primary btn-sm" type="submit" disabled=${busy || !name.trim()}>+ New shelf</button>
      ${err && html`<span className="save-msg">${err}</span>`}
    </form>`;
}

// A shelf preview on My bookshelf: header (name + count + Rename/Delete + See
// all) and a strip of the first few covers. The Exclusion shelf has no
// Rename/Delete — it is built in.
function ShelfPreview({ shelf, previewCount, onRename, onDelete }) {
  const [editing, setEditing] = React.useState(false);
  const [name, setName] = React.useState(shelf.name);
  const [confirmDel, setConfirmDel] = React.useState(false);
  const [err, setErr] = React.useState("");

  const saveName = async () => {
    const n = name.trim();
    if (!n || n === shelf.name) { setEditing(false); setName(shelf.name); return; }
    try { await onRename(shelf.shelf_id, n); setEditing(false); }
    catch (e) { setErr(e.message); }
  };

  const preview = shelf.books.slice(0, previewCount);
  const more = shelf.books.length - preview.length;

  return html`
    <div className=${`shelf-preview ${shelf.is_exclusion ? "shelf-preview-exclusion" : ""}`}>
      <div className="shelf-preview-header">
        ${editing
          ? html`<span className="inline-edit">
              <input type="text" value=${name} onChange=${(e) => setName(e.target.value)} />
              <button className="btn btn-ghost btn-sm" onClick=${saveName}>Save</button>
              <button className="card-remove" onClick=${() => { setEditing(false); setName(shelf.name); }}>Cancel</button>
            </span>`
          : html`
              <${Link} to=${`/shelves/${shelf.shelf_id}`} className="shelf-row-name">${shelf.name}<//>
              <span className="shelf-count">${shelf.books.length}</span>
              <span className="shelf-actions">
                ${!shelf.is_exclusion && html`
                  <button className="card-remove" onClick=${() => setEditing(true)}>Rename</button>
                  ${confirmDel
                    ? html`<span>Delete?${" "}
                        <button className="card-remove" onClick=${() => onDelete(shelf.shelf_id)}>Yes</button>${" "}
                        <button className="card-remove" onClick=${() => setConfirmDel(false)}>No</button></span>`
                    : html`<button className="card-remove" onClick=${() => setConfirmDel(true)}>Delete</button>`}`}
                <${Link} to=${`/shelves/${shelf.shelf_id}`} className="see-all">See all →<//>
              </span>`}
      </div>
      ${shelf.is_exclusion && html`<p className="exclusion-note">${EXCLUSION_NOTE}</p>`}
      ${err && html`<div className="save-msg">${err}</div>`}
      ${preview.length === 0
        ? html`<p className="preview-empty">No books on this shelf yet.</p>`
        : html`<div className="cover-strip">
            ${preview.map((b) => html`<${CoverThumb} key=${b.book_id} book=${b} />`)}
            ${more > 0 &&
              html`<${Link} to=${`/shelves/${shelf.shelf_id}`} className="see-all"
                     style=${{ alignSelf: "center" }}>+${more} more<//>`}
          </div>`}
    </div>`;
}

export function Shelf() {
  const [library, setLibrary] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [shelfPage, setShelfPage] = React.useState(0);
  const previewCount = usePreviewCount();
  const [recs, runRecs] = useRecommendations(getLibraryRecommendations);
  const [hideRecs, setHideRecs] = useHideExcluded();

  const reload = React.useCallback(() => {
    return getShelf().then(setLibrary).catch((e) => setError(e.message));
  }, []);

  React.useEffect(() => {
    let active = true;
    getShelf()
      .then((l) => active && setLibrary(l))
      .catch((e) => active && setError(e.message))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, []);

  const onRename = async (shelfId, name) => { await renameShelf(shelfId, name); await reload(); };
  const onDelete = async (shelfId) => { try { await deleteShelf(shelfId); await reload(); } catch (e) { setError(e.message); } };

  if (loading) return html`<div className="page"><${Loading} /></div>`;
  if (error) return html`<div className="page"><${ErrorState} message=${error} /></div>`;

  const exclusion = library.shelves.find((s) => s.is_exclusion);
  const shelves = library.shelves.filter((s) => !s.is_exclusion);
  const pageCount = Math.max(1, Math.ceil(shelves.length / SHELVES_PER_PAGE));
  const page = Math.min(shelfPage, pageCount - 1);
  const visibleShelves = shelves.slice(page * SHELVES_PER_PAGE, page * SHELVES_PER_PAGE + SHELVES_PER_PAGE);
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  return html`
    <div className="page section">
      <div className="shelf-head">
        <div>
          <h1>Your bookshelf</h1>
          <p className="muted">
            ${plural(library.total, "book", "books")} on ${plural(shelves.length, "shelf", "shelves")}${library.excluded ? ` · ${library.excluded} excluded` : ""}.
          </p>
        </div>
        <${RecButton} state=${recs} run=${runRecs} label="Get recommendations →" />
      </div>
      <${RecommendationsPanel} state=${recs} hide=${hideRecs} />

      ${exclusion && html`
        <section className="shelf-section">
          <${ShelfPreview} shelf=${exclusion} previewCount=${previewCount} />
          <div className="exclusion-setting">
            <${ExclusionToggle} hide=${hideRecs} onChange=${setHideRecs} />
            <span className="muted exclusion-setting-hint">When off, excluded books show grayed in recommendations instead of being hidden.</span>
          </div>
        </section>`}

      <section className="shelf-section">
        <h2>Shelves<span className="shelf-count">${shelves.length}</span></h2>
        <${NewShelfForm} onCreated=${reload} />
        ${visibleShelves.map((s) => html`
          <${ShelfPreview} key=${s.shelf_id} shelf=${s} previewCount=${previewCount}
            onRename=${onRename} onDelete=${onDelete} />`)}
        ${pageCount > 1 && html`
          <div className="pager">
            <button className="btn btn-ghost btn-sm" disabled=${page === 0}
                    onClick=${() => setShelfPage(page - 1)}>← Prev</button>
            <span className="page-info">Shelves ${page + 1} / ${pageCount}</span>
            <button className="btn btn-ghost btn-sm" disabled=${page >= pageCount - 1}
                    onClick=${() => setShelfPage(page + 1)}>Next →</button>
          </div>`}
        ${shelves.length === 0 &&
          html`<p className="empty-note">No shelves yet — create one, or add a book to a new shelf from its page.</p>`}
      </section>
    </div>`;
}

// ---- Single shelf screen ----------------------------------------------------

// Upload a bookshelf photo, review the matched books, and add the chosen ones to
// this shelf. The image is processed in memory server-side and never stored;
// unmatched spines are omitted. Books already on the shelf start unchecked.
function PhotoImport({ shelfId, onAdded }) {
  const fileRef = React.useRef(null);
  const [status, setStatus] = React.useState("idle"); // idle | loading | review | error
  const [candidates, setCandidates] = React.useState([]);
  const [selected, setSelected] = React.useState(new Set());
  const [notes, setNotes] = React.useState([]);
  const [err, setErr] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const reset = () => {
    setStatus("idle"); setCandidates([]); setSelected(new Set()); setNotes([]); setErr("");
  };

  const onPick = async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = ""; // allow re-picking the same file
    if (!file) return;
    setStatus("loading"); setErr("");
    try {
      const res = await parseBookshelfPhoto(shelfId, file);
      setCandidates(res.candidates);
      setNotes(res.notes || []);
      setSelected(new Set(res.candidates.filter((c) => !c.already_on_shelf).map((c) => c.book_id)));
      setStatus("review");
    } catch (e) { setErr(e.message); setStatus("error"); }
  };

  const toggle = (bid) =>
    setSelected((s) => { const n = new Set(s); n.has(bid) ? n.delete(bid) : n.add(bid); return n; });
  const remove = (bid) => {
    setCandidates((c) => c.filter((x) => x.book_id !== bid));
    setSelected((s) => { const n = new Set(s); n.delete(bid); return n; });
  };
  const confirm = async () => {
    const ids = candidates.filter((c) => selected.has(c.book_id)).map((c) => c.book_id);
    if (ids.length === 0) { reset(); return; }
    setBusy(true); setErr("");
    try { await bulkAddToShelf(shelfId, ids); reset(); await onAdded(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  return html`
    <div className="photo-import">
      <input type="file" accept="image/*" ref=${fileRef} style=${{ display: "none" }} onChange=${onPick} />
      ${status === "idle" &&
        html`<button className="btn btn-ghost btn-sm"
                     onClick=${() => fileRef.current && fileRef.current.click()}>📷 Add from photo</button>`}
      ${status === "loading" &&
        html`<p className="muted">Reading the shelf photo… this can take a few seconds.</p>`}
      ${status === "error" &&
        html`<p className="save-msg">${err}${" "}<button className="card-remove" onClick=${reset}>Try again</button></p>`}
      ${status === "review" && html`
        <div className="photo-review">
          ${notes.map((n, i) => html`<p key=${i} className="muted" style=${{ fontSize: "13px" }}>${n}</p>`)}
          ${candidates.length === 0
            ? html`<p className="muted">No books from the photo matched our catalog.</p>`
            : html`<div className="photo-candidates">
                ${candidates.map((c) => html`
                  <label key=${c.book_id} className=${`photo-cand ${c.already_on_shelf ? "on-shelf" : ""}`}>
                    <input type="checkbox" checked=${selected.has(c.book_id)} onChange=${() => toggle(c.book_id)} />
                    <div className="photo-cand-cover"><${Cover} book=${c} /></div>
                    <div className="photo-cand-body">
                      <div className="book-title">${c.title}</div>
                      ${c.author && html`<div className="book-author">${c.author}</div>`}
                      ${c.already_on_shelf && html`<span className="tag">Already on this shelf</span>`}
                    </div>
                    <button className="card-remove"
                            onClick=${(e) => { e.preventDefault(); remove(c.book_id); }}>Remove</button>
                  </label>`)}
              </div>`}
          <div className="photo-actions">
            <button className="btn btn-primary btn-sm" disabled=${busy || selected.size === 0} onClick=${confirm}>
              ${busy ? "Adding…" : `Add ${selected.size} to shelf`}
            </button>
            <button className="btn btn-ghost btn-sm" disabled=${busy} onClick=${reset}>Cancel</button>
          </div>
          ${err && html`<p className="save-msg">${err}</p>`}
        </div>`}
    </div>`;
}

export function ShelfPage({ id }) {
  const [shelf, setShelf] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [editing, setEditing] = React.useState(false);
  const [name, setName] = React.useState("");
  const [confirmDel, setConfirmDel] = React.useState(false);
  const [recs, runRecs] = useRecommendations(() => getShelfRecommendations(id));
  const [hideRecs, setHideRecs] = useHideExcluded();

  const reload = React.useCallback(() => {
    return getShelfById(id)
      .then((s) => { setShelf(s); setName(s.name); })
      .catch((e) => setError(e.message));
  }, [id]);

  React.useEffect(() => {
    let active = true;
    getShelfById(id)
      .then((s) => { if (active) { setShelf(s); setName(s.name); } })
      .catch((e) => active && setError(e.message))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [id]);

  const removeBook = async (bookId) => {
    try { await removeBookFromShelf(id, bookId); await reload(); } catch (e) { setError(e.message); }
  };
  const saveName = async () => {
    const n = name.trim();
    if (!n || n === shelf.name) { setEditing(false); setName(shelf.name); return; }
    try { await renameShelf(id, n); await reload(); setEditing(false); } catch (e) { setError(e.message); }
  };
  const doDelete = async () => {
    try { await deleteShelf(id); navigate("/shelf"); } catch (e) { setError(e.message); }
  };
  if (loading) return html`<div className="page"><${Loading} /></div>`;
  if (error) return html`<div className="page"><${ErrorState} message=${error} /></div>`;

  return html`
    <div className="page section">
      <${Link} to="/shelf" className="back-link">← Back to your bookshelf<//>
      <div className="shelf-page-header">
        ${editing
          ? html`<span className="inline-edit">
              <input type="text" value=${name} onChange=${(e) => setName(e.target.value)} />
              <button className="btn btn-ghost btn-sm" onClick=${saveName}>Save</button>
              <button className="card-remove" onClick=${() => { setEditing(false); setName(shelf.name); }}>Cancel</button>
            </span>`
          : html`
              <h1>${shelf.name}</h1>
              ${!shelf.is_exclusion && html`
                <span className="shelf-actions">
                  <button className="card-remove" onClick=${() => setEditing(true)}>Rename</button>
                  ${confirmDel
                    ? html`<span>Delete?${" "}
                        <button className="card-remove" onClick=${doDelete}>Yes</button>${" "}
                        <button className="card-remove" onClick=${() => setConfirmDel(false)}>No</button></span>`
                    : html`<button className="card-remove" onClick=${() => setConfirmDel(true)}>Delete</button>`}
                </span>`}`}
      </div>
      ${shelf.is_exclusion
        ? html`
            <p className="exclusion-note">${EXCLUSION_NOTE}</p>
            <div className="exclusion-setting" style=${{ marginBottom: "28px" }}>
              <${ExclusionToggle} hide=${hideRecs} onChange=${setHideRecs} />
              <span className="muted exclusion-setting-hint">When off, excluded books show grayed in recommendations instead of being hidden.</span>
            </div>`
        : html`
            <div className="shelf-page-actions">
              <${RecButton} state=${recs} run=${runRecs} label="Get recommendation →" />
              <${PhotoImport} shelfId=${id} onAdded=${reload} />
            </div>
            <${RecommendationsPanel} state=${recs} hide=${hideRecs} />`}
      ${shelf.books.length === 0
        ? html`<p className="empty-note">
            ${shelf.is_exclusion
              ? "Nothing excluded. Add a book here from its page to keep it out of recommendations."
              : "No books on this shelf yet. Add some from a book's page."}
          </p>`
        : html`<div className="grid">
            ${shelf.books.map((b) => html`
              <div key=${b.book_id} className="grid-cell">
                <${BookCard} book=${b} />
                ${/* Rating stars hidden for now — ratings aren't surfaced in the UI yet. */ ""}
                <div style=${{ textAlign: "center" }}>
                  <button className="card-remove" onClick=${() => removeBook(b.book_id)}>Remove from shelf</button>
                </div>
              </div>`)}
          </div>`}
    </div>`;
}

// ---- Auth pages -------------------------------------------------------------

export function Login() {
  const { login } = useAuth();
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError("");
    try { await login({ email, password }); navigate("/shelf"); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  return html`
    <div className="page"><div className="auth-wrap"><div className="auth-card">
      <h1>Welcome back</h1>
      <p className="auth-sub">Sign in to your bookshelf.</p>
      ${error && html`<div className="form-error">${error}</div>`}
      <form onSubmit=${submit}>
        <div className="field">
          <label>Email</label>
          <input type="email" value=${email} autoComplete="email"
                 onChange=${(e) => setEmail(e.target.value)} required />
        </div>
        <div className="field">
          <label>Password</label>
          <input type="password" value=${password} autoComplete="current-password"
                 onChange=${(e) => setPassword(e.target.value)} required />
        </div>
        <button className="btn btn-primary" type="submit" disabled=${busy}>
          ${busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="auth-alt">New here? <${Link} to="/signup">Create an account<//></p>
    </div></div></div>`;
}

export function Signup() {
  const { signup } = useAuth();
  const [email, setEmail] = React.useState("");
  const [displayName, setDisplayName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      await signup({ email, password, display_name: displayName || null });
      navigate("/shelf");
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  return html`
    <div className="page"><div className="auth-wrap"><div className="auth-card">
      <h1>Create your account</h1>
      <p className="auth-sub">Join to build your bookshelf.</p>
      ${error && html`<div className="form-error">${error}</div>`}
      <form onSubmit=${submit}>
        <div className="field">
          <label>Email</label>
          <input type="email" value=${email} autoComplete="email"
                 onChange=${(e) => setEmail(e.target.value)} required />
        </div>
        <div className="field">
          <label>Display name <span className="muted">(optional)</span></label>
          <input type="text" value=${displayName}
                 onChange=${(e) => setDisplayName(e.target.value)} />
        </div>
        <div className="field">
          <label>Password <span className="muted">(min 8 characters)</span></label>
          <input type="password" value=${password} autoComplete="new-password"
                 minLength=${8} onChange=${(e) => setPassword(e.target.value)} required />
        </div>
        <button className="btn btn-primary" type="submit" disabled=${busy}>
          ${busy ? "Creating…" : "Join →"}
        </button>
      </form>
      <p className="auth-alt">Already have an account? <${Link} to="/login">Sign in<//></p>
    </div></div></div>`;
}

// ---- "What's my next read?" assistant (global, all screens) -----------------

// One verified recommendation card: cover + title (links to the book) + the
// agent's one-line reason. Excluded books are grayed with a badge (like the
// Find-similar / recommendations panels).
function AssistantRecCard({ r }) {
  return html`
    <div className=${`asst-rec ${r.excluded ? "asst-rec-excluded" : ""}`}>
      <${Link} to=${`/books/${r.book_id}`} className="asst-rec-cover"><${Cover} book=${r} /><//>
      <div className="asst-rec-body">
        <${Link} to=${`/books/${r.book_id}`} className="asst-rec-title">${r.title}<//>
        ${r.author && html`<div className="book-author">${r.author}</div>`}
        ${r.why && html`<p className="asst-rec-why">${r.why}</p>`}
        ${r.excluded && html`<span className="tag tag-excluded">On your Exclusion shelf</span>`}
      </div>
    </div>`;
}

// One completed exchange: the reader's message, the reply, the recommendation
// cards (respecting the hide-excluded toggle), any notes, and the tool trace.
function AssistantTurn({ turn, hide }) {
  const [showTrace, setShowTrace] = React.useState(false);
  const recs = turn.recommendations || [];
  const { visible, allExcluded } = splitExcluded(recs, hide);
  const trace = turn.tool_trace || [];
  return html`
    <div className="asst-turn">
      <div className="asst-msg asst-msg-user">${turn.user}</div>
      <div className="asst-msg asst-msg-bot">
        ${turn.reply && html`<p>${turn.reply}</p>`}
        ${recs.length > 0 && html`
          <div className="asst-recs">
            ${allExcluded
              ? html`<p className="muted">All suggestions are on your Exclusion shelf.</p>`
              : visible.map((r, i) => html`<${AssistantRecCard} key=${r.book_id || i} r=${r} />`)}
          </div>`}
        ${(turn.notes || []).map((n, i) => html`<p key=${i} className="asst-note">${n}</p>`)}
        ${trace.length > 0 && html`
          <div className="asst-trace">
            <button className="asst-trace-toggle" onClick=${() => setShowTrace((s) => !s)}>
              ${showTrace ? "Hide steps" : "How I found these"} · ${trace.length}
            </button>
            ${showTrace &&
              html`<ul>${trace.map((t, i) => html`<li key=${i}>${t.tool}</li>`)}</ul>`}
          </div>`}
      </div>
    </div>`;
}

const ASSISTANT_INTRO =
  "Tell me what you feel like reading — a vibe, a book you loved, a genre and a length — and I'll find you something.";

export function AssistantBox() {
  const { user } = useAuth();
  const [open, setOpen] = React.useState(false);
  const [sessionId, setSessionId] = React.useState(null);
  const [turns, setTurns] = React.useState([]);
  const [input, setInput] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState("");
  const [hide] = useHideExcluded();
  const bodyRef = React.useRef(null);

  React.useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [turns, busy, open]);

  // Signed-in readers only: the API requires auth, so don't show the launcher at
  // all to signed-out visitors (all hooks above run first, so this is safe).
  if (!user) return null;

  const submit = async (e) => {
    e.preventDefault();
    const message = input.trim();
    if (!message || busy) return;
    setErr("");
    setInput("");
    setTurns((t) => [...t, { user: message, pending: true }]);
    setBusy(true);
    try {
      const reply = await sendAssistant({ message, session_id: sessionId });
      setSessionId(reply.session_id);
      setTurns((t) => [
        ...t.slice(0, -1),
        {
          user: message,
          reply: reply.reply,
          recommendations: reply.recommendations,
          unverified: reply.unverified,
          tool_trace: reply.tool_trace,
          notes: reply.notes,
        },
      ]);
    } catch (e) {
      setErr(e.message);
      setTurns((t) => t.slice(0, -1));
    } finally {
      setBusy(false);
    }
  };

  const newChat = () => { setTurns([]); setSessionId(null); setErr(""); };

  if (!open) {
    return html`<button className="asst-launcher" onClick=${() => setOpen(true)}
                        aria-label="Open the next-read assistant">✶ Next read</button>`;
  }

  return html`
    <div className="asst-panel" role="dialog" aria-label="What's my next read?">
      <div className="asst-header">
        <span className="asst-title">✶ What's my next read?</span>
        <span className="asst-header-actions">
          ${turns.length > 0 && html`<button className="asst-ghost" onClick=${newChat}>New chat</button>`}
          <button className="asst-ghost" onClick=${() => setOpen(false)} aria-label="Close">✕</button>
        </span>
      </div>

      <div className="asst-body" ref=${bodyRef}>
        ${turns.length === 0 && !busy &&
          html`<p className="asst-intro">${ASSISTANT_INTRO}</p>`}
        ${turns.map((t, i) =>
          t.pending
            ? html`<div className="asst-turn" key=${i}>
                <div className="asst-msg asst-msg-user">${t.user}</div>
                <div className="asst-msg asst-msg-bot asst-thinking">Looking through the catalog…</div>
              </div>`
            : html`<${AssistantTurn} key=${i} turn=${t} hide=${hide} />`)}
        ${err && html`<p className="asst-error">${err}</p>`}
      </div>

      <div className="asst-footer">
        <form className="asst-input" onSubmit=${submit}>
          <input type="text" value=${input} placeholder="What are you in the mood for?"
                 disabled=${busy} onChange=${(e) => setInput(e.target.value)} />
          <button className="btn btn-primary btn-sm" type="submit" disabled=${busy || !input.trim()}>
            ${busy ? "…" : "Ask"}
          </button>
        </form>
      </div>
    </div>`;
}
