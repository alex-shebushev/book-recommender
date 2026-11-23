// Minimal history-API router for a 3-view SPA. Avoids a CDN router dependency.
// The FastAPI deep-link fallback serves index.html for /books/:id and /shelf,
// so a hard refresh on any route still boots the app.

import React from "react";
import htm from "htm";

const html = htm.bind(React.createElement);
const listeners = new Set();

function emit() { listeners.forEach((fn) => fn()); }

export function navigate(to) {
  if (to !== window.location.pathname + window.location.search) {
    window.history.pushState({}, "", to);
    emit();
  }
}

window.addEventListener("popstate", emit);

// Current pathname as reactive state (used for route matching).
export function usePath() {
  const [path, setPath] = React.useState(window.location.pathname);
  React.useEffect(() => {
    const onChange = () => setPath(window.location.pathname);
    listeners.add(onChange);
    return () => listeners.delete(onChange);
  }, []);
  return path;
}

// Current query string (e.g. "?q=dune") as reactive state — pathname can stay
// "/" while only the query changes, so search needs its own subscription.
export function useSearch() {
  const [search, setSearch] = React.useState(window.location.search);
  React.useEffect(() => {
    const onChange = () => setSearch(window.location.search);
    listeners.add(onChange);
    return () => listeners.delete(onChange);
  }, []);
  return search;
}

// Internal link that uses pushState instead of a full navigation.
export function Link({ to, className, children, onClick }) {
  const handle = (e) => {
    // Let modified clicks (new tab, etc.) behave normally.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    if (onClick) onClick(e);
    navigate(to);
    window.scrollTo(0, 0);
  };
  return html`<a href=${to} className=${className} onClick=${handle}>${children}</a>`;
}
