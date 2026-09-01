"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Conversation = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

type RetrievalScope = {
  text_collection: string | null;
  page_collection: string | null;
  document_ids: string[] | null;
};

type MessagePayload = {
  citations: string[];
  cited: string[];
  grounded: boolean;
  verified: boolean;
  searches: string[];
  scope: RetrievalScope | null;
  warnings: string[];
  failed: boolean;
};

type ChatMessage = {
  id: number | null;
  role: "user" | "assistant";
  content: string;
  payload: MessagePayload | null;
};

type KbCollection = {
  name: string;
  document_count: number;
  chunk_count?: number;
  symbol_count?: number;
};

type KbDocument = {
  id: string;
  path: string;
  status: string;
  chunks?: number;
  symbols?: number;
  pages?: number;
};

// "" = the backend's configured default collection.
const DEFAULT_COLLECTION = "";

// Live state for the turn being streamed.
type LiveTurn = {
  searches: string[];
  answer: string;
  citations: string[];
  warnings: string[];
};

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [live, setLive] = useState<LiveTurn | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // --- retrieval scope (collection / document selectors) ---
  const [textCollections, setTextCollections] = useState<KbCollection[]>([]);
  const [visualCollections, setVisualCollections] = useState<KbCollection[]>([]);
  const [scopeText, setScopeText] = useState(DEFAULT_COLLECTION);
  const [scopePage, setScopePage] = useState(DEFAULT_COLLECTION);
  const [documents, setDocuments] = useState<KbDocument[]>([]);
  const [scopeDocument, setScopeDocument] = useState("");
  const [docsLoading, setDocsLoading] = useState(false);

  const refreshCollections = useCallback(async () => {
    try {
      const r = await fetch(`${API_URL}/rag/collections`);
      if (r.ok) {
        const data = await r.json();
        setTextCollections(data.text ?? []);
        setVisualCollections(data.visual ?? []);
      }
    } catch {
      /* knowledge base unreachable; the agent degrades with a warning */
    }
  }, []);

  useEffect(() => {
    refreshCollections();
  }, [refreshCollections]);

  // When the text collection changes, load its documents ("parts").
  useEffect(() => {
    setScopeDocument("");
    setDocuments([]);
    if (!scopeText) return;
    let cancelled = false;
    setDocsLoading(true);
    fetch(
      `${API_URL}/rag/documents?kind=text&collection=${encodeURIComponent(scopeText)}`
    )
      .then((r) => (r.ok ? r.json() : { documents: [] }))
      .then((data) => {
        if (!cancelled) setDocuments(data.documents ?? []);
      })
      .catch(() => {
        /* an unlistable collection still works; the selector just stays empty */
      })
      .finally(() => {
        if (!cancelled) setDocsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scopeText]);

  const refreshConversations = useCallback(async () => {
    try {
      const r = await fetch(`${API_URL}/chat/conversations`);
      if (r.ok) setConversations(await r.json());
    } catch {
      /* backend unreachable */
    }
  }, []);

  const loadConversation = useCallback(async (id: string) => {
    try {
      const r = await fetch(`${API_URL}/chat/conversations/${id}`);
      if (r.ok) {
        const detail = await r.json();
        setMessages(detail.messages);
      }
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    refreshConversations();
  }, [refreshConversations]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, live]);

  async function newConversation() {
    try {
      const r = await fetch(`${API_URL}/chat/conversations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const created: Conversation = await r.json();
      await refreshConversations();
      setSelectedId(created.id);
      setMessages([]);
    } catch (err) {
      setErrorMsg(`ساخت گفتگو ناموفق بود: ${String(err)}`);
    }
  }

  async function deleteConversation(id: string) {
    await fetch(`${API_URL}/chat/conversations/${id}`, { method: "DELETE" });
    if (selectedId === id) {
      setSelectedId(null);
      setMessages([]);
    }
    await refreshConversations();
  }

  async function sendMessage(e: FormEvent) {
    e.preventDefault();
    const content = input.trim();
    if (!content || busy) return;
    if (!selectedId) {
      setErrorMsg("اول یک گفتگو بساز.");
      return;
    }
    setBusy(true);
    setErrorMsg(null);
    setInput("");
    setMessages((prev) => [
      ...prev,
      { id: null, role: "user", content, payload: null },
    ]);
    setLive({ searches: [], answer: "", citations: [], warnings: [] });

    const scope: RetrievalScope = {
      text_collection: scopeText || null,
      page_collection: scopePage || null,
      document_ids: scopeDocument ? [scopeDocument] : null,
    };

    try {
      const r = await fetch(
        `${API_URL}/chat/conversations/${selectedId}/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content, ...scope }),
        }
      );
      if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);

      // POST + SSE: EventSource can't POST, so parse the stream by hand.
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          if (!frame.startsWith("data: ")) continue;
          const event = JSON.parse(frame.slice("data: ".length));
          applyEvent(event);
        }
      }
    } catch (err) {
      setErrorMsg(`ارسال پیام ناموفق بود: ${String(err)}`);
    } finally {
      setLive(null);
      setBusy(false);
      await loadConversation(selectedId);
      await refreshConversations();
    }

    function applyEvent(event: Record<string, unknown>) {
      setLive((prev) => {
        const turn = prev ?? { searches: [], answer: "", citations: [], warnings: [] };
        if (event.type === "search") {
          return { ...turn, searches: [...turn.searches, String(event.query)] };
        }
        if (event.type === "search_result") {
          const citations = [
            ...turn.citations,
            ...((event.citations as string[]) ?? []),
          ];
          return {
            ...turn,
            citations: Array.from(new Set(citations)),
            warnings: [...turn.warnings, ...((event.warnings as string[]) ?? [])],
          };
        }
        if (event.type === "delta") {
          return { ...turn, answer: turn.answer + String(event.text) };
        }
        return turn;
      });
      if (event.type === "error") {
        setErrorMsg(String(event.detail ?? "خطای ناشناخته در جریان پاسخ"));
      }
    }
  }

  return (
    <main className="container wide">
      <header className="header">
        <h1>گفتگو با مستندات</h1>
        <a className="nav-link" href="/">
          ← داشبورد پروژه‌ها
        </a>
      </header>
      <p className="muted small">
        دربارهٔ مستندات STM32 (مرجع‌ها، دیتاشیت‌ها، HAL) بپرس؛ ایجنت خودش تصمیم
        می‌گیرد چه جستجوهایی در پایگاه دانش اجرا کند و پاسخ را با ارجاع می‌دهد.
      </p>

      <div className="chat-columns">
        {/* فهرست گفتگوها */}
        <section className="card">
          <h2>گفتگوها</h2>
          <button onClick={newConversation}>گفتگوی جدید</button>
          <ul className="conversation-list">
            {conversations.map((c) => (
              <li
                key={c.id}
                className={`conversation-item ${selectedId === c.id ? "selected" : ""}`}
                onClick={() => {
                  setSelectedId(c.id);
                  loadConversation(c.id);
                }}
              >
                <div className="conversation-row">
                  <span className="conversation-title">
                    {c.title || "بدون عنوان"}
                  </span>
                  <button
                    className="danger conversation-delete"
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteConversation(c.id);
                    }}
                  >
                    حذف
                  </button>
                </div>
              </li>
            ))}
          </ul>
          {conversations.length === 0 && (
            <p className="muted small">هنوز گفتگویی نیست.</p>
          )}
        </section>

        {/* پیام‌ها */}
        <section className="card chat-panel">
          {!selectedId && (
            <p className="muted">برای شروع، یک گفتگوی جدید بساز یا انتخاب کن.</p>
          )}
          {selectedId && (
            <>
              <div className="message-list">
                {messages.map((m) => (
                  <div key={m.id ?? `local-${m.content}`} className={`bubble bubble-${m.role}`}>
                    <div className="bubble-text">{m.content}</div>
                    {m.payload?.scope?.text_collection && (
                      <div className="muted small" dir="ltr">
                        scope: {m.payload.scope.text_collection}
                        {m.payload.scope.document_ids?.length
                          ? " · filtered to selected document"
                          : ""}
                      </div>
                    )}
                    {m.payload && m.payload.cited.length > 0 && (
                      <div className="chip-row">
                        {m.payload.cited.map((c) => (
                          <span key={c} className="citation-chip" dir="ltr">
                            {c}
                          </span>
                        ))}
                      </div>
                    )}
                    {m.payload && m.payload.warnings.length > 0 && (
                      <div className="error-text small">
                        {m.payload.warnings.join(" · ")}
                      </div>
                    )}
                  </div>
                ))}

                {live && (
                  <div className="bubble bubble-assistant">
                    {live.searches.length > 0 && (
                      <div className="chip-row">
                        {live.searches.map((q, i) => (
                          <span key={i} className="search-chip">
                            جستجو: {q}
                          </span>
                        ))}
                      </div>
                    )}
                    {live.answer && <div className="bubble-text">{live.answer}</div>}
                    {!live.answer && (
                      <span className="muted small">
                        در حال برنامه‌ریزی و اجرای جستجو…
                      </span>
                    )}
                  </div>
                )}
                <div ref={bottomRef} />
              </div>

              {/* محدودهٔ جستجو */}
              <div className="scope-row">
                <label className="field-label">
                  مجموعهٔ متنی
                  <select
                    value={scopeText}
                    onChange={(e) => setScopeText(e.target.value)}
                    disabled={busy}
                  >
                    <option value={DEFAULT_COLLECTION}>پیش‌فرض</option>
                    {textCollections.map((c) => (
                      <option key={c.name} value={c.name}>
                        {c.name} ({c.document_count} سند)
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field-label">
                  مجموعهٔ صفحات
                  <select
                    value={scopePage}
                    onChange={(e) => setScopePage(e.target.value)}
                    disabled={busy}
                  >
                    <option value={DEFAULT_COLLECTION}>پیش‌فرض</option>
                    {visualCollections.map((c) => (
                      <option key={c.name} value={c.name}>
                        {c.name} ({c.document_count} سند)
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field-label">
                  مستند (بخش)
                  <select
                    value={scopeDocument}
                    onChange={(e) => setScopeDocument(e.target.value)}
                    disabled={busy || !scopeText || docsLoading}
                  >
                    <option value="">
                      {docsLoading
                        ? "در حال بارگذاری…"
                        : scopeText
                          ? "همهٔ مستندها"
                          : "اول مجموعهٔ متنی را انتخاب کن"}
                    </option>
                    {documents.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.path}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <form onSubmit={sendMessage} className="chat-form">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  rows={2}
                  placeholder="سؤالت را بپرس… مثلاً: تفاوت حالت DMA و وقفه در SPI روی F407 چیست؟"
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      sendMessage(e);
                    }
                  }}
                />
                <button type="submit" disabled={busy || !input.trim()}>
                  {busy ? "در حال پاسخ…" : "ارسال"}
                </button>
              </form>
            </>
          )}
          {errorMsg && <p className="error-text">{errorMsg}</p>}
        </section>
      </div>
    </main>
  );
}
