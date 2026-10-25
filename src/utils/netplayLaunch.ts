/**
 * The netplay launch this device has running: the Steam app, its ROM, and
 * whether this device hosts the session or joined it.
 *
 * A netplay launch writes a one-off command onto the game's shortcut, so it is
 * not over when the game starts. Its end has to end the session (the host's
 * RomM note and heartbeat, or the guest's appended config) and put the normal
 * command back. This record is how the game's exit knows it was a netplay
 * launch, and how a frontend that started again while the backend still holds
 * the session knows what to finish.
 *
 * Written by the launch funnel once the session is prepared, ended by the
 * session manager when the app stops, and taken over at plugin load. Mirrored
 * into one localStorage row so a reload mid-game keeps it, as the session
 * manager's breadcrumbs do.
 */

import { endHostSession, endJoinSession, logError } from "../api/backend";
import { reconfirmLaunchOptions } from "./launchOptionsReconcile";
import { capturePruneLeaseAdmission } from "./pruneLease";

export type NetplayRole = "host" | "guest";

export interface NetplayLaunch {
  appId: number;
  romId: number;
  role: NetplayRole;
}

const ROW_KEY = "romm-tender:netplay-launch";
const ROW_VERSION = 1;

let current: NetplayLaunch | null = null;
// The record read back at plugin load, until the settle has decided its fate.
let takenOver: NetplayLaunch | null = null;
const ending = new Map<number, Promise<void>>();

function readRow(): NetplayLaunch | null {
  try {
    const raw = localStorage.getItem(ROW_KEY);
    if (raw === null) return null;
    const row = JSON.parse(raw) as Record<string, unknown> | null;
    if (typeof row !== "object" || row === null || row.v !== ROW_VERSION) return null;
    const { appId, romId, role } = row;
    if (typeof appId !== "number" || typeof romId !== "number") return null;
    if (role !== "host" && role !== "guest") return null;
    return { appId, romId, role };
  } catch (e) {
    logError(`netplay: reading the launch record failed: ${e}`);
    return null;
  }
}

function writeRow(launch: NetplayLaunch | null): void {
  try {
    if (launch === null) localStorage.removeItem(ROW_KEY);
    else localStorage.setItem(ROW_KEY, JSON.stringify({ v: ROW_VERSION, ...launch }));
  } catch (e) {
    logError(`netplay: writing the launch record failed: ${e}`);
  }
}

function setCurrent(launch: NetplayLaunch | null): void {
  current = launch;
  writeRow(launch);
}

export function getNetplayLaunch(): NetplayLaunch | null {
  return current;
}

export function isNetplayLaunchApp(appId: number): boolean {
  return current?.appId === appId;
}

export function recordNetplayLaunch(launch: NetplayLaunch): void {
  setCurrent(launch);
}

async function finish(launch: NetplayLaunch): Promise<void> {
  try {
    if (launch.role === "host") {
      const ended = await endHostSession();
      if (!ended.note_removed) {
        logError("netplay: the session note is still on RomM; the backend deletes it at its next start");
      }
    } else {
      await endJoinSession();
    }
  } catch (e) {
    logError(`netplay: ending the ${launch.role} session failed: ${e}`);
  }
  const restored = await reconfirmLaunchOptions(
    launch.romId,
    launch.appId,
    "Netplay end",
    capturePruneLeaseAdmission(),
  );
  if (restored.status !== "ready") {
    logError(
      `netplay: the normal launch command for appId ${launch.appId} was not put back (${restored.status}); ` +
        "the next launch or the startup reconcile puts it back",
    );
  }
  if (current === launch) setCurrent(null);
}

/**
 * End the netplay launch *appId* opened: end its session, put the normal launch
 * command back through the shared re-confirm, then forget the launch.
 *
 * A no-op for an app that opened none. A second call while the first is under
 * way joins it rather than ending the session twice: the exit and a stale-record
 * check before the next netplay launch can both reach here.
 */
export function endNetplayLaunch(appId: number): Promise<void> {
  const running = ending.get(appId);
  if (running) return running;
  const launch = current;
  if (launch?.appId !== appId) return Promise.resolve();
  const run = finish(launch).finally(() => ending.delete(appId));
  ending.set(appId, run);
  return run;
}

/**
 * Take over the launch a previous frontend recorded, at plugin load and before
 * anything here can launch.
 *
 * With no record, a host session the backend still holds has no game whose exit
 * would end it, so it is ended now; the backend answers with nothing to do when
 * it holds none. Ending it later, once the running games are known, could end a
 * session the user started in the meantime.
 */
export async function resumeNetplayLaunchAtLoad(): Promise<void> {
  takenOver = readRow();
  current = takenOver;
  if (takenOver !== null) return;
  try {
    await endHostSession();
  } catch (e) {
    logError(`netplay: ending a leftover host session failed: ${e}`);
  }
}

/**
 * End the launch taken over at load if its game is gone, once the session
 * manager has decided which games still run. A launch whose game still runs is
 * kept, so its exit ends it; one started since load is not this call's.
 */
export async function settleNetplayLaunchAtLoad(isRunning: (appId: number, romId: number) => boolean): Promise<void> {
  const left = takenOver;
  takenOver = null;
  if (left === null || current !== left || isRunning(left.appId, left.romId)) return;
  await endNetplayLaunch(left.appId);
}

export function resetNetplayLaunchForTests(): void {
  current = null;
  takenOver = null;
  ending.clear();
}
