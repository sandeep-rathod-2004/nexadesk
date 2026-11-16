import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthResult } from "./lib/api";

const api = vi.hoisted(() => ({
  register: vi.fn(),
  login: vi.fn(),
  refreshSession: vi.fn(),
  setAccessToken: vi.fn(),
  logout: vi.fn(),
  getModel: vi.fn(),
  listConversations: vi.fn(),
  getMessages: vi.fn(),
  createConversation: vi.fn(),
  updateConversationDocuments: vi.fn(),
  renameConversation: vi.fn(),
  deleteConversation: vi.fn(),
  cancelChat: vi.fn(),
  streamChat: vi.fn(),
  listKnowledgeDocuments: vi.fn(),
  uploadKnowledgeDocument: vi.fn(),
  deleteKnowledgeDocument: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  }
}));

vi.mock("./lib/api", () => api);

import App from "./App";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const access = {
  access_token: "in-memory-token",
  token_type: "bearer" as const,
  user: { id: "user-1", email: "person@example.com" },
  workspaces: [{ id: "workspace-1", name: "Workspace One", role: "owner" }]
};
const firstConversation = {
  id: "conversation-1",
  workspace_id: "workspace-1",
  title: "Planning notes",
  chat_type: "normal" as const,
  document_ids: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z"
};

async function signIn(user = userEvent.setup(), openNavigation = true) {
  window.history.replaceState(null, "", "/login");
  api.login.mockResolvedValue(access);
  render(<App />);
  await screen.findByLabelText("Email");
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.com" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a sufficiently long password" } });
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByRole("heading", { name: "Welcome to NexaDesk" });
  if (openNavigation) {
    await user.click(screen.getByRole("button", { name: "Open sidebar" }));
    await screen.findByRole("button", { name: /New conversation/ });
  }
  return user;
}

describe("conversation management UI", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/login");
    window.sessionStorage.clear();
    window.localStorage.clear();
    api.refreshSession.mockRejectedValue(new api.ApiError("No session", 401));
    api.logout.mockResolvedValue(undefined);
    api.listConversations.mockResolvedValue([]);
    api.listKnowledgeDocuments.mockResolvedValue([]);
    api.getMessages.mockResolvedValue([]);
    api.getModel.mockResolvedValue({ provider: "Ollama", model: "phi3:mini" });
    api.createConversation.mockResolvedValue(firstConversation);
    api.updateConversationDocuments.mockResolvedValue(firstConversation);
    api.renameConversation.mockImplementation(async (_token: string, _id: string, title: string) => ({
      ...firstConversation,
      title
    }));
    api.deleteConversation.mockResolvedValue(undefined);
  });

  describe("authentication pages", () => {
    beforeEach(() => {
      vi.clearAllMocks();
      window.history.replaceState(null, "", "/login");
      api.refreshSession.mockRejectedValue(new api.ApiError("No session", 401));
      api.logout.mockResolvedValue(undefined);
    });

    it("creates an account and returns to Login without establishing a session", async () => {
      const user = userEvent.setup();
      api.register.mockResolvedValue({
        user: { id: "user-1", email: "ada@example.com", full_name: "Ada Lovelace" },
        workspaces: access.workspaces
      });
      render(<App />);
      await screen.findByRole("button", { name: "Sign in" });
      await user.click(screen.getByRole("button", { name: "Create an account" }));
      fireEvent.change(screen.getByLabelText("Full name"), { target: { value: "Ada Lovelace" } });
      fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@example.com" } });
      fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct horse battery staple" } });
      fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "correct horse battery staple" } });
      await user.click(screen.getByRole("button", { name: "Create account" }));

      expect(await screen.findByText("Your account was created. Sign in with your new credentials.")).toBeTruthy();
      expect(screen.getByRole("heading", { name: "Welcome back" })).toBeTruthy();
      expect(api.register).toHaveBeenCalledWith(
        "Ada Lovelace",
        "ada@example.com",
        "correct horse battery staple",
        "My Workspace"
      );
      expect(api.login).not.toHaveBeenCalled();
      expect(api.getModel).not.toHaveBeenCalled();
      expect(window.location.pathname).toBe("/login");
    });

    it("shows the explicit unregistered-email login message", async () => {
      const user = userEvent.setup();
      api.login.mockRejectedValue(new api.ApiError(
        "This email is not registered. Please register first.",
        401
      ));
      render(<App />);
      await screen.findByLabelText("Email");
      fireEvent.change(screen.getByLabelText("Email"), {
        target: { value: "unknown@example.com" }
      });
      fireEvent.change(screen.getByLabelText("Password"), {
        target: { value: "some password" }
      });
      await user.click(screen.getByRole("button", { name: "Sign in" }));

      expect((await screen.findByRole("alert")).textContent).toBe(
        "This email is not registered. Please register first."
      );
    });

    it("shows a clear mismatch error without submitting registration", async () => {
      const user = userEvent.setup();
      render(<App />);
      await screen.findByRole("button", { name: "Sign in" });
      await user.click(screen.getByRole("button", { name: "Create an account" }));
      fireEvent.change(screen.getByLabelText("Full name"), { target: { value: "Ada Lovelace" } });
      fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@example.com" } });
      fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct horse battery staple" } });
      fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "a different password" } });
      await user.click(screen.getByRole("button", { name: "Create account" }));

      expect((await screen.findByRole("alert")).textContent).toContain("Passwords do not match.");
      expect(api.register).not.toHaveBeenCalled();
    });

    it("shows explicit unavailable states for password recovery pages", async () => {
      const user = userEvent.setup();
      render(<App />);
      await screen.findByRole("button", { name: "Sign in" });
      await user.click(screen.getByRole("button", { name: "Forgot Password?" }));
      expect(screen.getByRole("heading", { name: "Forgot password?" })).toBeTruthy();
      expect(screen.getByText(/Password recovery is not available yet/)).toBeTruthy();
      expect((screen.getByRole("button", { name: "Recovery unavailable" }) as HTMLButtonElement).disabled).toBe(true);

      await user.click(screen.getByRole("button", { name: "Back to Login" }));
      await user.click(screen.getByRole("button", { name: "Create an account" }));
      expect(screen.getByRole("heading", { name: "Create your account" })).toBeTruthy();
    });

    it("renders the reset-password page without pretending the reset is active", async () => {
      window.history.replaceState(null, "", "/reset-password");
      render(<App />);

      expect(await screen.findByRole("heading", { name: "Reset password" })).toBeTruthy();
      expect(screen.getByText(/Password recovery is not available yet/)).toBeTruthy();
      expect((screen.getByRole("button", { name: "Reset unavailable" }) as HTMLButtonElement).disabled).toBe(true);
    });
  });

  it("shows the welcome dashboard with Normal Conversation selected by default", async () => {
    const user = await signIn(userEvent.setup(), false);

    expect(screen.getByRole("heading", { name: "Welcome to NexaDesk" })).toBeTruthy();
    expect(screen.getByText(
      "Your AI workspace. What would you like to work on today?"
    )).toBeTruthy();
    expect(screen.queryByText(/Good morning|Good afternoon|Good evening/)).toBeNull();
    expect(screen.queryByText(/Hey there! I’m NexaDesk/)).toBeNull();
    expect(screen.queryByText("Explain a complex topic")).toBeNull();
    expect(screen.getByRole("button", { name: /Normal Conversation/ }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: /Knowledge Base/ }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByRole("button", { name: "Start Chat" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Message NexaDesk" })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Start Chat" }));
    await waitFor(() => expect(api.createConversation).toHaveBeenCalledTimes(1));
    expect(api.createConversation).toHaveBeenCalledWith("in-memory-token", "workspace-1");
    expect(screen.getByRole("heading", { name: "Welcome to NexaDesk" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Start Chat" })).toBeTruthy();
  });

  it("changes the selected chat mode and opens document selection for Knowledge Base", async () => {
    const user = await signIn(userEvent.setup(), false);
    const knowledgeMode = screen.getByRole("button", { name: /Knowledge Base/ });

    await user.click(knowledgeMode);

    expect(knowledgeMode.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: /Normal Conversation/ }).getAttribute("aria-pressed")).toBe("false");
    await user.click(screen.getByRole("button", { name: "Start Chat" }));

    expect(await screen.findByRole("dialog", { name: "Create a Knowledge Chat" })).toBeTruthy();
    expect(api.createConversation).not.toHaveBeenCalled();
  });

  it("creates a Knowledge Chat only after selecting a ready document", async () => {
    api.listKnowledgeDocuments.mockResolvedValue([{
      id: "document-1",
      workspace_id: "workspace-1",
      filename: "policy.pdf",
      media_type: "application/pdf",
      size_bytes: 100,
      status: "ready",
      error_message: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z"
    }]);
    const user = await signIn(userEvent.setup(), false);

    await user.click(screen.getByRole("button", { name: /Knowledge Base/ }));
    await user.click(screen.getByRole("button", { name: "Start Chat" }));
    await user.click(await screen.findByLabelText("policy.pdf"));
    const createKnowledgeChat = screen.getByRole("button", { name: "Create Knowledge Chat" });
    fireEvent.click(createKnowledgeChat);
    fireEvent.click(createKnowledgeChat);

    await waitFor(() => expect(api.createConversation).toHaveBeenCalledWith(
      "in-memory-token",
      "workspace-1",
      "New conversation",
      "knowledge",
      ["document-1"]
    ));
    expect(api.createConversation).toHaveBeenCalledTimes(1);
  });

  it("prevents duplicate Normal Chat creation on repeated Start Chat clicks", async () => {
    const user = await signIn(userEvent.setup(), false);
    const startButton = screen.getByRole("button", { name: "Start Chat" });

    fireEvent.click(startButton);
    fireEvent.click(startButton);

    await waitFor(() => expect(api.createConversation).toHaveBeenCalledTimes(1));
    expect(api.createConversation).toHaveBeenCalledWith("in-memory-token", "workspace-1");
  });

  it("opens and dismisses the mobile navigation drawer and first-use hint", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    const user = await signIn(userEvent.setup(), false);

    expect(screen.getByText("Open navigation — Explore your workspace features")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    expect(screen.queryByText("Open navigation — Explore your workspace features")).toBeNull();
    expect(screen.getByRole("button", { name: /New conversation/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Analytics (coming soon)" }).hasAttribute("disabled")).toBe(true);

    await user.click(screen.getByRole("button", { name: "Close navigation" }));
    expect(screen.getByRole("button", { name: "Open menu" }).getAttribute("aria-expanded")).toBe("false");
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Open menu" }).getAttribute("aria-expanded")).toBe("false");
    expect(window.localStorage.getItem("nexadesk.navigation-hint.user-1")).toBe("dismissed");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1024 });
    fireEvent(window, new Event("resize"));
  });

  it("collapses and reopens the desktop sidebar while preserving its preference", async () => {
    const user = await signIn(userEvent.setup(), false);
    const sidebar = screen.getByRole("complementary", {
      hidden: true
    });
    const toggle = screen.getByRole("button", { name: "Open sidebar" });

    expect(sidebar.getAttribute("aria-hidden")).toBe("true");
    await user.click(toggle);
    expect(sidebar.getAttribute("aria-hidden")).toBe("false");
    expect(screen.getByRole("region", { name: "Normal Chats" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Knowledge Chats" })).toBeTruthy();
    expect(window.localStorage.getItem("nexadesk.sidebar.user-1")).toBe("expanded");

    await user.click(within(screen.getByRole("banner")).getByRole("button", {
      name: "Collapse sidebar"
    }));
    expect(sidebar.getAttribute("aria-hidden")).toBe("true");
    expect(window.localStorage.getItem("nexadesk.sidebar.user-1")).toBe("collapsed");
    await user.click(screen.getByRole("button", { name: "Open sidebar" }));
    expect(sidebar.getAttribute("aria-hidden")).toBe("false");

    cleanup();
    api.refreshSession.mockResolvedValue(access);
    render(<App />);
    await screen.findByRole("heading", { name: "Welcome to NexaDesk" });
    expect(
      screen.getByRole("complementary", { name: "Workspace navigation" }).getAttribute("aria-hidden")
    ).toBe("false");
    expect(within(screen.getByRole("banner")).getByRole("button", {
      name: "Collapse sidebar"
    })).toBeTruthy();
  });

  it("shows the backend-configured Ollama model", async () => {
    await signIn();

    const modelBadge = await screen.findByRole("button", { name: "Configured AI model" });
    expect(modelBadge.textContent).toContain("Ollama · phi3:mini");
    expect(api.getModel).toHaveBeenCalledWith("in-memory-token");
  }, 10000);

  it("uses the configured Gemini provider in the dashboard and processing notice", async () => {
    api.getModel.mockResolvedValue({
      provider: "Gemini",
      model: "gemini-2.0-flash"
    });
    await signIn();

    expect(await screen.findByText("Messages are processed by your configured Gemini provider.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Configured AI model" }).textContent)
      .toContain("Gemini · gemini-2.0-flash");
  });

  it("shows provider information as unavailable when the model endpoint fails", async () => {
    api.getModel.mockRejectedValue(new Error("Service unavailable"));
    await signIn();

    expect(await screen.findByText("AI provider information is currently unavailable.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Configured AI model" }).textContent)
      .toContain("AI provider unavailable");
  });

  it("derives Getting Started progress from the workspace and ready documents", async () => {
    api.listKnowledgeDocuments.mockResolvedValue([{
      id: "doc-1",
      workspace_id: "workspace-1",
      filename: "guide.txt",
      media_type: "text/plain",
      size_bytes: 20,
      status: "ready",
      error_message: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z"
    }]);
    await signIn();

    const progress = await screen.findByRole("progressbar", {
      name: "Getting started progress"
    });
    await waitFor(() => expect(progress.getAttribute("aria-valuenow")).toBe("2"));
    expect(screen.getByText("2 of 4")).toBeTruthy();
    expect(screen.getByText("Coming soon · no saved customization is available")).toBeTruthy();
    expect(screen.getByText("Coming soon · installation is not available")).toBeTruthy();
    expect(api.listKnowledgeDocuments).toHaveBeenCalledWith(
      "in-memory-token",
      "workspace-1"
    );
  });

  it("creates conversations, loads history, renames and confirms deletion", async () => {
    const user = await signIn();
    await user.click(screen.getByRole("button", { name: /New conversation/ }));

    expect(api.createConversation).toHaveBeenCalledWith(
      "in-memory-token",
      "workspace-1"
    );
    const conversationButton = await screen.findByRole("button", { name: /^Planning notes$/ });
    await user.click(conversationButton);
    await waitFor(() => expect(api.getMessages).toHaveBeenCalledWith(
      "in-memory-token",
      "conversation-1"
    ));

    await user.click(screen.getByRole("button", { name: "Rename Planning notes" }));
    const titleInput = screen.getByRole("textbox", { name: "Conversation title" });
    await user.clear(titleInput);
    await user.type(titleInput, "Updated notes");
    await user.click(screen.getByRole("button", { name: "Save conversation name" }));
    expect(await screen.findByRole("button", { name: /^Updated notes$/ })).toBeTruthy();
    expect(api.renameConversation).toHaveBeenCalledWith(
      "in-memory-token",
      "conversation-1",
      "Updated notes"
    );

    await user.click(screen.getByRole("button", { name: "Delete Updated notes" }));
    expect(await screen.findByRole("alertdialog")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.deleteConversation).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Delete Updated notes" }));
    await user.click(screen.getByRole("button", { name: /^Delete$/ }));
    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith(
      "in-memory-token",
      "conversation-1"
    ));
    expect(await screen.findByText("No conversations yet. Start a new one.")).toBeTruthy();
  });

  it("creates a Knowledge Chat with only the selected ready documents", async () => {
    api.listKnowledgeDocuments.mockResolvedValue([
      {
        id: "document-1",
        workspace_id: "workspace-1",
        filename: "Travel policy.pdf",
        media_type: "application/pdf",
        size_bytes: 123,
        status: "ready",
        error_message: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z"
      },
      {
        id: "document-2",
        workspace_id: "workspace-1",
        filename: "Employee guide.pdf",
        media_type: "application/pdf",
        size_bytes: 456,
        status: "ready",
        error_message: null,
        created_at: "2026-01-02T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z"
      }
    ]);
    api.createConversation.mockResolvedValue({
      ...firstConversation,
      chat_type: "knowledge",
      document_ids: ["document-1"]
    });
    const user = await signIn();
    await user.click(screen.getByRole("button", { name: "New Knowledge Chat" }));
    await user.click(screen.getByLabelText("Travel policy.pdf"));
    await user.click(screen.getByRole("button", { name: "Create Knowledge Chat" }));

    await waitFor(() => expect(api.createConversation).toHaveBeenCalledWith(
      "in-memory-token",
      "workspace-1",
      "New conversation",
      "knowledge",
      ["document-1"]
    ));
    expect(await screen.findByRole("heading", { name: "Welcome to NexaDesk" })).toBeTruthy();
  });

  it("updates sources for an existing Knowledge Chat", async () => {
    const knowledgeConversation = {
      ...firstConversation,
      chat_type: "knowledge" as const,
      document_ids: ["document-1"]
    };
    api.listConversations.mockResolvedValue([knowledgeConversation]);
    api.listKnowledgeDocuments.mockResolvedValue([
      {
        id: "document-1",
        workspace_id: "workspace-1",
        filename: "Travel policy.pdf",
        media_type: "application/pdf",
        size_bytes: 123,
        status: "ready",
        error_message: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z"
      },
      {
        id: "document-2",
        workspace_id: "workspace-1",
        filename: "Employee guide.pdf",
        media_type: "application/pdf",
        size_bytes: 456,
        status: "ready",
        error_message: null,
        created_at: "2026-01-02T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z"
      }
    ]);
    api.updateConversationDocuments.mockResolvedValue({
      ...knowledgeConversation,
      document_ids: ["document-1", "document-2"]
    });
    const user = await signIn();
    await user.click(await screen.findByRole("button", { name: "Planning notes" }));
    await user.click(await screen.findByRole("button", { name: "Sources" }));
    await user.click(screen.getByLabelText("Employee guide.pdf"));
    await user.click(screen.getByRole("button", { name: "Save sources" }));

    await waitFor(() => expect(api.updateConversationDocuments).toHaveBeenCalledWith(
      "in-memory-token",
      "conversation-1",
      ["document-1", "document-2"]
    ));
  });

  it("loads previous messages and prevents duplicate chat submission", async () => {
    api.listConversations.mockResolvedValue([firstConversation]);
    const user = await signIn();
    api.getMessages.mockResolvedValue([{
      id: "message-1",
      role: "user",
      content: "Previously saved question",
      created_at: "2026-01-01T00:00:00Z"
    }]);
    api.streamChat.mockImplementation(async (
      _text: string,
      _token: string,
      _workspaceId: string,
      _conversationId: string | null,
      _requestId: string,
      _signal: AbortSignal,
      onToken: (token: string) => void
    ) => {
      onToken("Saved ");
      onToken("response");
      return {
        reply: "Saved response",
        model: "local",
        conversation_id: "conversation-1",
        grounded: false,
        sources: []
      };
    });

    await user.click(await screen.findByRole("button", { name: /^Planning notes$/ }));
    expect(await screen.findByText("Previously saved question")).toBeTruthy();
    await user.type(screen.getByRole("textbox", { name: "Message NexaDesk" }), "A new question");

    const form = screen.getByRole("textbox", { name: "Message NexaDesk" }).closest("form");
    if (!form) throw new Error("Chat form was not rendered.");
    fireEvent.submit(form);
    fireEvent.submit(form);

    await waitFor(() => expect(api.streamChat).toHaveBeenCalledTimes(1));
    expect(api.streamChat).toHaveBeenCalledWith(
      "A new question",
      "in-memory-token",
      "workspace-1",
      "conversation-1",
      expect.any(String),
      expect.any(AbortSignal),
      expect.any(Function),
      expect.any(Function)
    );
    expect(await screen.findByText("Saved response")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Configured AI model" }).textContent)
      .toContain("Ollama · phi3:mini");
  });

  it("streams tokens progressively and marks stopped output as interrupted", async () => {
    const user = await signIn();
    let finishStream: ((result: {
      reply: string;
      model: string;
      conversation_id: string;
      grounded: boolean;
      sources: [];
      status: "cancelled";
    }) => void) | undefined;
    api.streamChat.mockImplementation((
      _text: string, _token: string, _workspaceId: string,
      _conversationId: string | null, _requestId: string, _signal: AbortSignal,
      onToken: (token: string) => void
    ) => new Promise(resolve => {
      finishStream = resolve;
      onToken("Partial ");
    }));
    api.cancelChat.mockResolvedValue({ status: "cancellation_requested" });

    await user.type(screen.getByRole("textbox", { name: "Message NexaDesk" }), "Stream this");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText("Partial")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Stop generating" }));

    await waitFor(() => expect(api.cancelChat).toHaveBeenCalledWith(
      "in-memory-token",
      expect.any(String),
      expect.any(AbortSignal)
    ));
    finishStream?.({
      reply: "Partial ",
      model: "phi3:mini",
      conversation_id: "conversation-1",
      grounded: false,
      sources: [],
      status: "cancelled"
    });
    expect(await screen.findByText(/this is an incomplete response/)).toBeTruthy();
    const savedArticleText = screen.getAllByRole("article").map(article => article.textContent);
    expect(savedArticleText.some(text => text?.includes("Stream this"))).toBe(true);
    expect(savedArticleText.some(text => text?.includes("Partial"))).toBe(true);
    expect((screen.getByRole("textbox", { name: "Message NexaDesk" }) as HTMLTextAreaElement).value)
      .toBe("");
    expect(screen.getByRole("button", { name: "Send message" })).toBeTruthy();
  });

  it("restores the authenticated session and selected workspace after reload", async () => {
    const restored: AuthResult = {
      ...access,
      workspaces: [
        ...access.workspaces,
        { id: "workspace-2", name: "Workspace Two", role: "member" }
      ]
    };
    window.history.replaceState(null, "", "/");
    window.sessionStorage.setItem("nexadesk.workspace.user-1", "workspace-2");
    api.refreshSession.mockResolvedValue(restored);

    render(<App />);

    await screen.findByRole("heading", { name: "Welcome to NexaDesk" });
    await userEvent.setup().click(screen.getByRole("button", { name: "Open sidebar" }));
    await screen.findByRole("button", { name: /New conversation/ });
    expect(api.setAccessToken).toHaveBeenCalledWith("in-memory-token");
    expect(
      (screen.getByLabelText("Select workspace") as HTMLSelectElement).value
    ).toBe("workspace-2");
    expect(api.listConversations).toHaveBeenCalledWith(
      "in-memory-token",
      "workspace-2"
    );
  });

  it("restores the selected conversation after refresh", async () => {
    const restoredMessages = [{
      id: "message-1",
      role: "user" as const,
      content: "Earlier question",
      created_at: "2026-01-02T00:00:00Z",
      status: "completed" as const
    }];
    window.history.replaceState(null, "", "/");
    window.sessionStorage.setItem(
      "nexadesk.conversation.user-1.workspace-1",
      "conversation-1"
    );
    api.refreshSession.mockResolvedValue(access);
    api.listConversations.mockResolvedValue([firstConversation]);
    api.getMessages.mockResolvedValue(restoredMessages);

    render(<App />);

    expect(await screen.findByText("Earlier question")).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Choose a chat mode" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Start Chat" })).toBeNull();
    expect(api.getMessages).toHaveBeenCalledWith(
      "in-memory-token",
      "conversation-1"
    );
  });

  it("shows chat mode selection after refresh restores an empty conversation", async () => {
    window.history.replaceState(null, "", "/");
    window.sessionStorage.setItem(
      "nexadesk.conversation.user-1.workspace-1",
      "conversation-1"
    );
    api.refreshSession.mockResolvedValue(access);
    api.listConversations.mockResolvedValue([firstConversation]);
    api.getMessages.mockResolvedValue([]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Welcome to NexaDesk" })).toBeTruthy();
    expect(await screen.findByRole("group", { name: "Choose a chat mode" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Start Chat" })).toBeTruthy();
    expect(api.getMessages).toHaveBeenCalledWith(
      "in-memory-token",
      "conversation-1"
    );
  });

  it("restores the selected workspace view after refresh", async () => {
    window.history.replaceState(null, "", "/");
    window.sessionStorage.setItem("nexadesk.view.user-1", "knowledge");
    api.refreshSession.mockResolvedValue(access);
    api.listKnowledgeDocuments.mockResolvedValue([]);

    render(<App />);

    await waitFor(() => expect(api.listKnowledgeDocuments).toHaveBeenCalledWith(
      "in-memory-token",
      "workspace-1"
    ));
  });

  it("shows a bounded session restoration error and retries server validation", async () => {
    api.refreshSession
      .mockRejectedValueOnce(new Error("Network unavailable"))
      .mockResolvedValueOnce(access);
    render(<App />);

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Welcome to NexaDesk" })).toBeNull();
    await userEvent.setup().click(
      screen.getByRole("button", { name: "Retry session restoration" })
    );
    expect(await screen.findByRole("heading", { name: "Welcome to NexaDesk" })).toBeTruthy();
    expect(api.refreshSession).toHaveBeenCalledTimes(2);
  });

  it("shows a retry state instead of loading forever when validation is slow", async () => {
    vi.useFakeTimers();
    api.refreshSession.mockImplementation(() => new Promise(() => undefined));
    render(<App />);

    expect(screen.getByText("Restoring your session…")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Welcome to NexaDesk" })).toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });

    expect(screen.getByText("Session restoration is taking longer than expected.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry session restoration" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Welcome to NexaDesk" })).toBeNull();
  });

  it("does not render protected dashboard content before session validation succeeds", async () => {
    let resolveSession: ((result: AuthResult) => void) | undefined;
    api.refreshSession.mockImplementation(() => new Promise(resolve => {
      resolveSession = resolve;
    }));
    render(<App />);

    expect(screen.getByText("Restoring your session…")).toBeTruthy();
    expect(screen.queryByText("Workspace One")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Welcome to NexaDesk" })).toBeNull();
    await act(async () => resolveSession?.(access));
    expect(await screen.findByRole("heading", { name: "Welcome to NexaDesk" })).toBeTruthy();
  });

  it("completes session restoration safely through StrictMode effect replay", async () => {
    let resolveSession: ((result: AuthResult) => void) | undefined;
    api.refreshSession.mockImplementation(() => new Promise(resolve => {
      resolveSession = resolve;
    }));
    render(<StrictMode><App /></StrictMode>);

    expect(api.refreshSession).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Restoring your session…")).toBeTruthy();
    await act(async () => resolveSession?.(access));
    expect(await screen.findByRole("heading", { name: "Welcome to NexaDesk" })).toBeTruthy();
  });

  it("supports Escape cancellation while the chat input is focused", async () => {
    const user = await signIn();
    let finishStream: ((result: {
      reply: string;
      model: string;
      conversation_id: string;
      grounded: boolean;
      sources: [];
      status: "cancelled";
    }) => void) | undefined;
    api.streamChat.mockImplementation((
      _text: string, _token: string, _workspaceId: string,
      _conversationId: string | null, _requestId: string, _signal: AbortSignal,
      onToken: (token: string) => void
    ) => new Promise(resolve => {
      finishStream = resolve;
      onToken("Partial");
    }));
    api.cancelChat.mockResolvedValue({ status: "cancellation_requested" });

    await user.type(screen.getByRole("textbox", { name: "Message NexaDesk" }), "Cancel with Escape");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const textbox = screen.getByRole("textbox", { name: "Message NexaDesk" });
    await user.click(textbox);
    await user.keyboard("{Escape}");

    await waitFor(() => expect(api.cancelChat).toHaveBeenCalledTimes(1));
    finishStream?.({
      reply: "Partial",
      model: "phi3:mini",
      conversation_id: "conversation-1",
      grounded: false,
      sources: [],
      status: "cancelled"
    });
    expect(await screen.findByText(/this is an incomplete response/)).toBeTruthy();
  });

  it("shows loading, empty and history error states", async () => {
    api.listConversations.mockResolvedValue([firstConversation]);
    const user = await signIn();
    let rejectMessages: ((error: Error) => void) | undefined;
    api.getMessages.mockImplementation(() => new Promise((_resolve, reject) => {
      rejectMessages = reject;
    }));
    await user.click(await screen.findByRole("button", { name: /^Planning notes$/ }));
    expect(screen.getByRole("status").textContent).toContain("Loading conversation");

    rejectMessages?.(new Error("Conversation could not be loaded."));
    expect(await screen.findByText("Conversation could not be loaded.", {
      selector: ".conversation-error"
    })).toBeTruthy();
    expect(await screen.findByRole("button", { name: /^Planning notes$/ })).toBeTruthy();
  });

  it("opens the functional knowledge base page", async () => {
    const user = await signIn();
    api.listKnowledgeDocuments.mockResolvedValue([]);

    await user.click(screen.getByRole("button", { name: /Knowledge base/ }));

    expect(await screen.findByRole("heading", { name: "Knowledge base" })).toBeTruthy();
    expect(await screen.findByText("No documents yet")).toBeTruthy();
    expect(api.listKnowledgeDocuments).toHaveBeenCalledWith(
      "in-memory-token",
      "workspace-1"
    );
  });
});
