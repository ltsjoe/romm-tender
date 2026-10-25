import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { toaster } from "@decky/api";
import * as backend from "../api/backend";
import type { HostedSession } from "../api/backend";
import { emitDeckyEvent } from "../test-utils/decky-api-mock";
import {
  HOSTED_SESSIONS_POLL_MS,
  getHostedSessions,
  pollHostedSessions,
  resetHostedSessionsForTests,
  startHostedSessionPolling,
  stopHostedSessionPolling,
} from "./hostedSessions";
import type { DownloadCompleteEvent } from "../types";

function session(overrides: Partial<HostedSession> = {}): HostedSession {
  return {
    key: "7:42:2026-10-25T20:00:00+00:00",
    rom_id: 42,
    rom_name: "Crash Team Racing",
    app_id: 100,
    host_user_id: 7,
    host_username: "sam",
    kind: "retroarch",
    started_at: "2026-10-25T20:00:00+00:00",
    state: "join",
    ...overrides,
  };
}

const answer = (sessions: HostedSession[]) => ({ success: true as const, sessions });

const completed = (romId: number): DownloadCompleteEvent => ({
  rom_id: romId,
  rom_name: "Crash Team Racing",
  platform_name: "PlayStation",
  file_path: "/roms/psx/ctr.chd",
  app_id: 100,
  launch_options: "",
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  resetHostedSessionsForTests();
});

afterEach(() => {
  stopHostedSessionPolling();
  vi.useRealTimers();
});

describe("hostedSessions — the poller", () => {
  it("asks at start and then every 30 seconds, whether or not the panel is open", async () => {
    vi.mocked(backend.listHostedSessions).mockResolvedValue(answer([]));

    startHostedSessionPolling();
    await vi.advanceTimersByTimeAsync(0);
    expect(backend.listHostedSessions).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(HOSTED_SESSIONS_POLL_MS);
    expect(backend.listHostedSessions).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(HOSTED_SESSIONS_POLL_MS);
    expect(backend.listHostedSessions).toHaveBeenCalledTimes(3);
    expect(HOSTED_SESSIONS_POLL_MS).toBe(30_000);
  });

  it("starts one timer however often it is started, and asks nothing once stopped", async () => {
    vi.mocked(backend.listHostedSessions).mockResolvedValue(answer([]));

    startHostedSessionPolling();
    startHostedSessionPolling();
    await vi.advanceTimersByTimeAsync(HOSTED_SESSIONS_POLL_MS);
    expect(backend.listHostedSessions).toHaveBeenCalledTimes(2);

    stopHostedSessionPolling();
    await vi.advanceTimersByTimeAsync(HOSTED_SESSIONS_POLL_MS * 3);
    expect(backend.listHostedSessions).toHaveBeenCalledTimes(2);
  });

  it("drops an answer that lands after the poller stopped", async () => {
    let resolveLate!: (value: ReturnType<typeof answer>) => void;
    vi.mocked(backend.listHostedSessions).mockReturnValue(new Promise((resolve) => (resolveLate = resolve)));

    startHostedSessionPolling();
    stopHostedSessionPolling();
    resolveLate(answer([session()]));
    await vi.advanceTimersByTimeAsync(0);

    expect(getHostedSessions()).toEqual([]);
    expect(toaster.toast).not.toHaveBeenCalled();
  });

  it("asks again at once when a download a session waits on completes", async () => {
    vi.mocked(backend.listHostedSessions).mockResolvedValue(answer([session({ state: "download" })]));
    startHostedSessionPolling();
    await vi.advanceTimersByTimeAsync(0);
    expect(backend.listHostedSessions).toHaveBeenCalledTimes(1);

    emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", completed(42));
    await vi.advanceTimersByTimeAsync(0);

    expect(backend.listHostedSessions).toHaveBeenCalledTimes(2);
  });

  it("does not ask again for a download no session waits on", async () => {
    vi.mocked(backend.listHostedSessions).mockResolvedValue(answer([session({ state: "join" })]));
    startHostedSessionPolling();
    await vi.advanceTimersByTimeAsync(0);

    emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", completed(42));
    await vi.advanceTimersByTimeAsync(0);

    expect(backend.listHostedSessions).toHaveBeenCalledTimes(1);
  });
});

describe("hostedSessions — what a poll keeps and says", () => {
  it("holds the sessions the answer names", async () => {
    vi.mocked(backend.listHostedSessions).mockResolvedValue(answer([session()]));

    await pollHostedSessions();

    expect(getHostedSessions()).toEqual([session()]);
  });

  it("toasts a session once, when it first appears", async () => {
    vi.mocked(backend.listHostedSessions).mockResolvedValue(answer([session()]));

    await pollHostedSessions();
    await pollHostedSessions();

    expect(toaster.toast).toHaveBeenCalledTimes(1);
    expect(toaster.toast).toHaveBeenCalledWith({ title: "Tender", body: "sam is hosting Crash Team Racing" });
  });

  it("toasts a new session beside one seen before, and only the new one", async () => {
    vi.mocked(backend.listHostedSessions).mockResolvedValueOnce(answer([session()]));
    await pollHostedSessions();
    vi.mocked(toaster.toast).mockClear();

    const second = session({ key: "8:9:2026-10-25T21:00:00+00:00", host_username: "alex", rom_name: "Tekken 3" });
    vi.mocked(backend.listHostedSessions).mockResolvedValueOnce(answer([session(), second]));
    await pollHostedSessions();

    expect(toaster.toast).toHaveBeenCalledTimes(1);
    expect(toaster.toast).toHaveBeenCalledWith({ title: "Tender", body: "alex is hosting Tekken 3" });
  });

  it("does not toast again for a key seen before that drops out and comes back", async () => {
    vi.mocked(backend.listHostedSessions)
      .mockResolvedValueOnce(answer([session()]))
      .mockResolvedValueOnce(answer([]))
      .mockResolvedValueOnce(answer([session()]));

    await pollHostedSessions();
    await pollHostedSessions();
    await pollHostedSessions();

    expect(toaster.toast).toHaveBeenCalledTimes(1);
    expect(getHostedSessions()).toEqual([session()]);
  });

  it("stays quiet on a server without the activity feed, and shows no session", async () => {
    vi.mocked(backend.listHostedSessions).mockResolvedValueOnce(answer([session()]));
    await pollHostedSessions();
    vi.mocked(toaster.toast).mockClear();

    vi.mocked(backend.listHostedSessions).mockResolvedValueOnce({
      success: false,
      reason: "unsupported",
      message: "Multiplayer needs RomM 5.0.0 or newer on the server.",
    });
    await pollHostedSessions();

    expect(toaster.toast).not.toHaveBeenCalled();
    expect(getHostedSessions()).toEqual([]);
  });

  it("stays quiet when the call itself fails, and logs it at debug level", async () => {
    vi.mocked(backend.listHostedSessions).mockResolvedValueOnce(answer([session()]));
    await pollHostedSessions();
    vi.mocked(toaster.toast).mockClear();

    vi.mocked(backend.listHostedSessions).mockRejectedValueOnce(new Error("bridge down"));
    await pollHostedSessions();

    expect(toaster.toast).not.toHaveBeenCalled();
    expect(getHostedSessions()).toEqual([]);
    expect(backend.debugLog).toHaveBeenCalledWith(
      expect.stringContaining("hosted sessions: poll failed: Error: bridge down"),
    );
  });

  it("keeps the newer answer when two polls land out of order", async () => {
    let resolveOlder!: (value: ReturnType<typeof answer>) => void;
    vi.mocked(backend.listHostedSessions)
      .mockReturnValueOnce(new Promise((resolve) => (resolveOlder = resolve)))
      .mockResolvedValueOnce(answer([]));

    const older = pollHostedSessions();
    await pollHostedSessions();
    resolveOlder(answer([session()]));
    await older;

    expect(getHostedSessions()).toEqual([]);
    expect(toaster.toast).not.toHaveBeenCalled();
  });
});
