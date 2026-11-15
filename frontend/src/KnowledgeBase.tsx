import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react";
import { AlertCircle, BookOpen, FileText, LoaderCircle, Trash2, Upload, X } from "lucide-react";
import {
  ApiError,
  deleteKnowledgeDocument,
  KnowledgeDocument,
  listKnowledgeDocuments,
  uploadKnowledgeDocument
} from "./lib/api";

type Props = {
  token: string;
  workspaceId: string;
  onUnauthorized: (error: ApiError) => void;
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function KnowledgeBase({ token, workspaceId, onUnauthorized }: Props) {
  const documentInput = useRef<HTMLInputElement>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [pendingDelete, setPendingDelete] = useState<KnowledgeDocument | null>(null);
  const [deleting, setDeleting] = useState(false);
  const unauthorizedHandler = useRef(onUnauthorized);
  unauthorizedHandler.current = onUnauthorized;

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    void listKnowledgeDocuments(token, workspaceId)
      .then(items => {
        if (active) setDocuments(items);
      })
      .catch(reason => {
        if (!active) return;
        if (reason instanceof ApiError && reason.status === 401) {
          unauthorizedHandler.current(reason);
        }
        setError(reason instanceof Error ? reason.message : "Could not load documents.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [token, workspaceId]);

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    setSelectedFile(event.target.files?.[0] ?? null);
    setError("");
  }

  async function submitUpload(event: FormEvent) {
    event.preventDefault();
    if (!selectedFile || uploading) return;
    setUploading(true);
    setUploadProgress(0);
    setError("");
    try {
      const document = await uploadKnowledgeDocument(
        token,
        workspaceId,
        selectedFile,
        setUploadProgress
      );
      setDocuments(current => [document, ...current.filter(item => item.id !== document.id)]);
      setSelectedFile(null);
      const input = documentInput.current;
      if (input) input.value = "";
      if (document.status === "failed") {
        setError(document.error_message ?? "This document could not be processed.");
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        unauthorizedHandler.current(reason);
      }
      setError(reason instanceof Error ? reason.message : "The document upload failed.");
    } finally {
      setUploading(false);
      setUploadProgress(0);
    }
  }

  async function confirmDelete() {
    if (!pendingDelete || deleting) return;
    setDeleting(true);
    setError("");
    try {
      await deleteKnowledgeDocument(token, workspaceId, pendingDelete.id);
      setDocuments(current => current.filter(item => item.id !== pendingDelete.id));
      setPendingDelete(null);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        unauthorizedHandler.current(reason);
      }
      setError(reason instanceof Error ? reason.message : "Could not delete this document.");
    } finally {
      setDeleting(false);
    }
  }

  return <section className="knowledge-layout" aria-labelledby="knowledge-title">
    <div className="knowledge-heading">
      <div>
        <div className="eyebrow"><span className="eyebrow-line"/> WORKSPACE SOURCES</div>
        <h1 id="knowledge-title">Knowledge base</h1>
        <p>Upload trusted documents so NexaDesk can answer with relevant sources.</p>
      </div>
      <span className="knowledge-count"><BookOpen size={15}/>{documents.length} documents</span>
    </div>

    <form className="upload-card" onSubmit={submitUpload}>
      <input
        ref={documentInput}
        id="knowledge-file"
        type="file"
        accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
        onChange={chooseFile}
        aria-label="Choose a PDF, DOCX, or TXT file"
        disabled={uploading}
      />
      <div className="upload-copy">
        <span className="upload-icon"><Upload size={19}/></span>
        <div><strong>Add a document</strong><small>PDF, DOCX, or TXT · Up to 10 MB</small></div>
      </div>
      <div className="upload-controls">
        {selectedFile && <span className="selected-file">{selectedFile.name} · {formatSize(selectedFile.size)}</span>}
        <label className={`choose-file-button ${uploading ? "disabled" : ""}`} htmlFor="knowledge-file">
          Choose file
        </label>
        <button className="upload-button" type="submit" disabled={!selectedFile || uploading}>
          {uploading ? <><LoaderCircle size={15} className="spin"/> Uploading…</> : "Upload"}
        </button>
      </div>
      {uploading && <div className="upload-progress" role="status">
        <span style={{ width: `${uploadProgress}%` }}/>
        <small>{uploadProgress === 100 ? "Processing document…" : `Uploading ${uploadProgress}%`}</small>
      </div>}
    </form>

    {error && <div className="knowledge-error" role="alert"><AlertCircle size={15}/>{error}</div>}

    <div className="document-section">
      <div className="document-section-title"><h2>Documents</h2><span>Private to this workspace</span></div>
      {loading && <p className="knowledge-state" role="status">Loading documents…</p>}
      {!loading && documents.length === 0 && !error && <div className="knowledge-empty">
        <span><BookOpen size={21}/></span><strong>No documents yet</strong>
        <p>Upload a PDF, DOCX, or TXT file to ground answers in your own knowledge.</p>
      </div>}
      {!loading && documents.length > 0 && <div className="document-list">
        {documents.map(document => <article className="document-row" key={document.id}>
          <span className="document-icon"><FileText size={17}/></span>
          <div className="document-details">
            <strong>{document.filename}</strong>
            <small>{formatSize(document.size_bytes)} · Added {new Date(document.created_at).toLocaleDateString()}</small>
            {document.error_message && <small className="document-error">{document.error_message}</small>}
          </div>
          <span className={`document-status ${document.status}`}>
            {document.status === "processing" ? <LoaderCircle size={12} className="spin"/> : null}
            {document.status}
          </span>
          <button type="button" className="document-delete" aria-label={`Delete ${document.filename}`} onClick={() => setPendingDelete(document)} disabled={deleting}>
            <Trash2 size={15}/>
          </button>
        </article>)}
      </div>}
    </div>

    {pendingDelete && <div className="dialog-scrim" role="presentation">
      <section className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="document-delete-title" aria-describedby="document-delete-description">
        <button type="button" className="dialog-close" aria-label="Close confirmation" onClick={() => setPendingDelete(null)} disabled={deleting}><X size={16}/></button>
        <h2 id="document-delete-title">Delete document?</h2>
        <p id="document-delete-description">“{pendingDelete.filename}” and its indexed content will be permanently deleted.</p>
        <div className="dialog-actions">
          <button type="button" onClick={() => setPendingDelete(null)} disabled={deleting}>Cancel</button>
          <button type="button" className="danger-button" onClick={() => void confirmDelete()} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete"}
          </button>
        </div>
      </section>
    </div>}
  </section>;
}
