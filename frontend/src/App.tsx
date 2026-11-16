import { FormEvent, lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  ArrowUp, Bot, BookOpen, ChevronDown, CircleHelp,
  Check, FileText, LockKeyhole, Menu, MessageSquare,
  Pencil, Plus, Settings2, Sparkles, ThumbsDown, ThumbsUp, Trash2, X, Zap,
  LogOut
} from "lucide-react";
import {
  ApiError, AuthResult, Conversation, createConversation, deleteConversation,
  cancelChat, getMessages, getModel, KnowledgeDocument, listConversations,
  listKnowledgeDocuments, logout, ModelInfo, PersistedMessage,
  refreshSession, setAccessToken,
  renameConversation, SourceReference, StreamMilestone, streamChat,
  updateConversationDocuments
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
const initial: Message[] = [];
const SESSION_RESTORE_TIMEOUT_MS = 10_000;

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

function navigationHintKey(userId: string): string {
  return `nexadesk.navigation-hint.${userId}`;
}

function sidebarPreferenceKey(userId: string): string {
  return `nexadesk.sidebar.${userId}`;
}

export default function App() {
  const [messages, setMessages] = useState<Message[]>(initial);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true);
  const [selectedChatType, setSelectedChatType] = useState<"normal" | "knowledge">("normal");
  const [isMobileLayout, setIsMobileLayout] = useState(
    () => window.matchMedia?.("(max-width: 820px)").matches ?? window.innerWidth <= 820
  );
  const [navigationHintVisible, setNavigationHintVisible] = useState(false);
  const [activeView, setActiveView] = useState<"conversations" | "knowledge">("conversations");
  const [auth, setAuth] = useState<AuthResult | null>(null);
  const [authRestoring, setAuthRestoring] = useState(true);
  const [authRestoreFailed, setAuthRestoreFailed] = useState(false);
  const [authRestoreTimedOut, setAuthRestoreTimedOut] = useState(false);
  const [restoreAttempt, setRestoreAttempt] = useState(0);
  const [modelInfo, setModelInfo] = useState<ModelInfo | null>(null);
  const [modelInfoUnavailable, setModelInfoUnavailable] = useState(false);
  const [authPage, setAuthPage] = useState<AuthPageRoute>(() => authPageFromPath(window.location.pathname));
  const [workspaceId, setWorkspaceId] = useState("");
  const [authError, setAuthError] = useState("");
  const [chatError, setChatError] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [knowledgeDocuments, setKnowledgeDocuments] = useState<KnowledgeDocument[]>([]);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [knowledgeError, setKnowledgeError] = useState("");
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [conversationBusy, setConversationBusy] = useState(false);
  const [conversationError, setConversationError] = useState("");
  const [renameId, setRenameId] = useState<string | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [pendingDelete, setPendingDelete] = useState<Conversation | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [documentDialog, setDocumentDialog] = useState<{
    conversationId: string | null;
    selectedDocumentIds: string[];
    error: string;
  } | null>(null);
  const [documentSaveBusy, setDocumentSaveBusy] = useState(false);
  const [showAllNormalChats, setShowAllNormalChats] = useState(false);
  const [showAllKnowledgeChats, setShowAllKnowledgeChats] = useState(false);
  const sendInFlight = useRef(false);
  const startConversationInFlight = useRef(false);
  const saveDocumentsInFlight = useRef(false);
  const sidebarRef = useRef<HTMLElement>(null);
  const activeChatRequest = useRef<AbortController | null>(null);
  const activeRequestId = useRef<string | null>(null);
  const stopRequested = useRef(false);
  const currentWorkspaceId = useRef(workspaceId);
  currentWorkspaceId.current = workspaceId;
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
  const normalChats = conversations.filter(conversation => conversation.chat_type !== "knowledge");
  const knowledgeChats = conversations.filter(conversation => conversation.chat_type === "knowledge");
  const activeConversation = conversations.find(item => item.id === conversationId) ?? null;

  useEffect(() => {
    const handlePopState = () => setAuthPage(authPageFromPath(window.location.pathname));
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    const mediaQuery = window.matchMedia?.("(max-width: 820px)");
    const updateLayout = () => {
      const mobile = mediaQuery?.matches ?? window.innerWidth <= 820;
      setIsMobileLayout(mobile);
      if (!mobile) setMobileNav(false);
    };
    window.addEventListener("resize", updateLayout);
    mediaQuery?.addEventListener("change", updateLayout);
    return () => {
      window.removeEventListener("resize", updateLayout);
      mediaQuery?.removeEventListener("change", updateLayout);
    };
  }, []);

  useEffect(() => {
    let active = true;
    let settled = false;
    const timeoutId = window.setTimeout(() => {
      if (active && !settled) setAuthRestoreTimedOut(true);
    }, SESSION_RESTORE_TIMEOUT_MS);
    void refreshSession()
      .then(result => {
        if (!active) return;
        setAuthRestoreFailed(false);
        setAuthRestoreTimedOut(false);
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
      .catch(error => {
        if (!active) return;
        setAccessToken(null);
        setAuthRestoreFailed(!(error instanceof ApiError && error.status === 401));
      })
      .finally(() => {
        settled = true;
        window.clearTimeout(timeoutId);
        if (active) setAuthRestoring(false);
      });
    return () => {
      active = false;
      window.clearTimeout(timeoutId);
    };
  }, [restoreAttempt]);

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
        if (!active) return;
        if (
          typeof info.provider !== "string" || !info.provider.trim() ||
          typeof info.model !== "string" || !info.model.trim()
        ) {
          setModelInfoUnavailable(true);
          return;
        }
        setModelInfo(info);
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

  useEffect(() => {
    if (!auth) {
      setNavigationHintVisible(false);
      return;
    }
    setNavigationHintVisible(
      window.localStorage.getItem(navigationHintKey(auth.user.id)) !== "dismissed"
    );
  }, [auth]);

  useEffect(() => {
    if (!auth) return;
    setSidebarCollapsed(
      window.localStorage.getItem(sidebarPreferenceKey(auth.user.id)) !== "expanded"
    );
  }, [auth?.user.id]);

  useEffect(() => {
    if (!mobileNav) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileNav(false);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [mobileNav]);

  useEffect(() => {
    if (!documentDialog) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !documentSaveBusy) {
        setDocumentDialog(null);
      }
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [documentDialog, documentSaveBusy]);

  const sidebarHidden = isMobileLayout ? !mobileNav : sidebarCollapsed;

  useEffect(() => {
    sidebarRef.current?.toggleAttribute("inert", sidebarHidden);
  }, [sidebarHidden]);

  useEffect(() => {
    if (!auth || !workspaceId) {
      setKnowledgeDocuments([]);
      setKnowledgeLoading(false);
      setKnowledgeError("");
      return;
    }
    let active = true;
    setKnowledgeDocuments([]);
    setKnowledgeLoading(true);
    setKnowledgeError("");
    void listKnowledgeDocuments(auth.access_token, workspaceId)
      .then(documents => {
        if (active) setKnowledgeDocuments(documents);
      })
      .catch(error => {
        if (!active) return;
        if (error instanceof ApiError && error.status === 401) {
          expireSession(error);
          return;
        }
        setKnowledgeDocuments([]);
        setKnowledgeError(
          error instanceof Error ? error.message : "Could not load workspace knowledge."
        );
      })
      .finally(() => {
        if (active) setKnowledgeLoading(false);
      });
    return () => { active = false; };
  }, [auth, workspaceId]);

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
    setKnowledgeDocuments([]);
    setKnowledgeLoading(false);
    setKnowledgeError("");
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
    setMobileNav(false);
    if (auth) {
      window.sessionStorage.setItem(viewStorageKey(auth.user.id), view);
    }
  }

  function toggleNavigation() {
    setNavigationHintVisible(false);
    if (auth) {
      window.localStorage.setItem(navigationHintKey(auth.user.id), "dismissed");
    }
    if (isMobileLayout) {
      setMobileNav(value => !value);
      return;
    }
    setSidebarCollapsed(value => {
      const next = !value;
      if (auth) {
        window.localStorage.setItem(
          sidebarPreferenceKey(auth.user.id),
          next ? "collapsed" : "expanded"
        );
      }
      return next;
    });
  }

  function closeNavigation() {
    if (isMobileLayout) {
      setMobileNav(false);
      return;
    }
    setSidebarCollapsed(true);
    if (auth) {
      window.localStorage.setItem(sidebarPreferenceKey(auth.user.id), "collapsed");
    }
  }

  function retrySessionRestore() {
    setAuthRestoreFailed(false);
    setAuthRestoreTimedOut(false);
    setAuthRestoring(true);
    setRestoreAttempt(attempt => attempt + 1);
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

  function openDocumentDialog(conversation: Conversation | null = null) {
    setDocumentDialog({
      conversationId: conversation?.id ?? null,
      selectedDocumentIds: conversation?.document_ids ?? [],
      error: ""
    });
    setMobileNav(false);
  }

  async function startNewConversation(
    chatType: "normal" | "knowledge" = "normal",
    documentIds?: string[]
  ): Promise<boolean> {
    if (
      !auth || !workspaceId || conversationBusy || busy ||
      startConversationInFlight.current
    ) return false;
    if (chatType === "knowledge" && documentIds === undefined) {
      openDocumentDialog();
      return true;
    }
    startConversationInFlight.current = true;
    const requestId = ++conversationRequest.current;
    setMessagesLoading(false);
    setConversationBusy(true);
    setConversationError("");
    setChatError("");
    try {
      const created = chatType === "normal" && documentIds === undefined
        ? await createConversation(auth.access_token, workspaceId)
        : await createConversation(
            auth.access_token,
            workspaceId,
            "New conversation",
            chatType,
            documentIds ?? []
          );
      if (requestId !== conversationRequest.current) return false;
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
      setDocumentDialog(null);
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        expireSession(error);
      } else {
        const message = error instanceof Error ? error.message : "Could not create a conversation.";
        if (chatType === "knowledge" && documentDialog) {
          setDocumentDialog(current => current ? { ...current, error: message } : current);
        } else {
          setConversationError(message);
        }
      }
      return false;
    } finally {
      startConversationInFlight.current = false;
      setConversationBusy(false);
    }
  }

  async function saveDocumentSelection() {
    if (
      !auth || !documentDialog || documentSaveBusy ||
      saveDocumentsInFlight.current
    ) return;
    if (documentDialog.selectedDocumentIds.length === 0) {
      setDocumentDialog(current => current
        ? { ...current, error: "Select at least one ready document." }
        : current
      );
      return;
    }
    saveDocumentsInFlight.current = true;
    setDocumentSaveBusy(true);
    if (documentDialog.conversationId === null) {
      try {
        const created = await startNewConversation(
          "knowledge",
          documentDialog.selectedDocumentIds
        );
        if (!created) {
          setDocumentDialog(current => current
            ? { ...current, error: current.error || "Could not create this Knowledge Chat." }
            : current
          );
        }
      } finally {
        saveDocumentsInFlight.current = false;
        setDocumentSaveBusy(false);
      }
      return;
    }
    try {
      const updated = await updateConversationDocuments(
        auth.access_token,
        documentDialog.conversationId,
        documentDialog.selectedDocumentIds
      );
      setConversations(current => current.map(item => item.id === updated.id ? updated : item));
      setDocumentDialog(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        expireSession(error);
      } else {
        setDocumentDialog(current => current
          ? {
              ...current,
              error: error instanceof Error
                ? error.message
                : "Could not update this Knowledge Chat."
            }
          : current
        );
      }
    } finally {
      saveDocumentsInFlight.current = false;
      setDocumentSaveBusy(false);
    }
  }

  async function openConversation(
    conversation: Conversation,
    preserveView = false
  ) {
    if (!auth) return;
    setMobileNav(false);
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

  function renderConversationRows(items: Conversation[], showAll: boolean) {
    const visibleItems = showAll ? items : items.slice(0, 2);
    return <>
      {visibleItems.map(conversation => <div className={`recent-row ${conversation.id === conversationId ? "selected" : ""}`} key={conversation.id}>
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
      {items.length > 2 && <button
        className="conversation-show-more"
        type="button"
        onClick={() => conversationGroupToggle(items[0]?.chat_type)}
      >
        {showAll ? "Show less" : `Show ${items.length - 2} more`}
      </button>}
    </>;
  }

  function conversationGroupToggle(chatType: "normal" | "knowledge" | undefined) {
    if (chatType === "knowledge") {
      setShowAllKnowledgeChats(value => !value);
    } else {
      setShowAllNormalChats(value => !value);
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

  async function submit(e?: FormEvent, suggestedText?: string) {
    e?.preventDefault();
    const text = (suggestedText ?? input).trim();
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

  const activeWorkspace = auth?.workspaces.find(item => item.id === workspaceId);
  const workspaceCreated = Boolean(activeWorkspace);
  const knowledgeConnected = knowledgeDocuments.some(document => document.status === "ready");
  const completedSteps = Number(workspaceCreated) + Number(knowledgeConnected);
  const progressPercent = (completedSteps / 4) * 100;
  const modelLabel = modelInfo
    ? `${modelInfo.provider} · ${modelInfo.model}`
    : modelInfoUnavailable
      ? "AI provider unavailable"
      : "Loading AI provider…";
  const modelProvider = modelInfo?.provider.toLowerCase();
  const providerProcessingMessage = modelProvider?.includes("gemini")
    ? "Messages are processed by your configured Gemini provider."
    : modelProvider?.includes("ollama")
      ? "Messages are processed by your configured local Ollama runtime."
      : "AI provider information is currently unavailable.";

  if ((authRestoring && authRestoreTimedOut) || (!authRestoring && authRestoreFailed)) {
    return <main className="auth-page"><section className="auth-card session-restore-error" role="alert">
      <div className="brand-row auth-brand"><div className="brand-mark"><Sparkles size={19}/></div><span className="brand-name">nexa<span>desk</span></span></div>
      <p>{authRestoreTimedOut
        ? "Session restoration is taking longer than expected."
        : "We couldn't restore your session. Check your connection and try again."}</p>
      <button type="button" className="auth-submit" onClick={retrySessionRestore}>Retry session restoration</button>
    </section></main>;
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
    <aside ref={sidebarRef} id="workspace-navigation" className={`sidebar ${mobileNav ? "sidebar-open" : ""} ${sidebarCollapsed ? "sidebar-collapsed" : ""}`} aria-label="Workspace navigation" aria-hidden={sidebarHidden}>
      <div className="brand-row"><div className="brand-mark"><Sparkles size={19}/></div><span className="brand-name">nexa<span>desk</span></span><button className="icon-button mobile-close" onClick={closeNavigation} aria-label={isMobileLayout ? "Close menu" : "Collapse sidebar"}><X size={18}/></button></div>
      <label className="workspace-switch"><div className="workspace-avatar">N</div><div className="workspace-copy"><strong>{auth.workspaces.find(item => item.id === workspaceId)?.name ?? "Workspace"}</strong><small>{auth.user.email}</small></div><ChevronDown size={15} aria-hidden="true"/><select aria-label="Select workspace" value={workspaceId} onChange={event => {
        const selectedId = event.target.value;
        setWorkspaceId(selectedId);
        setKnowledgeDocuments([]);
        setKnowledgeLoading(Boolean(selectedId));
        setKnowledgeError("");
        window.sessionStorage.setItem(`nexadesk.workspace.${auth.user.id}`, selectedId);
        clearCurrentConversation();
      }} disabled={busy || conversationBusy}>{auth.workspaces.map(workspace => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}</select></label>
      <button className="new-chat" onClick={() => void startNewConversation()} disabled={!workspaceId || conversationBusy || busy}><Plus size={17}/> New conversation</button>
      <button className="new-knowledge-chat" onClick={() => void startNewConversation("knowledge")} disabled={!workspaceId || conversationBusy || busy}><BookOpen size={16}/> New Knowledge Chat</button>
      <div className="nav-label">WORKSPACE</div>
      <nav className="nav-list">
        <button className={`nav-item ${activeView === "conversations" ? "active" : ""}`} onClick={() => selectView("conversations")}><MessageSquare size={17}/> Conversations <span className="nav-count">{conversations.length}</span></button>
        <button className={`nav-item ${activeView === "knowledge" ? "active" : ""}`} onClick={() => selectView("knowledge")}><BookOpen size={17}/> Knowledge base</button>
        <button className="nav-item unavailable-control" type="button" disabled title="Analytics is coming soon" aria-label="Analytics (coming soon)"><MessageSquare size={17}/> Analytics <span className="coming-soon-label">Coming soon</span></button>
        <button className="nav-item unavailable-control" type="button" disabled title="Integrations are coming soon" aria-label="Integrations (coming soon)"><Zap size={17}/> Integrations <span className="coming-soon-label">Coming soon</span></button>
      </nav>
      <div className="nav-label recent-label">RECENT CHATS</div>
      <div className="recent-list" aria-label="Conversation history">
        {conversationsLoading && <p className="conversation-list-state">Loading conversations…</p>}
        {!conversationsLoading && conversations.length === 0 && !conversationError && <p className="conversation-list-state">No conversations yet. Start a new one.</p>}
        {!conversationsLoading && <>
          <section className="conversation-group" aria-label="Normal Chats">
            <h2>Normal Chats <span>{normalChats.length}</span></h2>
            {normalChats.length
              ? renderConversationRows(normalChats, showAllNormalChats)
              : <p className="conversation-list-state">No normal chats yet.</p>}
          </section>
          <section className="conversation-group" aria-label="Knowledge Chats">
            <h2>Knowledge Chats <span>{knowledgeChats.length}</span></h2>
            {knowledgeChats.length
              ? renderConversationRows(knowledgeChats, showAllKnowledgeChats)
              : <p className="conversation-list-state">No knowledge chats yet.</p>}
          </section>
        </>}
      </div>
      {conversationError && <p className="conversation-error" role="alert">{conversationError}</p>}
      <div className="sidebar-bottom">
        <div className="sidebar-progress">
          <div className="rail-title"><span>GETTING STARTED</span><span className="completion">{knowledgeLoading ? "Loading…" : `${completedSteps} of 4`}</span></div>
          <div className="check-progress" role="progressbar" aria-label="Getting started progress" aria-valuemin={0} aria-valuemax={4} aria-valuenow={completedSteps} aria-valuetext={`${completedSteps} of 4 steps complete`}><span style={{ width: `${progressPercent}%` }}/></div>
          <div className={`sidebar-progress-step ${workspaceCreated ? "done" : ""}`}>{workspaceCreated ? <Check size={12}/> : null}<span>{workspaceCreated ? "Workspace available" : "Create or join a workspace"}</span></div>
          <div className={`sidebar-progress-step ${knowledgeConnected ? "done" : ""}`}>{knowledgeConnected ? <Check size={12}/> : null}<span>{knowledgeLoading ? "Checking workspace sources…" : knowledgeError ? "Knowledge status unavailable" : knowledgeConnected ? "A document is ready" : knowledgeDocuments.some(document => document.status === "processing") ? "Documents are processing" : "Add a ready source document"}</span></div>
          <div className="sidebar-progress-step unavailable-step"><span><strong>Customize assistant</strong><small>Coming soon · no saved customization is available</small></span></div>
          <div className="sidebar-progress-step unavailable-step"><span><strong>Install on your site</strong><small>Coming soon · installation is not available</small></span></div>
        </div>
        <div className="usage-card"><div className="usage-top"><span>Your role</span><span>{activeWorkspace?.role ?? "Unavailable"}</span></div><small>Conversations are saved to this workspace</small></div>
        <div className="sidebar-provider-note"><strong>AI provider &amp; processing</strong><span>{providerProcessingMessage}</span></div>
        <button className="nav-item unavailable-control" type="button" disabled title="Settings are coming soon" aria-label="Settings (coming soon)"><Settings2 size={17}/> Settings <span className="coming-soon-label">Coming soon</span></button>
        <button className="profile-row" onClick={signOut}><div className="profile-avatar">{auth.user.email.slice(0, 2).toUpperCase()}</div><div className="profile-copy"><strong>{auth.user.email}</strong><small>Sign out</small></div><LogOut size={17} className="muted"/></button>
      </div>
    </aside>
    {mobileNav && <button className="mobile-scrim" onClick={() => setMobileNav(false)} aria-label="Close navigation"/>}
    <main className={`main-panel ${activeView === "conversations" ? "chat-main-panel" : ""}`}>
      <header className="topbar">
        <div className="topbar-left">
          <button className="icon-button nav-toggle" onClick={toggleNavigation} aria-label={isMobileLayout ? (mobileNav ? "Close menu" : "Open menu") : (sidebarCollapsed ? "Open sidebar" : "Collapse sidebar")} title={isMobileLayout ? (mobileNav ? "Close navigation" : "Open navigation") : (sidebarCollapsed ? "Open sidebar" : "Collapse sidebar")} aria-controls="workspace-navigation" aria-expanded={isMobileLayout ? mobileNav : !sidebarCollapsed}><Menu size={21}/></button>
          {navigationHintVisible && <div className="navigation-hint" role="status">Open navigation — Explore your workspace features</div>}
          <div className="topbar-brand"><span className="brand-mark"><Sparkles size={17}/></span><span className="brand-name">nexa<span>desk</span></span></div>
        </div>
        <div className="topbar-actions">
          {activeConversation?.chat_type === "knowledge" && activeView === "conversations" && <button
            className="manage-sources-button"
            type="button"
            onClick={() => openDocumentDialog(activeConversation)}
            disabled={busy || conversationBusy || documentSaveBusy}
          ><BookOpen size={15}/> Sources</button>}
          <button className="model-selector topbar-model unavailable-control" type="button" disabled title="Configured AI provider and model" aria-label="Configured AI model"><span className="model-orb"><Bot size={14}/></span>{modelLabel}</button>
          <button className="icon-button unavailable-control" type="button" disabled title="Help is coming soon" aria-label="Help (coming soon)"><CircleHelp size={18}/></button>
        </div>
      </header>
      {activeView === "knowledge"
        ? <KnowledgeBase
            token={auth.access_token}
            workspaceId={workspaceId}
            onUnauthorized={expireSession}
            documents={knowledgeDocuments}
            documentsLoading={knowledgeLoading}
            documentsError={knowledgeError}
            onDocumentsChange={(sourceWorkspaceId, documents) => {
              if (sourceWorkspaceId !== currentWorkspaceId.current) return;
              setKnowledgeDocuments(documents);
              const documentIds = new Set(documents.map(document => document.id));
              setConversations(current => current.map(conversation =>
                conversation.chat_type === "knowledge"
                  ? {
                      ...conversation,
                      document_ids: (conversation.document_ids ?? []).filter(id => documentIds.has(id))
                    }
                  : conversation
              ));
              setKnowledgeError("");
            }}
          />
        : <section className="chat-layout" id="chat">
          <div className="chat-column">
            <div className={`conversation ${messages.length === 0 && !messagesLoading ? "conversation-empty" : ""}`} role="log" aria-live="polite">
              {messagesLoading
                ? <p className="history-state" role="status">Loading conversation…</p>
                : messages.length === 0
                  ? <div className="empty-state">
                    <span className="empty-state-icon"><Sparkles size={25}/></span>
                    <h1>Welcome to NexaDesk</h1>
                    <p className="empty-subtitle">Your AI workspace. What would you like to work on today?</p>
                    {conversationId === null && <>
                    <div className="chat-mode-grid" role="group" aria-label="Choose a chat mode">
                      <button
                        type="button"
                        className={`chat-mode-card ${selectedChatType === "normal" ? "selected" : ""}`}
                        aria-pressed={selectedChatType === "normal"}
                        onClick={() => setSelectedChatType("normal")}
                      >
                        <span className="chat-mode-icon"><MessageSquare size={22}/></span>
                        <span className="chat-mode-copy">
                          <strong>Normal Conversation</strong>
                          <span>General AI chat for everyday tasks.</span>
                        </span>
                        <span className="chat-mode-indicator" aria-hidden="true">
                          {selectedChatType === "normal" && <Check size={15}/>}
                        </span>
                      </button>
                      <button
                        type="button"
                        className={`chat-mode-card ${selectedChatType === "knowledge" ? "selected" : ""}`}
                        aria-pressed={selectedChatType === "knowledge"}
                        onClick={() => setSelectedChatType("knowledge")}
                      >
                        <span className="chat-mode-icon"><BookOpen size={22}/></span>
                        <span className="chat-mode-copy">
                          <strong>Knowledge Base</strong>
                          <span>Chat with your documents and data.</span>
                        </span>
                        <span className="chat-mode-indicator" aria-hidden="true">
                          {selectedChatType === "knowledge" && <Check size={15}/>}
                        </span>
                      </button>
                    </div>
                    <button
                      type="button"
                      className="start-chat-button"
                      disabled={!workspaceId || conversationBusy || busy}
                      onClick={() => void startNewConversation(selectedChatType)}
                    >
                      {conversationBusy ? "Starting…" : "Start Chat"}
                    </button>
                    </>}
                  </div>
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
                      {m.role === "assistant" && <div className="message-tools">
                        <button type="button" disabled title="Message feedback is coming soon" aria-label="Helpful (coming soon)"><ThumbsUp size={14}/></button>
                        <button type="button" disabled title="Message feedback is coming soon" aria-label="Not helpful (coming soon)"><ThumbsDown size={14}/></button>
                      </div>}
                    </div>
                  </article>)}
              {busy && messages[messages.length - 1]?.content === "" && <article className="message-row assistant"><div className="message-avatar assistant"><Sparkles size={16}/></div><div className="message-body"><div className="message-meta"><strong>NexaDesk</strong><span>Thinking</span></div><div className="typing"><i/><i/><i/></div></div></article>}
            </div>
            {conversationError && <div className="chat-error" role="alert">{conversationError}</div>}
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
                <div className="composer-hints"><button type="button" className="attach-control unavailable-control" disabled title="File attachments are coming soon" aria-label="Attach files (coming soon)"><Plus size={15}/> Attach</button><span className="hint-divider"/><span><LockKeyhole size={13}/> Saved to workspace</span></div>
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
          </div>
        </section>}
    </main>
    {documentDialog && <div
      className="dialog-scrim"
      role="presentation"
      onMouseDown={event => {
        if (event.target === event.currentTarget && !documentSaveBusy) {
          setDocumentDialog(null);
        }
      }}
    >
      <section
        className="document-picker-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="document-picker-title"
        aria-describedby="document-picker-description"
      >
        <div className="document-picker-heading">
          <div>
            <h2 id="document-picker-title">
              {documentDialog.conversationId ? "Manage Knowledge Chat sources" : "Create a Knowledge Chat"}
            </h2>
            <p id="document-picker-description">
              This chat can retrieve evidence only from the ready documents you select.
            </p>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close document selection"
            onClick={() => setDocumentDialog(null)}
            disabled={documentSaveBusy}
          ><X size={18}/></button>
        </div>
        {knowledgeLoading
          ? <p className="document-picker-state" role="status">Loading workspace documents…</p>
          : knowledgeError
            ? <p className="document-picker-error" role="alert">{knowledgeError}</p>
            : knowledgeDocuments.filter(document => document.status === "ready").length === 0
              ? <div className="document-picker-empty">
                <p>No ready documents are available in this workspace.</p>
                <button type="button" onClick={() => {
                  setDocumentDialog(null);
                  selectView("knowledge");
                }}>Open Knowledge Base</button>
              </div>
              : <div className="document-picker-list">
                {knowledgeDocuments
                  .filter(document => document.status === "ready")
                  .map(document => <label className="document-picker-option" key={document.id}>
                    <input
                      type="checkbox"
                      checked={documentDialog.selectedDocumentIds.includes(document.id)}
                      onChange={event => setDocumentDialog(current => current
                        ? {
                            ...current,
                            error: "",
                            selectedDocumentIds: event.target.checked
                              ? [...current.selectedDocumentIds, document.id]
                              : current.selectedDocumentIds.filter(id => id !== document.id)
                          }
                        : current
                      )}
                    />
                    <FileText size={17}/>
                    <span>{document.filename}</span>
                  </label>)}
              </div>}
        {documentDialog.error && <p className="document-picker-error" role="alert">{documentDialog.error}</p>}
        <div className="dialog-actions">
          <button type="button" onClick={() => setDocumentDialog(null)} disabled={documentSaveBusy}>Cancel</button>
          <button
            type="button"
            onClick={() => void saveDocumentSelection()}
            disabled={documentSaveBusy || knowledgeLoading || Boolean(knowledgeError) || !knowledgeDocuments.some(document => document.status === "ready")}
          >{documentSaveBusy ? "Saving…" : documentDialog.conversationId ? "Save sources" : "Create Knowledge Chat"}</button>
        </div>
      </section>
    </div>}
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
