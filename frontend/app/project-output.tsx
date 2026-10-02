"use client";

import { useCallback, useEffect, useState } from "react";

// --------------------------------------------------------------------------
// API shapes (backend/app/api/routes/delivery.py)
// --------------------------------------------------------------------------

export type ProjectFile = {
  path: string;
  kind: string;
  size_bytes: number;
  generated: boolean;
};

export type Diagnostic = {
  file: string;
  line: number;
  column: number;
  severity: string;
  code: string;
  message: string;
  tool: string;
};

export type BuildResult = {
  status: string;
  exit_code: number;
  duration_ms: number;
  toolchain: string;
  attempt: number;
  size: { text: number; data: number; bss: number; flash_total: number; ram_total: number };
  diagnostics: Diagnostic[];
  log_tail: string;
};

export type BuildView = {
  latest: {
    attempt: number;
    status: string;
    error: string | null;
    result: BuildResult | null;
  };
  attempts: {
    attempt: number;
    status: string;
    build_status: string | null;
    errors: number | null;
    manual: boolean;
  }[];
};

export type TreeNode = {
  name: string;
  path: string;
  file?: ProjectFile;
  children: TreeNode[];
};

// --------------------------------------------------------------------------
// Pure helpers (unit-tested)
// --------------------------------------------------------------------------

/** Fold the flat file list into folders first, then files, both sorted. */
export function buildTree(files: ProjectFile[]): TreeNode {
  const root: TreeNode = { name: "", path: "", children: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;
    parts.forEach((part, index) => {
      const path = parts.slice(0, index + 1).join("/");
      let child = node.children.find((c) => c.name === part);
      if (!child) {
        child = { name: part, path, children: [] };
        node.children.push(child);
      }
      if (index === parts.length - 1) child.file = file;
      node = child;
    });
  }
  const sort = (node: TreeNode) => {
    node.children.sort((a, b) => {
      const aFile = a.file ? 1 : 0;
      const bFile = b.file ? 1 : 0;
      return aFile - bFile || a.name.localeCompare(b.name);
    });
    node.children.forEach(sort);
  };
  sort(root);
  return root;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** "used / total (pct%)", or just "used" for a part with no capacity row. */
export function memoryUsage(used: number, total: number): string {
  if (!total) return formatBytes(used);
  const pct = ((100 * used) / total).toFixed(1);
  return `${formatBytes(used)} / ${formatBytes(total)} (${pct}%)`;
}

export function diagnosticLabel(d: Diagnostic): string {
  const where = d.file ? `${d.file}${d.line ? `:${d.line}` : ""}` : "<link>";
  return `${where}: ${d.message}${d.code ? ` [${d.code}]` : ""}`;
}

const BUILD_FA: Record<string, string> = {
  ok: "موفق",
  failed: "ناموفق",
  timeout: "تمام شدن زمان",
  unavailable: "سندباکس در دسترس نیست",
};

const BUILD_BADGE: Record<string, string> = {
  ok: "done",
  failed: "failed",
  timeout: "cancelled",
  unavailable: "pending",
};

// --------------------------------------------------------------------------
// Components
// --------------------------------------------------------------------------

function Tree({
  node,
  selected,
  onSelect,
}: {
  node: TreeNode;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  return (
    <ul className="file-tree">
      {node.children.map((child) =>
        child.file ? (
          <li key={child.path}>
            <button
              className={`file-entry ${selected === child.path ? "selected" : ""}`}
              onClick={() => onSelect(child.path)}
              title={formatBytes(child.file.size_bytes)}
            >
              {child.name}
              {child.file.generated && <span className="file-tag">AI</span>}
            </button>
          </li>
        ) : (
          <li key={child.path}>
            <details open={child.path.split("/").length < 3}>
              <summary>{child.name}/</summary>
              <Tree node={child} selected={selected} onSelect={onSelect} />
            </details>
          </li>
        ),
      )}
    </ul>
  );
}

function BuildPanel({
  apiUrl,
  projectId,
  view,
  canRebuild,
  onRebuilt,
}: {
  apiUrl: string;
  projectId: string;
  view: BuildView;
  canRebuild: boolean;
  onRebuilt: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { latest } = view;
  const result = latest.result;
  const active = latest.status === "pending" || latest.status === "running";

  async function rebuild() {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(`${apiUrl}/projects/${projectId}/build`, { method: "POST" });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail ?? `HTTP ${r.status}`);
      }
      onRebuilt();
    } catch (err) {
      setError(`build دوباره ناموفق بود: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  }

  const errors = result?.diagnostics.filter((d) => d.severity === "error") ?? [];
  const warnings = result?.diagnostics.filter((d) => d.severity === "warning") ?? [];

  return (
    <div className="build-panel">
      <div className="project-row">
        <strong>Build — تلاش {latest.attempt}</strong>
        {active ? (
          <span className={`badge badge-${latest.status}`}>در حال build…</span>
        ) : result ? (
          <span className={`badge badge-${BUILD_BADGE[result.status] ?? "pending"}`}>
            {BUILD_FA[result.status] ?? result.status}
          </span>
        ) : (
          <span className="badge badge-failed">بدون نتیجه</span>
        )}
      </div>
      {latest.error && <p className="error-text">{latest.error}</p>}

      {result && (
        <div className="build-stats small">
          <span>
            Flash: {memoryUsage(result.size.text + result.size.data, result.size.flash_total)}
          </span>
          <span>
            RAM: {memoryUsage(result.size.data + result.size.bss, result.size.ram_total)}
          </span>
          <span>
            {errors.length} خطا · {warnings.length} هشدار
          </span>
          {result.duration_ms > 0 && <span>{(result.duration_ms / 1000).toFixed(1)}s</span>}
          {result.toolchain && <code>{result.toolchain}</code>}
        </div>
      )}

      {(errors.length > 0 || warnings.length > 0) && (
        <ul className="diagnostics">
          {[...errors, ...warnings].map((d, i) => (
            <li key={i} className={`diag diag-${d.severity}`}>
              {diagnosticLabel(d)}
            </li>
          ))}
        </ul>
      )}

      {result?.log_tail && (
        <details className="build-log">
          <summary>لاگ build</summary>
          <pre className="code-block">{result.log_tail}</pre>
        </details>
      )}

      {view.attempts.length > 1 && (
        <p className="muted small">
          تلاش‌ها:{" "}
          {view.attempts
            .slice()
            .reverse()
            .map(
              (a) =>
                `#${a.attempt} ${a.build_status ?? a.status}${a.manual ? " (دستی)" : ""}`,
            )
            .join(" ← ")}
        </p>
      )}

      <div className="button-row">
        <button onClick={rebuild} disabled={!canRebuild || active || busy}>
          {busy ? "در حال ارسال…" : "build دوباره"}
        </button>
        <a className="nav-link" href={`${apiUrl}/projects/${projectId}/download`}>
          دانلود zip
        </a>
        <a
          className="nav-link"
          href={`${apiUrl}/projects/${projectId}/download?include_binaries=true`}
        >
          zip + باینری
        </a>
      </div>
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}

/**
 * Files, code viewer and build panel for one project.
 *
 * `version` changes whenever the project's task list changes (the parent
 * polls it), which is when files or the build result can have changed.
 */
export default function ProjectOutput({
  apiUrl,
  projectId,
  projectStatus,
  version,
}: {
  apiUrl: string;
  projectId: string;
  projectStatus: string;
  version: string;
}) {
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [build, setBuild] = useState<BuildView | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const [f, b] = await Promise.all([
        fetch(`${apiUrl}/projects/${projectId}/files`),
        fetch(`${apiUrl}/projects/${projectId}/build`),
      ]);
      if (f.ok) setFiles((await f.json()).files);
      setBuild(b.ok ? await b.json() : null);
    } catch {
      /* backend unreachable — the health badge already shows it */
    }
  }, [apiUrl, projectId]);

  useEffect(() => {
    refresh();
  }, [refresh, version, tick]);

  // A manual rebuild does not change the pipeline's task list, so poll here
  // until it finishes.
  const building = build?.latest.status === "pending" || build?.latest.status === "running";
  useEffect(() => {
    if (!building) return;
    const t = setInterval(() => setTick((n) => n + 1), 2500);
    return () => clearInterval(t);
  }, [building]);

  useEffect(() => {
    setSelected(null);
    setContent(null);
  }, [projectId]);

  async function open(path: string) {
    setSelected(path);
    setContent("…");
    try {
      const r = await fetch(`${apiUrl}/projects/${projectId}/files/${path}`);
      const type = r.headers.get("content-type") ?? "";
      if (!r.ok) setContent(`HTTP ${r.status}`);
      else if (type.startsWith("text/")) setContent(await r.text());
      else setContent("(فایل باینری — از دانلود zip استفاده کن)");
    } catch (err) {
      setContent(String(err));
    }
  }

  if (files.length === 0 && !build) {
    return <p className="muted small">هنوز فایلی تولید نشده.</p>;
  }

  const canRebuild = projectStatus !== "pending" && projectStatus !== "running";

  return (
    <div className="project-output">
      {build && (
        <BuildPanel
          apiUrl={apiUrl}
          projectId={projectId}
          view={build}
          canRebuild={canRebuild && files.length > 0}
          onRebuilt={() => setTick((n) => n + 1)}
        />
      )}
      {files.length > 0 && (
        <div className="file-browser">
          <nav className="file-pane">
            <Tree node={buildTree(files)} selected={selected} onSelect={open} />
          </nav>
          <div className="code-pane">
            {selected ? (
              <>
                <code className="small">{selected}</code>
                <pre className="code-block code-view">{content}</pre>
              </>
            ) : (
              <p className="muted small">یک فایل را انتخاب کن.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
