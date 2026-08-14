"use client";

import {
  useEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type FormEvent,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type ChatEvent =
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

const SESSION_KEY = "permit-agent-session-id";

function getSessionId(): string {
  let id = sessionStorage.getItem(SESSION_KEY);
  if (!id) {
    id = crypto.randomUUID();
    sessionStorage.setItem(SESSION_KEY, id);
  }
  return id;
}

function toolStatusLabel(name: string): string {
  // Subagent delegation reads better as "Delegating to X" than "Using Agent".
  if (name === "Agent" || name === "Task") return "Delegating to a subagent…";
  return `Using ${name}…`;
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

export default function ChatPage() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  // A turn can span several rounds (the backend automatically nudges the
  // orchestrator roughly once a minute — see chat_loop.py). Each round ends
  // with a "done" event; this flags that the *next* event should start a
  // fresh bubble instead of appending to the previous round's, so every
  // turn summary shows up as its own chat message.
  const startNewBubbleRef = useRef(true);

  // sessionStorage is per-tab (unlike localStorage), which is exactly the
  // isolation model this app wants: refresh keeps the conversation, a new
  // tab gets an independent one.
  useEffect(() => {
    setSessionId(getSessionId());
  }, []);

  // Instant (not smooth) scroll: turns can stream for minutes with events
  // arriving every second or two (including ~60s status-check gaps), and a
  // smooth-scroll animation restarting on every single update looks jittery
  // rather than reliably keeping the latest content in view.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [messages]);

  function applyEvent(event: ChatEvent) {
    setMessages((prev) => {
      let next = prev;
      if (startNewBubbleRef.current) {
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

  async function sendMessage() {
    const text = input.trim();
    if (!text || !sessionId || busy) return;

    setInput("");
    setBusy(true);
    startNewBubbleRef.current = true;
    setMessages((prev) => [...prev, { role: "user", text, status: null, notices: [] }]);

    try {
      // Must match basePath in next.config.js.
      const res = await fetch("/permits/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });

      if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body}`);
      }
      if (!res.body) {
        throw new Error("No response body from server");
      }

      // Backend streams newline-delimited JSON events (not one big blob) —
      // reading the body incrementally here is what makes the reply appear
      // token-by-token instead of all at once. A single request can stay
      // open for several minutes: the backend automatically re-prompts the
      // orchestrator roughly once a minute for a status update while it's
      // still working (see chat_loop.py), so events keep arriving instead
      // of the connection going quiet.
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          if (!line.trim()) continue;
          applyEvent(JSON.parse(line) as ChatEvent);
        }
      }
    } catch (err) {
      applyEvent({ type: "error", message: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  const hasStarted = messages.length > 0;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    sendMessage();
  }

  // Before the first message: a centered hero (title + prompt box), no
  // header or log. After: normal layout with the prompt box docked at the
  // bottom — the same transition ChatGPT/Claude's own landing uses.
  return (
    <main className={`chat ${hasStarted ? "" : "chat-landing"}`}>
      {hasStarted && (
        <header className="chat-header">
          <h1>Permit Research Agent</h1>
        </header>
      )}

      {hasStarted ? (
        <div className="chat-log">
          {messages.map((m, i) => (
            <div key={i} className={`bubble ${m.role}`}>
              {m.notices.length > 0 && (
                <div className="notices">
                  {m.notices.map((n, j) => (
                    <div key={j} className="notice">
                      {n}
                    </div>
                  ))}
                </div>
              )}
              {m.text &&
                (m.role === "assistant" ? (
                  <div className="text markdown">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                      {m.text}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="text">{m.text}</div>
                ))}
              {m.status && (
                <div className="status-line">
                  <span className="status-dot" />
                  {m.status}
                </div>
              )}
              {m.error && <div className="error">Error: {m.error}</div>}
              {m.costUsd != null && (
                <div className="cost">turn cost: ${m.costUsd.toFixed(4)}</div>
              )}
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      ) : (
        <div className="landing">
          <h1 className="landing-title">Get full Permit Details with Permit Agent</h1>
          <form className="chat-input landing-input" onSubmit={handleSubmit}>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="e.g. What's required for a residential electrical permit in Austin, TX?"
              disabled={busy || !sessionId}
              autoFocus
            />
            <button type="submit" disabled={busy || !sessionId}>
              {busy ? "Working..." : "Send"}
            </button>
          </form>
        </div>
      )}

      {hasStarted && (
        <form className="chat-input" onSubmit={handleSubmit}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="e.g. What's required for a residential electrical permit in Austin, TX?"
            disabled={busy || !sessionId}
          />
          <button type="submit" disabled={busy || !sessionId}>
            {busy ? "Working..." : "Send"}
          </button>
        </form>
      )}
    </main>
  );
}
