// Auth state for the SPA: a context holding the current user, backed by the
// session cookie. Self-contained (binds its own html) so it has no import cycle
// with ui.js, which consumes useAuth.

import React from "react";
import htm from "htm";
import { getMe, login as apiLogin, signup as apiSignup, logout as apiLogout } from "./api.js";

const html = htm.bind(React.createElement);
const AuthContext = React.createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = React.useState(null);
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    getMe()
      .then((u) => setUser(u))
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  const value = {
    user,
    loading,
    login: async (creds) => { const u = await apiLogin(creds); setUser(u); return u; },
    signup: async (creds) => { const u = await apiSignup(creds); setUser(u); return u; },
    logout: async () => { await apiLogout(); setUser(null); },
  };

  return html`<${AuthContext.Provider} value=${value}>${children}<//>`;
}

export function useAuth() {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}
