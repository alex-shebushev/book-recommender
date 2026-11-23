// Shared UI components for the Book Recommender SPA.

import React from "react";
import htm from "htm";
import { Link, usePath, navigate } from "./router.js";
import { useAuth } from "./auth.js";

export const html = htm.bind(React.createElement);

// Book cover with a graceful fallback when Open Library has no image.
export function Cover({ book }) {
  const [failed, setFailed] = React.useState(false);
  if (failed || !book.cover_url) {
    return html`
      <div className="cover-wrap">
        <div className="cover-fallback">${book.title}</div>
      </div>`;
  }
  return html`
    <div className="cover-wrap">
      <img src=${book.cover_url} alt=${`Cover of ${book.title}`}
           loading="lazy" onError=${() => setFailed(true)} />
    </div>`;
}

// Five-star rating (filled vs empty). Renders nothing when there is no rating.
export function Stars({ rating }) {
  if (!rating) return null;
  const full = "★".repeat(rating);
  const empty = "★".repeat(5 - rating);
  return html`<span className="stars">${full}<span className="empty">${empty}</span></span>`;
}

export function BookCard({ book }) {
  const authors = (book.authors || []).join(", ");
  return html`
    <${Link} to=${`/books/${book.book_id}`} className="book-card">
      <${Cover} book=${book} />
      <div>
        <div className="book-title">${book.title}</div>
        ${authors && html`<div className="book-author">${authors}</div>`}
      </div>
      <div className="book-tags">
        ${(book.genres || []).slice(0, 2).map(
          (g) => html`<span className="tag" key=${g}>${g}</span>`
        )}
      </div>
    <//>`;
}

export function Loading({ label = "Loading…" }) {
  return html`<div className="state">${label}</div>`;
}

export function ErrorState({ message }) {
  return html`<div className="state">Something went wrong: ${message}</div>`;
}

export function NavBar() {
  const path = usePath();
  const { user, logout } = useAuth();
  const params = new URLSearchParams(window.location.search);
  const [term, setTerm] = React.useState(params.get("q") || "");

  // Keep the box in sync when navigation changes the query (e.g. clearing it).
  React.useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    setTerm(p.get("q") || "");
  }, [path]);

  const submit = (e) => {
    e.preventDefault();
    const q = term.trim();
    navigate(q ? `/?q=${encodeURIComponent(q)}` : "/");
  };

  const signOut = async () => {
    await logout();
    navigate("/");
  };

  const isShelf = path.startsWith("/shelf");
  const isBrowse = path === "/" || path.startsWith("/books");

  return html`
    <nav className="nav">
      <div className="nav-inner">
        <${Link} to="/" className="wordmark">Bookshelf<span className="star">✶</span><//>
        <form className="nav-search" onSubmit=${submit} role="search">
          <span aria-hidden="true">⌕</span>
          <input type="search" value=${term} placeholder="Search for books or authors…"
                 onChange=${(e) => setTerm(e.target.value)} aria-label="Search books" />
        </form>
        <div className="nav-links">
          <${Link} to="/" className=${`nav-link ${isBrowse ? "active" : ""}`}>Browse<//>
          ${user
            ? html`
                <${Link} to="/shelf" className=${`nav-link ${isShelf ? "active" : ""}`}>My Shelf<//>
                <span className="nav-user">${user.display_name || user.email}</span>
                <button className="nav-link nav-signout" onClick=${signOut}>Sign out</button>`
            : html`
                <${Link} to="/login" className="nav-link">Sign in<//>
                <${Link} to="/signup" className="btn btn-primary btn-sm">Join →<//>`}
        </div>
      </div>
    </nav>`;
}
