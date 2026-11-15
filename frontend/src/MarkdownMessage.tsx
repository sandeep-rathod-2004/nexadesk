import { ReactNode, useState } from "react";
import ReactMarkdown, { Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import remarkGfm from "remark-gfm";

function safeMarkdownUrl(url: string | undefined): string | null {
  if (!url) return null;
  const normalized = url.trim();
  if (!normalized || normalized.startsWith("//") || normalized.startsWith("\\\\")) {
    return null;
  }
  const scheme = normalized.match(/^([a-z][a-z\d+.-]*):/i)?.[1]?.toLowerCase();
  if (scheme && !["http", "https", "mailto"].includes(scheme)) return null;
  return normalized;
}

function textContent(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textContent).join("");
  if (node && typeof node === "object" && "props" in node) {
    return textContent((node.props as { children?: ReactNode }).children);
  }
  return "";
}

type CodeBlockProps = {
  children?: ReactNode;
};

function CodeBlock({ children }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const codeElement = Array.isArray(children) ? children[0] : children;
  const className =
    codeElement && typeof codeElement === "object" && "props" in codeElement
      ? (codeElement.props as { className?: string }).className ?? ""
      : "";
  const language = className.match(/language-([\w-]+)/)?.[1] ?? "text";

  async function copyCode() {
    try {
      await navigator.clipboard.writeText(textContent(children));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="markdown-code-block">
      <div className="markdown-code-toolbar">
        <span>{language}</span>
        <button type="button" onClick={() => void copyCode()} aria-label="Copy code">
          {copied ? "Copied" : "Copy code"}
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

const components: Components = {
  a({ href, children, ...props }) {
    const safeHref = safeMarkdownUrl(href);
    if (!safeHref) return <span className="markdown-link-text">{children}</span>;
    return (
      <a href={safeHref} rel="noreferrer noopener" {...props}>
        {children}
      </a>
    );
  },
  pre({ children }) {
    return <CodeBlock>{children}</CodeBlock>;
  }
};

function markdownUrlTransform(url: string): string {
  return safeMarkdownUrl(url) ?? "";
}

export default function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[
          rehypeHighlight,
          { languages: { bash, css, javascript, json, python, sql, typescript, xml, yaml } }
        ]]}
        components={components}
        urlTransform={markdownUrlTransform}
        skipHtml
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
