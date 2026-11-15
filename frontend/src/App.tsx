import { FormEvent, lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  Activity, ArrowUp, Bot, BookOpen, ChevronDown, CircleHelp, Command,
  Check, FileText, LifeBuoy, LockKeyhole, Menu, MessageSquare, MoreHorizontal,
  Pencil, Plus, Settings2, ShieldCheck, Sparkles, ThumbsDown, ThumbsUp, Trash2, X, Zap
} from "lucide-react";
import {
  ApiError, AuthResult, Conversation, createConversation, deleteConversation,
  cancelChat, getMessages, getModel, listConversations, logout, ModelInfo, PersistedMessage,
  refreshSession, setAccessToken,
  renameConversation, SourceReference, StreamMilestone, streamChat
} from "./lib/api";
import AuthPage, { AuthPageRoute } from "./AuthPage";
import KnowledgeBase from "./KnowledgeBase";

const MarkdownMessage = lazy(() => import("./MarkdownMessage"));

type Message = {
  id: string;
  role: "assistant" | "user";
  content: string;
  time: string;
  sources?: SourceReference[];
  status?: "completed" | "interrupted" | "generating";
};
const initial: Message[] = [{
  id: "welcome", role: "assistant",
  content: "Hey there! I’m NexaDesk, your AI workspace assistant. Ask me a question, explore an idea, or start with one of the prompts below. I’ll be transparent when I can’t verify something.",
  time: "Now"
}];
const prompts = [
  { icon: BookOpen, title: "Explain a complex topic", detail: "Break it down into simple steps" },
  { icon: FileText, title: "Draft a customer response", detail: "Clear, helpful and on-brand" },
  { icon: Sparkles, title: "Brainstorm new ideas", detail: "Explore possibilities together" }
];

function authPageFromPath(pathname: string): AuthPageRoute {
  if (pathname === "/register") return "register";
  if (pathname === "/forgot-password") return "forgot-password";
  if (pathname === "/reset-password") return "reset-password";
  return "login";
}

function authPath(page: AuthPageRoute): string {
  return page === "login" ? "/login" : `/${page}`;
}

function viewStorageKey(userId: string): string {
  return `nexadesk.view.${userId}`;
}

function conversationStorageKey(userId: string, workspaceId: string): string {
  return `nexadesk.conversation.${userId}.${workspaceId}`;
}

export default function App() {
  const [messages, setMessages] = useState<Message[]>(initial);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [activeView, setActiveView] = useState<"conversations" | "knowledge">("conversations");
  const [auth, setAuth] = useState<AuthResult | null>(null);
  const [authRestoring, setAuthRestoring] = useState(true);
  const [modelInfo, setModelInfo] = useState<ModelInfo | null>(null);
  const [modelInfoUnavailable, setModelInfoUnavailable] = useState(false);
  const [authPage, setAuthPage] = useState<AuthPageRoute>(() => authPageFromPath(window.location.pathname));
  const [workspaceId, setWorkspaceId] = useState("");
  const [authError, setAuthError] = useState("");
  const [chatError, setChatError] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [conversationBusy, setConversationBusy] = useState(false);
  const [conversationError, setConversationError] = useState("");
  const [renameId, setRenameId] = useState<string | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [pendingDelete, setPendingDelete] = useState<Conversation | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const sendInFlight = useRef(false);
  const activeChatRequest = useRef<AbortController | null>(null);
  const activeRequestId = useRef<string | null>(null);
  const stopRequested = useRef(false);
  const [stopPending, setStopPending] = useState(false);
  const retryableRequest = useRef<{
    message: string;
    workspaceId: string;
    conversationId: string | null;
    requestId: string;
    userMessageId: string;
    assistantMessageId: string;
  } | null>(null);
  const conversationRequest = useRef(0);
  const conversationListRequest = useRef(0);

  useEffect(() => {
    const handlePopState = () => setAuthPage(authPageFromPath(window.location.pathname));
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    let active = true;
    void refreshSession()
      .then(result => {
        if (!active) return;
        setAccessToken(result.access_token);
        setAuth(result);
        const key = `nexadesk.workspace.${result.user.id}`;
        const savedWorkspace = window.sessionStorage.getItem(key);
        const selectedWorkspace = result.workspaces.find(
          workspace => workspace.id === savedWorkspace
        ) ?? result.workspaces[0];
        setWorkspaceId(selectedWorkspace?.id ?? "");
        setActiveView(
          window.sessionStorage.getItem(viewStorageKey(result.user.id)) === "knowledge"
            ? "knowledge"
            : "conversations"
        );
        if (["/login", "/register", "/forgot-password", "/reset-password"]
          .includes(window.location.pathname)) {
          window.history.replaceState(null, "", "/");
        }
      })
      .catch(() => {
        if (active) setAccessToken(null);
      })
      .finally(() => {
        if (active) setAuthRestoring(false);
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!auth) {
      setModelInfo(null);
      setModelInfoUnavailable(false);
      return;
    }
    let active = true;
    setModelInfo(null);
    setModelInfoUnavailable(false);
    void getModel(auth.access_token)
      .then(info => {
        if (active) setModelInfo(info);
      })
      .catch(error => {
        if (error instanceof ApiError && error.status === 401) {
          if (active) expireSession(error);
          return;
        }
        if (active) setModelInfoUnavailable(true);
      });
    return () => { active = false; };
  }, [auth]);

  function expireSession(error: ApiError) {
    activeChatRequest.current?.abort();
    conversationListRequest.current += 1;
    setAccessToken(null);
    setAuth(null);
    window.history.replaceState(null, "", "/login");
    setAuthPage("login");
    setActiveView("conversations");
    setWorkspaceId("");
    setConversations([]);
    setConversationsLoading(false);
    setPendingDelete(null);
    clearCurrentConversation();
    setAuthError(error.message);
  }

  function navigateAuthPage(page: AuthPageRoute) {
    window.history.pushState(null, "", authPath(page));
    setAuthPage(page);
    setAuthError("");
  }

  function acceptLogin(result: AuthResult) {
    setAccessToken(result.access_token);
    setAuth(result);
    const key = `nexadesk.workspace.${result.user.id}`;
    const savedWorkspace = window.sessionStorage.getItem(key);
    const selectedWorkspace = result.workspaces.find(
      workspace => workspace.id === savedWorkspace
    ) ?? result.workspaces[0];
    setWorkspaceId(selectedWorkspace?.id ?? "");
    setActiveView(
      window.sessionStorage.getItem(viewStorageKey(result.user.id)) === "knowledge"
        ? "knowledge"
        : "conversations"
    );
    window.history.replaceState(null, "", "/");
  }

  function selectView(view: "conversations" | "knowledge") {
    setActiveView(view);
    if (auth) {
      window.sessionStorage.setItem(viewStorageKey(auth.user.id), view);
    }
  }

  function signOut() {
    void logout().catch(() => {
      setAuthError("The session could not be revoked on the server. Please try again.");
    });
    setAccessToken(null);
    conversationListRequest.current += 1;
    setAuth(null);
    window.history.replaceState(null, "", "/login");
    setAuthPage("login");
    setWorkspaceId("");
    setConversations([]);
    setConversationsLoading(false);
    setInput("");
    clearCurrentConversation();
  }

  useEffect(() => {
    if (!auth || !workspaceId) {
      setConversations([]);
      setConversationsLoading(false);
      return;
    }
    let active = true;
    const requestId = ++conversationListRequest.current;
    setConversationsLoading(true);
    setConversationError("");
    void listConversations(auth.access_token, workspaceId)
      .then(items => {
        if (active && requestId === conversationListRequest.current) {
          setConversations(items);
          const savedId = window.sessionStorage.getItem(
            conversationStorageKey(auth.user.id, workspaceId)
          );
          const savedConversation = items.find(item => item.id === savedId);
          if (savedConversation) {
            void openConversation(savedConversation, true);
          } else if (savedId) {
            window.sessionStorage.removeItem(
              conversationStorageKey(auth.user.id, workspaceId)
            );
          }
        }
      })
      .catch(error => {
        if (!active || requestId !== conversationListRequest.current) return;
        if (error instanceof ApiError && error.status === 401) {
          expireSession(error);
        } else {
          setConversationError(error instanceof Error ? error.message : "Could not load conversations.");
        }
      })
      .finally(() => {
        if (active && requestId === conversationListRequest.current) {
          setConversationsLoading(false);
        }
      });
    return () => { active = false; };
  }, [auth, workspaceId]);

  function clearCurrentConversation() {
    activeChatRequest.current?.abort();
    retryableRequest.current = null;
    if (auth && workspaceId) {
      window.sessionStorage.removeItem(
        conversationStorageKey(auth.user.id, workspaceId)
      );
    }
    conversationRequest.current += 1;
    setConversationId(null);
    setMessages(initial);
    setMessagesLoading(false);
    setChatError("");
    setConversationError("");
  }

  async function startNewConversation() {
    if (!auth || !workspaceId || conversationBusy || busy) return;
    const requestId = ++conversationRequest.current;
    setMessagesLoading(false);
    setConversationBusy(true);
    setConversationError("");
    setChatError("");
    try {
      const created = await createConversation(auth.access_token, workspaceId);
      if (requestId !== conversationRequest.current) return;
      conversationListRequest.current += 1;
      setConversationsLoading(false);
      setConversations(current => [created, ...current.filter(item => item.id !== created.id)]);
      setConversationId(created.id);
      window.sessionStorage.setItem(
        conversationStorageKey(auth.user.id, workspaceId),
        created.id
      );
      setMessages(initial);
      selectView("conversations");
      setRenameId(null);
      setMobileNav(false);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        expireSession(error);
      } else {
        setConversationError(error instanceof Error ? error.message : "Could not create a conversation.");
      }
    } finally {
      setConversationBusy(false);
    }
  }

  async function openConversation(
    conversation: Conversation,
    preserveView = false
  ) {
    if (!auth) return;
    const requestId = ++conversationRequest.current;
    if (!preserveView) selectView("conversations");
    setConversationId(conversation.id);
    window.sessionStorage.setItem(
      conversationStorageKey(auth.user.id, conversation.workspace_id),
      conversation.id
    );
    setMessages(initial);
    setMessagesLoading(true);
    setChatError("");
    setConversationError("");
    setRenameId(null);
    try {
      const stored: PersistedMessage[] = await getMessages(auth.access_token, conversation.id);
      if (requestId !== conversationRequest.current) return;
      setMessages(stored.length === 0 ? initial : stored.map(message => ({
        id: message.id,
        role: message.role,
        content: message.content,
        time: new Date(message.created_at).toLocaleTimeString([], {
          hour: "numeric",
          minute: "2-digit"
        }),
        sources: message.sources,
        status: message.status
      })));
      setMobileNav(false);
    } catch (error) {
      if (requestId !== conversationRequest.current) return;
      if (error instanceof ApiError && error.status === 401) {
        expireSession(error);
      } else {
        setConversationError(error instanceof Error ? error.message : "Could not load this conversation.");
        if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
          window.sessionStorage.removeItem(
            conversationStorageKey(auth.user.id, conversation.workspace_id)
          );
          setConversations(current => current.filter(item => item.id !== conversation.id));
          clearCurrentConversation();
        }
      }
    } finally {
      if (requestId === conversationRequest.current) setMessagesLoading(false);
    }
  }

  async function submitRename(e: FormEvent, conversation: Conversation) {
    e.preventDefault();
    if (!auth || !renameTitle.trim()) return;
    setConversationBusy(true);
    setConversationError("");
    try {
      const updated = await renameConversation(
        auth.access_token,
        conversation.id,
        renameTitle.trim()
      );
      conversationListRequest.current += 1;
      setConversations(current => current.map(item => item.id === updated.id ? updated : item));
      setRenameId(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        expireSession(error);
      } else {
        setConversationError(error instanceof Error ? error.message : "Could not rename this conversation.");
      }
    } finally {
      setConversationBusy(false);
    }
  }

  async function confirmDeleteConversation() {
    if (!auth || !pendingDelete || deleteBusy) return;
    setDeleteBusy(true);
    setConversationError("");
    try {
      await deleteConversation(auth.access_token, pendingDelete.id);
      conversationListRequest.current += 1;
      setConversations(current => current.filter(item => item.id !== pendingDelete.id));
      if (conversationId === pendingDelete.id) clearCurrentConversation();
      setPendingDelete(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        expireSession(error);
        setPendingDelete(null);
      } else {
        setConversationError(error instanceof Error ? error.message : "Could not delete this conversation.");
      }
    } finally {
      setDeleteBusy(false);
    }
  }

  async function stopGeneration() {
    if (!auth || !activeRequestId.current || stopRequested.current) return;
    const requestId = activeRequestId.current;
    stopRequested.current = true;
    setStopPending(true);
    const stopController = new AbortController();
    const fallbackTimer = window.setTimeout(() => {
      stopController.abort();
      activeChatRequest.current?.abort();
    }, 5000);
    try {
      await cancelChat(auth.access_token, requestId, stopController.signal);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) return;
      if (error instanceof ApiError && error.status === 401) {
        expireSession(error);
      }
      activeChatRequest.current?.abort();
    } finally {
      window.clearTimeout(fallbackTimer);
    }
  }

  async function submit(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || busy || sendInFlight.current || !auth || !workspaceId) return;
    const clickedAt = performance.now();
    let firstTokenPaintLogged = false;
    const logMilestone = (stage: StreamMilestone) => {
      console.info(JSON.stringify({
        event: "nexadesk_ttft",
        stage,
        request_id: activeRequestId.current,
        elapsed_ms: Math.round((performance.now() - clickedAt) * 100) / 100
      }));
    };
    logMilestone("send_clicked");
    sendInFlight.current = true;
    setChatError("");
    setInput("");
    setBusy(true);
    const request = retryableRequest.current;
    const retryingInterrupted = request &&
      request.message === text &&
      request.workspaceId === workspaceId &&
      (
        request.conversationId === conversationId ||
        messages.some(message =>
          message.id === request.assistantMessageId &&
          message.status === "interrupted"
        )
      );
    const previousInterruptedText = retryingInterrupted
      ? messages.find(message => message.id === request.assistantMessageId)?.content ?? ""
      : "";
    const requestId = retryingInterrupted ? request.requestId : crypto.randomUUID();
    const requestConversationId = retryingInterrupted
      ? request.conversationId
      : conversationId;
    retryableRequest.current = {
      message: text,
      workspaceId,
      conversationId: requestConversationId,
      requestId,
      userMessageId: retryingInterrupted ? request.userMessageId : crypto.randomUUID(),
      assistantMessageId: retryingInterrupted ? request.assistantMessageId : crypto.randomUUID()
    };
    const controller = new AbortController();
    activeChatRequest.current = controller;
    activeRequestId.current = requestId;
    stopRequested.current = false;
    setStopPending(false);
    const { userMessageId, assistantMessageId } = retryableRequest.current;
    setMessages(prev => {
      const userExists = prev.some(message => message.id === userMessageId);
      if (!userExists) {
        return [...prev, {
          id: userMessageId, role: "user", content: text, time: "Now"
        }, {
          id: assistantMessageId, role: "assistant", content: "", time: "Just now",
          status: "generating"
        }];
      }
      return prev.map(message => message.id === assistantMessageId
        ? { ...message, content: "", sources: [], status: "generating" }
        : message
      );
    });
    try {
      const result = await streamChat(
        text,
        auth.access_token,
        workspaceId,
        requestConversationId,
        requestId,
        controller.signal,
        token => {
          if (stopRequested.current) return;
          setMessages(prev => prev.map(message =>
            message.id === assistantMessageId
              ? { ...message, content: message.content + token }
              : message
          ));
          if (!firstTokenPaintLogged) {
            firstTokenPaintLogged = true;
            requestAnimationFrame(() => {
              requestAnimationFrame(() => {
                console.info(JSON.stringify({
                  event: "nexadesk_ttft",
                  stage: "first_token_displayed",
                  request_id: requestId,
                  elapsed_ms: Math.round((performance.now() - clickedAt) * 100) / 100
                }));
              });
            });
          }
        },
        stage => logMilestone(stage)
      );
      if (result.status === "cancelled") {
        setConversationId(result.conversation_id);
        window.sessionStorage.setItem(
          conversationStorageKey(auth.user.id, workspaceId),
          result.conversation_id
        );
        setMessages(prev => prev.map(message => message.id === assistantMessageId
          ? {
              ...message,
              content: result.reply,
              sources: [],
              status: "interrupted"
            }
          : message
        ));
        setChatError("Generation stopped. The partial response is marked as interrupted.");
        const listRequestId = ++conversationListRequest.current;
        void listConversations(auth.access_token, workspaceId)
          .then(updated => {
            if (listRequestId === conversationListRequest.current) {
              setConversations(updated);
            }
          })
          .catch(error => {
            if (error instanceof ApiError && error.status === 401) {
              expireSession(error);
            } else {
              setConversationError(
                error instanceof Error
                  ? error.message
                  : "Could not refresh conversation history."
              );
            }
          });
        return;
      }
      setModelInfo({ provider: "Ollama", model: result.model });
      setConversationId(result.conversation_id);
      window.sessionStorage.setItem(
        conversationStorageKey(auth.user.id, workspaceId),
        result.conversation_id
      );
      setMessages(prev => prev.map(message =>
        message.id === assistantMessageId
          ? { ...message, content: result.reply, sources: result.sources, status: "completed" }
          : message
      ));
      retryableRequest.current = null;
      const listRequestId = ++conversationListRequest.current;
      try {
        const updated = await listConversations(auth.access_token, workspaceId);
        if (listRequestId === conversationListRequest.current) {
          setConversations(updated);
          setConversationError("");
        }
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) throw error;
        setConversationError(error instanceof Error ? error.message : "Could not refresh conversation history.");
      }
      setChatError("");
    } catch (err) {
      if (retryableRequest.current?.requestId !== requestId) return;
      if (err instanceof ApiError && err.status === 401) {
        setMessages(prev => prev.filter(message =>
          message.id !== userMessageId && message.id !== assistantMessageId
        ));
        expireSession(err);
      } else if (
        controller.signal.aborted ||
        (err instanceof ApiError && err.status === 0)
      ) {
        setMessages(prev => prev.map(message => message.id === assistantMessageId
          ? { ...message, status: "interrupted" }
          : message
        ));
        if (!stopRequested.current) setInput(text);
        setChatError("The connection stopped before completion. The partial response is marked as interrupted.");
      } else {
        if (retryingInterrupted) {
          setMessages(prev => prev.map(message => message.id === assistantMessageId
            ? {
                ...message,
                content: previousInterruptedText,
                sources: [],
                status: "interrupted"
              }
            : message
          ));
        } else {
          setMessages(prev => prev.filter(message =>
            message.id !== userMessageId && message.id !== assistantMessageId
          ));
        }
        if (!stopRequested.current) setInput(text);
        setChatError(err instanceof Error ? err.message : "I couldn't complete that request.");
      }
    } finally {
      if (activeChatRequest.current === controller) activeChatRequest.current = null;
      if (activeRequestId.current === requestId) activeRequestId.current = null;
      stopRequested.current = false;
      setStopPending(false);
      sendInFlight.current = false;
      setBusy(false);
    }
  }

  if (authRestoring) {
    return <main className="auth-page"><section className="auth-card" role="status">
      <div className="brand-row auth-brand"><div className="brand-mark"><Sparkles size={19}/></div><span className="brand-name">nexa<span>desk</span></span></div>
      <p>Restoring your session…</p>
    </section></main>;
  }

  if (!auth) {
    return <AuthPage
      page={authPage}
      onNavigate={navigateAuthPage}
      onAuthenticated={acceptLogin}
      authMessage={authError}
    />;
  }

  return <div className="app-shell">
    <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
      <div className="brand-row"><div className="brand-mark"><Sparkles size={19}/></div><span className="brand-name">nexa<span>desk</span></span><button className="icon-button mobile-close" onClick={() => setMobileNav(false)} aria-label="Close menu"><X size={18}/></button></div>
      <label className="workspace-switch"><div className="workspace-avatar">N</div><div className="workspace-copy"><strong>{auth.workspaces.find(item => item.id === workspaceId)?.name ?? "Workspace"}</strong><small>{auth.user.email}</small></div><select aria-label="Select workspace" value={workspaceId} onChange={event => {
        const selectedId = event.target.value;
        setWorkspaceId(selectedId);
        window.sessionStorage.setItem(`nexadesk.workspace.${auth.user.id}`, selectedId);
        clearCurrentConversation();
      }} disabled={busy || conversationBusy}>{auth.workspaces.map(workspace => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}</select></label>
      <button className="new-chat" onClick={() => void startNewConversation()} disabled={!workspaceId || conversationBusy || busy}><Plus size={17}/> New conversation <span>⌘ K</span></button>
      <div className="nav-label">WORKSPACE</div>
      <nav className="nav-list">
        <button className={`nav-item ${activeView === "conversations" ? "active" : ""}`} onClick={() => {
          selectView("conversations");
          setMobileNav(false);
        }}><MessageSquare size={17}/> Conversations <span className="nav-count">{conversations.length}</span></button>
        <button className={`nav-item ${activeView === "knowledge" ? "active" : ""}`} onClick={() => {
          selectView("knowledge");
          setMobileNav(false);
        }}><BookOpen size={17}/> Knowledge base</button>
        <a className="nav-item" href="#analytics"><Activity size={17}/> Analytics</a>
        <a className="nav-item" href="#integrations"><Command size={17}/> Integrations</a>
      </nav>
      <div className="nav-label recent-label">RECENT</div>
      <div className="recent-list" aria-label="Conversation history">
        {conversationsLoading && <p className="conversation-list-state">Loading conversations…</p>}
        {!conversationsLoading && conversations.length === 0 && !conversationError && <p className="conversation-list-state">No conversations yet. Start a new one.</p>}
        {!conversationsLoading && conversations.map(conversation => <div className={`recent-row ${conversation.id === conversationId ? "selected" : ""}`} key={conversation.id}>
          {renameId === conversation.id
            ? <form className="rename-form" onSubmit={event => void submitRename(event, conversation)}>
              <input aria-label="Conversation title" autoFocus maxLength={200} value={renameTitle} onChange={event => setRenameTitle(event.target.value)} />
              <button type="submit" aria-label="Save conversation name" disabled={conversationBusy || !renameTitle.trim()}><Check size={14}/></button>
              <button type="button" aria-label="Cancel rename" onClick={() => setRenameId(null)}><X size={14}/></button>
            </form>
            : <>
              <button className={`recent-item ${conversation.id === conversationId ? "selected" : ""}`} onClick={() => void openConversation(conversation)} disabled={messagesLoading || conversationBusy || busy}>
                <span className="recent-dot"/> <span className="recent-title">{conversation.title}</span>
              </button>
              <span className="conversation-actions">
                <button type="button" aria-label={`Rename ${conversation.title}`} title="Rename conversation" onClick={() => {
                  setRenameId(conversation.id);
                  setRenameTitle(conversation.title);
                  setConversationError("");
                }} disabled={conversationBusy || busy}><Pencil size={13}/></button>
                <button type="button" aria-label={`Delete ${conversation.title}`} title="Delete conversation" onClick={() => setPendingDelete(conversation)} disabled={conversationBusy || deleteBusy || busy}><Trash2 size={13}/></button>
              </span>
            </>}
        </div>)}
      </div>
      {conversationError && <p className="conversation-error" role="alert">{conversationError}</p>}
      <div className="sidebar-bottom">
        <div className="usage-card"><div className="usage-top"><span>Your role</span><span>{auth.workspaces.find(item => item.id === workspaceId)?.role ?? "Member"}</span></div><div className="usage-track"><span/></div><small>Conversations are saved to this workspace</small></div>
        <button className="nav-item"><Settings2 size={17}/> Settings</button>
        <button className="profile-row" onClick={signOut}><div className="profile-avatar">{auth.user.email.slice(0, 2).toUpperCase()}</div><div className="profile-copy"><strong>{auth.user.email}</strong><small>Sign out</small></div><MoreHorizontal size={18} className="muted"/></button>
      </div>
    </aside>
    {mobileNav && <button className="mobile-scrim" onClick={() => setMobileNav(false)} aria-label="Close navigation"/>}
    <main className="main-panel">
      <header className="topbar">
        <div className="topbar-left"><button className="icon-button mobile-menu" onClick={() => setMobileNav(true)} aria-label="Open menu"><Menu size={19}/></button><div className="breadcrumb">Workspace <span>/</span> <strong>{activeView === "knowledge" ? "Knowledge base" : "Conversations"}</strong></div></div>
        <div className="topbar-actions"><div className="status-pill"><span/> Ollama</div><button className="icon-button" aria-label="Help"><CircleHelp size={18}/></button><button className="icon-button" aria-label="More options"><MoreHorizontal size={19}/></button></div>
      </header>
      {activeView === "knowledge"
        ? <KnowledgeBase
            token={auth.access_token}
            workspaceId={workspaceId}
            onUnauthorized={expireSession}
          />
        : <section className="chat-layout" id="chat">
        <div className="chat-column">
          <div className="chat-heading"><div><div className="eyebrow"><span className="eyebrow-line"/> YOUR AI WORKSPACE</div><h1>Good morning. <span>Let’s make progress.</span></h1><p>A thoughtful assistant for the work that matters.</p></div><button className="model-selector" aria-label="Configured AI model"><span className="model-orb"><Bot size={15}/></span> {modelInfo ? `${modelInfo.provider} · ${modelInfo.model}` : modelInfoUnavailable ? "Ollama · unavailable" : "Ollama · loading"} <ChevronDown size={14}/></button></div>
          <div className="conversation"><div className="date-divider"><span/> TODAY <span/></div>
            {messagesLoading
              ? <p className="history-state" role="status">Loading conversation…</p>
              : messages.map(m => <article className={`message-row ${m.role}`} key={m.id}>
                <div className={`message-avatar ${m.role}`}>{m.role === "assistant" ? <Sparkles size={16}/> : auth.user.email.slice(0, 2).toUpperCase()}</div>
                <div className="message-body">
                  <div className="message-meta">
                    <strong>{m.role === "assistant" ? "NexaDesk" : "You"}</strong>
                    <span>{m.time}</span>
                  </div>
                  <div className="message-content">
                    {m.role === "assistant"
                      ? <Suspense fallback={<div>{m.content}</div>}><MarkdownMessage content={m.content}/></Suspense>
                      : m.content}
                  </div>
                  {m.status === "interrupted" && <span className="message-interrupted" role="status">
                    Generation stopped · this is an incomplete response
                  </span>}
                  {m.sources && m.sources.length > 0 && <div className="message-sources" aria-label="Sources">
                    {m.sources.map((source, index) => <details className="message-source" key={source.chunk_id ?? source.id}>
                      <summary>{source.citation_label ?? `[${index + 1}]`} {source.document_name} · {source.location}</summary>
                      {source.claims?.map((claim, claimIndex) => <p className="message-source-claim" key={`${claimIndex}-${claim}`}>Supports: {claim}</p>)}
                      <p>{source.excerpt}</p>
                    </details>)}
                  </div>}
                  {m.role === "assistant" && m.id !== "welcome" && <div className="message-tools">
                    <button aria-label="Helpful"><ThumbsUp size={14}/></button>
                    <button aria-label="Not helpful"><ThumbsDown size={14}/></button>
                  </div>}
                </div>
              </article>)}
            {busy && messages[messages.length - 1]?.content === "" && <article className="message-row assistant"><div className="message-avatar assistant"><Sparkles size={16}/></div><div className="message-body"><div className="message-meta"><strong>NexaDesk</strong><span>Thinking</span></div><div className="typing"><i/><i/><i/></div></div></article>}
          </div>
          {messages.length === 1 && <div className="prompt-grid">{prompts.map(({icon: Icon, title, detail}) => <button className="prompt-card" key={title} onClick={() => setInput(title)}><span className="prompt-icon"><Icon size={17}/></span><span><strong>{title}</strong><small>{detail}</small></span><ArrowUp size={15} className="prompt-arrow"/></button>)}</div>}
          <form className="composer" onSubmit={submit}>
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === "Escape" && busy) {
                  e.preventDefault();
                  void stopGeneration();
                } else if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void submit();
                }
              }}
              placeholder="Message NexaDesk…"
              rows={2}
              maxLength={4000}
              aria-label="Message NexaDesk"
              disabled={messagesLoading}
            />
            <div className="composer-footer">
              <div className="composer-hints"><span><Plus size={15}/> Attach</span><span className="hint-divider"/><span><LockKeyhole size={13}/> Saved to workspace</span></div>
              <div className="send-side">
                <small>{input.length}/4000</small>
                {busy
                  ? <button className="stop-button" type="button" onClick={() => void stopGeneration()} aria-label="Stop generating">
                      <X size={15}/><span>{stopPending ? "Stopping…" : "Stop"}</span>
                    </button>
                  : <button className="send-button" type="submit" disabled={!input.trim() || messagesLoading} aria-label="Send message"><ArrowUp size={18}/></button>}
              </div>
            </div>
          </form>
          {chatError && <div className="chat-error" role="alert">{chatError}</div>}
          <div className="disclaimer"><ShieldCheck size={13}/> AI can make mistakes. Verify important information before acting.</div>
        </div>
        <aside className="right-rail">
          <div className="rail-card welcome-card"><div className="rail-card-top"><span className="mini-icon purple"><Zap size={15}/></span><span className="rail-kicker">QUICK OVERVIEW</span><MoreHorizontal size={17}/></div><h3>Your workspace,<br/>at a glance.</h3><p>Your local AI environment is ready to explore.</p><div className="overview-stats"><div><strong>01</strong><small>Assistant</small></div><div><strong>Local</strong><small>Runtime</small></div></div></div>
          <div className="rail-card"><div className="rail-title"><span>GETTING STARTED</span><span className="completion">1 of 4</span></div><div className="check-progress"><span/></div><div className="checklist"><div className="check-row done"><span className="check-circle">✓</span><span><strong>Workspace created</strong><small>Your environment is set up</small></span></div><div className="check-row"><span className="check-circle"/><span><strong>Connect knowledge</strong><small>Add trusted source material</small></span></div><div className="check-row"><span className="check-circle"/><span><strong>Customize assistant</strong><small>Make it yours</small></span></div><div className="check-row"><span className="check-circle"/><span><strong>Install on your site</strong><small>Meet your customers</small></span></div></div></div>
          <div className="rail-card security-card"><div className="security-icon"><ShieldCheck size={17}/></div><div><strong>Built with privacy in mind</strong><p>Local-first development. Messages are sent to the model running on your machine.</p><a href="#security">Explore security approach <ArrowUp size={12}/></a></div></div>
          <div className="rail-footer"><span><LifeBuoy size={13}/> Help center</span><span>v0.1.0 · Preview</span></div>
        </aside>
      </section>}
    </main>
    {pendingDelete && <div className="dialog-scrim" role="presentation">
      <section className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-title" aria-describedby="delete-description">
        <h2 id="delete-title">Delete conversation?</h2>
        <p id="delete-description">“{pendingDelete.title}” and its saved messages will be permanently deleted.</p>
        <div className="dialog-actions">
          <button type="button" onClick={() => setPendingDelete(null)} disabled={deleteBusy}>Cancel</button>
          <button type="button" className="danger-button" onClick={() => void confirmDeleteConversation()} disabled={deleteBusy}>
            {deleteBusy ? "Deleting…" : "Delete"}
          </button>
        </div>
      </section>
    </div>}
  </div>;
}
