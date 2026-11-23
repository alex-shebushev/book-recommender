// App entry: wraps everything in AuthProvider, routes to the pages, mounts React.

import React from "react";
import { createRoot } from "react-dom/client";
import { html, NavBar, Loading } from "./ui.js";
import { usePath, Link, navigate } from "./router.js";
import { Catalog, BookDetail, Shelf, ShelfPage, Login, Signup, AssistantBox } from "./pages.js";
import { AuthProvider, useAuth } from "./auth.js";

// Gate a route behind sign-in. Redirects to /login once we know there's no user.
function RequireAuth({ children }) {
  const { user, loading } = useAuth();
  React.useEffect(() => {
    if (!loading && !user) navigate("/login");
  }, [loading, user]);

  if (loading) return html`<div className="page"><${Loading} /></div>`;
  if (!user) {
    return html`
      <div className="page">
        <div className="state">
          Please <${Link} to="/login" className="nav-link">sign in<//> to view your shelf.
        </div>
      </div>`;
  }
  return children;
}

function Route() {
  const path = usePath();

  if (path === "/") return html`<${Catalog} />`;
  if (path === "/login") return html`<${Login} />`;
  if (path === "/signup") return html`<${Signup} />`;
  if (path === "/shelf") return html`<${RequireAuth}><${Shelf} /><//>`;
  if (path.startsWith("/shelves/")) {
    const id = path.split("/")[2];
    if (id) return html`<${RequireAuth}><${ShelfPage} id=${id} /><//>`;
  }
  if (path.startsWith("/books/")) {
    const id = path.split("/")[2];
    if (id) return html`<${BookDetail} id=${id} />`;
  }
  return html`
    <div className="page">
      <div className="state">
        Page not found. <${Link} to="/" className="nav-link">Go to catalog<//>
      </div>
    </div>`;
}

function App() {
  return html`<${AuthProvider}>
    <${NavBar} />
    <${Route} />
    <${AssistantBox} />
  <//>`;
}

createRoot(document.getElementById("root")).render(html`<${App} />`);
