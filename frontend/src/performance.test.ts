import { afterEach, describe, expect, it, vi } from "vitest";
import { recordFrontendPerformance } from "./performance";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("frontend performance measurements", () => {
  it("records stage and elapsed time without sensitive payload fields", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);

    recordFrontendPerformance("refresh_request_completed", performance.now() - 25);

    expect(info).toHaveBeenCalledTimes(1);
    const event = JSON.parse(String(info.mock.calls[0][0])) as Record<string, unknown>;
    expect(event.event).toBe("nexadesk_frontend_performance");
    expect(event.stage).toBe("refresh_request_completed");
    expect(typeof event.elapsed_ms).toBe("number");
    expect(event).not.toHaveProperty("token");
    expect(event).not.toHaveProperty("password");
  });

  it("deduplicates public shell measurement under StrictMode replay", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);

    recordFrontendPerformance("public_shell_rendered");
    recordFrontendPerformance("public_shell_rendered");

    expect(info).toHaveBeenCalledTimes(1);
  });
});
