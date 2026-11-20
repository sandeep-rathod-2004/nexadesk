import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  cancelChat,
  createConversation,
  deleteKnowledgeDocument,
  deleteConversation,
  getModel,
  listKnowledgeDocuments,
  listConversations,
  renameConversation,
  refreshSession,
  retryRefreshSession,
  setAccessToken,
  streamChat,
  uploadKnowledgeDocument
} from "./lib/api";

const token = "test-access-token";
const conversation = {
  id: "conversation-1",
  workspace_id: "workspace-1",
  title: "Planning",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z"
};

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe("conversation API client", () => {
  it("shares one in-flight session restoration request", async () => {
    let resolveResponse: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      new Promise<Response>(resolve => {
      resolveResponse = resolve;
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const first = refreshSession();
    const second = refreshSession();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await vi.waitFor(() => expect(resolveResponse).toBeDefined());
    resolveResponse?.(new Response(JSON.stringify({
      access_token: "restored-access-token",
      token_type: "bearer",
      user: { id: "user-1", email: "person@example.com" },
      workspaces: []
    }), { status: 200 }));

    await expect(Promise.all([first, second])).resolves.toEqual([
      {
        access_token: "restored-access-token",
        token_type: "bearer",
        user: { id: "user-1", email: "person@example.com" },
        workspaces: []
      },
      {
        access_token: "restored-access-token",
        token_type: "bearer",
        user: { id: "user-1", email: "person@example.com" },
        workspaces: []
      }
    ]);
    expect(fetchMock.mock.calls[0]?.[1]?.credentials).toBe("include");
  });

  it("times out one refresh request and allows a retry after settlement", async () => {
    vi.useFakeTimers();
    let callCount = 0;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      callCount += 1;
      if (callCount === 1) {
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted", "AbortError"));
          });
        });
      }
      return Promise.resolve(new Response(JSON.stringify({
        access_token: "retried-access-token",
        token_type: "bearer",
        user: { id: "user-1", email: "person@example.com" },
        workspaces: []
      }), { status: 200 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const pending = refreshSession();
    const pendingResult = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(10_000);
    await pendingResult;

    await expect(retryRefreshSession()).resolves.toMatchObject({
      access_token: "retried-access-token"
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("reads the authenticated backend model configuration", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ provider: "Ollama", model: "phi3:mini" }), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getModel(token)).resolves.toEqual({
      provider: "Ollama",
      model: "phi3:mini"
    });

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain("/model");
    expect(options.headers.Authorization).toBe(`Bearer ${token}`);
    expect(url).not.toContain(token);
  });

  it("refreshes an expired access token once and retries with the rotated session", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "expired" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        access_token: "rotated-access-token",
        token_type: "bearer",
        user: { id: "user-1", email: "person@example.com" },
        workspaces: []
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        provider: "Ollama", model: "phi3:mini"
      }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    setAccessToken("expired-access-token");

    await expect(getModel("expired-access-token")).resolves.toEqual({
      provider: "Ollama",
      model: "phi3:mini"
    });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toContain("/auth/refresh");
    expect(fetchMock.mock.calls[1][1].credentials).toBe("include");
    expect(fetchMock.mock.calls[2][1].headers.Authorization)
      .toBe("Bearer rotated-access-token");
  });

  it("preserves transient refresh failures instead of converting them to session expiry", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "expired" }), { status: 401 }))
      .mockRejectedValueOnce(new TypeError("Network unavailable"));
    vi.stubGlobal("fetch", fetchMock);
    setAccessToken("expired-access-token");

    await expect(getModel("expired-access-token"))
      .rejects.toThrow("Network unavailable");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("parses streamed tokens and completion without putting credentials in the URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      [
        'event: started\ndata: {"stage":"preparing"}\n\n',
        'event: token\ndata: {"token":"Hello "}\n\n',
        'event: token\ndata: {"token":"world"}\n\n',
        'event: complete\ndata: {"reply":"Hello world","model":"phi3:mini","conversation_id":"conversation-1","grounded":true,"sources":[]}\n\n'
      ].join(""),
      { status: 200, headers: { "Content-Type": "text/event-stream" } }
    ));
    vi.stubGlobal("fetch", fetchMock);
    const onToken = vi.fn();
    const onMilestone = vi.fn();

    await expect(streamChat(
      "Question",
      token,
      "workspace-1",
      null,
      "request-1",
      new AbortController().signal,
      onToken,
      onMilestone
    )).resolves.toMatchObject({
      reply: "Hello world",
      conversation_id: "conversation-1",
      grounded: true
    });

    expect(onToken.mock.calls).toEqual([["Hello "], ["world"]]);
    expect(onMilestone.mock.calls.map(([stage]) => stage)).toEqual([
      "request_started",
      "response_headers_received",
      "sse_started",
      "first_token_received",
      "stream_completed"
    ]);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain("/chat/stream");
    expect(new Headers(options.headers).get("x-request-id")).toBe("request-1");
    expect(url).not.toContain(token);
    expect(options.headers.Authorization).toBe(`Bearer ${token}`);
    expect(JSON.parse(options.body)).toMatchObject({
      request_id: "request-1",
      workspace_id: "workspace-1"
    });
  });

  it("does not treat a disconnected stream as a completed response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      'event: token\ndata: {"token":"Partial"}\n\n',
      { status: 200, headers: { "Content-Type": "text/event-stream" } }
    )));
    const onToken = vi.fn();

    await expect(streamChat(
      "Question",
      token,
      "workspace-1",
      null,
      "request-2",
      new AbortController().signal,
      onToken
    )).rejects.toMatchObject({ name: "ApiError", status: 0 });
    expect(onToken).toHaveBeenCalledWith("Partial");
  });

  it("distinguishes a cancellation SSE event from successful completion", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      [
        'event: token\ndata: {"token":"Partial"}\n\n',
        'event: cancelled\ndata: {"reply":"Partial","model":"phi3:mini","conversation_id":"conversation-1","grounded":false,"sources":[],"status":"interrupted"}\n\n',
        'event: token\ndata: {"token":"must not display"}\n\n'
      ].join(""),
      { status: 200, headers: { "Content-Type": "text/event-stream" } }
    )));
    const onToken = vi.fn();

    await expect(streamChat(
      "Question", token, "workspace-1", null, "request-cancel",
      new AbortController().signal, onToken
    )).resolves.toMatchObject({
      status: "cancelled",
      reply: "Partial",
      sources: []
    });
    expect(onToken).toHaveBeenCalledTimes(1);
  });

  it("requests cancellation through the authenticated endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "cancellation_requested" }), { status: 202 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(cancelChat(token, "request-5")).resolves.toEqual({
      status: "cancellation_requested"
    });
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain("/chat/stream/request-5/cancel");
    expect(url).not.toContain(token);
    expect(options.method).toBe("POST");
    expect(options.headers.Authorization).toContain(token);
  });

  it("handles unauthorized streaming responses without exposing server details", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "internal detail" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "internal detail" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "internal detail" }), { status: 403 }));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    const onToken = vi.fn();

    await expect(streamChat(
      "Question", token, "workspace-1", null, "request-3", signal, onToken
    )).rejects.toMatchObject({
      name: "ApiError",
      status: 401,
      message: "Your session has expired. Please sign in again."
    });
    await expect(streamChat(
      "Question", token, "workspace-1", null, "request-4", signal, onToken
    )).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      message: "This resource is unavailable or you no longer have access."
    });
    expect(fetchMock.mock.calls.every(([url]) => !url.includes(token))).toBe(true);
  });

  it("uses authenticated list and keeps the token out of the URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([conversation]), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listConversations(token, "workspace-1")).resolves.toEqual([conversation]);

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain("/conversations?workspace_id=workspace-1");
    expect(url).not.toContain(token);
    expect(options.headers.Authorization).toBe(`Bearer ${token}`);
  });

  it("creates, renames and deletes through the existing conversation resource", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(conversation), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...conversation, title: "Updated" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await createConversation(token, "workspace-1");
    await renameConversation(token, "conversation-1", "Updated");
    await expect(deleteConversation(token, "conversation-1")).resolves.toBeUndefined();

    expect(fetchMock.mock.calls.map(([url, options]) => [url, options.method])).toEqual([
      [`${import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api/v1"}/conversations`, "POST"],
      [`${import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api/v1"}/conversations/conversation-1`, "PATCH"],
      [`${import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api/v1"}/conversations/conversation-1`, "DELETE"]
    ]);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      workspace_id: "workspace-1",
      title: "New conversation"
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ title: "Updated" });
    expect(fetchMock.mock.calls.every(([, options]) => options.headers.Authorization === `Bearer ${token}`)).toBe(true);
  });

  it("converts unauthorized and forbidden responses into safe API errors", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "internal detail" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "internal detail" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "internal detail" }), { status: 403 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listConversations(token, "workspace-1")).rejects.toMatchObject({
      name: "ApiError",
      status: 401,
      message: "Your session has expired. Please sign in again."
    });
    await expect(renameConversation(token, "conversation-1", "Updated")).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      message: "This resource is unavailable or you no longer have access."
    });
    expect(fetchMock.mock.calls[0][0]).not.toContain(token);
  });

  it("uses authenticated workspace-scoped knowledge endpoints", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listKnowledgeDocuments(token, "workspace-1")).resolves.toEqual([]);
    await deleteKnowledgeDocument(token, "workspace-1", "doc-1");

    expect(fetchMock.mock.calls[0][0]).toContain("/workspaces/workspace-1/documents");
    expect(fetchMock.mock.calls[1][0]).toContain("/workspaces/workspace-1/documents/doc-1");
    expect(fetchMock.mock.calls.every(([url, options]) =>
      !url.includes(token) && options.headers.Authorization === ["Bear", "er ", token].join("")
    )).toBe(true);
  });

  it("uploads documents with an authenticated header and reports progress", async () => {
    const requests: FakeXMLHttpRequest[] = [];
    class FakeXMLHttpRequest {
      upload = {} as XMLHttpRequestUpload;
      open = vi.fn();
      setRequestHeader = vi.fn();
      status = 0;
      responseText = "";
      onload: ((event: ProgressEvent) => void) | null = null;
      onerror: ((event: ProgressEvent) => void) | null = null;
      constructor() {
        requests.push(this);
      }
      send = vi.fn(() => {
        this.status = 201;
        this.responseText = JSON.stringify({ id: "doc-1", status: "ready" });
        this.onload?.(new ProgressEvent("load"));
      });
    }
    vi.stubGlobal("XMLHttpRequest", FakeXMLHttpRequest);
    const progress = vi.fn();

    await expect(uploadKnowledgeDocument(
      token,
      "workspace-1",
      new File(["text"], "notes.txt", { type: "text/plain" }),
      progress
    )).resolves.toMatchObject({ id: "doc-1", status: "ready" });
    const xhr = requests[0];

    expect(xhr.open).toHaveBeenCalledWith(
      "POST",
      expect.stringContaining("/workspaces/workspace-1/documents")
    );
    expect(xhr.setRequestHeader).toHaveBeenCalledWith(
      "Authorization",
      ["Bear", "er ", token].join("")
    );
    expect(xhr.send).toHaveBeenCalledWith(expect.any(FormData));
    expect(progress).toHaveBeenCalledWith(100);
  });
});
