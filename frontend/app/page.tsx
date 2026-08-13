"use client";

import { useEffect, useRef, useState } from "react";

type ChatEvent =
  | { type: "text_delta"; text: string }
  | { type: "tool_call"; name: string; input: unknown }
  | { type: "tool_result"; content: unknown }
  | { type: "action_needed"; message: string }
  | { type: "done"; cost_usd: number | null }
  | { type: "error"; message: string };

type Activity = {
  kind: "tool_call" | "tool_result" | "action_needed";
  label: string;
};

type Message = {
  role: "user" | "assistant";
  text: string;
  activity: Activity[];
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

export default function ChatPage() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // sessionStorage is per-tab (unlike localStorage), which is exactly the
  // isolation model this app wants: refresh keeps the conversation, a new
  // tab gets an independent one.
  useEffect(() => {
    setSessionId(getSessionId());
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function applyEvent(event: ChatEvent) {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (!last || last.role !== "assistant") return prev;

      switch (event.type) {
        case "text_delta":
          last.text += event.text;
          break;
        case "tool_call":
          last.activity = [
            ...last.activity,
            { kind: "tool_call", label: `Using ${event.name}` },
          ];
          break;
        case "tool_result":
          last.activity = [
            ...last.activity,
            { kind: "tool_result", label: "Got tool result" },
          ];
          break;
        case "action_needed":
          last.activity = [
            ...last.activity,
            { kind: "action_needed", label: event.message },
          ];
          break;
        case "done":
          last.costUsd = event.cost_usd;
          break;
        case "error":
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
    setMessages((prev) => [
      ...prev,
      { role: "user", text, activity: [] },
      { role: "assistant", text: "", activity: [] },
    ]);

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
      // token-by-token instead of all at once.
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

  return (
    <main className="chat">
      <header className="chat-header">
        <h1>Permit Research Agent</h1>
      </header>

      <div className="chat-log">
        {messages.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            {m.activity.length > 0 && (
              <div className="activity">
                {m.activity.map((a, j) => (
                  <span key={j} className={`chip ${a.kind}`}>
                    {a.label}
                  </span>
                ))}
              </div>
            )}
            <div className="text">{m.text}</div>
            {m.error && <div className="error">Error: {m.error}</div>}
            {m.costUsd != null && (
              <div className="cost">turn cost: ${m.costUsd.toFixed(4)}</div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <form
        className="chat-input"
        onSubmit={(e) => {
          e.preventDefault();
          sendMessage();
        }}
      >
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
    </main>
  );
}
