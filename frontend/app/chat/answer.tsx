"use client";

/**
 * Rendering an agent answer.
 *
 * Two jobs that pull against each other:
 *
 *  1. Full markdown. The model emits headings, tables, nested lists, task
 *     lists and fenced code, so this uses react-markdown + remark-gfm
 *     rather than the hand-rolled splitter it replaces (which honoured only
 *     `**bold**` and fences, and showed `##` and `-` as literal text).
 *  2. Clickable citations. `[path:1-2]` and `[<uuid>#p7]` tokens must become
 *     buttons that open the source viewer.
 *
 * Citations are handled as a *remark plugin* on the mdast, not by regex over
 * the rendered HTML: by the time markdown is parsed, a path like
 * `stm32f4xx_hal_spi.c:1643-1743` may already have been split across nodes,
 * and patching HTML back together is how XSS gets in. Rewriting text nodes
 * before rendering keeps react-markdown's escaping intact -- nothing here
 * ever touches `dangerouslySetInnerHTML`.
 */

import type { Element, Root as HastRoot } from "hast";
import type { Root, Text } from "mdast";
import { ReactNode, memo } from "react";
import Markdown, { defaultUrlTransform } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { visit } from "unist-util-visit";

/**
 * A citation token as the answer prompt requires it to be written:
 *   [hal-mini/Src/stm32f4xx_hal_spi.c:1643-1743]   a text chunk
 *   [07c66e7d-...-c8a2008e3cd7#p213]               a manual page
 */
const CITATION_RE = /\[([^\]\s]+?(?::\d+-\d+|#p\d+))\]/g;

/** Split a citation into its display parts. */
export function citationLabel(citation: string): string {
  const page = citation.match(/^(.+)#p(\d+)$/);
  if (page) {
    const name = page[1].split("/").pop() || page[1];
    // A bare document uuid is noise in a label; "page 213" is the useful part.
    return isUuid(name) ? `page ${page[2]}` : `${name} p${page[2]}`;
  }
  const at = citation.lastIndexOf(":");
  const lines = at === -1 ? "" : citation.slice(at + 1);
  if (!/^\d+-\d+$/.test(lines)) return citation.split("/").pop() || citation;
  const path = citation.slice(0, at);
  return `${path.split("/").pop() || path}:${lines}`;
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    value
  );
}

/**
 * Rewrite `[citation]` text into a link carrying a `citation:` URL.
 *
 * A link node is used rather than a custom node type so every downstream
 * plugin still sees a well-formed tree; the `a` renderer below turns it into
 * a button. Code spans and code blocks are skipped: inside a fence, brackets
 * are the code's own, not a reference.
 */
function remarkCitations() {
  return (tree: Root) => {
    visit(tree, "text", (node: Text, index, parent) => {
      if (!parent || index === null || index === undefined) return;
      if (parent.type === "link" || parent.type === "linkReference") return;

      // A fresh regex per node: the /g one is stateful, and sharing `lastIndex`
      // across nodes would skip citations at random.
      const pattern = new RegExp(CITATION_RE.source, "g");
      const children: Root["children"] = [];
      let cursor = 0;
      let match: RegExpExecArray | null;
      while ((match = pattern.exec(node.value)) !== null) {
        if (match.index > cursor) {
          children.push({
            type: "text",
            value: node.value.slice(cursor, match.index),
          } as Text);
        }
        children.push({
          type: "link",
          url: `citation:${encodeURIComponent(match[1])}`,
          title: match[1],
          children: [{ type: "text", value: match[1] } as Text],
        } as never);
        cursor = match.index + match[0].length;
      }
      if (children.length === 0) return;
      if (cursor < node.value.length) {
        children.push({ type: "text", value: node.value.slice(cursor) } as Text);
      }

      parent.children.splice(index, 1, ...(children as never[]));
      return index + children.length; // skip what was just inserted
    });
  };
}

/**
 * Force `dir="ltr"` on code, tables and anything else where mixing an RTL
 * paragraph direction with Latin identifiers reorders characters on screen.
 * Done on the hast so it applies to whatever markdown produced the node.
 */
function rehypeLtrCode() {
  return (tree: HastRoot) => {
    visit(tree, "element", (node: Element) => {
      if (["pre", "code", "table"].includes(node.tagName)) {
        node.properties = { ...node.properties, dir: "ltr" };
      }
    });
  };
}

export type AnswerBodyProps = {
  text: string;
  onCitation: (citation: string) => void;
  /** Marks an answer whose stream was cut short. */
  partial?: boolean;
};

function AnswerBodyImpl({ text, onCitation, partial }: AnswerBodyProps) {
  return (
    <div className={`answer markdown ${partial ? "answer-partial" : ""}`}>
      <Markdown
        remarkPlugins={[remarkGfm, remarkCitations]}
        rehypePlugins={[rehypeLtrCode, [rehypeHighlight, { detect: true }]]}
        // The default transform strips unknown protocols (its job: blocking
        // `javascript:`), which would silently delete every `citation:` URL
        // this component depends on. Allow that one scheme, defer on the rest.
        urlTransform={(url) =>
          url.startsWith("citation:") ? url : defaultUrlTransform(url)
        }
        // Links are the only thing intercepted; everything else renders as
        // react-markdown's own safe output.
        components={{
          a({ href, children, ...props }) {
            if (href?.startsWith("citation:")) {
              const citation = decodeURIComponent(href.slice("citation:".length));
              return (
                <button
                  type="button"
                  className="citation-link"
                  dir="ltr"
                  title={citation}
                  onClick={() => onCitation(citation)}
                >
                  {citationLabel(citation)}
                </button>
              );
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                {children as ReactNode}
              </a>
            );
          },
          // Tables can overflow a chat bubble; give them their own scroller.
          table({ children, ...props }) {
            return (
              <div className="table-scroll">
                <table {...props}>{children as ReactNode}</table>
              </div>
            );
          },
        }}
      >
        {text}
      </Markdown>
      {partial && (
        <p className="warning-text small">پاسخ ناتمام ماند (جریان قطع شد).</p>
      )}
    </div>
  );
}

/**
 * Memoised on the text: a streaming turn re-renders on every token, and
 * re-parsing the markdown of an unchanged earlier message is wasted work.
 */
export const AnswerBody = memo(AnswerBodyImpl);
