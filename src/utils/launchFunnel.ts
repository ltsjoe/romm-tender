/**
 * The launch funnel for every launch Tender starts itself: the play row's Play
 * and Host multiplayer, and the Join on a hosted session's card on Main.
 *
 * Each runs the shared pre-launch gate (`launchGate.ts`) and acts on its
 * verdict, then hands the game to Steam. The caller supplies only what differs
 * between surfaces: the questions the gate may put to the user
 * ({@link LaunchPrompts}), and where its button shows the funnel's progress.
 *
 * A normal launch re-confirms the shortcut's command just before `RunGame`. A
 * netplay launch instead asks the backend for its session once the gate allows
 * the launch, writes that session's one-off command and launches on it, with no
 * re-confirm, which would put the normal command back. The session's end
 * restores the normal command (`netplayLaunch.ts`).
 *
 * Steam's direct launches go through the watcher in `launchInterceptor.ts`,
 * which runs the same gate with its own reactions.
 */

import { showToast } from "./toast";
import {
  checkCoreChange,
  checkLocalDrift,
  confirmSlotChoice,
  debugLog,
  endHostSession,
  endJoinSession,
  getSaveSetupInfo,
  isSaveTrackingConfigured,
  logError,
  preLaunchSync,
  probeReachability,
  type NetplayLaunchResult,
} from "../api/backend";
import { reportServerReachable } from "./connectionState";
import { getMigrationState } from "./migrationStore";
import { applyLaunchGateSetupOutcome, resolveSaveSetupOutcome } from "./saveSetup";
import { runLaunchGate, markLaunchSkipped } from "./launchGate";
import type { GateVerdict, LaunchGateOps, PreLaunchSyncOutcome } from "./launchGate";
import { NO_LAUNCH_TARGET_TOAST_BODY, romHasLaunchTarget } from "./launchTarget";
import { reconfirmLaunchOptions } from "./launchOptionsReconcile";
import {
  capturePruneLeaseAdmission,
  isPruneLeaseAdmissionCurrent,
  isPruneLeaseCancellation,
  withPruneLease,
  type PruneLeaseAdmission,
} from "./pruneLease";
import { isSessionActive } from "./sessionManager";
import { isAppRunning } from "./runningApps";
import { saveSyncToastBody } from "./saveSyncToast";
import { setLaunchOptionsConfirmed } from "./steamShortcuts";
import { endNetplayLaunch, getNetplayLaunch, recordNetplayLaunch, type NetplayRole } from "./netplayLaunch";
import { BENIGN_SYNC_SKIP_REASONS } from "../types";
import { detach } from "./detach";
import type { LaunchPrompts } from "./launchInterceptor";

/** What the caller's button shows while the funnel works, and where it settles. */
export type LaunchFunnelState = "syncing" | "launching" | "play" | "conflict";

export interface NetplayLaunchSpec {
  role: NetplayRole;
  /** Start hosting, or prepare the join. Asked only once the gate allows the launch. */
  prepare: () => Promise<NetplayLaunchResult>;
}

export interface LaunchRequest {
  appId: number;
  /** `null` for a game Tender does not know, which launches without the gate. */
  romId: number | null;
  /** Names the caller in the funnel's log lines (`CustomPlayButton`). */
  context: string;
  prompts: LaunchPrompts;
  setState: (state: LaunchFunnelState) => void;
  /** Scopes the launch's admission to a mounted component, whose unmount cancels it. */
  leaseOwner?: string;
  netplay?: NetplayLaunchSpec;
}

const NETPLAY_FAILED_TOAST_BODY = "Couldn't start the multiplayer session";
const REAUTH_TOAST_BODY = "Your RomM sign-in does not allow multiplayer";
const REAUTH_TOAST_SUBTEXT = "Sign in again with your username and password in Tender › Settings › Connections.";

// Save-slot tracking gate. Delegates branch handling to applyLaunchGateSetupOutcome
// so the per-outcome side effects (toast + saves-tab switch vs auto-confirm) stay
// testable on their own.
//
// The try only guards the network call (getSaveSetupInfo). Post-result branching
// (resolveSaveSetupOutcome + applyLaunchGateSetupOutcome) sits OUTSIDE the try so
// that an exception in a side-effect callback (toast / dispatchEvent / confirm)
// cannot silently flip "abort" → "proceed" — the abort-propagation bug pattern
// #619 was opened to prevent.
async function ensureTrackingConfigured(rid: number): Promise<"proceed" | "abort"> {
  const trackingResult = await isSaveTrackingConfigured(rid).catch(() => ({ configured: true }));
  if (trackingResult.configured) return "proceed";

  let setupInfo;
  /* istanbul ignore next -- network-IO + defer-to-launch fallback; behavior tested at service layer */
  try {
    setupInfo = await getSaveSetupInfo(rid);
  } catch {
    // Network/backend failure — defer to launch rather than blocking the user.
    return "proceed";
  }

  /* istanbul ignore next -- delegates to applyLaunchGateSetupOutcome; logic covered in src/utils/saveSetup.test.ts */
  return applyLaunchGateSetupOutcome(resolveSaveSetupOutcome(setupInfo), {
    rid,
    confirmSlotChoice,
    toast: (body) => showToast(body),
    dispatchSavesTab: () => globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } })),
  });
}

// Detects emulator core change since last launch; if changed, puts the
// core-change question to the user. Returns true to proceed, false to bail.
async function confirmCoreChangeIfNeeded(rid: number, prompts: LaunchPrompts): Promise<boolean> {
  const coreCheck = await checkCoreChange(rid).catch(
    (): { changed: boolean; old_core?: string; new_core?: string; old_label?: string; new_label?: string } => ({
      changed: false,
    }),
  );
  if (!coreCheck.changed) return true;
  return prompts.confirmCoreChange(
    coreCheck.old_label ?? coreCheck.old_core ?? "Unknown",
    coreCheck.new_label ?? coreCheck.new_core ?? "Unknown",
  );
}

// Online pre-launch sync, mapped onto the gate's PreLaunchSyncOutcome (the
// gate routes it to conflict / sync_failed / allow). Keeps the 15s timeout, the
// "syncing" state, the benign skips, and the success toast — all the
// side-effects the verdict mapping can't carry stay here; conflict resolution
// and the fallback confirm are the verdict switch's in `actOnVerdict`.
//
// Like the watcher, this MUST NOT fail open: a throw or timeout returns
// `{ success: false }` (→ sync_failed → fallback confirm) rather than
// propagating to the gate's blanket catch and silently launching on stale
// saves (#1050).
async function runPreLaunchSync(req: LaunchRequest, rid: number): Promise<PreLaunchSyncOutcome> {
  req.setState("syncing");
  let result: Awaited<ReturnType<typeof preLaunchSync>>;
  try {
    result = await Promise.race([
      preLaunchSync(rid),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error("timeout")), 15000)),
    ]);
  } catch (e) {
    detach(debugLog(`${req.context}: pre-launch sync failed: ${e}`));
    return { success: false, message: "" };
  }

  detach(
    debugLog(
      `${req.context}: preLaunchSync result: synced=${result.synced} conflicts=${result.conflicts?.length ?? 0} success=${result.success}`,
    ),
  );

  // Benign skip: either RetroArch writes saves to the content dir (#239), or
  // this game's emulator keeps no per-game save file set the plugin can carry
  // (#1858). NOT a failure — proceed to launch silently (no toast, no
  // fallback-launch confirm). Both are standing facts about the machine, so a
  // confirm on every single launch would be pure noise.
  if (result.reason !== undefined && BENIGN_SYNC_SKIP_REASONS.includes(result.reason)) {
    detach(debugLog(`${req.context}: pre-launch sync skipped (${result.reason}) — launching`));
    return { success: true, message: result.message };
  }

  if (result.conflicts && result.conflicts.length > 0) {
    return { success: result.success, message: result.message, conflicts: result.conflicts };
  }

  if (!result.success) {
    detach(
      debugLog(
        `${req.context}: pre-launch sync failed: reason=${result.reason ?? ""} errors=[${result.errors?.join(", ") ?? ""}] message=${result.message}`,
      ),
    );
    // Any resolved failure must surface as sync_failed, not silently proceed.
    // Failures with no errors array — DEVICE_NOT_REGISTERED,
    // blocked_by_migration, save_sort_changed — still mean sync didn't run;
    // without this the user plays on stale local saves believing pre-launch
    // sync happened (#1050).
    return { success: false, message: result.message };
  }

  const toastBody = saveSyncToastBody(result.uploaded, result.downloaded);
  if (toastBody) {
    showToast(toastBody);
  }
  return { success: true, message: result.message };
}

// The gate's callbacks for this ROM. Reachability is a FRESH probe at the press,
// so a stale page-open connection flag never gates the launch.
function funnelOps(req: LaunchRequest, rid: number): LaunchGateOps {
  return {
    migrationPending: () => getMigrationState().pending,
    hasLaunchTarget: () => romHasLaunchTarget(rid, req.context),
    ensureTrackingConfigured: () => ensureTrackingConfigured(rid),
    checkCoreChange: () => confirmCoreChangeIfNeeded(rid, req.prompts),
    checkReachability: async () => {
      // A resolved probe is a definitive reachability signal → feed the shared
      // store so the badge/Download re-derive (#1345). A throw is a bridge error,
      // not a server verdict, so it does NOT flip the store — but the launch still
      // treats it as offline (fail-safe).
      try {
        const { online } = await probeReachability();
        reportServerReachable(online);
        return online;
      } catch (e) {
        logError(`${req.context}: reachability probe failed (treating as offline): ${e}`);
        return false;
      }
    },
    preLaunchSync: () => runPreLaunchSync(req, rid),
    checkLocalDrift: async () =>
      (
        await checkLocalDrift(rid).catch((e) => {
          logError(`${req.context}: local-drift check failed (treating as not-drifted): ${e}`);
          return { drifted: false, rom_id: rid };
        })
      ).drifted,
  };
}

// Final step of a normal launch: hand off to Steam. Marks the appId in the
// shared skip-set immediately before RunGame so this RunGame does NOT re-enter
// the global watcher and re-gate a launch that already ran the funnel (the
// double-gate fix C1).
async function dispatchLaunch(req: LaunchRequest, gameId: string, admission: PruneLeaseAdmission): Promise<void> {
  if (!isPruneLeaseAdmissionCurrent(admission)) return;
  req.setState("launching");
  // Heal any mid-session launch_options drift on this shortcut before launch
  // (#1150) via the shared bounded-race re-confirm. Ordinary I/O failures stay
  // best-effort; timeout or plugin teardown cancels this launch.
  if (req.romId) {
    const reconfirm = await reconfirmLaunchOptions(req.romId, req.appId, req.context, admission);
    if (reconfirm.status === "cancelled") return;
    if (reconfirm.status === "timeout") {
      req.setState("play");
      return;
    }
  }
  markLaunchSkipped(req.appId);
  SteamClient.Apps.RunGame(gameId, "", -1, 100);
}

function showNetplayRefusal(result: { reason: string; message: string }): void {
  if (result.reason === "reauth_required") {
    showToast(REAUTH_TOAST_BODY, { subtext: REAUTH_TOAST_SUBTEXT });
    return;
  }
  showToast(result.message || NETPLAY_FAILED_TOAST_BODY);
}

/**
 * Write the session's one-off command under the lease the backend issued with
 * it. The lease is handed back once the write settles, on every path.
 */
async function writeOneOffCommand(
  req: LaunchRequest,
  prepared: Extract<NetplayLaunchResult, { success: true }>,
  admission: PruneLeaseAdmission,
): Promise<boolean> {
  try {
    return await withPruneLease(
      prepared.prune_lease_token,
      `${req.context}: netplay launch`,
      async (signal) => !signal.aborted && (await setLaunchOptionsConfirmed(req.appId, prepared.launch_options)),
      req.context,
      admission,
    );
  } catch (e) {
    if (!isPruneLeaseCancellation(e, admission)) {
      logError(`${req.context}: writing the multiplayer launch command failed: ${e}`);
    }
    return false;
  }
}

// Final step of a netplay launch. Prepares the session now that the gate has
// allowed the launch, so a refused gate leaves no session to undo.
async function dispatchNetplayLaunch(
  req: LaunchRequest,
  netplay: NetplayLaunchSpec,
  romId: number,
  gameId: string,
  admission: PruneLeaseAdmission,
): Promise<void> {
  if (!isPruneLeaseAdmissionCurrent(admission)) return;
  req.setState("launching");
  let prepared: NetplayLaunchResult;
  try {
    prepared = await netplay.prepare();
  } catch (e) {
    // The backend may have started the session and lost only its answer.
    logError(`${req.context}: preparing the ${netplay.role} session failed: ${e}`);
    await (netplay.role === "host" ? endHostSession() : endJoinSession()).catch((endError: unknown) =>
      logError(`${req.context}: ending the unanswered ${netplay.role} session failed: ${endError}`),
    );
    showToast(NETPLAY_FAILED_TOAST_BODY);
    req.setState("play");
    return;
  }
  if (!prepared.success) {
    detach(debugLog(`${req.context}: the ${netplay.role} session was refused: ${prepared.reason}`));
    showNetplayRefusal(prepared);
    req.setState("play");
    return;
  }

  recordNetplayLaunch({ appId: req.appId, romId, role: netplay.role });
  const written = await writeOneOffCommand(req, prepared, admission);
  if (!written || !isPruneLeaseAdmissionCurrent(admission)) {
    await endNetplayLaunch(req.appId);
    if (isPruneLeaseAdmissionCurrent(admission)) {
      showToast(NETPLAY_FAILED_TOAST_BODY);
      req.setState("play");
    }
    return;
  }
  markLaunchSkipped(req.appId);
  SteamClient.Apps.RunGame(gameId, "", -1, 100);
}

// Map a gate verdict onto the caller's UI. `dispatch` is the launch's final
// step, which marks the skip-set, so every launch from here is exempt from the
// watcher (no double-gate). Each non-launch branch settles the caller's state.
// Returns "retry" only from the offline-drift branch when the user asks to
// re-probe — the gate loop re-runs the gate on that; every other outcome
// returns "done".
async function actOnVerdict(
  req: LaunchRequest,
  verdict: GateVerdict,
  rid: number,
  dispatch: () => Promise<void>,
): Promise<"done" | "retry"> {
  switch (verdict.decision) {
    case "allow":
      await dispatch();
      return "done";
    case "abort":
    case "block":
      // abort: the user saw setup/core UI and declined. block/migration_pending:
      // the QAM/page already surfaces it. Both bail silently to "play".
      // block/no_launch_target has no such standing surface at the moment of the
      // press — the page states it, but the press must not read as a dead button.
      if (verdict.decision === "block" && verdict.reason === "no_launch_target") {
        showToast(NO_LAUNCH_TARGET_TOAST_BODY);
      }
      req.setState("play");
      return "done";
    case "conflict": {
      const resolution = await req.prompts.resolveConflicts(verdict.conflicts);
      if (resolution === "cancel") {
        req.setState("conflict");
        return "done";
      }
      // Conflicts resolved — notify sibling components to refresh, then launch.
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: rid } }));
      await dispatch();
      return "done";
    }
    case "offline_drift": {
      const choice = await req.prompts.askOfflineDrift();
      if (choice === "start_anyway") {
        await dispatch();
        return "done";
      }
      if (choice === "retry") {
        // Re-run the gate (re-probes via the fast reachability check). The
        // button stays interactive while the modal is open; flip to "syncing"
        // so the user sees the gate working again instead of a dead "play".
        req.setState("syncing");
        return "retry";
      }
      req.setState("play");
      return "done";
    }
    case "sync_failed": {
      const proceed = await req.prompts.confirmFallbackLaunch(verdict.message);
      if (proceed) {
        await dispatch();
        return "done";
      }
      req.setState("play");
      return "done";
    }
  }
}

// Run the gate and act on its verdict, looping while the user keeps choosing
// "Retry" on the offline-drift question. Each retry is a fresh user action, so
// the loop is bounded by the user. An unexpected throw from the gate or a
// verdict's prompt (framework-level) would otherwise leave the caller frozen on
// "syncing"; the watcher never traps the user's game, and the equivalent here is
// settling back on "play".
async function gateAndDispatch(req: LaunchRequest, rid: number, dispatch: () => Promise<void>): Promise<void> {
  try {
    let verdict = await runLaunchGate(req.appId, rid, funnelOps(req, rid));
    while ((await actOnVerdict(req, verdict, rid, dispatch)) === "retry") {
      verdict = await runLaunchGate(req.appId, rid, funnelOps(req, rid));
    }
  } catch (e) {
    detach(debugLog(`${req.context}: launch unexpected error — resetting to play: ${e}`));
    req.setState("play");
  }
}

/**
 * Whether an earlier netplay launch is out of the way: none recorded, or one
 * whose game is gone, which is ended here. `false` while its game still runs.
 */
async function earlierNetplayLaunchSettled(): Promise<boolean> {
  const earlier = getNetplayLaunch();
  if (earlier === null) return true;
  if (isSessionActive(earlier.romId) || isAppRunning(earlier.appId)) return false;
  await endNetplayLaunch(earlier.appId);
  return true;
}

async function launchNetplay(
  req: LaunchRequest,
  netplay: NetplayLaunchSpec,
  romId: number,
  gameId: string,
  admission: PruneLeaseAdmission,
): Promise<void> {
  // A running game cannot take a new launch command, and bringing it forward
  // would start no session.
  if (isSessionActive(romId) || isAppRunning(req.appId)) {
    showToast("This game is already running. Quit it before starting a multiplayer session.");
    req.setState("play");
    return;
  }
  if (!(await earlierNetplayLaunchSettled())) {
    showToast("A multiplayer session is already running. Quit its game before starting another.");
    req.setState("play");
    return;
  }
  await gateAndDispatch(req, romId, () => dispatchNetplayLaunch(req, netplay, romId, gameId, admission));
}

/**
 * Launch *req*'s game through the funnel. Resolves once the launch was handed
 * to Steam or settled without one; the caller's state carries which.
 */
export async function launchThroughFunnel(req: LaunchRequest): Promise<void> {
  const { appId, romId } = req;
  const gameId = appStore.GetAppOverviewByAppID(appId)?.GetGameID?.() ?? String(appId);
  const admission = capturePruneLeaseAdmission(req.leaseOwner);
  detach(debugLog(`${req.context}: launch appId=${appId} gameId=${gameId}`));

  if (req.netplay) {
    if (romId !== null) await launchNetplay(req, req.netplay, romId, gameId, admission);
    return;
  }

  // Non-RomM / unresolved ROM — nothing to gate, launch straight through.
  if (!romId) {
    await dispatchLaunch(req, gameId, admission);
    return;
  }

  // Already-running guard (#1148 round 2) — the sibling of the launch
  // interceptor's guard, since a press here is the other launch path and its
  // button's enabled state derives from cached install/conflict status, not
  // running state. A press on an already-running game must NOT run the
  // pre-launch sync: it would upload the save mid-session while the emulator
  // holds the file open and manufacture a conflict at exit. Skip the whole
  // gate/sync funnel and just bring the game to front — `dispatchLaunch`
  // skip-marks the appId so the resulting RunGame doesn't re-enter the
  // interceptor and re-gate either.
  if (isSessionActive(romId) || isAppRunning(appId)) {
    detach(debugLog(`${req.context}: appId=${appId} already running — skipping pre-launch sync`));
    await dispatchLaunch(req, gameId, admission);
    return;
  }

  await gateAndDispatch(req, romId, () => dispatchLaunch(req, gameId, admission));
}
