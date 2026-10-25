import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act, waitFor } from "@testing-library/react";
import { toaster } from "@decky/api";
import * as backend from "../api/backend";
import type { HostedSession } from "../api/backend";
import { HostedSessionCard } from "./HostedSessionCard";
import { setDownloads } from "../utils/downloadStore";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import { consumeLaunchSkip } from "../utils/launchGate";
import { getNetplayLaunch, resetNetplayLaunchForTests } from "../utils/netplayLaunch";
import type { DownloadItem } from "../types";

vi.mock("../utils/steamShortcuts", () => ({
  setLaunchOptionsConfirmed: vi.fn().mockResolvedValue(true),
}));

const JOIN_COMMAND =
  'flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L pcsx_rearmed_libretro.so --connect 100.64.0.2 --sram-mode=noload-nosave %ROM%" "/roms/psx/ctr.chd"';

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

function download(overrides: Partial<DownloadItem> = {}): DownloadItem {
  return {
    rom_id: 42,
    rom_name: "Crash Team Racing",
    platform_name: "PlayStation",
    file_name: "ctr.chd",
    status: "downloading",
    progress: 0.4,
    bytes_downloaded: 400,
    total_bytes: 1000,
    resumable: false,
    ...overrides,
  };
}

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement | null {
  return (
    (Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text) as
      HTMLButtonElement | undefined) ?? null
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  consumeLaunchSkip(100);
  localStorage.clear();
  resetNetplayLaunchForTests();
  setDownloads([]);
  vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "default" });
  vi.mocked(backend.checkCoreChange).mockResolvedValue({ changed: false });
  vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
  vi.mocked(backend.preLaunchSync).mockResolvedValue({ success: true, message: "", synced: 0, conflicts: [] });
  vi.mocked(backend.prepareJoinSession).mockResolvedValue({
    success: true,
    rom_id: 42,
    app_id: 100,
    kind: "retroarch",
    connection: "direct",
    launch_options: JOIN_COMMAND,
    prune_lease_token: "netplay_launch:2",
  });
  vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);
  vi.stubGlobal("SteamClient", { Apps: { RunGame: vi.fn() } });
  vi.stubGlobal("appStore", {
    GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-100" })),
    allApps: [],
  });
});

describe("HostedSessionCard", () => {
  it("names the host and the game on a row Steam can focus", () => {
    const { container } = render(<HostedSessionCard session={session()} />);

    const card = container.querySelector('[data-activate="true"]');
    expect(card?.textContent).toContain("sam is hosting");
    expect(card?.textContent).toContain("Crash Team Racing");
  });

  it("joins through the launch funnel as a guest", async () => {
    const { container } = render(<HostedSessionCard session={session()} />);

    await act(async () => {
      buttonByText(container, "Join")!.click();
    });

    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-100", "", -1, 100));
    expect(backend.prepareJoinSession).toHaveBeenCalledWith(42, 7, "2026-10-25T20:00:00+00:00");
    expect(backend.preLaunchSync).toHaveBeenCalledWith(42);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(100, JOIN_COMMAND);
    expect(backend.getRomRelaunchOptions).not.toHaveBeenCalled();
    expect(getNetplayLaunch()).toEqual({ appId: 100, romId: 42, role: "guest" });
  });

  it("toasts a refused join and offers Join again", async () => {
    vi.mocked(backend.prepareJoinSession).mockResolvedValue({
      success: false,
      reason: "session_ended",
      message: "This multiplayer session has ended.",
    });
    const { container } = render(<HostedSessionCard session={session()} />);

    await act(async () => {
      buttonByText(container, "Join")!.click();
    });

    await waitFor(() =>
      expect(toaster.toast).toHaveBeenCalledWith({ title: "Tender", body: "This multiplayer session has ended." }),
    );
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
    expect(buttonByText(container, "Join")?.disabled).toBe(false);
  });

  it("offers Download for a game not on this device, and starts the download the game page starts", async () => {
    vi.mocked(backend.startDownload).mockResolvedValue({ success: true, message: "Download started" });
    const { container } = render(<HostedSessionCard session={session({ state: "download" })} />);

    expect(buttonByText(container, "Join")).toBeNull();
    await act(async () => {
      buttonByText(container, "Download")!.click();
    });

    expect(backend.startDownload).toHaveBeenCalledWith(42, false, null, null, false);
    expect(toaster.toast).not.toHaveBeenCalled();
  });

  it("holds the Download press while the game downloads", () => {
    setDownloads([download()]);
    const { container } = render(<HostedSessionCard session={session({ state: "download" })} />);

    expect(buttonByText(container, "Downloading…")?.disabled).toBe(true);
    expect(buttonByText(container, "Download")).toBeNull();
  });

  it("toasts the message of a download the backend refused", async () => {
    vi.mocked(backend.startDownload).mockResolvedValue({ success: false, message: "Not enough space" });
    const { container } = render(<HostedSessionCard session={session({ state: "download" })} />);

    await act(async () => {
      buttonByText(container, "Download")!.click();
    });

    expect(toaster.toast).toHaveBeenCalledWith({ title: "Tender", body: "Not enough space" });
    expect(buttonByText(container, "Download")?.disabled).toBe(false);
  });

  it("sends a download that needs a choice about files on disk to the game's page", async () => {
    vi.mocked(backend.startDownload).mockResolvedValue({
      success: false,
      reason: "target_occupied",
      message: "Something is already at this game's location",
    } as unknown as Awaited<ReturnType<typeof backend.startDownload>>);
    const { container } = render(<HostedSessionCard session={session({ state: "download" })} />);

    await act(async () => {
      buttonByText(container, "Download")!.click();
    });

    expect(toaster.toast).toHaveBeenCalledWith({
      title: "Tender",
      body: "Crash Team Racing has files on disk already. Open its page and press Download there to choose what to do.",
    });
  });

  it("toasts a download call that failed outright", async () => {
    vi.mocked(backend.startDownload).mockRejectedValue(new Error("bridge down"));
    const { container } = render(<HostedSessionCard session={session({ state: "download" })} />);

    await act(async () => {
      buttonByText(container, "Download")!.click();
    });

    expect(toaster.toast).toHaveBeenCalledWith({
      title: "Tender",
      body: "Download failed. Is the RomM server running?",
    });
  });

  it("says the core is missing and offers no Join", () => {
    const { container } = render(<HostedSessionCard session={session({ state: "missing_core" })} />);

    const card = container.querySelector('[data-activate="true"]');
    expect(card?.textContent).toContain(
      "RetroDECK on this device does not have the emulator core this session runs on",
    );
    expect(container.querySelectorAll("button")).toHaveLength(0);
  });

  it("says the game is not in this library and offers no Join", () => {
    const { container } = render(<HostedSessionCard session={session({ state: "not_in_library", app_id: null })} />);

    const card = container.querySelector('[data-activate="true"]');
    expect(card?.textContent).toContain("This game is not in Tender's library on this device");
    expect(container.querySelectorAll("button")).toHaveLength(0);
  });
});
