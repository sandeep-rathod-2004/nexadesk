import { recordFrontendPerformance } from "../performance";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api/v1";

export type WorkspaceAccess = { id: string; name: string; role: string };
export type AuthResult = {
  access_token: string;
  token_type: "bearer";
  user: { id: string; email: string; full_name?: string | null };
  workspaces: WorkspaceAccess[];
};
export type RegistrationResult = {
  user: { id: string; email: string; full_name?: string | null };
  workspaces: WorkspaceAccess[];
};
export type Conversation = {
  id: string;
  workspace_id: string;
  title: string;
  chat_type: "normal" | "knowledge";
  document_ids: string[];
  created_at: string;
  updated_at: string;
};
export type PersistedMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  sources?: SourceReference[];
  status?: "completed" | "interrupted";
};
export type ChatResult = {
  reply: string;
  model: string;
  conversation_id: string;
  grounded: boolean;
  sources: SourceReference[];
  replayed?: boolean;
  status?: "completed" | "cancelled";
};
export type ModelInfo = {
  provider: string;
  model: string;
};
export type StreamMilestone =
  | "send_clicked"
  | "request_started"
  | "response_headers_received"
  | "sse_started"
  | "first_token_received"
  | "stream_completed"
  | "stream_cancelled";
export type SourceReference = {
  id: string;
  chunk_id?: string;
  document_id: string;
  document_name: string;
  location: string;
  excerpt: string;
  similarity: number;
  citation_label?: string | null;
  claims?: string[];
};
export type KnowledgeDocument = {
  id: string;
  workspace_id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  status: "processing" | "ready" | "failed";
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let sessionAccessToken: string | null = null;
let refreshInFlight: Promise<AuthResult> | null = null;
let refreshAbortController: AbortController | null = null;
const REFRESH_REQUEST_TIMEOUT_MS = 10_000;

export function setAccessToken(token: string | null): void {
  sessionAccessToken = token;
}

export function refreshSession(): Promise<AuthResult> {
  if (refreshInFlight) return refreshInFlight;

  const refreshStartedAt = performance.now();
  recordFrontendPerformance("refresh_request_started");
  refreshAbortController = new AbortController();
  const controller = refreshAbortController;
  const timeoutId = window.setTimeout(() => controller.abort(), REFRESH_REQUEST_TIMEOUT_MS);

  refreshInFlight = request<AuthResult>("/auth/refresh", {
    method: "POST",
    signal: controller.signal
  })
    .then(result => {
      setAccessToken(result.access_token);
      recordFrontendPerformance("refresh_request_completed", refreshStartedAt);
      return result;
    })
    .catch(error => {
      recordFrontendPerformance("refresh_request_failed", refreshStartedAt);
      throw error;
    })
    .finally(() => {
      window.clearTimeout(timeoutId);
      if (refreshAbortController === controller) {
        refreshInFlight = null;
        refreshAbortController = null;
      }
    });
  return refreshInFlight;
}

export async function retryRefreshSession(): Promise<AuthResult> {
  const previous = refreshInFlight;
  if (previous) {
    await previous.catch(() => undefined);
  }

  if (refreshInFlight) return refreshSession();
  return refreshSession();
}

async function fetchWithSession(
  url: string,
  init: RequestInit,
  token?: string
): Promise<Response> {
  const activeToken = token ? sessionAccessToken ?? token : undefined;
  const headers: Record<string, string> = {};
  new Headers(init.headers).forEach((value, key) => {
    headers[key] = value;
  });
  if (activeToken) {
    delete headers.authorization;
    delete headers.Authorization;
    headers.Authorization = `Bearer ${activeToken}`;
  }
  const response = await fetch(url, {
    ...init,
    headers,
    credentials: "include"
  });
  if (!activeToken || response.status !== 401) return response;

  let refreshed: AuthResult;
  try {
    refreshed = await refreshSession();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return response;
    throw error;
  }
  const retryHeaders: Record<string, string> = {};
  new Headers(init.headers).forEach((value, key) => {
    retryHeaders[key] = value;
  });
  delete retryHeaders.authorization;
  delete retryHeaders.Authorization;
  retryHeaders.Authorization = `Bearer ${refreshed.access_token}`;
  return fetch(url, {
    ...init,
    headers: retryHeaders,
    credentials: "include"
  });
}

async function request<T>(
  path: string,
  options: {
    token?: string;
    method?: string;
    body?: unknown;
    signal?: AbortSignal;
  } = {}
): Promise<T> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.token) headers.Authorization = `Bearer ${options.token}`;
  const response = await fetchWithSession(`${API_BASE}${path}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal
  }, options.token);
  if (response.status === 204) return undefined as T;
  const payload: unknown = await response.json().catch(() => undefined);
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload &&
      typeof payload.detail === "string"
        ? payload.detail
        : `Request failed (${response.status})`;
    if (response.status === 401 && options.token) {
      throw new ApiError("Your session has expired. Please sign in again.", response.status);
    }
    if (response.status === 403 || response.status === 404) {
      throw new ApiError("This resource is unavailable or you no longer have access.", response.status);
    }
    throw new ApiError(detail, response.status);
  }
  return payload as T;
}

export function register(
  fullName: string,
  email: string,
  password: string,
  workspaceName: string
): Promise<RegistrationResult> {
  return request("/auth/register", {
    method: "POST",
    body: {
      full_name: fullName,
      email,
      password,
      workspace_name: workspaceName
    }
  });
}

export async function login(email: string, password: string): Promise<AuthResult> {
  const result = await request<AuthResult>("/auth/login", {
    method: "POST",
    body: { email, password }
  });
  setAccessToken(result.access_token);
  return result;
}

export async function logout(): Promise<void> {
  try {
    await request("/auth/logout", { method: "POST" });
  } finally {
    setAccessToken(null);
  }
}

export function getModel(token: string): Promise<ModelInfo> {
  return request("/model", { token });
}

export function listConversations(
  token: string,
  workspaceId: string,
  chatType?: "normal" | "knowledge"
): Promise<Conversation[]> {
  const typeQuery = chatType ? `&chat_type=${chatType}` : "";
  return request(`/conversations?workspace_id=${encodeURIComponent(workspaceId)}${typeQuery}`, {
    token
  });
}

export function createConversation(
  token: string,
  workspaceId: string,
  title = "New conversation",
  chatType: "normal" | "knowledge" = "normal",
  documentIds: string[] = []
): Promise<Conversation> {
  const body = chatType === "normal" && documentIds.length === 0
    ? { workspace_id: workspaceId, title }
    : {
        workspace_id: workspaceId,
        title,
        chat_type: chatType,
        document_ids: documentIds
      };
  return request("/conversations", {
    method: "POST",
    token,
    body
  });
}

export function updateConversationDocuments(
  token: string,
  conversationId: string,
  documentIds: string[]
): Promise<Conversation> {
  return request(`/conversations/${encodeURIComponent(conversationId)}/documents`, {
    method: "PUT",
    token,
    body: { document_ids: documentIds }
  });
}

export function renameConversation(
  token: string,
  conversationId: string,
  title: string
): Promise<Conversation> {
  return request(`/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    token,
    body: { title }
  });
}

export function deleteConversation(
  token: string,
  conversationId: string
): Promise<void> {
  return request(`/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
    token
  });
}

export function getMessages(
  token: string,
  conversationId: string
): Promise<PersistedMessage[]> {
  return request(`/conversations/${encodeURIComponent(conversationId)}/messages`, {
    token
  });
}

export function sendMessage(
  message: string,
  token: string,
  workspaceId: string,
  conversationId: string | null
): Promise<ChatResult> {
  return request("/chat", {
    method: "POST",
    token,
    body: {
      message,
      workspace_id: workspaceId,
      conversation_id: conversationId
    }
  });
}

export function cancelChat(
  token: string,
  requestId: string,
  signal?: AbortSignal
): Promise<{ status: string }> {
  return request(
    `/chat/stream/${encodeURIComponent(requestId)}/cancel`,
    { method: "POST", token, signal }
  );
}

export async function streamChat(
  message: string,
  token: string,
  workspaceId: string,
  conversationId: string | null,
  requestId: string,
  signal: AbortSignal,
  onToken: (token: string) => void,
  onMilestone?: (stage: StreamMilestone, elapsedMs: number) => void
): Promise<ChatResult> {
  const requestStartedAt = performance.now();
  onMilestone?.("request_started", 0);
  let response: Response;
  try {
    response = await fetchWithSession(`${API_BASE}/chat/stream`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "X-Request-ID": requestId,
        "Content-Type": "application/json",
        Accept: "text/event-stream"
      },
      body: JSON.stringify({
        message,
        workspace_id: workspaceId,
        conversation_id: conversationId,
        request_id: requestId
      }),
      signal
    }, token);
  } catch (error) {
    if (signal.aborted) throw error;
    throw new ApiError("The connection was interrupted before the response started.", 0);
  }
  onMilestone?.("response_headers_received", performance.now() - requestStartedAt);

  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => undefined);
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload &&
      typeof payload.detail === "string"
        ? payload.detail
        : `Request failed (${response.status})`;
    if (response.status === 401) {
      throw new ApiError("Your session has expired. Please sign in again.", response.status);
    }
    if (response.status === 403 || response.status === 404) {
      throw new ApiError("This resource is unavailable or you no longer have access.", response.status);
    }
    throw new ApiError(detail, response.status);
  }
  if (!response.headers.get("content-type")?.includes("text/event-stream") || !response.body) {
    throw new ApiError("The server did not start a streaming response.", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let streamEnded = false;
  let cancellationReceived = false;
  let firstTokenReceived = false;
  let result: ChatResult | null = null;
  let streamError: ApiError | null = null;

  function dispatch(frame: string) {
    let eventName = "message";
    const data: string[] = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    let payload: unknown;
    try {
      payload = JSON.parse(data.join("\n"));
    } catch {
      streamError = new ApiError("The server sent an invalid streaming event.", 502);
      return;
    }
    if (typeof payload !== "object" || payload === null) {
      streamError = new ApiError("The server sent an invalid streaming event.", 502);
      return;
    }
    if (eventName === "started") {
      onMilestone?.("sse_started", performance.now() - requestStartedAt);
    } else if (eventName === "token") {
      if (cancellationReceived) return;
      if ("token" in payload && typeof payload.token === "string") {
        if (!firstTokenReceived) {
          firstTokenReceived = true;
          onMilestone?.("first_token_received", performance.now() - requestStartedAt);
        }
        onToken(payload.token);
      }
      else streamError = new ApiError("The server sent an invalid token event.", 502);
    } else if (eventName === "complete") {
      if (
        "reply" in payload &&
        typeof payload.reply === "string" &&
        "model" in payload &&
        typeof payload.model === "string" &&
        "conversation_id" in payload &&
        typeof payload.conversation_id === "string" &&
        "grounded" in payload &&
        typeof payload.grounded === "boolean" &&
        "sources" in payload &&
        Array.isArray(payload.sources)
      ) {
        result = { ...payload, status: "completed" } as ChatResult;
        onMilestone?.("stream_completed", performance.now() - requestStartedAt);
      } else {
        streamError = new ApiError("The server sent an invalid completion event.", 502);
      }
    } else if (eventName === "cancelled") {
      if (
        "reply" in payload &&
        typeof payload.reply === "string" &&
        "model" in payload &&
        typeof payload.model === "string" &&
        "conversation_id" in payload &&
        typeof payload.conversation_id === "string" &&
        "grounded" in payload &&
        typeof payload.grounded === "boolean" &&
        "sources" in payload &&
        Array.isArray(payload.sources) &&
        "status" in payload &&
        payload.status === "interrupted"
      ) {
        result = { ...payload, status: "cancelled" } as ChatResult;
        cancellationReceived = true;
        onMilestone?.("stream_cancelled", performance.now() - requestStartedAt);
      } else {
        streamError = new ApiError("The server sent an invalid cancellation event.", 502);
      }
    } else if (eventName === "error") {
      const message = "message" in payload && typeof payload.message === "string"
        ? payload.message
        : "The response could not be completed.";
      streamError = new ApiError(message, 502);
    }
  }

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary: RegExpExecArray | null;
      const separator = /\r?\n\r?\n/;
      while ((boundary = separator.exec(buffer)) !== null) {
        dispatch(buffer.slice(0, boundary.index));
        buffer = buffer.slice(boundary.index + boundary[0].length);
        if (streamError) throw streamError;
      }
      if (done) {
        streamEnded = true;
        break;
      }
    }
    if (buffer.trim()) dispatch(buffer);
    if (streamError) throw streamError;
    if (!result) {
      throw new ApiError("The connection ended before the response was completed.", 0);
    }
    return result;
  } catch (error) {
    if (signal.aborted) throw error;
    if (error instanceof ApiError) throw error;
    throw new ApiError("The connection was interrupted. The response was not saved.", 0);
  } finally {
    if (!streamEnded) {
      try {
        await reader.cancel();
      } catch {
        // The fetch may already have canceled the reader.
      }
    }
    reader.releaseLock();
  }
}

export function listKnowledgeDocuments(
  token: string,
  workspaceId: string
): Promise<KnowledgeDocument[]> {
  return request(
    `/workspaces/${encodeURIComponent(workspaceId)}/documents`,
    { token }
  );
}

export function uploadKnowledgeDocument(
  token: string,
  workspaceId: string,
  file: File,
  onProgress: (percentage: number) => void
): Promise<KnowledgeDocument> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    form.append("file", file);
    xhr.open(
      "POST",
      `${API_BASE}/workspaces/${encodeURIComponent(workspaceId)}/documents`
    );
    xhr.withCredentials = true;
    xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) {
        onProgress(Math.min(99, Math.round((event.loaded / event.total) * 100)));
      }
    };
    xhr.onerror = () => reject(new ApiError("The upload could not reach the server.", 0));
    xhr.onload = () => {
      let payload: unknown;
      try {
        payload = JSON.parse(xhr.responseText || "null");
      } catch {
        payload = undefined;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(100);
        resolve(payload as KnowledgeDocument);
        return;
      }
      const detail =
        typeof payload === "object" && payload !== null && "detail" in payload &&
        typeof payload.detail === "string"
          ? payload.detail
          : `Request failed (${xhr.status})`;
      if (xhr.status === 401) {
        reject(new ApiError("Your session has expired. Please sign in again.", xhr.status));
      } else if (xhr.status === 403 || xhr.status === 404) {
        reject(new ApiError("This resource is unavailable or you no longer have access.", xhr.status));
      } else {
        reject(new ApiError(detail, xhr.status));
      }
    };
    xhr.send(form);
  });
}

export function deleteKnowledgeDocument(
  token: string,
  workspaceId: string,
  documentId: string
): Promise<void> {
  return request(
    `/workspaces/${encodeURIComponent(workspaceId)}/documents/${encodeURIComponent(documentId)}`,
    { method: "DELETE", token }
  );
}
