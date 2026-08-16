"use client";

import {
  useEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type ChatEvent =
  | { type: "text_message"; text: string }
  | { type: "text_delta"; text: string }
  | { type: "tool_call"; name: string; input: unknown }
  | { type: "tool_result"; content: unknown }
  | { type: "action_needed"; message: string }
  | { type: "done"; cost_usd: number | null }
  | { type: "error"; message: string };

type Message = {
  role: "user" | "assistant";
  text: string;
  // Transient "what's happening right now" line — replaced in place by each
  // new tool_call, cleared once real text starts streaming again. This is
  // deliberately not a growing list: only the current action matters, same
  // as how Claude Code shows one live status line rather than a log.
  status: string | null;
  // Persistent, not cleared — things the user actually needs to notice
  // (e.g. a permission prompt), unlike the transient status line above.
  notices: string[];
  costUsd?: number | null;
  error?: string;
};

type Conversation = {
  id: string;
  title: string;
  updated_at: string;
};

type StoredMessage = {
  role: "user" | "assistant" | "system";
  content: string;
};

type AuthUser = {
  id: number | string;
  name: string;
  email: string;
  email_verified: boolean;
};

type AuthMode = "login" | "register";

type AuthResponse = {
  status: string;
  user: AuthUser | null;
  message?: string;
};

const APP_BASE_PATH = (process.env.NEXT_PUBLIC_APP_BASE_PATH || "/permits").replace(/\/$/, "");

function toolStatusLabel(name: string): string {
  // Subagent delegation reads better as "Delegating to X" than "Using Agent".
  if (name === "Agent" || name === "Task") return "Delegating to a subagent…";
  return `Using ${name}…`;
}

function submitOnEnter(event: KeyboardEvent<HTMLTextAreaElement>) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }
}

function authApiPath(path: string): string {
  return `${APP_BASE_PATH}${path}`;
}

function activeConversationKey(userId: number | string): string {
  return `permit-agent:active-conversation:${userId}`;
}

function conversationIdFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("chat");
}

function conversationUrl(id?: string): string {
  return id
    ? `${window.location.pathname}?chat=${encodeURIComponent(id)}`
    : window.location.pathname;
}

function authErrorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "message" in body) {
    const message = String((body as { message?: unknown }).message || "").trim();
    if (message) return message;
  }
  return fallback;
}

async function authApi(path: string, options: RequestInit = {}): Promise<AuthResponse> {
  const response = await fetch(authApiPath(path), {
    ...options,
    credentials: "include",
    headers: {
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(options.headers || {}),
    },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(authErrorMessage(body, "Authentication failed. Please try again."));
  }
  return body as AuthResponse;
}

function googleStartUrl(): string {
  const next =
    typeof window === "undefined"
      ? APP_BASE_PATH || "/"
      : `${window.location.pathname}${window.location.search}`;
  return `${authApiPath("/api/auth/google/start")}?next=${encodeURIComponent(next)}`;
}

// Fee/document tables can be wider than the bubble — scroll the table
// itself (matching .table-wrap in globals.css) instead of the whole page.
const markdownComponents = {
  table: ({ ...props }: ComponentPropsWithoutRef<"table">) => (
    <div className="table-wrap">
      <table {...props} />
    </div>
  ),
};

function ArchitectureFlow() {
  return (
    <section className="architecture-view">
      <header>
        <p>System map</p>
        <h1>Permit Agent architecture</h1>
      </header>
      <div className="flow">
        <article>
          <span>01</span>
          <h2>User request</h2>
          <p>Address, jurisdiction, permit type, project details, and research objective.</p>
        </article>
        <div className="flow-arrow">↓</div>
        <article>
          <span>02</span>
          <h2>Main orchestrator agent</h2>
          <p>Routes work through Claude Agent SDK, decides when to search AHJ data, call tools, and delegate to specialist agents.</p>
          <dl><dt>Agents</dt><dd>ahj-fetch-agent, parsing-agent, vector-ingest-agent, compile-agent</dd><dt>Tools</dt><dd>Task, find_ahj, get_structured_permit_data, vector_search, save_report</dd><dt>State</dt><dd>Thread history is stored per signed-in user in PostgreSQL.</dd></dl>
        </article>
        <div className="flow-arrow split">↙ ↓ ↘</div>
        <div className="flow-grid">
          <article>
            <span>03A</span>
            <h2>AHJ fetch agent</h2>
            <p>Finds authority-having-jurisdiction sites, portals, forms, and official source pages.</p>
            <dl><dt>Skill</dt><dd>Official-source discovery and raw document capture</dd></dl>
          </article>
          <article>
            <span>03B</span>
            <h2>Parsing agent</h2>
            <p>Extracts fees, timelines, deadlines, required documents, submission rules, and raw source text.</p>
            <dl><dt>Skill</dt><dd>Structured Postgres extraction from saved AHJ documents</dd></dl>
          </article>
          <article>
            <span>03C</span>
            <h2>Vector ingest agent</h2>
            <p>Searches pgvector permit chunks to ground answers in stored source material.</p>
            <dl><dt>Skill</dt><dd>Unstructured content chunking, embeddings, and semantic retrieval</dd></dl>
          </article>
        </div>
        <div className="flow-arrow">↓</div>
        <article>
          <span>04</span>
          <h2>Compile agent</h2>
          <p>Returns a structured answer with source-backed permit requirements, fees, timelines, documents, and next steps.</p>
          <dl><dt>Output</dt><dd>Final permit report using structured tables plus RAG context</dd></dl>
        </article>
      </div>
    </section>
  );
}

function AuthGate({ onAuthed }: { onAuthed: (user: AuthUser) => void }) {
  const [mode, setMode] = useState<AuthMode>("login");
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const authErrorCode = params.get("auth_error");
    if (authErrorCode) {
      const messages: Record<string, string> = {
        google_not_configured: "Google login is not configured yet.",
        google_state_mismatch: "Google login expired. Please try again.",
        google_token_failed: "Google did not complete the login. Please try again.",
        google_email_unverified: "Google did not return a verified email address.",
        google_login_failed: "Google login failed. Please try again.",
      };
      setError(messages[authErrorCode] || "Sign in failed. Please try again.");
      window.history.replaceState({}, "", window.location.pathname);
    }

    authApi("/api/auth/me")
      .then((data) => {
        if (data.user) onAuthed(data.user);
      })
      .catch(() => undefined)
      .finally(() => setReady(true));
  }, [onAuthed]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);

    try {
      const body =
        mode === "register"
          ? { name: name.trim(), email: email.trim(), password }
          : { email: email.trim(), password };
      const data = await authApi(mode === "register" ? "/api/auth/register" : "/api/auth/login", {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (!data.user) throw new Error("The app did not return an account.");
      onAuthed(data.user);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function startGoogleAuth() {
    window.location.href = googleStartUrl();
  }

  if (!ready) {
    return (
      <main className="auth-page">
        <div className="auth-card auth-card-compact">Checking your session...</div>
      </main>
    );
  }

  return (
    <main className="auth-page">
      <section className="auth-card">
        <div className="auth-heading">
          <p className="auth-kicker">Permit Agent account</p>
          <h1>Permit Research Agent</h1>
        </div>

        <div className="auth-tabs" role="tablist" aria-label="Authentication mode">
          <button
            type="button"
            className={mode === "login" ? "active" : ""}
            onClick={() => setMode("login")}
          >
            Log in
          </button>
          <button
            type="button"
            className={mode === "register" ? "active" : ""}
            onClick={() => setMode("register")}
          >
            Sign up
          </button>
        </div>

        <button type="button" className="google-auth" onClick={startGoogleAuth}>
          Continue with Google
        </button>

        <div className="auth-divider">
          <span>or</span>
        </div>

        <form className="auth-form" onSubmit={handleSubmit}>
          {mode === "register" && (
            <label>
              Name
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="name"
                required
              />
            </label>
          )}

          <label>
            Email
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              required
            />
          </label>

          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === "register" ? "new-password" : "current-password"}
              minLength={mode === "register" ? 8 : undefined}
              required
            />
          </label>

          {error && <div className="auth-error">{error}</div>}

          <button type="submit" disabled={busy}>
            {busy ? "Working..." : mode === "register" ? "Create account" : "Log in"}
          </button>
        </form>
      </section>
    </main>
  );
}

export default function ChatPage() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [view, setView] = useState<"chat" | "architecture">("chat");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [agentRunning, setAgentRunning] = useState(false);
  const [editingConversationId, setEditingConversationId] = useState<string | null>(null);
  const [conversationTitleDraft, setConversationTitleDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  // A turn can span several rounds (the backend automatically nudges the
  // orchestrator roughly every two minutes — see chat_loop.py). Each round ends
  // with a "done" event; this flags that the *next* event should start a
  // fresh bubble instead of appending to the previous round's, so every
  // turn summary shows up as its own chat message.
  const startNewBubbleRef = useRef(true);

  useEffect(() => {
    if (!user) return;
    refreshConversations();
  }, [user]);

  useEffect(() => {
    if (!user) return;
    const handleHistoryNavigation = () => {
      const id = conversationIdFromUrl();
      setView("chat");
      setAgentRunning(false);
      startNewBubbleRef.current = true;
      if (id) {
        setConversationId(id);
        window.localStorage.setItem(activeConversationKey(user.id), id);
        loadMessages(id);
      } else {
        setConversationId(null);
        setMessages([]);
        window.localStorage.removeItem(activeConversationKey(user.id));
      }
    };
    window.addEventListener("popstate", handleHistoryNavigation);
    return () => window.removeEventListener("popstate", handleHistoryNavigation);
  }, [user]);

  // Instant (not smooth) scroll: turns can run for minutes with new durable
  // messages arriving through polling, and a
  // smooth-scroll animation restarting on every single update looks jittery
  // rather than reliably keeping the latest content in view.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [messages]);

  // Runs live on the server independently of this page. Poll both durable
  // messages and run state so switching chats or refreshing can reattach to
  // an active run without losing progress.
  useEffect(() => {
    if (!user || !conversationId || view !== "chat") {
      setAgentRunning(false);
      return;
    }
    let cancelled = false;
    const poll = async () => {
      const response = await fetch(
        authApiPath(`/api/conversations/${conversationId}/status`),
        { credentials: "include" },
      );
      if (!response.ok || cancelled) return;
      const data = await response.json();
      if (cancelled) return;
      setAgentRunning(Boolean(data.running));
      await loadMessages(conversationId);
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [user, conversationId, view]);

  function applyEvent(event: ChatEvent) {
    setMessages((prev) => {
      if (event.type === "text_message") {
        startNewBubbleRef.current = false;
        const last = prev[prev.length - 1];
        if (
          last?.role === "assistant" &&
          !last.text &&
          last.notices.length === 0 &&
          !last.error
        ) {
          return [
            ...prev.slice(0, -1),
            { ...last, text: event.text, status: null },
          ];
        }
        return [...prev, { role: "assistant", text: event.text, status: null, notices: [] }];
      }

      let next = prev;
      // Refs are read when React executes this queued updater, not when
      // applyEvent is called. A later event may already have changed the
      // ref by then, so also enforce the role boundary here: agent events
      // must never attach status/notices/errors to a user message.
      const previousLast = prev[prev.length - 1];
      if (startNewBubbleRef.current || previousLast?.role !== "assistant") {
        next = [...prev, { role: "assistant", text: "", status: null, notices: [] }];
        startNewBubbleRef.current = false;
      } else {
        next = [...prev];
      }
      const last = next[next.length - 1];

      switch (event.type) {
        case "text_delta":
          last.text += event.text;
          last.status = null;
          break;
        case "tool_call":
          last.status = toolStatusLabel(event.name);
          break;
        case "tool_result":
          // Deliberately no status change — avoids a flicker between "Using
          // X" and a generic "got result" for the split second before the
          // next tool_call or text_delta arrives.
          break;
        case "action_needed":
          last.notices = [...last.notices, event.message];
          break;
        case "done":
          last.status = null;
          last.costUsd = event.cost_usd;
          // This round is over — whatever arrives next (another round's
          // status update, or nothing) starts a brand new bubble.
          startNewBubbleRef.current = true;
          break;
        case "error":
          last.status = null;
          last.error = event.message;
          break;
      }
      return next;
    });
  }

  async function refreshConversations(resetToNew = true) {
    const response = await fetch(authApiPath("/api/conversations"), { credentials: "include" });
    if (!response.ok) return;
    const data = await response.json();
    setConversations(data.conversations);
    if (resetToNew) {
      setView("chat");
      const urlId = conversationIdFromUrl();
      const savedId =
        urlId ||
        (user ? window.localStorage.getItem(activeConversationKey(user.id)) : null);
      const canRestore = data.conversations.some(
        (conversation: Conversation) => conversation.id === savedId,
      );
      if (savedId && canRestore) {
        setConversationId(savedId);
        if (user) window.localStorage.setItem(activeConversationKey(user.id), savedId);
        window.history.replaceState({}, "", conversationUrl(savedId));
        await loadMessages(savedId);
      } else {
        setConversationId(null);
        setMessages([]);
        window.history.replaceState({}, "", conversationUrl());
      }
    }
  }

  async function loadMessages(id: string) {
    const response = await fetch(authApiPath(`/api/conversations/${id}/messages`), {
      credentials: "include",
    });
    if (!response.ok) return;
    const data = await response.json();
    const stored: Message[] = data.messages
      .filter((item: StoredMessage) => item.role === "user" || item.role === "assistant")
      .map((item: StoredMessage) => ({
        role: item.role as "user" | "assistant",
        text: item.content,
        status: null,
        notices: [],
      }));
    setMessages((current) => {
      const unchanged =
        current.length === stored.length &&
        current.every(
          (message, index) =>
            message.role === stored[index].role && message.text === stored[index].text,
        );
      return unchanged ? current : stored;
    });
  }

  async function openConversation(id: string) {
    if (busy) return;
    setView("chat");
    setSidebarOpen(false);
    setConversationId(id);
    if (user) window.localStorage.setItem(activeConversationKey(user.id), id);
    window.history.pushState({}, "", conversationUrl(id));
    setAgentRunning(false);
    startNewBubbleRef.current = true;
    await loadMessages(id);
  }

  function newConversation() {
    setView("chat");
    setSidebarOpen(false);
    setConversationId(null);
    if (user) window.localStorage.removeItem(activeConversationKey(user.id));
    window.history.pushState({}, "", conversationUrl());
    setMessages([]);
    setInput("");
    setAgentRunning(false);
    startNewBubbleRef.current = true;
  }

  async function activeConversation(text: string) {
    if (conversationId) return conversationId;
    const response = await fetch(authApiPath("/api/conversations"), {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: text.slice(0, 80) }),
    });
    if (!response.ok) throw new Error("Could not create conversation");
    const data = await response.json();
    setConversations((current) => [data, ...current]);
    setConversationId(data.id);
    if (user) window.localStorage.setItem(activeConversationKey(user.id), data.id);
    window.history.replaceState({}, "", conversationUrl(data.id));
    return data.id as string;
  }

  async function deleteConversation(id: string, title: string) {
    if (!window.confirm(`Delete "${title}"? This cannot be undone.`)) return;
    setBusy(true);
    const response = await fetch(authApiPath(`/api/conversations/${id}`), {
      method: "DELETE",
      credentials: "include",
    });
    if (response.ok) {
      setConversations((current) => current.filter((item) => item.id !== id));
      if (conversationId === id) newConversation();
    }
    setBusy(false);
  }

  function beginRenameConversation(id: string, currentTitle: string) {
    setEditingConversationId(id);
    setConversationTitleDraft(currentTitle);
  }

  function cancelRenameConversation() {
    setEditingConversationId(null);
    setConversationTitleDraft("");
  }

  async function renameConversation(id: string, currentTitle: string) {
    const title = conversationTitleDraft.trim();
    if (!title || title === currentTitle) {
      cancelRenameConversation();
      return;
    }

    setBusy(true);
    try {
      const response = await fetch(authApiPath(`/api/conversations/${id}`), {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      if (!response.ok) throw new Error("Could not rename the conversation");
      const updated = await response.json();
      setConversations((current) =>
        current.map((conversation) =>
          conversation.id === id ? { ...conversation, ...updated } : conversation,
        ),
      );
      cancelRenameConversation();
    } catch (err) {
      window.alert((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function sendMessage() {
    const text = input.trim();
    if (!text || busy || agentRunning || !user) return;

    setInput("");
    setBusy(true);
    startNewBubbleRef.current = true;
    // Show the user's message immediately. Conversation creation and the
    // agent request can take time and should not hold back the local bubble.
    setMessages((prev) => [...prev, { role: "user", text, status: null, notices: [] }]);
    let activeId = conversationId;
    try {
      activeId = await activeConversation(text);
    } catch (err) {
      setMessages((prev) => [...prev, { role: "assistant", text: "", status: null, notices: [], error: (err as Error).message }]);
      setBusy(false);
      return;
    }
    setConversations((current) =>
      current
        .map((item) =>
          item.id === activeId
            ? { ...item, title: item.title === "New conversation" ? text.slice(0, 80) : item.title, updated_at: new Date().toISOString() }
            : item,
        )
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
    );
    try {
      // Must match basePath in next.config.js.
      const res = await fetch(authApiPath("/api/chat"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: activeId, message: text }),
      });

      if (!res.ok) {
        const body = await res.text();
        if (res.status === 401) setUser(null);
        throw new Error(`${res.status}: ${body}`);
      }
      setAgentRunning(true);
      await loadMessages(activeId);
    } catch (err) {
      applyEvent({ type: "error", message: (err as Error).message });
    } finally {
      await refreshConversations(false);
      if (activeId) setConversationId(activeId);
      setBusy(false);
    }
  }

  const hasStarted = view === "chat" && messages.length > 0;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    sendMessage();
  }

  async function logout() {
    await authApi("/api/auth/logout", { method: "POST" }).catch(() => undefined);
    setUser(null);
    if (user) window.localStorage.removeItem(activeConversationKey(user.id));
    window.history.replaceState({}, "", conversationUrl());
    setConversations([]);
    setConversationId(null);
    setMessages([]);
    setInput("");
    setAgentRunning(false);
  }

  if (!user) {
    return <AuthGate onAuthed={setUser} />;
  }

  return (
    <main className="workspace">
      <button className={sidebarOpen ? "sidebar-scrim visible" : "sidebar-scrim"} onClick={() => setSidebarOpen(false)} aria-label="Close menu" />
      <aside className={sidebarOpen ? "sidebar open" : "sidebar"}>
        <div className="sidebar-brand">
          <b>P</b>
          <div>
            <strong>Permit Agent</strong>
            <small>{user.email}</small>
          </div>
          <button className="sidebar-close" type="button" onClick={() => setSidebarOpen(false)} aria-label="Close menu">×</button>
        </div>
        <button className="new-thread" type="button" onClick={newConversation} disabled={busy}><span>＋</span> New conversation</button>
        <nav className="thread-list">
          <small>Recent chats</small>
          {conversations.length === 0 ? <p>No conversations yet</p> : conversations.map((item) => (
            <div className={item.id === conversationId && view === "chat" ? "thread active" : "thread"} key={item.id}>
              {editingConversationId === item.id ? (
                <form className="thread-rename-form" onSubmit={(event) => { event.preventDefault(); renameConversation(item.id, item.title); }}>
                  <input
                    value={conversationTitleDraft}
                    onChange={(event) => setConversationTitleDraft(event.target.value)}
                    onKeyDown={(event) => { if (event.key === "Escape") cancelRenameConversation(); }}
                    maxLength={120}
                    aria-label="Conversation name"
                    autoFocus
                  />
                </form>
              ) : (
                <button className="thread-open" type="button" onClick={() => openConversation(item.id)} disabled={busy}><span>◫</span><span>{item.title}</span></button>
              )}
              {editingConversationId === item.id ? (
                <button className="thread-rename save" type="button" onClick={() => renameConversation(item.id, item.title)} disabled={busy || !conversationTitleDraft.trim()} aria-label="Save conversation name" title="Save">✓</button>
              ) : (
                <button className="thread-rename" type="button" onClick={() => beginRenameConversation(item.id, item.title)} disabled={busy} aria-label={`Rename ${item.title}`} title="Rename conversation">✎</button>
              )}
              {editingConversationId === item.id ? (
                <button className="thread-delete cancel" type="button" onClick={cancelRenameConversation} disabled={busy} aria-label="Cancel rename" title="Cancel">×</button>
              ) : (
                <button className="thread-delete" type="button" onClick={() => deleteConversation(item.id, item.title)} disabled={busy} aria-label={`Delete ${item.title}`}>×</button>
              )}
            </div>
          ))}
        </nav>
        <button className={view === "architecture" ? "nav-link architecture-link active" : "nav-link architecture-link"} type="button" onClick={() => { setView("architecture"); setSidebarOpen(false); }}><span>⌘</span> Architecture</button>
        <div className="sidebar-account">
          <div className="avatar">{user.name.charAt(0).toUpperCase()}</div>
          <div className="account-copy"><strong>{user.name}</strong><small>{user.email}</small></div>
          <button className="logout-button" type="button" onClick={logout} title="Sign out" aria-label="Sign out">⇥</button>
        </div>
      </aside>
      <section className={`chat ${hasStarted ? "" : "chat-landing"}`}>
        <button className="mobile-menu" type="button" onClick={() => setSidebarOpen(true)} aria-label="Open menu">☰</button>
        {view === "architecture" ? <ArchitectureFlow /> : (
          <>
            {hasStarted && (
              <header className="chat-header">
                <div>
                  <h1>{conversations.find((item) => item.id === conversationId)?.title || "Permit research"}</h1>
                </div>
              </header>
            )}
            {hasStarted ? (
              <div className="chat-log">
                {messages.map((m, i) => (
                  <div key={i} className={`bubble ${m.role}`}>
                    {m.notices.length > 0 && <div className="notices">{m.notices.map((n, j) => <div key={j} className="notice">{n}</div>)}</div>}
                    {m.text && (m.role === "assistant" ? <div className="text markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{m.text}</ReactMarkdown></div> : <div className="text">{m.text}</div>)}
                    {m.status && <div className="status-line"><span className="status-dot" />{m.status}</div>}
                    {m.error && <div className="error">Error: {m.error}</div>}
                    {m.costUsd != null && <div className="cost">turn cost: ${m.costUsd.toFixed(4)}</div>}
                  </div>
                ))}
                {agentRunning && (
                  <div className="bubble assistant">
                    <div className="status-line"><span className="status-dot" />Permit Agent is working…</div>
                  </div>
                )}
                <div ref={bottomRef} />
              </div>
            ) : (
              <div className="landing">
                <div className="landing-mark">P</div>
                <h1 className="landing-title">What permit do you want to research?</h1>
                <form className="chat-input landing-input" onSubmit={handleSubmit}>
                <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={submitOnEnter} placeholder="e.g. What's required for a residential electrical permit in Austin, TX?" disabled={busy || agentRunning} autoFocus />
                  <button type="submit" disabled={busy || agentRunning || !input.trim()} aria-label="Send message">{busy ? "..." : "↑"}</button>
                </form>
              </div>
            )}
            {hasStarted && (
              <form className="chat-input" onSubmit={handleSubmit}>
                <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={submitOnEnter} placeholder={agentRunning ? "Permit Agent is working in this chat…" : "e.g. What's required for a residential electrical permit in Austin, TX?"} disabled={busy || agentRunning} />
                <button type="submit" disabled={busy || agentRunning || !input.trim()} aria-label="Send message">{busy ? "..." : "↑"}</button>
              </form>
            )}
          </>
        )}
      </section>
    </main>
  );
}
