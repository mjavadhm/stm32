/**
 * The answer renderer is where an LLM's output meets the DOM, so it gets
 * tested on two axes: does every markdown construct the model actually emits
 * render, and does nothing hostile in that text reach the page.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnswerBody, citationLabel } from "../app/chat/answer";

// Renders accumulate in document.body otherwise, so `screen` would see the
// previous test's output.
afterEach(cleanup);

const TEXT_CITATION = "hal-mini/Src/stm32f4xx_hal_spi.c:1643-1743";
const PAGE_CITATION = "07c66e7d-879c-4fb3-a7a4-9a8df381c613#p213";

function draw(text: string) {
  const onCitation = vi.fn();
  const { container } = render(
    <AnswerBody text={text} onCitation={onCitation} />
  );
  return { container, onCitation };
}

describe("markdown rendering", () => {
  it("renders headings instead of literal hashes", () => {
    const { container } = draw("## HAL_SPI_Transmit_DMA\n\nمتن");
    expect(container.querySelector("h2")?.textContent).toBe(
      "HAL_SPI_Transmit_DMA"
    );
    expect(container.textContent).not.toContain("##");
  });

  it("renders lists, including nested and ordered ones", () => {
    const { container } = draw(
      "- یک\n  - تودرتو\n- دو\n\n1. اول\n2. دوم"
    );
    expect(container.querySelectorAll("ul").length).toBe(2); // outer + nested
    expect(container.querySelector("ol")).not.toBeNull();
    expect(container.textContent).not.toMatch(/^- /m);
  });

  it("renders GFM tables in their own scroll container", () => {
    const { container } = draw(
      "| رجیستر | بیت |\n| --- | --- |\n| SPI_CR2 | TXDMAEN |"
    );
    expect(container.querySelector(".table-scroll table")).not.toBeNull();
    expect(container.querySelector("th")?.textContent).toBe("رجیستر");
    expect(container.querySelector("td")?.textContent).toBe("SPI_CR2");
  });

  it("renders task lists, block quotes and rules", () => {
    const { container } = draw("- [x] شد\n- [ ] نشد\n\n> نقل\n\n---");
    expect(container.querySelectorAll('input[type="checkbox"]').length).toBe(2);
    expect(container.querySelector("blockquote")).not.toBeNull();
    expect(container.querySelector("hr")).not.toBeNull();
  });

  it("highlights fenced code and forces it left-to-right", () => {
    const { container } = draw(
      "```c\nHAL_StatusTypeDef HAL_SPI_Transmit(SPI_HandleTypeDef *hspi);\n```"
    );
    const code = container.querySelector("pre code");
    expect(code?.className).toMatch(/hljs/);
    expect(container.querySelector("pre")?.getAttribute("dir")).toBe("ltr");
    // The language token is highlighted, not printed as part of the code.
    expect(code?.textContent).not.toContain("```");
  });

  it("renders inline code, emphasis and strikethrough", () => {
    const { container } = draw("`SPI_CR2` و **پررنگ** و ~~حذف~~");
    expect(container.querySelector("code")?.textContent).toBe("SPI_CR2");
    expect(container.querySelector("strong")?.textContent).toBe("پررنگ");
    expect(container.querySelector("del")?.textContent).toBe("حذف");
  });
});

describe("citations", () => {
  it("turns a text citation into a button that reports the full token", async () => {
    const { container, onCitation } = draw(`این تابع [${TEXT_CITATION}] است.`);
    const button = container.querySelector("button.citation-link");
    expect(button).not.toBeNull();
    expect(button?.getAttribute("title")).toBe(TEXT_CITATION);
    // The label is shortened, but the click carries the exact citation --
    // that string is what /rag/source resolves.
    expect(button?.textContent).toBe("stm32f4xx_hal_spi.c:1643-1743");
    (button as HTMLButtonElement).click();
    expect(onCitation).toHaveBeenCalledWith(TEXT_CITATION);
  });

  it("turns a page citation into a button too", () => {
    const { container, onCitation } = draw(`صفحه [${PAGE_CITATION}] را ببین.`);
    const button = container.querySelector("button.citation-link");
    expect(button?.getAttribute("title")).toBe(PAGE_CITATION);
    (button as HTMLButtonElement).click();
    expect(onCitation).toHaveBeenCalledWith(PAGE_CITATION);
  });

  it("finds citations inside list items and headings", () => {
    const { container } = draw(
      `- مورد [${TEXT_CITATION}]\n\n## سر [${PAGE_CITATION}]`
    );
    expect(container.querySelectorAll("button.citation-link").length).toBe(2);
  });

  it("leaves a bracketed token inside a code fence alone", () => {
    const { container } = draw(`\`\`\`\nsee [${TEXT_CITATION}] here\n\`\`\``);
    expect(container.querySelector("button.citation-link")).toBeNull();
    expect(container.querySelector("pre")?.textContent).toContain(
      TEXT_CITATION
    );
  });

  it("labels a bare-uuid page citation readably", () => {
    expect(citationLabel(PAGE_CITATION)).toBe("page 213");
    expect(citationLabel(TEXT_CITATION)).toBe("stm32f4xx_hal_spi.c:1643-1743");
  });
});

describe("untrusted model output", () => {
  it("escapes raw HTML instead of executing it", () => {
    const { container } = draw(
      '<img src=x onerror="alert(1)">\n\n<script>alert(2)</script>'
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    // The text survives as text; what matters is that no element carries it
    // as an attribute.
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.textContent).toContain("onerror");
  });

  it("does not turn a javascript: link into a citation button", () => {
    const { container } = draw("[کلیک](javascript:alert(1))");
    expect(container.querySelector("button.citation-link")).toBeNull();
    expect(container.innerHTML).not.toContain("javascript:alert");
  });

  it("opens ordinary links in a new tab without referrer leakage", () => {
    const { container } = draw("[سند](https://example.com/rm0090.pdf)");
    const link = container.querySelector("a");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toContain("noreferrer");
  });
});

describe("partial answers", () => {
  it("marks a turn whose stream was cut short", () => {
    render(<AnswerBody text="نیمه" onCitation={() => {}} partial />);
    expect(screen.getByText(/ناتمام/)).toBeTruthy();
  });

  it("says nothing about completeness for a finished turn", () => {
    render(<AnswerBody text="کامل" onCitation={() => {}} />);
    expect(screen.queryByText(/ناتمام/)).toBeNull();
  });
});
