const publicShellStages = new Set<string>();

export type FrontendPerformanceStage =
  | "public_shell_rendered"
  | "refresh_request_started"
  | "refresh_request_completed"
  | "refresh_request_failed"
  | "authentication_restoration_completed"
  | "dashboard_interactive"
  | "model_loading_started"
  | "model_loading_completed"
  | "model_loading_failed"
  | "conversations_loading_started"
  | "conversations_loading_completed"
  | "conversations_loading_failed"
  | "documents_loading_started"
  | "documents_loading_completed"
  | "documents_loading_failed";

export function recordFrontendPerformance(
  stage: FrontendPerformanceStage,
  startedAt?: number,
): void {
  if (typeof performance === "undefined") return;

  const elapsedMs = typeof startedAt === "number"
    ? Math.round((performance.now() - startedAt) * 100) / 100
    : undefined;

  if (stage === "public_shell_rendered" && publicShellStages.has(stage)) return;
  if (stage === "public_shell_rendered") publicShellStages.add(stage);

  const event: Record<string, string | number> = {
    event: "nexadesk_frontend_performance",
    stage,
  };
  if (elapsedMs !== undefined) event.elapsed_ms = elapsedMs;
  console.info(JSON.stringify(event));
}
