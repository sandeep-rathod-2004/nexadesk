import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import MarkdownMessage from "./MarkdownMessage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MarkdownMessage", () => {
  it("renders common Markdown and GitHub-flavored tables", () => {
    const { container } = render(
      <MarkdownMessage
        content={[
          "# Heading",
          "",
          "**bold** and *italic*",
          "",
          "1. First",
          "2. Second",
          "",
          "- Alpha",
          "- Beta",
          "",
          "> A quoted note",
          "",
          "| Name | Value |",
          "| --- | --- |",
          "| Item | 42 |",
          "",
          "[Safe link](https://example.com)"
        ].join("\n")}
      />
    );

    expect(container.querySelector("h1")?.textContent).toBe("Heading");
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(container.querySelector("em")?.textContent).toBe("italic");
    expect(container.querySelector("ol li")?.textContent).toBe("First");
    expect(container.querySelector("ul li")?.textContent).toBe("Alpha");
    expect(container.querySelector("blockquote")?.textContent?.trim()).toBe("A quoted note");
    expect(container.querySelector("table tbody td")?.textContent).toBe("Item");
    expect(screen.getByRole("link", { name: "Safe link" }).getAttribute("href"))
      .toBe("https://example.com");
  });

  it("highlights fenced code and provides a copy button", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText }
    });
    const { container } = render(
      <MarkdownMessage content={"```js\nconst answer = 42;\n```"} />
    );

    expect(container.querySelector(".markdown-code-toolbar")?.textContent).toContain("js");
    expect(container.querySelector("code.hljs")).toBeTruthy();
    expect(container.querySelector(".hljs-keyword")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Copy code" }));
    expect(writeText).toHaveBeenCalledWith("const answer = 42;\n");
    const copyButton = await screen.findByRole("button", { name: "Copy code" });
    expect(copyButton.textContent).toBe("Copied");
  });

  it("does not render raw HTML or allow unsafe links", () => {
    const { container } = render(
      <MarkdownMessage
        content={'<img src=x onerror="alert(1)" />\n\n<script>alert(1)</script>\n\n[bad](javascript:alert%281%29)'}
      />
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.querySelector("a[href^='javascript:']")).toBeNull();
    expect(container.querySelector(".markdown-link-text")?.textContent).toBe("bad");
  });

  it("renders a progressively incomplete fenced code block", () => {
    const { container } = render(
      <MarkdownMessage content={"```python\nprint('still streaming')"} />
    );

    expect(container.querySelector("pre code")?.textContent).toBe("print('still streaming')\n");
    expect(screen.getByRole("button", { name: "Copy code" })).toBeTruthy();
  });
});
