import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

afterEach(cleanup);

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
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z"
};

async function signIn(user = userEvent.setup()) {
  window.history.replaceState(null, "", "/login");
  api.login.mockResolvedValue(access);
  render(<App />);
  await screen.findByLabelText("Email");
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.com" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a sufficiently long password" } });
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByRole("button", { name: /New conversation/ });
  return user;
}

describe("conversation management UI", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/login");
    window.sessionStorage.clear();
    api.refreshSession.mockRejectedValue(new api.ApiError("No session", 401));
    api.logout.mockResolvedValue(undefined);
    api.listConversations.mockResolvedValue([]);
    api.getMessages.mockResolvedValue([]);
    api.getModel.mockResolvedValue({ provider: "Ollama", model: "phi3:mini" });
    api.createConversation.mockResolvedValue(firstConversation);
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

  it("shows the backend-configured Ollama model", async () => {
    await signIn();

    const modelBadge = await screen.findByRole("button", { name: "Configured AI model" });
    expect(modelBadge.textContent).toContain("Ollama · phi3:mini");
    expect(api.getModel).toHaveBeenCalledWith("in-memory-token");
  }, 10000);

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
    expect(await screen.findByRole("alert")).toBeTruthy();
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
