/**
 * The multiplayer sessions other users of this RomM server are hosting, as the
 * last poll established them.
 *
 * The poller runs from plugin load to unload, open panel or not and a game
 * running or not, so the toast that announces a session reaches a player who is
 * already in a game. Each session is announced once per key; Main renders a
 * card per session from this store.
 *
 * A poll that establishes nothing (a server older than the activity feed, an
 * unreachable server, a failed call) leaves no session standing and says
 * nothing: the cards would only offer a Join that cannot work.
 *
 * Every write installs a new array, so {@link getHostedSessions} can serve as a
 * `useSyncExternalStore` snapshot.
 */

import { useSyncExternalStore } from "react";
import { addEventListener, removeEventListener } from "@decky/api";
import { debugLog, listHostedSessions, type HostedSession } from "../api/backend";
import { showToast } from "./toast";
import { detach } from "./detach";
import type { DownloadCompleteEvent } from "../types";

export const HOSTED_SESSIONS_POLL_MS = 30_000;

let sessions: readonly HostedSession[] = [];
const listeners = new Set<() => void>();
const announced = new Set<string>();
let timer: ReturnType<typeof setInterval> | null = null;
let downloadListener: ((event: DownloadCompleteEvent) => void) | null = null;
// Bumped by every stop, so an answer still in flight when the poller stopped
// never lands.
let epoch = 0;
// Issue order of the polls, so an older answer that lands last never replaces
// a newer one.
let issued = 0;
let applied = 0;

function setSessions(next: readonly HostedSession[]): void {
  if (next.length === 0 && sessions.length === 0) return;
  sessions = next;
  listeners.forEach((fn) => fn());
}

export function getHostedSessions(): readonly HostedSession[] {
  return sessions;
}

function onHostedSessionsChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function useHostedSessions(): readonly HostedSession[] {
  return useSyncExternalStore(onHostedSessionsChange, getHostedSessions);
}

function announce(found: readonly HostedSession[]): void {
  for (const session of found) {
    if (announced.has(session.key)) continue;
    announced.add(session.key);
    showToast(`${session.host_username} is hosting ${session.rom_name}`);
  }
}

/** Ask the backend once and apply its answer, unless a newer one already landed. */
export async function pollHostedSessions(): Promise<void> {
  const startedIn = epoch;
  const ticket = ++issued;
  let found: readonly HostedSession[] = [];
  try {
    const result = await listHostedSessions();
    if (result.success) found = result.sessions;
    else detach(debugLog(`hosted sessions: none established (${result.reason})`));
  } catch (e) {
    detach(debugLog(`hosted sessions: poll failed: ${e}`));
  }
  if (startedIn !== epoch || ticket < applied) return;
  applied = ticket;
  announce(found);
  setSessions(found);
}

/** Start the poller: one poll now, then one every {@link HOSTED_SESSIONS_POLL_MS}. Idempotent. */
export function startHostedSessionPolling(): void {
  if (timer !== null) return;
  detach(pollHostedSessions());
  timer = setInterval(() => detach(pollHostedSessions()), HOSTED_SESSIONS_POLL_MS);
  // A card reading Download turns into Join once a poll sees the game installed;
  // asking at the download's end instead of up to 30 seconds later.
  downloadListener = (event: DownloadCompleteEvent) => {
    if (sessions.some((session) => session.rom_id === event.rom_id && session.state === "download")) {
      detach(pollHostedSessions());
    }
  };
  addEventListener<[DownloadCompleteEvent]>("download_complete", downloadListener);
}

export function stopHostedSessionPolling(): void {
  epoch++;
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
  if (downloadListener !== null) {
    removeEventListener("download_complete", downloadListener);
    downloadListener = null;
  }
}

export function resetHostedSessionsForTests(): void {
  stopHostedSessionPolling();
  sessions = [];
  announced.clear();
  issued = 0;
  applied = 0;
}
