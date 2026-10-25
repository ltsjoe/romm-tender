import { describe, it, expect, vi, beforeEach } from "vitest";
import * as backend from "../api/backend";
import { setLaunchOptionsConfirmed } from "./steamShortcuts";
import {
  endNetplayLaunch,
  getNetplayLaunch,
  isNetplayLaunchApp,
  recordNetplayLaunch,
  resetNetplayLaunchForTests,
  resumeNetplayLaunchAtLoad,
  settleNetplayLaunchAtLoad,
} from "./netplayLaunch";

// The normal command goes back through the shared re-confirm, which confirm-sets
// it onto the shortcut. Mocked so the write is observable without SteamClient.
vi.mock("./steamShortcuts", () => ({
  setLaunchOptionsConfirmed: vi.fn().mockResolvedValue(true),
}));

const ROW_KEY = "romm-tender:netplay-launch";
const NORMAL_COMMAND = 'flatpak run net.retrodeck.retrodeck "/roms/psx/game.chd"';
const HOST = { appId: 100, romId: 42, role: "host" as const };
const GUEST = { appId: 200, romId: 9, role: "guest" as const };

function seedRow(row: unknown): void {
  localStorage.setItem(ROW_KEY, JSON.stringify(row));
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  resetNetplayLaunchForTests();
  vi.mocked(backend.endHostSession).mockResolvedValue({ success: true, note_removed: true });
  vi.mocked(backend.endJoinSession).mockResolvedValue({ success: true });
  vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({
    success: true,
    app_id: 100,
    launch_options: NORMAL_COMMAND,
    prune_lease_token: "restore-lease",
  });
  vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
});

describe("netplayLaunch — the record", () => {
  it("records the launch and mirrors it into one localStorage row", () => {
    recordNetplayLaunch(HOST);

    expect(getNetplayLaunch()).toEqual(HOST);
    expect(isNetplayLaunchApp(100)).toBe(true);
    expect(isNetplayLaunchApp(200)).toBe(false);
    expect(JSON.parse(localStorage.getItem(ROW_KEY) ?? "null")).toEqual({ v: 1, ...HOST });
  });
});

describe("netplayLaunch — the end of a session", () => {
  it("ends the host session, then puts the normal command back, then forgets the launch", async () => {
    recordNetplayLaunch(HOST);

    await endNetplayLaunch(100);

    expect(backend.endHostSession).toHaveBeenCalledTimes(1);
    expect(backend.endJoinSession).not.toHaveBeenCalled();
    expect(backend.getRomRelaunchOptions).toHaveBeenCalledWith(42);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(100, NORMAL_COMMAND);
    const endOrder = vi.mocked(backend.endHostSession).mock.invocationCallOrder[0]!;
    const restoreOrder = vi.mocked(backend.getRomRelaunchOptions).mock.invocationCallOrder[0]!;
    expect(endOrder).toBeLessThan(restoreOrder);
    // The restore's lease is handed back once the write is done.
    expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("restore-lease");
    expect(getNetplayLaunch()).toBeNull();
    expect(localStorage.getItem(ROW_KEY)).toBeNull();
  });

  it("ends a joined session with end_join_session, never end_host_session", async () => {
    recordNetplayLaunch(GUEST);

    await endNetplayLaunch(200);

    expect(backend.endJoinSession).toHaveBeenCalledTimes(1);
    expect(backend.endHostSession).not.toHaveBeenCalled();
    expect(backend.getRomRelaunchOptions).toHaveBeenCalledWith(9);
    expect(getNetplayLaunch()).toBeNull();
  });

  it("ends a session once however many times its app is reported stopped", async () => {
    recordNetplayLaunch(HOST);

    await Promise.all([endNetplayLaunch(100), endNetplayLaunch(100)]);
    await endNetplayLaunch(100);

    expect(backend.endHostSession).toHaveBeenCalledTimes(1);
    expect(backend.getRomRelaunchOptions).toHaveBeenCalledTimes(1);
  });

  it("leaves the launch alone when another app stops", async () => {
    recordNetplayLaunch(HOST);

    await endNetplayLaunch(555);

    expect(backend.endHostSession).not.toHaveBeenCalled();
    expect(backend.getRomRelaunchOptions).not.toHaveBeenCalled();
    expect(getNetplayLaunch()).toEqual(HOST);
  });

  it("still puts the normal command back and forgets the launch when ending the session fails", async () => {
    const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    vi.mocked(backend.endHostSession).mockRejectedValue(new Error("bridge down"));
    recordNetplayLaunch(HOST);

    await endNetplayLaunch(100);

    expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("netplay: ending the host session failed"));
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(100, NORMAL_COMMAND);
    expect(getNetplayLaunch()).toBeNull();
  });

  it("says so when RomM kept the session note, which the backend deletes at its next start", async () => {
    const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    vi.mocked(backend.endHostSession).mockResolvedValue({ success: true, note_removed: false });
    recordNetplayLaunch(HOST);

    await endNetplayLaunch(100);

    expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("the session note is still on RomM"));
    expect(getNetplayLaunch()).toBeNull();
  });
});

describe("netplayLaunch — taking over at plugin load", () => {
  const running = (appIds: number[]) => (appId: number) => appIds.includes(appId);

  it("takes the recorded launch over at once, before the running games are known", async () => {
    seedRow({ v: 1, ...HOST });

    await resumeNetplayLaunchAtLoad();

    expect(getNetplayLaunch()).toEqual(HOST);
    expect(backend.endHostSession).not.toHaveBeenCalled();
  });

  it("keeps a launch whose game is still running, so its exit ends it", async () => {
    seedRow({ v: 1, ...HOST });

    await resumeNetplayLaunchAtLoad();
    await settleNetplayLaunchAtLoad(running([100]));

    expect(getNetplayLaunch()).toEqual(HOST);
    expect(backend.endHostSession).not.toHaveBeenCalled();
    expect(backend.getRomRelaunchOptions).not.toHaveBeenCalled();
  });

  it("ends a host launch whose game is gone and puts the normal command back", async () => {
    seedRow({ v: 1, ...HOST });

    await resumeNetplayLaunchAtLoad();
    await settleNetplayLaunchAtLoad(running([]));

    expect(backend.endHostSession).toHaveBeenCalledTimes(1);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(100, NORMAL_COMMAND);
    expect(getNetplayLaunch()).toBeNull();
    expect(localStorage.getItem(ROW_KEY)).toBeNull();
  });

  it("ends a guest launch whose game is gone", async () => {
    seedRow({ v: 1, ...GUEST });

    await resumeNetplayLaunchAtLoad();
    await settleNetplayLaunchAtLoad(running([]));

    expect(backend.endJoinSession).toHaveBeenCalledTimes(1);
    expect(backend.endHostSession).not.toHaveBeenCalled();
    expect(getNetplayLaunch()).toBeNull();
  });

  it("leaves alone a launch started after load, which replaced the one it took over", async () => {
    seedRow({ v: 1, ...HOST });
    await resumeNetplayLaunchAtLoad();
    recordNetplayLaunch(GUEST);

    await settleNetplayLaunchAtLoad(running([]));

    expect(backend.endHostSession).not.toHaveBeenCalled();
    expect(backend.endJoinSession).not.toHaveBeenCalled();
    expect(getNetplayLaunch()).toEqual(GUEST);
  });

  it("ends a host session the backend may still hold, at once, when this frontend recorded none", async () => {
    await resumeNetplayLaunchAtLoad();

    expect(backend.endHostSession).toHaveBeenCalledTimes(1);
    expect(backend.getRomRelaunchOptions).not.toHaveBeenCalled();
    expect(getNetplayLaunch()).toBeNull();

    await settleNetplayLaunchAtLoad(running([]));
    expect(backend.endHostSession).toHaveBeenCalledTimes(1);
  });

  it("reads a row it does not recognise as no record at all", async () => {
    seedRow({ v: 99, appId: 100, romId: 42, role: "host" });

    await resumeNetplayLaunchAtLoad();

    expect(getNetplayLaunch()).toBeNull();
    expect(backend.endHostSession).toHaveBeenCalledTimes(1);
    expect(backend.getRomRelaunchOptions).not.toHaveBeenCalled();
  });

  it("logs a failed orphan cleanup and carries on", async () => {
    const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    vi.mocked(backend.endHostSession).mockRejectedValue(new Error("not ready"));

    await resumeNetplayLaunchAtLoad();

    expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("netplay: ending a leftover host session failed"));
    expect(getNetplayLaunch()).toBeNull();
  });
});
