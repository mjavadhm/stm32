"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { AnswerBody, citationLabel } from "./answer";

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
  /** The stream was cut short; the text is whatever arrived. */
  partial?: boolean;
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
  /** "text" for a code/doc chunk, "page" for a reference-manual image. */
  kind?: "text" | "page";
  path: string;
  lines: [number, number] | null;
  chunks: SourceChunk[];
  page?: number;
  image_url?: string;
};

type LiveTurn = {
  searches: string[];
  answer: string;
  reasoning: string;
  citations: string[];
  warnings: string[];
};

const EMPTY_TURN: LiveTurn = {
  searches: [],
  answer: "",
  reasoning: "",
  citations: [],
  warnings: [],
};

// "" = the backend's configured default collection.
const DEFAULT_COLLECTION = "";

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [live, setLive] = useState<LiveTurn | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  // Lets a turn be cancelled deliberately, and on unmount, instead of
  // leaving the fetch running with nobody reading it.
  const abortRef = useRef<AbortController | null>(null);
  // The selection as of *now*, readable from an async callback that closed
  // over an older render.
  const selectedIdRef = useRef<string | null>(null);
  selectedIdRef.current = selectedId;

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

  /**
   * Load one conversation's messages.
   *
   * `expectedId` guards a late response: a slow load for a conversation the
   * user has already navigated away from must not overwrite the current one.
   */
  const loadConversation = useCallback(
    async (id: string, expectedId?: string) => {
      try {
        const r = await fetch(`${API_URL}/chat/conversations/${id}`);
        if (!r.ok) return;
        const detail = await r.json();
        if (expectedId !== undefined && selectedIdRef.current !== expectedId) {
          return;
        }
        setMessages(detail.messages);
      } catch {
        /* ignore */
      }
    },
    []
  );

  useEffect(() => {
    refreshConversations();
    refreshCollections();
  }, [refreshConversations, refreshCollections]);

  // Abandon an in-flight turn when the page goes away.
  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, live]);

  /** Switch conversations, abandoning whatever the current turn was doing. */
  const selectConversation = useCallback(
    (id: string) => {
      if (id === selectedId) return;
      // Without this, the finished turn's `finally` would reload the *old*
      // conversation over the newly selected one.
      abortRef.current?.abort();
      abortRef.current = null;
      setLive(null);
      setBusy(false);
      setErrorMsg(null);
      setSource(null);
      setSourceError(null);
      setSelectedId(id);
      setMessages([]);
      loadConversation(id);
    },
    [selectedId, loadConversation]
  );

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
        // A page citation resolves against the visual collection, a text one
        // against the text collection.
        const isPage = /#p\d+$/.test(citation);
        const collection = isPage ? scopePage : scopeText;
        if (collection) query.set("collection", collection);
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
    [scopeText, scopePage]
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
      abortRef.current?.abort();
      abortRef.current = null;
      setLive(null);
      setBusy(false);
      setSelectedId(created.id);
      setMessages([]);
    } catch (err) {
      setErrorMsg(`ساخت گفتگو ناموفق بود: ${String(err)}`);
    }
  }

  async function deleteConversation(id: string) {
    if (selectedId === id) abortRef.current?.abort();
    await fetch(`${API_URL}/chat/conversations/${id}`, { method: "DELETE" });
    if (selectedId === id) {
      setSelectedId(null);
      setMessages([]);
      setLive(null);
      setBusy(false);
    }
    await refreshConversations();
  }

  function stopTurn() {
    // The backend persists whatever it generated before the hang-up, so the
    // partial answer is still in the transcript after the reload.
    abortRef.current?.abort();
  }

  async function sendMessage(e: FormEvent) {
    e.preventDefault();
    const content = input.trim();
    if (!content || busy) return;
    if (!selectedId) {
      setErrorMsg("اول یک گفتگو بساز.");
      return;
    }
    // Captured for the whole turn: `selectedId` may change under us, and
    // every effect below has to belong to the conversation it started in.
    const turnId = selectedId;
    const controller = new AbortController();
    abortRef.current = controller;

    setBusy(true);
    setErrorMsg(null);
    setInput("");
    setMessages((prev) => [
      ...prev,
      { id: null, role: "user", content, payload: null },
    ]);
    setLive({ ...EMPTY_TURN });

    const scope: RetrievalScope = {
      text_collection: scopeText || null,
      page_collection: scopePage || null,
      document_ids: scopeDocument ? [scopeDocument] : null,
    };

    let aborted = false;
    try {
      const r = await fetch(`${API_URL}/chat/conversations/${turnId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, ...scope }),
        signal: controller.signal,
      });
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
        for (const frame of frames) applyFrame(frame);
      }
      if (buffer) applyFrame(buffer); // a stream that ended without a blank line
    } catch (err) {
      aborted = controller.signal.aborted;
      if (!aborted) setErrorMsg(`ارسال پیام ناموفق بود: ${String(err)}`);
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      // Only touch the view if this turn's conversation is still the one on
      // screen; otherwise the user has moved on and owns the state now.
      if (!aborted || turnId === selectedId) {
        setLive(null);
        setBusy(false);
        await loadConversation(turnId, turnId);
        await refreshConversations();
      }
    }

    /** One SSE frame. `: ping` keep-alives and junk are ignored, not fatal. */
    function applyFrame(frame: string) {
      const line = frame.trimStart();
      if (!line.startsWith("data:")) return; // comment frame (heartbeat)
      let event: Record<string, unknown>;
      try {
        event = JSON.parse(line.slice("data:".length));
      } catch {
        // A single malformed frame must not abort a good stream.
        return;
      }
      applyEvent(event);
    }

    function applyEvent(event: Record<string, unknown>) {
      setLive((prev) => {
        const turn = prev ?? { ...EMPTY_TURN };
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
            warnings: Array.from(
              new Set([...turn.warnings, ...((event.warnings as string[]) ?? [])])
            ),
          };
        }
        if (event.type === "reasoning") {
          return { ...turn, reasoning: turn.reasoning + String(event.text) };
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
                onClick={() => selectConversation(c.id)}
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
                    <AnswerBody
                      text={m.content}
                      onCitation={openSource}
                      partial={m.payload?.partial}
                    />
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
                  {live.reasoning && !live.answer && (
                    <details className="sources reasoning" open>
                      <summary>در حال استدلال…</summary>
                      <div className="reasoning-text small">{live.reasoning}</div>
                    </details>
                  )}
                  {live.answer ? (
                    <AnswerBody text={live.answer} onCitation={openSource} />
                  ) : (
                    !live.reasoning && (
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
                    )
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
              {busy ? (
                <button type="button" onClick={stopTurn} title="توقف تولید پاسخ">
                  توقف
                </button>
              ) : (
                <button type="submit" disabled={!input.trim()}>
                  ارسال
                </button>
              )}
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

          {/* A visual hit has no text: the model was shown the page image. */}
          {source?.kind === "page" && source.image_url && (
            <div className="source-chunk">
              <div className="muted small">صفحهٔ {source.page} از مرجع</div>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                className="source-page"
                src={`${API_URL}${source.image_url}`}
                alt={`صفحهٔ ${source.page} از مستند مرجع`}
              />
            </div>
          )}

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
          {source && source.kind !== "page" && source.chunks.length === 0 && (
            <p className="muted pad">برای این ارجاع متنی پیدا نشد.</p>
          )}
        </aside>
      )}
    </main>
  );
}
