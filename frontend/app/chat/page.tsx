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

type SourceChunk = {
  name: string;
  kind: string;
  lines: [number, number] | null;
  text: string;
};

type SourceView = {
  citation: string;
  path: string;
  lines: [number, number] | null;
  chunks: SourceChunk[];
};

type LiveTurn = {
  searches: string[];
  answer: string;
  citations: string[];
  warnings: string[];
};

// "" = the backend's configured default collection.
const DEFAULT_COLLECTION = "";

/** A citation is `path:start-end`; show the file name, keep the rest as title. */
function citationLabel(citation: string): string {
  const [path, lines] = splitCitation(citation);
  const file = path.split("/").pop() || path;
  return lines ? `${file}:${lines}` : file;
}

function splitCitation(citation: string): [string, string | null] {
  const at = citation.lastIndexOf(":");
  if (at === -1) return [citation, null];
  const lines = citation.slice(at + 1);
  return /^\d+-\d+$/.test(lines)
    ? [citation.slice(0, at), lines]
    : [citation, null];
}

/** Render answer text: fenced code blocks as <pre>, [citations] clickable. */
function AnswerBody({
  text,
  onCitation,
}: {
  text: string;
  onCitation: (citation: string) => void;
}) {
  const blocks = text.split(/```/);
  return (
    <div className="answer">
      {blocks.map((block, index) =>
        index % 2 === 1 ? (
          <pre key={index} dir="ltr" className="code-block">
            <code>{block.replace(/^[a-zA-Z]*\n/, "")}</code>
          </pre>
        ) : (
          <Prose key={index} text={block} onCitation={onCitation} />
        )
      )}
    </div>
  );
}

/** Prose with `[path:1-2]` turned into buttons and `**bold**` honoured. */
function Prose({
  text,
  onCitation,
}: {
  text: string;
  onCitation: (citation: string) => void;
}) {
  const parts = text.split(/(\[[^\]\s]+:\d+-\d+\]|\*\*[^*]+\*\*|`[^`]+`)/g);
  return (
    <p className="prose">
      {parts.map((part, index) => {
        const citation = part.match(/^\[([^\]\s]+:\d+-\d+)\]$/);
        if (citation) {
          return (
            <button
              key={index}
              type="button"
              className="citation-link"
              dir="ltr"
              title={citation[1]}
              onClick={() => onCitation(citation[1])}
            >
              {citationLabel(citation[1])}
            </button>
          );
        }
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={index}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("`") && part.endsWith("`")) {
          return (
            <code key={index} dir="ltr">
              {part.slice(1, -1)}
            </code>
          );
        }
        return <span key={index}>{part}</span>;
      })}
    </p>
  );
}

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
  const [scopeOpen, setScopeOpen] = useState(false);

  // --- source viewer ---
  const [source, setSource] = useState<SourceView | null>(null);
  const [sourceLoading, setSourceLoading] = useState<string | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);

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
    refreshCollections();
  }, [refreshConversations, refreshCollections]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, live]);

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
        /* an unlistable collection still works; the selector stays empty */
      })
      .finally(() => {
        if (!cancelled) setDocsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scopeText]);

  const openSource = useCallback(
    async (citation: string) => {
      setSourceLoading(citation);
      setSourceError(null);
      setSource(null);
      try {
        const query = new URLSearchParams({ citation });
        if (scopeText) query.set("collection", scopeText);
        const r = await fetch(`${API_URL}/rag/source?${query}`);
        if (!r.ok) {
          const detail = await r.json().catch(() => ({}));
          throw new Error(detail.detail ?? `HTTP ${r.status}`);
        }
        setSource(await r.json());
      } catch (err) {
        setSourceError(`منبع در دسترس نیست: ${String(err)}`);
      } finally {
        setSourceLoading(null);
      }
    },
    [scopeText]
  );

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
          applyEvent(JSON.parse(frame.slice("data: ".length)));
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
        const turn =
          prev ?? { searches: [], answer: "", citations: [], warnings: [] };
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

  const scopeSummary = scopeDocument
    ? documents.find((d) => d.id === scopeDocument)?.path?.split("/").pop() ??
      "یک مستند"
    : scopeText || "پیش‌فرض";

  return (
    <main className="chat-shell">
      <aside className="chat-sidebar">
        <div className="sidebar-head">
          <a className="brand" href="/">
            <span className="brand-mark">S</span>
            <span>دستیار STM32</span>
          </a>
          <button className="ghost-button" onClick={newConversation}>
            + گفتگوی جدید
          </button>
        </div>

        <div className="sidebar-scroll">
          {conversations.length === 0 && (
            <p className="muted small pad">هنوز گفتگویی نیست.</p>
          )}
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
                <span className="conversation-title">
                  {c.title || "بدون عنوان"}
                </span>
                <button
                  className="icon-button"
                  title="حذف گفتگو"
                  onClick={(e) => {
                    e.stopPropagation();
                    deleteConversation(c.id);
                  }}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        </div>

        <a className="sidebar-foot nav-link" href="/">
          ← داشبورد پروژه‌ها
        </a>
      </aside>

      <section className="chat-main">
        <header className="chat-header">
          <div>
            <h1>گفتگو با مستندات</h1>
            <p className="muted small">
              ایجنت خودش تصمیم می‌گیرد چه جستجویی در پایگاه دانش اجرا کند و پاسخ
              را با ارجاع می‌دهد.
            </p>
          </div>
          <button
            className="ghost-button"
            onClick={() => setScopeOpen((open) => !open)}
          >
            محدوده: {scopeSummary}
          </button>
        </header>

        {scopeOpen && (
          <div className="scope-panel">
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
        )}

        {!selectedId && (
          <div className="empty-state">
            <p>برای شروع، یک گفتگوی جدید بساز.</p>
            <button onClick={newConversation}>+ گفتگوی جدید</button>
          </div>
        )}

        {selectedId && (
          <>
            <div className="message-list">
              {messages.map((m) => (
                <article
                  key={m.id ?? `local-${m.content}`}
                  className={`bubble bubble-${m.role}`}
                >
                  <div className="bubble-role">
                    {m.role === "user" ? "تو" : "دستیار"}
                  </div>
                  {m.role === "assistant" ? (
                    <AnswerBody text={m.content} onCitation={openSource} />
                  ) : (
                    <div className="bubble-text">{m.content}</div>
                  )}

                  {m.payload && m.payload.citations.length > 0 && (
                    <details className="sources">
                      <summary>
                        منابع بازیابی‌شده ({m.payload.citations.length})
                        {m.payload.cited.length > 0 &&
                          ` · ${m.payload.cited.length} ارجاع در متن`}
                      </summary>
                      <div className="chip-row">
                        {m.payload.citations.map((c) => (
                          <button
                            key={c}
                            type="button"
                            className={`citation-chip ${m.payload?.cited.includes(c) ? "cited" : ""}`}
                            dir="ltr"
                            title={c}
                            onClick={() => openSource(c)}
                          >
                            {sourceLoading === c ? "…" : citationLabel(c)}
                          </button>
                        ))}
                      </div>
                    </details>
                  )}

                  {m.payload?.searches?.length ? (
                    <details className="sources">
                      <summary>جستجوهای ایجنت ({m.payload.searches.length})</summary>
                      <ul className="search-list" dir="ltr">
                        {m.payload.searches.map((q, i) => (
                          <li key={i}>{q}</li>
                        ))}
                      </ul>
                    </details>
                  ) : null}

                  {m.payload && m.payload.warnings.length > 0 && (
                    <div className="warning-text small">
                      {m.payload.warnings.join(" · ")}
                    </div>
                  )}
                </article>
              ))}

              {live && (
                <article className="bubble bubble-assistant">
                  <div className="bubble-role">دستیار</div>
                  {live.searches.length > 0 && (
                    <div className="chip-row">
                      {live.searches.map((q, i) => (
                        <span key={i} className="search-chip" dir="ltr" title={q}>
                          جستجو: {q.length > 48 ? `${q.slice(0, 48)}…` : q}
                        </span>
                      ))}
                    </div>
                  )}
                  {live.answer ? (
                    <AnswerBody text={live.answer} onCitation={openSource} />
                  ) : (
                    <div className="thinking">
                      <span className="dot" />
                      <span className="dot" />
                      <span className="dot" />
                      <span className="muted small">
                        {live.searches.length
                          ? "در حال خواندن منابع…"
                          : "در حال برنامه‌ریزی جستجو…"}
                      </span>
                    </div>
                  )}
                </article>
              )}
              <div ref={bottomRef} />
            </div>

            <form onSubmit={sendMessage} className="chat-form">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                rows={1}
                placeholder="سؤالت را بپرس… مثلاً: تفاوت حالت DMA و وقفه در SPI روی F407 چیست؟"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    sendMessage(e);
                  }
                }}
              />
              <button type="submit" disabled={busy || !input.trim()}>
                {busy ? "…" : "ارسال"}
              </button>
            </form>
            {errorMsg && <p className="error-text pad">{errorMsg}</p>}
          </>
        )}
      </section>

      {(source || sourceError) && (
        <aside className="source-panel">
          <header className="source-head">
            <div>
              <strong dir="ltr">{source?.path ?? "منبع"}</strong>
              {source?.lines && (
                <span className="muted small" dir="ltr">
                  {" "}
                  lines {source.lines[0]}–{source.lines[1]}
                </span>
              )}
            </div>
            <button
              className="icon-button"
              onClick={() => {
                setSource(null);
                setSourceError(null);
              }}
            >
              ×
            </button>
          </header>
          {sourceError && <p className="error-text pad">{sourceError}</p>}
          {source?.chunks.map((chunk, index) => (
            <div key={index} className="source-chunk">
              <div className="muted small" dir="ltr">
                {chunk.kind} · {chunk.name}
                {chunk.lines ? ` · ${chunk.lines[0]}–${chunk.lines[1]}` : ""}
              </div>
              <pre dir="ltr" className="code-block">
                <code>{chunk.text}</code>
              </pre>
            </div>
          ))}
          {source && source.chunks.length === 0 && (
            <p className="muted pad">برای این ارجاع متنی پیدا نشد.</p>
          )}
        </aside>
      )}
    </main>
  );
}
