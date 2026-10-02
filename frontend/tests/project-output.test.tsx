import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ProjectOutput, {
  type BuildView,
  type ProjectFile,
  buildTree,
  diagnosticLabel,
  memoryUsage,
} from "../app/project-output";

const FILES: ProjectFile[] = [
  { path: "Makefile", kind: "source", size_bytes: 10, generated: false },
  { path: "Core/Src/main.c", kind: "source", size_bytes: 200, generated: true },
  { path: "Core/Inc/main.h", kind: "source", size_bytes: 50, generated: false },
  { path: "demo.ioc", kind: "ioc", size_bytes: 30, generated: false },
];

const FAILED_BUILD: BuildView = {
  latest: {
    attempt: 3,
    status: "done",
    error: null,
    result: {
      status: "failed",
      exit_code: 2,
      duration_ms: 4200,
      toolchain: "arm-none-eabi-gcc 12.2.1",
      attempt: 3,
      size: { text: 0, data: 0, bss: 0, flash_total: 1048576, ram_total: 131072 },
      diagnostics: [
        {
          file: "Core/Src/main.c",
          line: 42,
          column: 5,
          severity: "error",
          code: "",
          message: "'hspi1' undeclared",
          tool: "gcc",
        },
      ],
      log_tail: "make: *** [all] Error 2",
    },
  },
  attempts: [
    { attempt: 3, status: "done", build_status: "failed", errors: 1, manual: false },
    { attempt: 2, status: "done", build_status: "failed", errors: 2, manual: false },
    { attempt: 1, status: "done", build_status: "failed", errors: 4, manual: false },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("buildTree", () => {
  it("puts folders before files and nests paths", () => {
    const root = buildTree(FILES);

    expect(root.children.map((c) => c.name)).toEqual(["Core", "demo.ioc", "Makefile"]);
    const core = root.children[0];
    expect(core.children.map((c) => c.name)).toEqual(["Inc", "Src"]);
    expect(core.children[1].children[0].file?.generated).toBe(true);
    expect(core.children[1].children[0].path).toBe("Core/Src/main.c");
  });
});

describe("formatting", () => {
  it("shows memory against device capacity when known", () => {
    expect(memoryUsage(12120, 1048576)).toBe("11.8 KB / 1.0 MB (1.2%)");
    expect(memoryUsage(512, 0)).toBe("512 B");
  });

  it("labels diagnostics like gcc does", () => {
    const d = FAILED_BUILD.latest.result!.diagnostics[0];
    expect(diagnosticLabel(d)).toBe("Core/Src/main.c:42: 'hspi1' undeclared");
    expect(diagnosticLabel({ ...d, file: "", line: 0, code: "-Wx" })).toBe(
      "<link>: 'hspi1' undeclared [-Wx]",
    );
  });
});

describe("ProjectOutput", () => {
  it("renders the build result, its errors and the file tree", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const body = url.endsWith("/files") ? { files: FILES } : FAILED_BUILD;
        return new Response(JSON.stringify(body), { status: 200 });
      }),
    );

    render(
      <ProjectOutput apiUrl="http://api" projectId="p1" projectStatus="done" version="v1" />,
    );

    await waitFor(() => screen.getByText("Build — تلاش 3"));
    screen.getByText("ناموفق");
    screen.getByText("Core/Src/main.c:42: 'hspi1' undeclared");
    screen.getByText("Makefile");
    screen.getByText("Core/");
    const link = screen.getByText("دانلود zip") as HTMLAnchorElement;
    expect(link.href).toBe("http://api/projects/p1/download");
  });
});
