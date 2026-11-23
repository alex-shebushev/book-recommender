// Thin fetch client for the Book Recommender API (same origin as this SPA).
// Cookies (the session) ride along automatically on same-origin requests.

const BASE = "/api";

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* ignore */ }
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return res.json();
}

async function send(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  let data = {};
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

// -- catalog ------------------------------------------------------------------

export function getBooks({ q, genre, limit = 24, offset = 0 } = {}) {
  const p = new URLSearchParams();
  if (q) p.set("q", q);
  if (genre) p.set("genre", genre);
  p.set("limit", limit);
  p.set("offset", offset);
  return getJSON(`${BASE}/books?${p.toString()}`);
}

export const getBook = (id) => getJSON(`${BASE}/books/${id}`);
export const getSimilar = (id) => getJSON(`${BASE}/books/${id}/similar`);
export const getGenres = () => getJSON(`${BASE}/genres`);
export const getShelf = () => getJSON(`${BASE}/shelf`);

// -- bookshelf recommendations (find_similar_by_bookshelf; ~30 s per call) ----

export const getLibraryRecommendations = () => getJSON(`${BASE}/shelf/recommendations`);
export const getShelfRecommendations = (id) => getJSON(`${BASE}/shelves/${id}/recommendations`);

// -- "What's my next read?" assistant -----------------------------------------

// One turn of the conversational assistant (sign-in required). Pass
// { message, session_id? } — session_id continues a conversation, omit it to
// start a new one. The call can take ~10–40 s (tool-using agent).
export const sendAssistant = (body) => send("POST", `${BASE}/assistant`, body);

// -- auth ---------------------------------------------------------------------

export const signup = (body) => send("POST", `${BASE}/auth/signup`, body);
export const login = (body) => send("POST", `${BASE}/auth/login`, body);
export const logout = () => send("POST", `${BASE}/auth/logout`);

// /me returns null (not an error) when the visitor isn't signed in.
export async function getMe() {
  const res = await fetch(`${BASE}/auth/me`);
  return res.ok ? res.json() : null;
}

// -- ratings (require sign-in) ----------------------------------------------

// Your own rating/review of a book (independent of shelves). Send only the
// fields to change; null clears one.
export const putRating = (bookId, body) => send("PUT", `${BASE}/books/${bookId}/rating`, body);

// -- shelves (the built-in Exclusion shelf is one of them) --------------------

export const getShelves = () => getJSON(`${BASE}/shelves`);
export const getShelfById = (id) => getJSON(`${BASE}/shelves/${id}`);
export const createShelf = (name) => send("POST", `${BASE}/shelves`, { name });
export const renameShelf = (id, name) => send("PATCH", `${BASE}/shelves/${id}`, { name });
export const deleteShelf = (id) => send("DELETE", `${BASE}/shelves/${id}`);
export const addBookToShelf = (shelfId, bookId) =>
  send("PUT", `${BASE}/shelves/${shelfId}/books/${bookId}`);
export const removeBookFromShelf = (shelfId, bookId) =>
  send("DELETE", `${BASE}/shelves/${shelfId}/books/${bookId}`);
export const bulkAddToShelf = (shelfId, bookIds) =>
  send("POST", `${BASE}/shelves/${shelfId}/books/bulk`, { book_ids: bookIds });

// Upload a bookshelf photo (multipart). The server reads it in memory and never
// stores it; returns matched catalog books to review before confirming. ~5–20 s.
export function parseBookshelfPhoto(shelfId, file) {
  const form = new FormData();
  form.append("file", file);
  return fetch(`${BASE}/shelves/${shelfId}/parse-photo`, { method: "POST", body: form }).then(
    async (res) => {
      if (!res.ok) {
        let detail = "";
        try { detail = (await res.json()).detail || ""; } catch { /* ignore */ }
        throw new Error(detail || `Request failed (${res.status})`);
      }
      return res.json();
    }
  );
}
