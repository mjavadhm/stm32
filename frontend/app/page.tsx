"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type PinSelectionPolicy = "deterministic" | "explicit" | "llm";

type ProjectSummary = {
  id: string;
  name: string;
  request_type: string;
  status: string;
  pin_selection_policy: PinSelectionPolicy;
  error: string | null;
  created_at: string;
  updated_at: string;
};

type TaskInfo = {
  agent_name: string;
  status: string;
  result: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
};

type ProjectDetail = ProjectSummary & {
  user_request: string;
  tasks: TaskInfo[];
};

type AgentSetting = {
  agent_name: string;
  model: string;
  is_override: boolean;
  enabled: boolean;
};

type GenerationSetting = {
  pin_selection_policy: PinSelectionPolicy;
};

const STATUS_FA: Record<string, string> = {
  pending: "در صف",
  running: "در حال اجرا",
  done: "تمام شد",
  failed: "خطا",
  cancelled: "لغو شد",
};

const TYPE_FA: Record<string, string> = {
  full_project: "پروژه کامل",
  debug: "دیباگ",
  optimize: "بهینه‌سازی",
  test: "تست",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge badge-${status}`}>{STATUS_FA[status] ?? status}</span>
  );
}

export default function Home() {
  const [health, setHealth] = useState<"checking" | "ok" | "down">("checking");
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [agents, setAgents] = useState<AgentSetting[]>([]);
  const [generation, setGeneration] = useState<GenerationSetting>({
    pin_selection_policy: "deterministic",
  });
  const [modelEdits, setModelEdits] = useState<Record<string, string>>({});

  const [name, setName] = useState("");
  const [request, setRequest] = useState("");
  const [pinPolicy, setPinPolicy] = useState<"default" | PinSelectionPolicy>("default");
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const refreshProjects = useCallback(async () => {
    try {
      const r = await fetch(`${API_URL}/projects`);
      setProjects(await r.json());
      setHealth("ok");
    } catch {
      setHealth("down");
    }
  }, []);

  const refreshDetail = useCallback(async (id: string) => {
    try {
      const r = await fetch(`${API_URL}/projects/${id}`);
      if (r.ok) setDetail(await r.json());
    } catch {
      /* backend unreachable — health badge already shows it */
    }
  }, []);

  const refreshAgents = useCallback(async () => {
    try {
      const r = await fetch(`${API_URL}/agents/settings`);
      setAgents(await r.json());
    } catch {
      /* ignore */
    }
  }, []);

  const refreshGeneration = useCallback(async () => {
    try {
      const r = await fetch(`${API_URL}/generation/settings`);
      if (r.ok) setGeneration(await r.json());
    } catch {
      /* ignore */
    }
  }, []);

  // Initial load
  useEffect(() => {
    refreshProjects();
    refreshAgents();
    refreshGeneration();
  }, [refreshProjects, refreshAgents, refreshGeneration]);

  // Live polling (projects list + selected project detail)
  useEffect(() => {
    const t = setInterval(() => {
      refreshProjects();
      if (selectedId) refreshDetail(selectedId);
    }, 2500);
    return () => clearInterval(t);
  }, [selectedId, refreshProjects, refreshDetail]);

  async function createProject(e: FormEvent) {
    e.preventDefault();
    if (!name.trim() || !request.trim() || busy) return;
    setBusy(true);
    setErrorMsg(null);
    try {
      const r = await fetch(`${API_URL}/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          request: request.trim(),
          pin_selection_policy: pinPolicy === "default" ? null : pinPolicy,
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const created: ProjectSummary = await r.json();
      setName("");
      setRequest("");
      setPinPolicy("default");
      setSelectedId(created.id);
      await refreshProjects();
      await refreshDetail(created.id);
    } catch (err) {
      setErrorMsg(`ثبت درخواست ناموفق بود: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function cancelProject(id: string) {
    await fetch(`${API_URL}/projects/${id}/cancel`, { method: "POST" });
    await refreshProjects();
    await refreshDetail(id);
  }

  async function saveAgentModel(agentName: string) {
    const value = (modelEdits[agentName] ?? "").trim();
    await fetch(`${API_URL}/agents/settings/${agentName}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: value }), // "" پاک‌کردن override
    });
    setModelEdits((m) => ({ ...m, [agentName]: "" }));
    await refreshAgents();
  }

  async function toggleAgent(agent: AgentSetting) {
    await fetch(`${API_URL}/agents/settings/${agent.agent_name}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !agent.enabled }),
    });
    await refreshAgents();
  }

  async function saveGenerationPolicy(policy: PinSelectionPolicy) {
    await fetch(`${API_URL}/generation/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin_selection_policy: policy }),
    });
    await refreshGeneration();
  }

  return (
    <main className="container wide">
      <header className="header">
        <h1>دستیار هوشمند مهندسی STM32</h1>
        <div className="header-actions">
          <a className="nav-link" href="/chat">
            گفتگو با مستندات →
          </a>
          <span className={`status status-${health}`}>
            {health === "checking" && "در حال بررسی…"}
            {health === "ok" && "بک‌اند متصل ✅"}
            {health === "down" && "بک‌اند در دسترس نیست ❌"}
          </span>
        </div>
      </header>

      {/* درخواست جدید */}
      <section className="card">
        <h2>درخواست جدید</h2>
        <form onSubmit={createProject} className="form">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="نام پروژه (مثلاً mpu6050-demo)"
          />
          <textarea
            value={request}
            onChange={(e) => setRequest(e.target.value)}
            rows={3}
            placeholder="درخواست مهندسی… مثلاً: خواندن سنسور MPU6050 با SPI و DMA روی STM32F407"
          />
          <label className="field-label">
            سیاست انتخاب پایه
            <select
              value={pinPolicy}
              onChange={(e) =>
                setPinPolicy(e.target.value as "default" | PinSelectionPolicy)
              }
            >
              <option value="default">
                پیش‌فرض سراسری ({generation.pin_selection_policy})
              </option>
              <option value="deterministic">قطعی (پیشنهادی)</option>
              <option value="explicit">فقط پایه‌های صریح</option>
              <option value="llm">انتخاب ایجنت از گزینه‌های معتبر</option>
            </select>
          </label>
          <button type="submit" disabled={busy}>
            {busy ? "در حال ارسال…" : "اجرای خط لوله"}
          </button>
          {errorMsg && <p className="error-text">{errorMsg}</p>}
        </form>
      </section>

      <div className="columns">
        {/* لیست پروژه‌ها */}
        <section className="card">
          <h2>پروژه‌ها</h2>
          {projects.length === 0 && <p className="muted">هنوز پروژه‌ای ثبت نشده.</p>}
          <ul className="project-list">
            {projects.map((p) => (
              <li
                key={p.id}
                className={`project-item ${selectedId === p.id ? "selected" : ""}`}
                onClick={() => {
                  setSelectedId(p.id);
                  refreshDetail(p.id);
                }}
              >
                <div className="project-row">
                  <strong>{p.name}</strong>
                  <StatusBadge status={p.status} />
                </div>
                <div className="project-row muted small">
                  <span>{TYPE_FA[p.request_type] ?? p.request_type}</span>
                  <span>{new Date(p.created_at + "Z").toLocaleTimeString("fa-IR")}</span>
                  <span>{p.pin_selection_policy}</span>
                </div>
              </li>
            ))}
          </ul>
        </section>

        {/* جزئیات پروژه */}
        <section className="card">
          <h2>جزئیات اجرا</h2>
          {!detail && <p className="muted">یک پروژه را از لیست انتخاب کن.</p>}
          {detail && (
            <div className="detail">
              <div className="project-row">
                <strong>{detail.name}</strong>
                <StatusBadge status={detail.status} />
              </div>
              <p className="muted small">{detail.user_request}</p>
              {detail.error && <p className="error-text">{detail.error}</p>}

              <table className="table">
                <thead>
                  <tr>
                    <th>ایجنت</th>
                    <th>وضعیت</th>
                    <th>خروجی</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.tasks.map((t) => (
                    <tr key={t.agent_name}>
                      <td>
                        <code>{t.agent_name}</code>
                      </td>
                      <td>
                        <StatusBadge status={t.status} />
                      </td>
                      <td className="result-cell">
                        {t.error ? (
                          <span className="error-text">{t.error}</span>
                        ) : (
                          <code>{t.result ?? "—"}</code>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {(detail.status === "pending" || detail.status === "running") && (
                <button className="danger" onClick={() => cancelProject(detail.id)}>
                  لغو اجرا
                </button>
              )}
            </div>
          )}
        </section>
      </div>

      {/* تنظیمات ایجنت‌ها */}
      <section className="card">
        <div className="settings-heading">
          <h2>تنظیمات ایجنت‌ها</h2>
          <label className="inline-setting">
            سیاست سراسری پایه
            <select
              value={generation.pin_selection_policy}
              onChange={(e) =>
                saveGenerationPolicy(e.target.value as PinSelectionPolicy)
              }
            >
              <option value="deterministic">قطعی (پیشنهادی)</option>
              <option value="explicit">فقط پایه‌های صریح</option>
              <option value="llm">انتخاب ایجنت از گزینه‌های معتبر</option>
            </select>
          </label>
        </div>
        <p className="muted small">
          مدل هر ایجنت از دیتابیس خوانده می‌شود؛ خالی‌گذاشتن یعنی استفاده از مدل پیش‌فرض
          (LLM_MODEL).
        </p>
        <table className="table">
          <thead>
            <tr>
              <th>ایجنت</th>
              <th>مدل مؤثر</th>
              <th>مدل اختصاصی</th>
              <th>فعال</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => (
              <tr key={a.agent_name} className={a.enabled ? "" : "disabled-row"}>
                <td>
                  <code>{a.agent_name}</code>
                </td>
                <td>
                  <code>{a.model}</code>{" "}
                  {a.is_override && <span className="badge badge-running">override</span>}
                </td>
                <td className="model-cell">
                  <input
                    dir="ltr"
                    value={modelEdits[a.agent_name] ?? ""}
                    onChange={(e) =>
                      setModelEdits((m) => ({ ...m, [a.agent_name]: e.target.value }))
                    }
                    placeholder={a.is_override ? "خالی = حذف override" : a.model}
                  />
                  <button onClick={() => saveAgentModel(a.agent_name)}>ذخیره</button>
                </td>
                <td>
                  <input
                    type="checkbox"
                    checked={a.enabled}
                    onChange={() => toggleAgent(a)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
