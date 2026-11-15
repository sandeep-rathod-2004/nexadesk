import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
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

import KnowledgeBase from "./KnowledgeBase";

const document = {
  id: "doc-1",
  workspace_id: "workspace-1",
  filename: "travel-policy.txt",
  media_type: "text/plain",
  size_bytes: 64,
  status: "ready" as const,
  error_message: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z"
};

afterEach(cleanup);

describe("Knowledge Base UI", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listKnowledgeDocuments.mockResolvedValue([]);
    api.uploadKnowledgeDocument.mockResolvedValue(document);
    api.deleteKnowledgeDocument.mockResolvedValue(undefined);
  });

  it("uploads a document and shows its processing result", async () => {
    const user = userEvent.setup();
    const onDocumentsChange = vi.fn();
    render(<KnowledgeBase
      token="secret-token"
      workspaceId="workspace-1"
      onUnauthorized={vi.fn()}
      documents={[]}
      documentsLoading={false}
      documentsError=""
      onDocumentsChange={onDocumentsChange}
    />);
    const file = new File(["Travel reimbursement details"], "travel-policy.txt", {
      type: "text/plain"
    });

    await user.upload(screen.getByLabelText("Choose a PDF, DOCX, or TXT file"), file);
    await user.click(screen.getByRole("button", { name: "Upload" }));

    expect(await screen.findByText("travel-policy.txt")).toBeTruthy();
    expect(api.uploadKnowledgeDocument).toHaveBeenCalledWith(
      "secret-token",
      "workspace-1",
      file,
      expect.any(Function)
    );
    expect(screen.getByText("ready")).toBeTruthy();
    expect(onDocumentsChange).toHaveBeenCalledWith("workspace-1", [document]);
  });

  it("confirms document deletion before calling the API", async () => {
    const user = userEvent.setup();
    const onDocumentsChange = vi.fn();
    render(<KnowledgeBase
      token="secret-token"
      workspaceId="workspace-1"
      onUnauthorized={vi.fn()}
      documents={[document]}
      documentsLoading={false}
      documentsError=""
      onDocumentsChange={onDocumentsChange}
    />);

    await user.click(await screen.findByRole("button", { name: "Delete travel-policy.txt" }));
    expect(screen.getByRole("alertdialog")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.deleteKnowledgeDocument).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Delete travel-policy.txt" }));
    await user.click(screen.getByRole("button", { name: /^Delete$/ }));
    await waitFor(() => expect(api.deleteKnowledgeDocument).toHaveBeenCalledWith(
      "secret-token",
      "workspace-1",
      "doc-1"
    ));
    expect(screen.queryByText("travel-policy.txt")).toBeNull();
    expect(onDocumentsChange).toHaveBeenCalledWith("workspace-1", []);
  });

  it("shows empty and error states", async () => {
    render(<KnowledgeBase
      token="secret-token"
      workspaceId="workspace-1"
      onUnauthorized={vi.fn()}
      documents={[]}
      documentsLoading={false}
      documentsError="Could not reach service."
      onDocumentsChange={vi.fn()}
    />);

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText("No documents yet")).toBeNull();
  });
});
