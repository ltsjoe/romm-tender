import { describe, it, expect, vi, beforeEach } from "vitest";
import { toaster } from "@decky/api";
import * as backend from "../api/backend";
import type { NetplayLaunchResult } from "../api/backend";
import { setLaunchOptionsConfirmed } from "./steamShortcuts";
import { consumeLaunchSkip, markLaunchSkipped } from "./launchGate";
import { getMigrationState } from "./migrationStore";
import { isSessionActive } from "./sessionManager";
import { isAppRunning } from "./runningApps";
import { getNetplayLaunch, recordNetplayLaunch, resetNetplayLaunchForTests } from "./netplayLaunch";
import { launchThroughFunnel, type LaunchFunnelState, type LaunchRequest } from "./launchFunnel";
import type { LaunchPrompts } from "./launchInterceptor";

vi.mock("./steamShortcuts", () => ({
  setLaunchOptionsConfirmed: vi.fn().mockResolvedValue(true),
}));
// The real skip-set, spied so its order against RunGame is observable.
vi.mock("./launchGate", async (importActual) => {
  const actual = await importActual<typeof import("./launchGate")>();
  return { ...actual, markLaunchSkipped: vi.fn(actual.markLaunchSkipped) };
});
vi.mock("./migrationStore", () => ({
  getMigrationState: vi.fn(() => ({ pending: false })),
}));
vi.mock("./sessionManager", () => ({
  isSessionActive: vi.fn(() => false),
}));
vi.mock("./runningApps", () => ({
  isAppRunning: vi.fn(() => false),
}));

const APP_ID = 100;
const ROM_ID = 42;
const NORMAL_COMMAND = 'flatpak run net.retrodeck.retrodeck "/roms/psx/game.chd"';
const ONE_OFF_COMMAND =
  'flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L pcsx_rearmed_libretro.so --host %ROM%" "/roms/psx/game.chd"';

const prompts: LaunchPrompts = {
  confirmCoreChange: vi.fn<LaunchPrompts["confirmCoreChange"]>(),
  resolveConflicts: vi.fn<LaunchPrompts["resolveConflicts"]>(),
  askOfflineDrift: vi.fn<LaunchPrompts["askOfflineDrift"]>(),
  confirmFallbackLaunch: vi.fn<LaunchPrompts["confirmFallbackLaunch"]>(),
};

const hostStarted = (
  overrides: Partial<Extract<NetplayLaunchResult, { success: true }>> = {},
): NetplayLaunchResult => ({
  success: true,
  rom_id: ROM_ID,
  app_id: APP_ID,
  kind: "retroarch",
  launch_options: ONE_OFF_COMMAND,
  prune_lease_token: "netplay_launch:1",
  ...overrides,
});

let states: LaunchFunnelState[] = [];

function request(overrides: Partial<LaunchRequest> = {}): LaunchRequest {
  return {
    appId: APP_ID,
    romId: ROM_ID,
    context: "FunnelTest",
    prompts,
    setState: (state) => states.push(state),
    ...overrides,
  };
}

function hostRequest(prepare = vi.fn(() => Promise.resolve(hostStarted()))): LaunchRequest {
  return request({ netplay: { role: "host", prepare } });
}

const runGame = () => vi.mocked(SteamClient.Apps.RunGame);

beforeEach(() => {
  vi.clearAllMocks();
  consumeLaunchSkip(APP_ID);
  resetNetplayLaunchForTests();
  localStorage.clear();
  states = [];

  vi.mocked(getMigrationState).mockReturnValue({ pending: false });
  vi.mocked(isSessionActive).mockReturnValue(false);
  vi.mocked(isAppRunning).mockReturnValue(false);
  vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "default" });
  vi.mocked(backend.checkCoreChange).mockResolvedValue({ changed: false });
  vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
  vi.mocked(backend.preLaunchSync).mockResolvedValue({ success: true, message: "", synced: 0, conflicts: [] });
  vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({
    success: true,
    app_id: APP_ID,
    launch_options: NORMAL_COMMAND,
    prune_lease_token: "launch_reconfirm:1",
  });
  vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  vi.mocked(backend.endHostSession).mockResolvedValue({ success: true, note_removed: true });
  vi.mocked(backend.endJoinSession).mockResolvedValue({ success: true });
  vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);

  vi.stubGlobal("SteamClient", { Apps: { RunGame: vi.fn() } });
  vi.stubGlobal("appStore", {
    GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-100" })),
    allApps: [],
  });
});

describe("launchFunnel — the normal launch", () => {
  it("runs the gate, re-confirms the normal command, marks the launch and runs the game", async () => {
    await launchThroughFunnel(request());

    expect(backend.preLaunchSync).toHaveBeenCalledWith(ROM_ID);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(APP_ID, NORMAL_COMMAND);
    expect(runGame()).toHaveBeenCalledWith("gid-100", "", -1, 100);
    const setOrder = vi.mocked(setLaunchOptionsConfirmed).mock.invocationCallOrder[0]!;
    const markOrder = vi.mocked(markLaunchSkipped).mock.invocationCallOrder[0]!;
    expect(setOrder).toBeLessThan(markOrder);
    expect(markOrder).toBeLessThan(runGame().mock.invocationCallOrder[0]!);
    expect(states).toEqual(["syncing", "launching"]);
    expect(getNetplayLaunch()).toBeNull();
  });

  it("brings an already-running game forward without the gate", async () => {
    vi.mocked(isAppRunning).mockReturnValue(true);

    await launchThroughFunnel(request());

    expect(backend.preLaunchSync).not.toHaveBeenCalled();
    expect(runGame()).toHaveBeenCalledWith("gid-100", "", -1, 100);
  });
});

describe("launchFunnel — a netplay launch", () => {
  it("writes the session's one-off command after the gate and runs the game without re-confirming", async () => {
    const prepare = vi.fn(() => Promise.resolve(hostStarted()));

    await launchThroughFunnel(hostRequest(prepare));

    expect(backend.preLaunchSync).toHaveBeenCalledWith(ROM_ID);
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.preLaunchSync).mock.invocationCallOrder[0]!).toBeLessThan(
      prepare.mock.invocationCallOrder[0]!,
    );
    // The one-off command is the only write before the launch: the re-confirm
    // would have put the normal command back over it.
    expect(backend.getRomRelaunchOptions).not.toHaveBeenCalled();
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledTimes(1);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(APP_ID, ONE_OFF_COMMAND);
    const writeOrder = vi.mocked(setLaunchOptionsConfirmed).mock.invocationCallOrder[0]!;
    const markOrder = vi.mocked(markLaunchSkipped).mock.invocationCallOrder[0]!;
    expect(writeOrder).toBeLessThan(markOrder);
    expect(markOrder).toBeLessThan(runGame().mock.invocationCallOrder[0]!);
    expect(runGame()).toHaveBeenCalledWith("gid-100", "", -1, 100);
    expect(states).toEqual(["syncing", "launching"]);
  });

  it("hands the netplay_launch lease back once the command is written", async () => {
    await launchThroughFunnel(hostRequest());

    expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("netplay_launch:1");
    const releaseOrder = vi.mocked(backend.releasePruneConflictLease).mock.invocationCallOrder[0]!;
    expect(releaseOrder).toBeLessThan(runGame().mock.invocationCallOrder[0]!);
  });

  it("records the launch, its ROM and its role before the game runs", async () => {
    await launchThroughFunnel(request({ netplay: { role: "guest", prepare: () => Promise.resolve(hostStarted()) } }));

    expect(getNetplayLaunch()).toEqual({ appId: APP_ID, romId: ROM_ID, role: "guest" });
  });

  it("never prepares a session when the gate stops the launch", async () => {
    vi.mocked(getMigrationState).mockReturnValue({ pending: true });
    const prepare = vi.fn(() => Promise.resolve(hostStarted()));

    await launchThroughFunnel(hostRequest(prepare));

    expect(prepare).not.toHaveBeenCalled();
    expect(runGame()).not.toHaveBeenCalled();
    expect(states[states.length - 1]).toBe("play");
  });

  it("refuses while the game is already running, and says why", async () => {
    vi.mocked(isAppRunning).mockReturnValue(true);
    const prepare = vi.fn(() => Promise.resolve(hostStarted()));

    await launchThroughFunnel(hostRequest(prepare));

    expect(prepare).not.toHaveBeenCalled();
    expect(runGame()).not.toHaveBeenCalled();
    expect(toaster.toast).toHaveBeenCalledWith({
      title: "Tender",
      body: "This game is already running. Quit it before starting a multiplayer session.",
    });
  });

  it("refuses while another netplay game is still running", async () => {
    recordNetplayLaunch({ appId: 300, romId: 77, role: "host" });
    vi.mocked(isAppRunning).mockImplementation((appId) => appId === 300);
    const prepare = vi.fn(() => Promise.resolve(hostStarted()));

    await launchThroughFunnel(hostRequest(prepare));

    expect(prepare).not.toHaveBeenCalled();
    expect(toaster.toast).toHaveBeenCalledWith({
      title: "Tender",
      body: "A multiplayer session is already running. Quit its game before starting another.",
    });
    expect(getNetplayLaunch()).toEqual({ appId: 300, romId: 77, role: "host" });
  });

  it("ends an earlier netplay launch whose game is gone before starting this one", async () => {
    recordNetplayLaunch({ appId: 300, romId: 77, role: "host" });

    await launchThroughFunnel(hostRequest());

    expect(backend.endHostSession).toHaveBeenCalledTimes(1);
    expect(runGame()).toHaveBeenCalledTimes(1);
    expect(getNetplayLaunch()).toEqual({ appId: APP_ID, romId: ROM_ID, role: "host" });
  });

  it("tells the user to sign in again with a username and password when RomM refuses the permission", async () => {
    const prepare = vi.fn(() =>
      Promise.resolve<NetplayLaunchResult>({
        success: false,
        reason: "reauth_required",
        message: "Your RomM sign-in does not allow multiplayer.",
      }),
    );

    await launchThroughFunnel(hostRequest(prepare));

    expect(toaster.toast).toHaveBeenCalledWith({
      title: "Tender",
      body: "Your RomM sign-in does not allow multiplayer",
      subtext: "Sign in again with your username and password in Tender › Settings › Connections.",
    });
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(runGame()).not.toHaveBeenCalled();
    expect(getNetplayLaunch()).toBeNull();
    expect(states[states.length - 1]).toBe("play");
  });

  it("toasts the backend's message for any other refusal", async () => {
    const prepare = vi.fn(() =>
      Promise.resolve<NetplayLaunchResult>({
        success: false,
        reason: "unsupported",
        message: "Multiplayer needs RomM 5.0.0 or newer on the server.",
      }),
    );

    await launchThroughFunnel(hostRequest(prepare));

    expect(toaster.toast).toHaveBeenCalledWith({
      title: "Tender",
      body: "Multiplayer needs RomM 5.0.0 or newer on the server.",
    });
    expect(runGame()).not.toHaveBeenCalled();
    expect(backend.endHostSession).not.toHaveBeenCalled();
  });

  it("ends a host session whose start answer was lost, and says the launch failed", async () => {
    const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    const prepare = vi.fn(() => Promise.reject(new Error("bridge down")));

    await launchThroughFunnel(hostRequest(prepare));

    expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("FunnelTest: preparing the host session failed"));
    expect(backend.endHostSession).toHaveBeenCalledTimes(1);
    expect(toaster.toast).toHaveBeenCalledWith({ title: "Tender", body: "Couldn't start the multiplayer session" });
    expect(runGame()).not.toHaveBeenCalled();
    expect(states[states.length - 1]).toBe("play");
  });

  it("does not launch on a command it could not write, and ends the session it prepared", async () => {
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValueOnce(false).mockResolvedValue(true);

    await launchThroughFunnel(hostRequest());

    expect(runGame()).not.toHaveBeenCalled();
    expect(backend.endHostSession).toHaveBeenCalledTimes(1);
    // The normal command goes back through the re-confirm, over whatever landed.
    expect(setLaunchOptionsConfirmed).toHaveBeenLastCalledWith(APP_ID, NORMAL_COMMAND);
    expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("netplay_launch:1");
    expect(getNetplayLaunch()).toBeNull();
    expect(toaster.toast).toHaveBeenCalledWith({ title: "Tender", body: "Couldn't start the multiplayer session" });
    expect(states[states.length - 1]).toBe("play");
  });
});
