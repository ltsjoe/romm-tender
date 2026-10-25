/**
 * Custom Play button that replaces the native Steam Play button on RomM game
 * detail pages. Handles 3 primary states:
 * - Download: ROM not installed, click to download
 * - Play: ROM installed, launches the game (with pre-launch save sync)
 * - Syncing: Save sync in progress before launch
 *
 * Includes a dropdown menu button (arrow) to the right of the Play button
 * with actions: Host multiplayer (for a game that can be hosted) and Uninstall.
 * Play and Host multiplayer both launch through `utils/launchFunnel.ts`.
 */

import { useState, useEffect, useRef, FC, ReactElement } from "react";
import { addEventListener, removeEventListener } from "@decky/api";
import { showToast } from "../utils/toast";
import { Focusable, DialogButton, Menu, MenuItem, Navigation, showContextMenu } from "@decky/ui";
import { appActionButtonClasses, basicAppDetailsSectionStylerClasses } from "../utils/deckyUiInternals";
import { hideNativePlaySection, showNativePlaySection } from "../utils/styleInjector";
import { hasAnySaveConflict } from "../utils/saveStatus";
import {
  getCachedGameDetail,
  startDownload,
  adoptExistingRom,
  isTargetOccupied,
  isCandidatesFound,
  isUnusableNamesake,
  isCandidateVanished,
  isRenameCollisions,
  cancelDownload,
  pauseDownload,
  resumeDownload,
  getDownloadQueue,
  removeRom,
  debugLog,
  getSaveStatus,
  isCallableFailure,
  logError,
  stopRunningGame,
  getNetplayCapability,
  startHostSession,
} from "../api/backend";
import { getRommConnectionState, onRommConnectionChange, reportServerReachable } from "../utils/connectionState";
import { isBoundVanished, onBoundVanishedChange } from "../utils/vanishedBinding";
import { scrollToTop } from "../utils/scrollHelpers";
import { getEventTarget } from "../utils/events";
import { handleButtonDownloadFailure } from "../utils/downloadFailure";
import { comparisonForCandidate, showAdoptExistingModal } from "./AdoptExistingModal";
import { showAdoptCandidateModal } from "./AdoptCandidateModal";
import { showAdoptCollisionModal } from "./AdoptCollisionModal";
import { showAdoptUnusableModal } from "./AdoptUnusableModal";
import { showAdoptVanishedModal } from "./AdoptVanishedModal";
import { handleConflicts } from "./SyncConflictModal";
import { MODAL_LAUNCH_PROMPTS } from "./launchPrompts";
import { launchThroughFunnel, type LaunchRequest } from "../utils/launchFunnel";
import { showStopGameModal } from "./StopGameModal";
import { isSessionActive } from "../utils/sessionManager";
import { isAppRunning } from "../utils/runningApps";
import type {
  DownloadProgressEvent,
  DownloadCompleteEvent,
  DownloadFailedEvent,
  TargetOccupiedResult,
  CandidatesFoundResult,
  UnusableNamesakeResult,
  CandidateVanishedResult,
  CollisionChoice,
  UninstallProgressEvent,
} from "../types";
import { detach } from "../utils/detach";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import {
  capturePruneLeaseAdmission,
  mountPruneLeaseOwner,
  releasePruneLeasesByOwner,
  withPruneLease,
} from "../utils/pruneLease";

type PlayButtonState =
  | "loading"
  | "not_romm"
  | "download"
  | "conflict"
  | "syncing"
  | "play"
  | "launching"
  | "dl_complete"
  | "uninstall_pending"
  | "uninstalling";

interface DownloadProgress {
  bytesDownloaded: number;
  totalBytes: number;
  /** Server honoured the Range probe — Pause/Resume is offered. */
  resumable: boolean;
  /** True once a paused frame arrives; the transfer is frozen, awaiting Resume. */
  paused: boolean;
  /**
   * True once an `extracting` frame arrives — the byte transfer is done and the
   * multi-file ZIP is being unpacked. The transfer is not cancellable here, so
   * the right-side action becomes a disabled throbber instead of the cancel X /
   * Pause-Resume chevron.
   */
  extracting: boolean;
}

function lerpColor(a: [number, number, number], b: [number, number, number], t: number): string {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return `rgb(${r}, ${g}, ${bl})`;
}

// Download button blue gradient stops
const BLUE_LEFT: [number, number, number] = [26, 159, 255]; // #1a9fff
const BLUE_RIGHT: [number, number, number] = [0, 120, 212]; // #0078d4
// Play button visible green (computed from gradient + backgroundSize 330% + backgroundPosition 25%)
const GREEN_LEFT: [number, number, number] = [80, 200, 47]; // #50c82f
const GREEN_RIGHT: [number, number, number] = [24, 177, 78]; // #18b14e

function formatProgress(downloaded: number, total: number): string {
  // Show "x / y MB" with unit only on the total
  if (total < 1024) return `${downloaded} / ${total} B`;
  if (total < 1024 * 1024) return `${(downloaded / 1024).toFixed(1)} / ${(total / 1024).toFixed(1)} KB`;
  if (total < 1024 * 1024 * 1024)
    return `${(downloaded / (1024 * 1024)).toFixed(1)} / ${(total / (1024 * 1024)).toFixed(1)} MB`;
  return `${(downloaded / (1024 * 1024 * 1024)).toFixed(2)} / ${(total / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

interface CustomPlayButtonProps {
  appId: number;
}

// S3776 is raised on the declaration line, so its NOSONAR must stay there. prettier-ignore stops
// Prettier from relocating the trailing comment into the body (which would break the suppression).
// prettier-ignore
export const CustomPlayButton: FC<CustomPlayButtonProps> = ({ appId }) => { // NOSONAR(typescript:S3776) — remaining cc is the per-state render branching (download/dl_complete/uninstalling/launching/syncing/conflict/play each return a distinct button shape); the gate chain lives in utils/launchFunnel.ts, not here.
  const leaseOwner = `custom-play-button:${appId}`;
  const [state, setState] = useState<PlayButtonState>("loading");
  const [romId, setRomId] = useState<number | null>(null);
  const [romName, setRomName] = useState<string>("");
  const [actionPending, setActionPending] = useState(false);
  const [dlProgress, setDlProgress] = useState<DownloadProgress | null>(null);
  const [isOffline, setIsOffline] = useState(getRommConnectionState() === "offline");
  // Positive-knowledge only: set solely when RomM 404s the bound id, so an
  // unreachable server never reaches this state (#1570 F20).
  const [boundVanished, setBoundVanished] = useState(() => isBoundVanished(appId));
  // Running overlay (#1313): when the game is already running, the button shows
  // Resume (top precedence over install/conflict/download) and brings the game to
  // front instead of running the launch funnel. Seeded synchronously at init and
  // flipped live by the `romm_session_changed` listener.
  const [isRunning, setIsRunning] = useState(false);
  // Stop Game is outstanding. The backend refuses a concurrent stop outright
  // (a second stop request would destroy the save the emulator is flushing), so
  // this exists to keep the user from wanting to press it twice: the menu item
  // reads "Stopping..." and is disabled while the ladder runs, which can be
  // several seconds of no visible change.
  const [stopPending, setStopPending] = useState(false);
  // Something already sits where this ROM would be downloaded (#260). Read from
  // the cached detail's single `stat`, so the button says so instead of offering
  // an undifferentiated Download; the comparison itself arrives at click time.
  const [targetOccupied, setTargetOccupied] = useState(false);
  const [candidatePresent, setCandidatePresent] = useState(false);
  // Whether Host multiplayer belongs in the play row's menu (#1 D6). Held with
  // the rom it was read for, so a version switch never shows the answer for the
  // version it replaced.
  const [netplayCapability, setNetplayCapability] = useState<{ romId: number; available: boolean } | null>(null);
  // Per-file progress of an in-flight uninstall (multi-file ROMs only).
  const [uninstallProgress, setUninstallProgress] = useState<{ removed: number; total: number } | null>(null);
  // Set synchronously before the uninstall's first await, so a second press
  // cannot start a duplicate removal while React has not re-rendered yet.
  const uninstallPendingRef = useRef(false);
  const romIdRef = useRef<number | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const transitionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * Enter the download state, restating what the backend found on disk for this
   * ROM: content at its own location, and/or a candidate elsewhere in the
   * platform folder under another name.
   *
   * The single door into that state, because between them the two decide the
   * button's LABEL and both values only ever come from reads the backend took —
   * so neither can be derived here, and both go stale the moment a transfer
   * ends, a version switch rebinds the shortcut, or an uninstall deletes what
   * was found. Defaulting to `false` makes forgetting either one under-claim
   * ("Download" for content that is there, which the gate then catches at click
   * time) rather than over-claim ("Use Existing Files" for content that is gone).
   * The two callers that know the answers pass them.
   *
   * They stay separate rather than folding into one flag because they are
   * different states: an occupied target is compared where it lies, a candidate
   * is renamed into place, and only the first survives a re-`stat` of one path.
   */
  const enterDownloadState = (occupied = false, candidate = false) => {
    setTargetOccupied(occupied);
    setCandidatePresent(candidate);
    setState("download");
  };

  useEffect(() => {
    mountPruneLeaseOwner(leaseOwner);
    return () => {
      detach(releasePruneLeasesByOwner(leaseOwner));
    };
  }, [leaseOwner]);

  // Hide the native PlaySection via CSS while this component is mounted
  useEffect(() => {
    const cls = basicAppDetailsSectionStylerClasses?.PlaySection;
    if (cls) hideNativePlaySection(cls);
    return () => {
      showNativePlaySection();
    };
  }, []);

  // Clear a pending completion-flash timer on unmount
  useEffect(() => {
    return () => {
      if (transitionTimerRef.current) clearTimeout(transitionTimerRef.current);
    };
  }, []);

  // Rehydrate an in-flight or paused download on remount. The cached detail
  // only knows installed-or-not, so without this a paused (or still-running)
  // download shows a plain "Download" button — and a click would `start_download`
  // → truncate the partial .tmp → restart from 0, discarding the paused progress
  // the user expected to resume. Seed from the live queue so the Pause/Resume
  // state survives navigating away and back (#1124).
  const rehydrateInflightDownload = async (rid: number): Promise<void> => {
    try {
      const queue = await getDownloadQueue();
      // No post-await `cancelled` guard needed: React 18 no-ops a setState on an
      // unmounted component, and a remount keeps its own state.
      const entry = queue.downloads.find((d) => d.rom_id === rid);
      if (
        entry &&
        (entry.status === "downloading" ||
          entry.status === "queued" ||
          entry.status === "paused" ||
          entry.status === "extracting")
      ) {
        setActionPending(true);
        setDlProgress({
          bytesDownloaded: entry.bytes_downloaded,
          totalBytes: entry.total_bytes,
          resumable: entry.status === "extracting" ? false : entry.resumable,
          paused: entry.status === "paused",
          extracting: entry.status === "extracting",
        });
      }
    } catch (e) {
      logError(`CustomPlayButton: failed to rehydrate download state: ${e}`);
    }
  };

  // Initial load: determine ROM status from cache (instant, no network calls)
  useEffect(() => {
    let cancelled = false;

    async function init() {
      try {
        const cached = await getCachedGameDetail(appId);
        detach(debugLog(`CustomPlayButton init: appId=${appId} cached.found=${cached.found} cancelled=${cancelled}`));
        if (cancelled) return;
        if (!cached.found) {
          detach(debugLog(`CustomPlayButton: -> not_romm (not in cache)`));
          setState("not_romm");
          return;
        }

        const rid = cached.rom_id!;
        setRomId(rid);
        romIdRef.current = rid;
        if (cached.rom_name) setRomName(cached.rom_name);

        // Seed the running overlay from the live session/running-app state so a
        // button mounted mid-session (or after a reload-adoption) shows Resume
        // immediately, without waiting for a session event (#1313).
        setIsRunning(isSessionActive(rid) || isAppRunning(appId));

        if (cached.installed) {
          // Check for conflicts from cached save status
          const hasConflict = hasAnySaveConflict(cached.save_status);
          if (hasConflict) {
            detach(debugLog(`CustomPlayButton: -> conflict (from cache)`));
            setState("conflict");
          } else {
            detach(debugLog(`CustomPlayButton: -> play`));
            // The state settled here is the CACHED verdict. The live one arrives
            // on the `save_sync` broadcast the play section sends once its own
            // save-status read lands, which flips this button to Resolve Conflict
            // if a fresh conflict appeared. This button must not trigger that read
            // itself: the section wraps it and reads under a wider condition, so a
            // read from here is a second round-trip for a broadcast that already
            // happens (#1758).
            setState("play");
          }
        } else {
          detach(debugLog(`CustomPlayButton: -> download`));
          enterDownloadState(cached.target_path_occupied === true, cached.adoption_candidate_present === true);
          await rehydrateInflightDownload(rid);
        }
      } catch (e) {
        logError(`CustomPlayButton init error: ${e}`);
        if (!cancelled) {
          setState("not_romm");
        }
      }
    }

    detach(init());
    return () => {
      cancelled = true;
    };
  }, [appId]);

  // Asked each time the button settles on Play, which is when the play row's
  // menu can be opened. A failed read leaves Host multiplayer out.
  const playable = state === "play";
  useEffect(() => {
    if (romId === null || !playable) return;
    let cancelled = false;
    const readCapability = async (): Promise<void> => {
      try {
        const capability = await getNetplayCapability(romId);
        if (!cancelled) setNetplayCapability({ romId, available: capability.available });
      } catch (e) {
        detach(debugLog(`CustomPlayButton: multiplayer capability read failed: ${e}`));
      }
    };
    detach(readCapability());
    return () => {
      cancelled = true;
    };
  }, [romId, playable]);
  const hostable = netplayCapability !== null && netplayCapability.romId === romId && netplayCapability.available;

  // Listen for download events
  useEffect(() => {
    // The state the newest `save_sync` broadcast asked for. Read only by the
    // download-complete flash's timer, which clears it when the flash starts —
    // so what it holds when the flash ends is exactly what was announced under
    // the flash, and the timer lands there instead of unconditionally on Play.
    // Deferring rather than dropping is what keeps a conflict announced inside
    // the window from being lost for good: this button hears about one at mount,
    // on a version switch, and on this broadcast, and none of the three repeats
    // for a page that stays open. The rom it was about travels with it, because
    // a version switch inside the window rebinds romIdRef without cancelling the
    // timer.
    let lastAnnouncedState: { romId: number | null; state: PlayButtonState } | null = null;

    const progressListener = addEventListener<[DownloadProgressEvent]>(
      "download_progress",
      (evt: DownloadProgressEvent) => {
        if (evt.rom_id !== romIdRef.current) return;
        if (evt.status === "failed" || evt.status === "cancelled") {
          // A cancelled replace-download already removed a multi-file ROM's
          // directory at admission, so the stat behind the label is spent.
          enterDownloadState();
          setActionPending(false);
          setDlProgress(null);
        } else {
          // A frame that omits resumable (older shape / progress tick before
          // the headers land) keeps the prior verdict instead of resetting it.
          // The post-transfer `extracting` phase carries resumable:false and is
          // never paused — its bytes climb 0→100 again over the uncompressed total.
          const extracting = evt.status === "extracting";
          setDlProgress((prev) => ({
            bytesDownloaded: evt.bytes_downloaded,
            totalBytes: evt.total_bytes,
            resumable: extracting ? false : (evt.resumable ?? prev?.resumable ?? false),
            paused: extracting ? false : evt.status === "paused",
            extracting,
          }));
        }
      },
    );

    const completeListener = addEventListener<[DownloadCompleteEvent]>(
      "download_complete",
      (evt: DownloadCompleteEvent) => {
        if (evt.rom_id !== romIdRef.current) return;
        setDlProgress(null);
        setActionPending(false);
        lastAnnouncedState = null;
        setState("dl_complete");
        transitionTimerRef.current = setTimeout(() => {
          const announced = lastAnnouncedState;
          lastAnnouncedState = null;
          setState(announced !== null && announced.romId === romIdRef.current ? announced.state : "play");
        }, 1100);
      },
    );

    /* istanbul ignore next -- delegation line; end-to-end wiring tested in CustomPlayButton.test.tsx */
    const failedListener = addEventListener<[DownloadFailedEvent]>(
      "download_failed",
      // The global listener in index.tsx owns the failure toast; here we only
      // reset local UI so the user can retry.
      (evt: DownloadFailedEvent) =>
        handleButtonDownloadFailure(evt, romIdRef.current, () => {
          setDlProgress(null);
          setActionPending(false);
          enterDownloadState();
        }),
    );

    const uninstallProgressListener = addEventListener<[UninstallProgressEvent]>(
      "uninstall_progress",
      (evt: UninstallProgressEvent) => {
        if (evt.rom_id !== romIdRef.current) return;
        setUninstallProgress({ removed: evt.files_removed, total: evt.files_total });
      },
    );

    const onUninstall = (e: Event) => {
      const romId = (e as CustomEvent).detail?.rom_id;
      if (romId !== romIdRef.current) return;
      // The one site that cannot go through `enterDownloadState`: the transition
      // is conditional, so a LATER announcement of the same removal — any other
      // writer reloading off this event — cannot replace the pulse this button
      // is already showing. Not this component's own dispatch: `handleUninstall`
      // dispatches before it sets `uninstalling`, so both land in one React
      // batch and the pulse wins on ordering, guard or no guard. Clearing the
      // flags is unconditional either way — the uninstall deleted exactly the
      // content the stat found, and the candidate answer was read at page-open
      // against a folder this removal has just changed.
      setState((prev) => (prev === "uninstalling" ? prev : "download"));
      setActionPending(false);
      setTargetOccupied(false);
      setCandidatePresent(false);
    };
    globalThis.addEventListener("romm_rom_uninstalled", onUninstall);

    // A version switch re-bound this appId's shortcut to a new rom_id (#1298).
    // The picker already invalidated the cached detail; re-read it, adopt the new
    // rom_id, and re-derive the button state so Play↔Download flips with the new
    // version's install status. appId is stable per mount (the component is keyed
    // by it), so the `[appId]`-deps closure captures the right one.
    const handleVersionSwitched = async (): Promise<void> => {
      const cached = await getCachedGameDetail(appId);
      if (!cached.found || cached.rom_id == null) {
        // A switch fired but the rebound detail didn't resolve — the button is now
        // stale. Surface it at warn level (debugLog is dropped at the default level).
        logError(`CustomPlayButton: version_switched for appId ${appId} but cached detail not found — button may be stale`);
        return;
      }
      const rid = cached.rom_id;
      setRomId(rid);
      romIdRef.current = rid;
      if (cached.rom_name) setRomName(cached.rom_name);
      if (cached.installed) {
        setState(hasAnySaveConflict(cached.save_status) ? "conflict" : "play");
      } else {
        // Switched to a not-installed version — clear any download progress and
        // drop to the Download button. The occupancy answer comes from the ROM
        // being switched TO, which the detail just above already carries; the
        // outgoing version's answer says nothing about this one's location.
        setDlProgress(null);
        setActionPending(false);
        enterDownloadState(cached.target_path_occupied === true, cached.adoption_candidate_present === true);
      }
    };

    // Listen for save sync updates (e.g. background check found a conflict) and
    // version switches (Play↔Download flip).
    const onDataChanged = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.type === "version_switched") {
        if (detail.app_id !== appId) return;
        detach(
          handleVersionSwitched().catch((err) =>
            logError(`CustomPlayButton: version_switched handler failed for appId ${appId}: ${err}`),
          ),
        );
        return;
      }
      if (detail?.type !== "save_sync") return;
      if (detail.rom_id && detail.rom_id !== romIdRef.current) return;
      if (detail.has_conflict === undefined) return;
      const announced: PlayButtonState = detail.has_conflict ? "conflict" : "play";
      lastAnnouncedState = { romId: romIdRef.current, state: announced };
      setState((prev) => {
        // The removal lane owns the button from the press until the pulse ends —
        // `uninstall_pending` for however long the backend takes, `uninstalling`
        // for the pulse. Neither verdict is a state this button can offer there:
        // `announced` renders a PRESSABLE Play (or Resolve Conflict) over a
        // disabled "Uninstalling...", for content that is on its way out or
        // already gone. Nothing is deferred out of this lane either — the
        // resting state is Download on success, and on a failed removal
        // `handleUninstall` restores the state it captured at the press.
        if (prev === "uninstall_pending" || prev === "uninstalling") return prev;
        // The download flash holds the button for its own 1100ms and applies
        // `announced` from `lastAnnouncedState` when it ends.
        if (prev === "dl_complete") return prev;
        if (prev === "syncing" || prev === "launching" || prev === "download") return prev;
        return announced;
      });
    };
    globalThis.addEventListener("romm_data_changed", onDataChanged);

    // Re-derive the offline affordance live on any reachability signal (#1345):
    // the shared store flips when a server-touching call fails/succeeds or the
    // recovery probe reconnects, so Download/Play re-enable without a page
    // re-entry (the device symptom of Download staying blocked after reconnect).
    const unsubscribeConnection = onRommConnectionChange((s) => setIsOffline(s === "offline"));
    const unsubscribeVanished = onBoundVanishedChange(() => setBoundVanished(isBoundVanished(appId)));

    // Session start/stop (#1313) — flip the running overlay so the button shows
    // Resume for the live session and returns to Play when it ends. Matches on
    // romId (present in every dispatch); a stop for our rom clears the overlay
    // and the underlying play/conflict state shows through.
    const onSessionChanged = (e: WindowEventMap["romm_session_changed"]) => {
      if (e.detail.romId !== romIdRef.current) return;
      setIsRunning(e.detail.running);
      // Session end is the authoritative "not launching anymore" signal. Game
      // Mode remounts the page on return (init resets the state), but the
      // desktop windowed BPM does not — without this fallback an externally
      // killed emulator leaves the button stuck on "Launching...".
      if (!e.detail.running) {
        setState((prev) => (prev === "launching" ? "play" : prev));
      }
    };
    globalThis.addEventListener("romm_session_changed", onSessionChanged);

    return () => {
      removeEventListener("download_progress", progressListener);
      removeEventListener("download_complete", completeListener);
      removeEventListener("download_failed", failedListener);
      removeEventListener("uninstall_progress", uninstallProgressListener);
      globalThis.removeEventListener("romm_rom_uninstalled", onUninstall);
      globalThis.removeEventListener("romm_data_changed", onDataChanged);
      unsubscribeConnection();
      unsubscribeVanished();
      globalThis.removeEventListener("romm_session_changed", onSessionChanged);
    };
  }, [appId]);

  // Programmatically focus our Play/Download button after mount.
  // This beats HLTB and other plugins that also compete for initial focus.
  useEffect(() => {
    if (state !== "play" && state !== "download" && state !== "conflict") return;
    const timer = setTimeout(() => {
      if (containerRef.current) {
        const btn = containerRef.current.querySelector("button");
        if (btn) {
          btn.focus();
          btn.classList.add("gpfocus");
        }
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [state]);

  // The launch funnel's request for this ROM. The play button is the funnel's
  // page-aware caller: its prompts are the imperative modals, its progress the
  // button's own states, and its admission is scoped to this mounted button.
  const launchRequest = (): LaunchRequest => ({
    appId,
    romId,
    context: "CustomPlayButton",
    prompts: MODAL_LAUNCH_PROMPTS,
    setState,
    leaseOwner,
  });

  const handlePlay = async () => {
    if (state === "syncing" || state === "launching") return; // debounce
    await launchThroughFunnel(launchRequest());
  };

  // Host multiplayer (#1 D6): the same funnel, with the session started once the
  // gate allows the launch and its one-off command written in place of the
  // normal one.
  const handleHost = async () => {
    if (romId === null || state === "syncing" || state === "launching") return;
    const rid = romId;
    await launchThroughFunnel({
      ...launchRequest(),
      netplay: { role: "host", prepare: () => startHostSession(rid) },
    });
  };

  // Resume an already-running game: bring it to the foreground instead of
  // launching (#1313). Foregrounding is pure UI focus navigation the way Steam's
  // own gamescope "Resume Game" does it — `SteamUIStore.SetRunningApp(appId)` +
  // `NavigateToRunningApp()` — NOT a launch: it fires no `GameActionStart` (so the
  // launch interceptor never re-enters) and shows no "already running" dialog, so
  // the pre-launch sync funnel never runs mid-session (which would upload the save
  // while the emulator holds the file open). `RaiseWindowForGame` (the prior
  // approach) is a DESKTOP-overlay call that silently no-ops in gamescope Game Mode
  // — it reports Success but does nothing — so it is not used here.
  const handleResumeGame = async () => {
    // Liveness gate: the overlay can go stale (a session that ended without a stop
    // event reaching this button). If nothing is actually running, clear the
    // overlay and fall through to the normal launch funnel — self-heal, so a click
    // never strands the user on a dead Resume.
    if (!(isAppRunning(appId) || (romId !== null && isSessionActive(romId)))) {
      detach(debugLog(`CustomPlayButton: Resume on appId=${appId} but nothing is running — self-healing to launch`));
      setIsRunning(false);
      await handlePlay();
      return;
    }

    // NOSONAR(typescript:S7741) — SteamUIStore is an ambient Steam SP global; the
    // typeof guard keeps a genuinely-absent one from throwing ReferenceError.
    if (typeof SteamUIStore !== "undefined" && SteamUIStore) {
      // A present-but-broken store is the exact failure class this button was born
      // from (RaiseWindowForGame reporting Success while doing nothing) — a
      // throwing `SetRunningApp` / `NavigateToRunningApp` getter must NOT strand the
      // user with no foreground and no backstop. Any throw is swallowed and falls
      // through to the route nav below, mirroring how runningApps.ts wraps every
      // Steam-global access in try/catch.
      try {
        SteamUIStore.SetRunningApp(appId);
        if (typeof SteamUIStore.NavigateToRunningApp === "function") {
          SteamUIStore.NavigateToRunningApp();
          detach(debugLog(`CustomPlayButton: resumed appId=${appId} via SteamUIStore.NavigateToRunningApp`));
          return;
        }
      } catch (e) {
        detach(debugLog(`CustomPlayButton: resume — SteamUIStore threw, falling back to Navigate: ${e}`));
      }
    }
    // Older SteamUI without `NavigateToRunningApp` (API drift), an absent store, or a
    // store whose `SetRunningApp` / `NavigateToRunningApp` threw — navigate to the
    // running-app route directly. When the store was present and `SetRunningApp`
    // succeeded it already selected this app, so the foreground lands on it (the
    // decky-rocketjump fallback path).
    Navigation.Navigate("/apprunning");
    detach(debugLog(`CustomPlayButton: resumed appId=${appId} via Navigation.Navigate`));
  };

  // Drop the running overlay back to the underlying button state. Clearing
  // `isRunning` alone is not enough: the session-start path leaves the state at
  // "launching", so the overlay coming down would expose a stale "Launching..."
  // label instead of Play. Same reset the session-stop listener applies.
  const clearRunningOverlay = () => {
    setIsRunning(false);
    setState((prev) => (prev === "launching" ? "play" : prev));
  };

  // Stop Game is the only action that can reach the backend twice, and the
  // second reach is save-destroying. The backend's single-flight guard is the
  // load-bearing half (a remount, a second detail page, or the retry the error
  // toast invites all bypass anything held in this component's state); this
  // ref only stops the same button from firing twice. A ref, not the
  // `stopPending` state, because two clicks in one frame both read the old
  // state value — the ref is updated synchronously.
  const stopInFlightRef = useRef(false);

  // Stop the running game. Steam cannot do this itself: the shortcut execs
  // `flatpak run net.retrodeck.retrodeck` and flatpak's portal starts the
  // sandbox outside Steam's `reaper` ancestry, so `SteamClient.Apps.TerminateApp`
  // has nothing to signal (measured on-device: a no-op even with force=true).
  // The backend owns the kill instead — it resolves the flatpak instance's host
  // processes and runs a single-stop-request → grace → force ladder
  // (`services/game_process.py`). The `romId` is what tells it WHICH instance:
  // RetroDECK can have several live at once (a second game, ES-DE opened on its
  // own), and only the one running this ROM may be signalled.
  const handleStopGame = async () => {
    // A stop is already running — do not start a second one. The disabled menu
    // item makes this hard to reach; this is the guard for the paths that
    // bypass the render (a menu opened before the flag flipped, a double-fire
    // within one frame).
    if (stopInFlightRef.current) {
      detach(debugLog(`CustomPlayButton: Stop ignored for appId=${appId} — a stop is already in flight`));
      return;
    }

    // Stale-overlay self-heal, mirroring handleResumeGame: if nothing is
    // actually running, the overlay is stale — clear it back to Play without
    // prompting or touching the backend.
    if (!(isAppRunning(appId) || (romId !== null && isSessionActive(romId)))) {
      detach(debugLog(`CustomPlayButton: Stop on appId=${appId} but nothing is running — clearing stale overlay`));
      clearRunningOverlay();
      return;
    }

    // Without the rom id the backend cannot tell this game's instance from any
    // other live one, and stopping "whichever" is exactly the bug this argument
    // exists to fix. The detail lookup that fills `romId` normally lands long
    // before a running overlay can be pressed; if it somehow has not, say so and
    // leave the overlay up so Resume stays reachable.
    if (romId == null) {
      detach(debugLog(`CustomPlayButton: Stop on appId=${appId} but the rom id is not resolved yet — not stopping`));
      showToast("Couldn't stop the game — still loading its details");
      return;
    }

    // Destructive and unrecoverable: the emulator gets one chance to flush and
    // is forced after that, so anything unsaved is gone. Confirm first.
    if (!(await showStopGameModal())) {
      detach(debugLog(`CustomPlayButton: Stop cancelled for appId=${appId}`));
      return;
    }

    // Claimed only once the user has actually confirmed — an abandoned modal
    // must not leave Stop Game stuck reading "Stopping...".
    stopInFlightRef.current = true;
    setStopPending(true);
    try {
      const result = await stopRunningGame(romId);
      if (result.success || result.reason === "not_running") {
        // "not_running" is the same stale-overlay case caught one layer down:
        // the backend found nothing of RetroDECK's alive. Either way the game is
        // not running now, so the overlay must come down.
        detach(
          debugLog(
            `CustomPlayButton: stop_running_game for appId=${appId} — success=${result.success} ` +
              `reason=${result.reason ?? "none"} stopped=${result.stopped ?? 0} forced=${result.force_killed ?? 0}`,
          ),
        );
        clearRunningOverlay();
        return;
      }
      // Every other failure leaves the overlay UP on purpose. That includes
      // "game_not_running": RetroDECK is alive but the backend could not tie any
      // of its instances to this ROM, so it signalled nothing — the game may
      // well still be running, and Resume has to stay reachable either way.
      detach(
        debugLog(`CustomPlayButton: stop_running_game refused for appId=${appId} — reason=${result.reason ?? "none"}`),
      );
      showToast(result.message || "Couldn't stop the game");
    } catch (e) {
      // The overlay deliberately stays up: the call never reached a verdict, so
      // the game may well still be running and Resume must stay reachable.
      detach(debugLog(`CustomPlayButton: stop_running_game threw for appId=${appId}: ${e}`));
      showToast("Couldn't stop the game");
    } finally {
      // Released on every path, so a failed stop can be retried deliberately
      // (the backend, not this flag, is what makes a retry safe).
      stopInFlightRef.current = false;
      setStopPending(false);
    }
  };

  // Chevron menu for the running overlay — the single destructive Stop Game
  // action, mirroring the download-state showDownloadActionsMenu shape. While a
  // stop is in flight the item is disabled and reads "Stopping..." so the
  // seconds of no visible change don't read as a missed press.
  const showRunningActionsMenu = (e: MouseEvent) => {
    showContextMenu(
      <Menu label="Game Actions">
        <MenuItem key="stop" tone="destructive" disabled={stopPending} onClick={() => detach(handleStopGame())}>
          {stopPending ? "Stopping..." : "Stop Game"}
        </MenuItem>
      </Menu>,
      getEventTarget(e),
    );
  };

  // Resolve the conflict the button is already showing. This is a READ, not a
  // re-sync: it pulls the already-known conflict via `getSaveStatus` and hands
  // it to the shared resolution modal. Re-running the act-capable
  // `preLaunchSync` here (the pre-#1276 behavior) could upload/download OTHER
  // files in the ROM as a side effect and re-derive the conflict through a
  // different path than the one that set the button to "conflict" — so the
  // launch path keeps `preLaunchSync`, but conflict resolution must not act.
  const handleResolveConflict = async () => {
    if (!romId) return;
    setState("syncing");
    try {
      const result = await Promise.race([
        getSaveStatus(romId),
        new Promise<never>((_, reject) => setTimeout(() => reject(new Error("timeout")), 15000)),
      ]);

      if (isCallableFailure(result)) {
        detach(debugLog(`CustomPlayButton: resolve conflict deferred: ${result.message}`));
        showToast(result.message);
        setState("conflict");
        return;
      }

      // A failed status read leaves every file "unknown" and an empty server
      // list; treating that as "resolved" would drop the user back to Play
      // believing the conflict was cleared. Surface it and stay in conflict,
      // exactly like the network-throw catch below (#1276).
      if (result.server_query_failed) {
        // Only an explicit unreachable verdict is a connectivity signal — for
        // the store AND the copy. Off the bare flag both blamed the connection
        // for a ROM the server merely no longer has (#1570).
        const unreachable = result.server_query_reason === "server_unreachable";
        if (unreachable) {
          reportServerReachable(false);
        }
        detach(debugLog(`CustomPlayButton: resolve conflict — server query failed for rom ${romId}`));
        showToast(
          unreachable
            ? "Couldn't reach server to resolve conflict"
            : "RomM couldn't find this game's save data — conflict left unresolved",
        );
        setState("conflict");
        return;
      }
      // A clean status read proves the server is reachable again (#1345).
      reportServerReachable(true);

      if (result.conflicts && result.conflicts.length > 0) {
        const conflictResult = await handleConflicts(result.conflicts);
        if (conflictResult === "cancel") {
          setState("conflict");
          return;
        }
      }
      // Resolved here, or the conflict was already cleared elsewhere (empty/
      // absent conflicts) — notify siblings and go back to play.
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
      setState("play");
    } catch (e) {
      detach(debugLog(`CustomPlayButton: resolve conflict failed: ${e}`));
      showToast("Couldn't reach server to resolve conflict");
      setState("conflict");
    }
  };

  const handleDownload = async (
    replaceExisting = false,
    discardPath?: string,
    collisionChoice: CollisionChoice | null = null,
  ) => {
    if (!romId || actionPending) return;
    setActionPending(true);
    try {
      // Only a FIRST press reports what the page found. Every re-entry carries
      // `replace`, which is the user's answer to a refusal the page's report
      // already produced — reporting it again would ask the backstop to fire on
      // an answer it just received.
      const result = await startDownload(
        romId,
        replaceExisting,
        discardPath ?? null,
        collisionChoice,
        !replaceExisting && candidatePresent,
      );
      if (isRenameCollisions(result)) {
        // Carrying the discarded candidate's saves would land on names that are
        // taken. Nothing has been removed or moved; the one answer covers the
        // whole set, exactly as it does on the adopt exit.
        setActionPending(false);
        const answer = await showAdoptCollisionModal(result.collisions);
        if (answer !== "cancel") await handleDownload(replaceExisting, discardPath, answer);
        return;
      }
      if (isTargetOccupied(result)) {
        // Nothing was written and no transfer started — the backend refused so
        // the user can choose (#260). Back to idle before the dialog opens,
        // because Cancel returns to this button with nothing else to re-enable
        // it; adopt and replace each re-claim the flag on their own path.
        setTargetOccupied(true);
        setActionPending(false);
        await resolveOccupiedTarget(romId, result);
        return;
      }
      if (isCandidatesFound(result)) {
        // Same refusal contract, different subject: the target path was free and
        // the game is on disk under another name. Recorded, because the backend
        // just proved it — without this a cancelled dialog leaves the button
        // reading "Download" for content it has confirmed is there.
        setCandidatePresent(true);
        setActionPending(false);
        await resolveCandidates(romId, result);
        return;
      }
      if (isUnusableNamesake(result)) {
        // A namesake nothing can adopt — the other shape, or a link. Neither
        // flag moves: no content occupies this ROM's own path, and nothing here
        // is a candidate — what the page said stands, and the honest answer to
        // "is this game here" is the dialog the user is about to get.
        setActionPending(false);
        await resolveUnusable(result);
        return;
      }
      if (isCandidateVanished(result)) {
        // The backstop fired: this page said a copy was here and the search can
        // name nothing. The flag goes, because the one thing now known is that
        // what the page found is not there to be used.
        setCandidatePresent(false);
        setActionPending(false);
        await resolveVanished(result);
        return;
      }
      if (!result.success) {
        showToast(result.message || "Download failed");
        setActionPending(false);
      }
    } catch {
      showToast("Download failed — is RomM server running?");
      setActionPending(false);
    }
  };

  // Run the adopt/replace/cancel dialog and carry out the chosen exit. Replace
  // re-enters `handleDownload`; its guard reads the `actionPending` captured by
  // the render still executing here — false — not the live value, so the second
  // call is admitted regardless of what the caller set on the way in.
  const resolveOccupiedTarget = async (rid: number, occupied: TargetOccupiedResult) => {
    const choice = await showAdoptExistingModal(rid, occupied);
    if (choice === "replace") {
      await handleDownload(true);
      return;
    }
    if (choice === "adopt") {
      await handleAdopt(rid);
    }
  };

  // Offer what the search found. One candidate needs no list — there is nothing
  // to choose between — so it goes straight to the comparison. Both download
  // exits re-enter `handleDownload` with `replace`, which is what tells the
  // backend to skip the search rather than refuse a second time.
  //
  // They differ in what they hand back. Choosing a candidate and then Download
  // Instead names it, because the confirmation the user just answered says that
  // file is deleted. "None of These" names nothing: the user declined every
  // candidate rather than picking one, so none of them may be removed.
  const resolveCandidates = async (rid: number, found: CandidatesFoundResult) => {
    let candidate = found.candidates[0];
    if (candidate === undefined) return;
    if (found.candidates.length > 1) {
      const picked = await showAdoptCandidateModal(found);
      if (picked.kind === "cancel") return;
      if (picked.kind === "download") {
        await handleDownload(true);
        return;
      }
      candidate = picked.candidate;
    }
    const choice = await showAdoptExistingModal(rid, comparisonForCandidate(candidate, found.incoming), candidate.path);
    if (choice === "replace") {
      await handleDownload(true, candidate.path);
      return;
    }
    if (choice === "adopt") {
      await handleAdopt(rid, candidate.path);
    }
  };

  // Offer the only two honest exits for a namesake that cannot become this
  // install: fetch the server's copy alongside it, or stop. `replace` is what
  // carries the answer — it is what tells the backend the search has been
  // answered — and no candidate path goes with it, because nothing on disk is
  // being taken over or removed.
  const resolveUnusable = async (unusable: UnusableNamesakeResult) => {
    if ((await showAdoptUnusableModal(unusable)) === "download") await handleDownload(true);
  };

  // The backstop's two exits. Nothing is named, because nothing was found:
  // `replace` here only says the search has been answered.
  const resolveVanished = async (vanished: CandidateVanishedResult) => {
    if ((await showAdoptVanishedModal(vanished)) === "download") await handleDownload(true);
  };

  // Record what is on disk as the install, then write the launch command onto
  // the shortcut exactly as the download-complete listener does — an adopted
  // install is an install (ADR-0028), so it must be as launchable as a
  // downloaded one the moment the dialog closes.
  const handleAdopt = async (rid: number, candidatePath?: string, collisionChoice: CollisionChoice | null = null) => {
    setActionPending(true);
    const admission = capturePruneLeaseAdmission(leaseOwner);
    try {
      const result = await adoptExistingRom(rid, candidatePath ?? null, collisionChoice);
      if (isRenameCollisions(result)) {
        // Nothing has moved. The one answer covers the whole set, and a dismissed
        // dialog leaves the game exactly as it was.
        const answer = await showAdoptCollisionModal(result.collisions);
        if (answer !== "cancel") await handleAdopt(rid, candidatePath, answer);
        return;
      }
      if (!result.success) {
        showToast(result.message || "Couldn't use the existing files");
        return;
      }
      const adoptedAppId = result.app_id;
      if (adoptedAppId != null && result.launch_options !== undefined) {
        const launchOptions = result.launch_options;
        await withPruneLease(
          result.prune_lease_token,
          "ROM adopt",
          async (signal) => {
            if (signal.aborted) return;
            await setLaunchOptionsConfirmed(adoptedAppId, launchOptions).catch(() => false);
          },
          leaseOwner,
          admission,
        );
      }
      setTargetOccupied(false);
      setCandidatePresent(false);
      setState("play");
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "rom_adopted", rom_id: rid } }));
      showToast(`${romName || "ROM"} is ready to play`);
    } catch (e) {
      detach(debugLog(`CustomPlayButton: adopt failed: ${e}`));
      showToast("Couldn't use the existing files — is RomM server running?");
    } finally {
      setActionPending(false);
    }
  };

  // Cancel an in-flight download. Fire-and-forget: the backend emits a
  // cancelled download_progress frame that the progress listener reacts to
  // (resets to "download"). The inline .catch keeps the click non-throwing.
  const handleCancelDownload = () => {
    if (romId == null) return;
    detach(cancelDownload(romId).catch(() => {}));
  };

  // Pause an in-flight (resumable) download. Fire-and-forget: the backend
  // freezes the transfer and emits a "paused" download_progress frame the
  // listener reacts to (sets dlProgress.paused). .catch keeps the click safe.
  const handlePause = () => {
    if (romId == null) return;
    detach(pauseDownload(romId).catch(() => {}));
  };

  // Resume a paused download. The success path is fire-and-forget — the backend
  // re-begins the transfer from the partial .tmp and emits "downloading" frames
  // the listener reacts to (clears the paused flag). A REFUSAL has to be said out
  // loud: a resume can be turned down (content appeared at the game's location
  // while it sat paused, or a version switch stranded this target), and a silent
  // refusal leaves the user pressing a button that does nothing, with Cancel —
  // which discards the transferred bytes — as their only way out.
  const handleResume = () => {
    if (romId == null) return;
    detach(
      resumeDownload(romId)
        .then((result) => {
          if (result.success) return;
          showToast(
            isTargetOccupied(result)
              ? "Something else is at this game's location now — cancel the download and start again"
              : result.message || "Couldn't resume the download",
          );
        })
        .catch(() => showToast("Couldn't resume the download — is RomM server running?")),
    );
  };

  const handleUninstall = async () => {
    if (!romId || uninstallPendingRef.current) return;
    // Removing a large multi-file ROM takes long enough that a button which only
    // changes on completion reads as dead and gets pressed again (#1664). Claim
    // the press before the first await and show it immediately.
    uninstallPendingRef.current = true;
    const stateBeforeUninstall = state;
    setUninstallProgress(null);
    setState("uninstall_pending");
    detach(debugLog(`CustomPlayButton: uninstalling romId=${romId}`));
    try {
      const admission = capturePruneLeaseAdmission(leaseOwner);
      const result = await removeRom(romId);
      if (result.success) {
        // Reset the now-stale launch command to the uninstalled "" placeholder so a
        // raced-past not_installed launch execs `bin/rom-launcher` with no args (clean
        // exit 1) instead of a stale `flatpak run … "<deleted path>"` (#1051). Best-effort:
        // a launch-options hiccup must not turn a successful uninstall into an error.
        await withPruneLease(
          result.prune_lease_token,
          "ROM uninstall",
          async (signal) => {
            if (signal.aborted) return;
            await setLaunchOptionsConfirmed(appId, "").catch(() => false);
          },
          leaseOwner,
          admission,
        );
        globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: romId } }));
        showToast(`${romName || "ROM"} uninstalled`);
        // Dark pulse transition before showing Download button
        setState("uninstalling");
        transitionTimerRef.current = setTimeout(() => enterDownloadState(), 500);
        return;
      } else {
        showToast(result.message || "Uninstall failed");
        setState(stateBeforeUninstall);
      }
    } catch {
      showToast("Uninstall failed");
      setState(stateBeforeUninstall);
    } finally {
      uninstallPendingRef.current = false;
      setUninstallProgress(null);
    }
  };

  const showDropdownMenu = (e: MouseEvent) => {
    showContextMenu(
      <Menu label="RomM Actions">
        {hostable && (
          <MenuItem
            key="host"
            onClick={() => {
              detach(handleHost());
            }}
          >
            Host multiplayer
          </MenuItem>
        )}
        <MenuItem
          key="uninstall"
          tone="destructive"
          onClick={() => {
            detach(handleUninstall());
          }}
        >
          Uninstall
        </MenuItem>
      </Menu>,
      getEventTarget(e),
    );
  };

  // Pause/Resume + Cancel menu for a resumable download. When the transfer is
  // paused the primary entry is Resume; otherwise it's Pause. Cancel is always
  // offered.
  const showDownloadActionsMenu = (e: MouseEvent, paused: boolean) => {
    showContextMenu(
      <Menu label="Download Actions">
        {paused ? (
          <MenuItem key="resume" onClick={handleResume}>
            Resume
          </MenuItem>
        ) : (
          <MenuItem key="pause" onClick={handlePause}>
            Pause
          </MenuItem>
        )}
        <MenuItem key="cancel" tone="destructive" onClick={handleCancelDownload}>
          Cancel
        </MenuItem>
      </Menu>,
      getEventTarget(e),
    );
  };

  // Don't render for non-RomM games
  if (state === "not_romm" || state === "loading") {
    detach(debugLog(`CustomPlayButton: returning null (state=${state})`));
    return null;
  }
  detach(debugLog(`CustomPlayButton: rendering state=${state}`));

  // Dropdown arrow button style. Shared shape for the play-state chevron and
  // the download-state cancel X — both are 36px side actions on the right.
  const dropdownArrowStyle: React.CSSProperties = {
    height: "48px",
    width: "36px",
    minWidth: "36px",
    padding: 0,
    border: "none",
    borderRadius: "0 2px 2px 0",
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    borderLeft: "1px solid rgba(0, 0, 0, 0.2)",
  };

  // Consistent button container size across all states (Play has dropdown = 36px extra)
  const btnContainerStyle: React.CSSProperties = {
    display: "flex",
    flexDirection: "row",
    width: "200px",
    height: "48px",
  };

  const mainBtnStyle: React.CSSProperties = {
    height: "100%",
    flex: "1 1 auto",
    padding: "4px 12px",
    border: "none",
    color: "#fff",
    fontSize: "16px",
    fontWeight: "bold",
  };

  // Running overlay (#1313) — top precedence over install/conflict/download. The
  // green Resume button brings the live session to front via `handleResumeGame`;
  // a chevron beside it opens the Stop Game action, which confirms and then has
  // the backend terminate the emulator. No Uninstall entry here — uninstalling a
  // running game is a footgun.
  if (isRunning) {
    return (
      <Focusable
        ref={containerRef}
        className={[appActionButtonClasses?.PlayButtonContainer, appActionButtonClasses?.Green]
          .filter(Boolean)
          .join(" ")}
        style={btnContainerStyle}
      >
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play"].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px 0 0 2px",
            background: "linear-gradient(to right, #70d61d 0%, #01a75b 60%)",
            backgroundPosition: "25%",
            backgroundSize: "330% 100%",
          }}
          onClick={() => {
            detach(handleResumeGame());
          }}
          onFocus={scrollToTop}
        >
          Resume
        </DialogButton>
        <DialogButton
          className="romm-btn-cancel"
          aria-label="Game actions"
          title="Game actions"
          style={{
            ...dropdownArrowStyle,
            background: "rgba(255, 255, 255, 0.15)",
            color: "#fff",
          }}
          onClick={(e: MouseEvent) => showRunningActionsMenu(e)}
        >
          <svg width="12" height="8" viewBox="0 0 12 8" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path
              d="M1 1.5L6 6.5L11 1.5"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "dl_complete") {
    // "Ready!" state — must match the Play button exactly (same classes + Green tint)
    return (
      <Focusable
        className={[appActionButtonClasses?.PlayButtonContainer, appActionButtonClasses?.Green]
          .filter(Boolean)
          .join(" ")}
        style={btnContainerStyle}
      >
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play", "romm-dl-complete-flash"]
            .filter(Boolean)
            .join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #80e62a, #01b866)",
            filter: "brightness(1.2)",
          }}
          disabled
        >
          <span className="romm-dl-label">Ready!</span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "download") {
    const t = dlProgress && dlProgress.totalBytes > 0 ? dlProgress.bytesDownloaded / dlProgress.totalBytes : 0;
    const downloading = actionPending && dlProgress;
    const paused = downloading ? dlProgress.paused : false;
    const resumable = downloading ? dlProgress.resumable : false;
    // Post-transfer ZIP unpack for a multi-file ROM — bytes climb 0→100 again
    // over the uncompressed total. Not cancellable: the right-side action is a
    // disabled throbber rather than the cancel X / Pause-Resume chevron.
    const extracting = downloading ? dlProgress.extracting : false;

    // Fill color shifts from blue to green as download progresses. Extraction
    // begins right after the transfer hit 100% green, so it keeps the solid
    // green fill for visual continuity.
    let fillColor: string;
    if (extracting) {
      fillColor = `linear-gradient(to right, rgb(${GREEN_LEFT.join(",")}), rgb(${GREEN_RIGHT.join(",")}))`;
    } else if (downloading) {
      fillColor = `linear-gradient(to right, ${lerpColor(BLUE_LEFT, GREEN_LEFT, t)}, ${lerpColor(BLUE_RIGHT, GREEN_RIGHT, t)})`;
    } else {
      fillColor = "linear-gradient(to right, #1a9fff, #0078d4)";
    }

    // Pulse color shifts from blue to green with progress; a paused download
    // freezes to a dim amber so the whole group reads as "halted, not running".
    // Extraction holds the green pulse — it just finished the transfer.
    let pulseColor: string;
    if (paused) {
      pulseColor = "rgba(212,167,44,0.7)";
    } else if (extracting) {
      pulseColor = `rgb(${GREEN_LEFT.join(", ")})`;
    } else if (downloading) {
      pulseColor = lerpColor(BLUE_LEFT, GREEN_LEFT, t);
    } else {
      pulseColor = "rgba(26,159,255,0.7)";
    }

    let dlLabel: string;
    if (extracting) {
      dlLabel = `Extracting… ${Math.round(t * 100)}%`;
    } else if (paused) {
      dlLabel = "Paused";
    } else if (downloading) {
      dlLabel = formatProgress(dlProgress.bytesDownloaded, dlProgress.totalBytes);
    } else if (actionPending) {
      dlLabel = "Starting...";
    } else if (targetOccupied || candidatePresent) {
      // Pressing opens the comparison dialog (#260), so the label names that
      // action rather than a state: nothing is installed here, and a label
      // describing the files would read as "installed and ready". The verb
      // matches the dialog's own adopt button ("Use These Files") so the button
      // promises exactly what the dialog then offers.
      //
      // Both states earn the label. The user should not have to press Download
      // to learn their own copy is sitting in the folder under another name.
      //
      // `candidatePresent` can overpromise: the page and the click-time search
      // read the same folder knowing different things about it, and have
      // disagreed on the served shape, the platform folder, the matched name and
      // the listing itself. What the label promises is still kept — pressing
      // ends in a dialog either way — but not because those differences are
      // known to run one way. It holds because the search's last answer is a
      // backstop: this flag is sent back on the press, and a page that reported
      // a copy can never end in a silent download.
      dlLabel = "Use Existing Files";
    } else {
      dlLabel = "Download";
    }

    // Unfilled portion: darker shade of the current fill color. Extraction
    // keeps a dim green base (the transfer just completed green).
    let baseBg: string;
    if (isOffline) {
      baseBg = "linear-gradient(to right, #6b7b8b, #5a6a7a)";
    } else if (extracting) {
      baseBg = "linear-gradient(to right, #1a4d1a, #0f3320)";
    } else if (downloading) {
      baseBg = `linear-gradient(to right, ${lerpColor([10, 50, 90], [5, 35, 65], t)}, ${lerpColor([5, 35, 65], [5, 50, 30], t)})`;
    } else {
      baseBg = "linear-gradient(to right, #1a9fff, #0078d4)";
    }

    // While a download is actively running, the main button shares the row
    // with a right-side action section (the cancel X or a Pause/Resume
    // dropdown). Square off its right edge so it butts cleanly against that
    // section; idle/starting keeps the full pill radius. The pulse animation
    // lives on the container (romm-dl-active-group) so it spans the whole
    // control — button + action — as one cohesive pulsing group.
    // Only the idle Download action is blocked: a vanished bound ROM cannot be
    // fetched, so offering it can only produce the not_found toast. The button
    // stays visible rather than disappearing, matching how the picker shows a
    // vanished version dimmed instead of hiding it. An in-flight download keeps
    // its controls — that is a different action and out of scope.
    const downloadBlockedByVanished = boundVanished && !downloading && !paused && !extracting;
    const downloadBtn = (
      <DialogButton
        // romm-btn-download-idle carries the blue hover/focus highlight, which is
        // only correct for the idle/starting button (blue base). The active button
        // (downloading/paused/extracting) omits it so its dark baseBg + green fill
        // aren't repainted blue when focused — the rehydrated-paused device bug.
        className={[appActionButtonClasses?.PlayButton, "romm-btn-download", !downloading && "romm-btn-download-idle"]
          .filter(Boolean)
          .join(" ")}
        style={{
          ...mainBtnStyle,
          borderRadius: downloading ? "2px 0 0 2px" : "2px",
          background: baseBg,
        }}
        onClick={() => {
          detach(handleDownload());
        }}
        disabled={actionPending || isOffline || downloadBlockedByVanished}
      >
        {/* Progress fill bar — kept at its frozen width while paused. */}
        {downloading && (
          <div
            className="romm-dl-fill"
            style={{
              width: `${t * 100}%`,
              background: fillColor,
            }}
          />
        )}
        <span className="romm-dl-label">{dlLabel}</span>
      </DialogButton>
    );

    if (!downloading) {
      // Idle ("Download") or "Starting..." — single full-width button, no action.
      return (
        <Focusable ref={containerRef} className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
          {downloadBtn}
        </Focusable>
      );
    }

    const cancelX = (
      <DialogButton
        className="romm-btn-cancel"
        aria-label="Cancel download"
        title="Cancel download"
        style={{
          ...dropdownArrowStyle,
          background: "rgba(255, 255, 255, 0.15)",
          color: "#fff",
        }}
        onClick={handleCancelDownload}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d="M1 1L11 11M11 1L1 11"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
        </svg>
      </DialogButton>
    );

    // Resumable downloads (live or paused) get a dropdown chevron whose menu
    // offers Pause/Resume + Cancel; non-resumable downloads keep the direct
    // cancel X (the #1122 behavior — multi-file zips and Cloudflare can't
    // resume, so there's nothing to pause).
    const dropdown = (
      <DialogButton
        className="romm-btn-cancel"
        aria-label="Download actions"
        title="Download actions"
        style={{
          ...dropdownArrowStyle,
          background: "rgba(255, 255, 255, 0.15)",
          color: "#fff",
        }}
        onClick={(e: MouseEvent) => showDownloadActionsMenu(e, paused)}
      >
        <svg width="12" height="8" viewBox="0 0 12 8" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d="M1 1.5L6 6.5L11 1.5"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </DialogButton>
    );

    // Extraction is not cancellable — the right-side action is a disabled
    // throbber (same 36px slot, squared-left/rounded-right) so the control reads
    // "working, can't stop" rather than offering a cancel/pause it can't honour.
    const extractThrobber = (
      <DialogButton
        className="romm-btn-cancel"
        aria-label="Extracting"
        title="Extracting"
        style={{
          ...dropdownArrowStyle,
          background: "rgba(255, 255, 255, 0.15)",
          color: "#fff",
        }}
        disabled
      >
        <span className={`${appActionButtonClasses?.Throbber || ""} romm-throbber`.trim()} />
      </DialogButton>
    );

    // Active download: button + a right-side action section. The section is a
    // flex sub-container so the throbber-vs-dropdown-vs-X choice is a clean
    // conditional. The pulse runs on the container so it spans the whole group.
    let rightAction: ReactElement;
    if (extracting) {
      rightAction = extractThrobber;
    } else if (resumable) {
      rightAction = dropdown;
    } else {
      rightAction = cancelX;
    }
    return (
      <Focusable
        ref={containerRef}
        className={[appActionButtonClasses?.PlayButtonContainer, "romm-dl-active-group"].filter(Boolean).join(" ")}
        style={{ ...btnContainerStyle, "--romm-pulse-color": pulseColor } as React.CSSProperties}
      >
        {downloadBtn}
        <div style={{ display: "flex", flexDirection: "row", height: "100%" }}>{rightAction}</div>
      </Focusable>
    );
  }

  if (state === "uninstall_pending") {
    return (
      <Focusable className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-download"].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #47b3ff, #1a9fff)",
          }}
          disabled
        >
          <span className="romm-dl-label">
            {uninstallProgress
              ? `Uninstalling ${uninstallProgress.removed}/${uninstallProgress.total}`
              : "Uninstalling..."}
          </span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "uninstalling") {
    return (
      <Focusable className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-download", "romm-dl-uninstall-flash"]
            .filter(Boolean)
            .join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #47b3ff, #1a9fff)",
            filter: "brightness(1.3)",
          }}
          disabled
        >
          <span className="romm-dl-label">Uninstalled</span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "launching") {
    return (
      <Focusable className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play", isOffline && "romm-offline"]
            .filter(Boolean)
            .join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #70d61d 0%, #01a75b 60%)",
            backgroundPosition: "25%",
            backgroundSize: "330% 100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "8px",
          }}
          disabled
        >
          <span className={`${appActionButtonClasses?.Throbber || ""} romm-throbber`.trim()} />
          <span>Launching...</span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "syncing") {
    return (
      <Focusable className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play", isOffline && "romm-offline"]
            .filter(Boolean)
            .join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #70d61d 0%, #01a75b 60%)",
            backgroundPosition: "25%",
            backgroundSize: "330% 100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "8px",
          }}
          disabled
        >
          <span className={`${appActionButtonClasses?.Throbber || ""} romm-throbber`.trim()} />
          <span>Syncing saves...</span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "conflict") {
    return (
      <Focusable ref={containerRef} className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-conflict"].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #d4a72c, #b8941f)",
          }}
          onClick={() => {
            detach(handleResolveConflict());
          }}
        >
          Resolve Conflict
        </DialogButton>
      </Focusable>
    );
  }

  // state === "play"
  const playBg = isOffline
    ? "linear-gradient(to right, #6b7b6b 0%, #5a6a5a 60%)"
    : "linear-gradient(to right, #70d61d 0%, #01a75b 60%)";
  const dropdownBg = isOffline
    ? "linear-gradient(to right, #5a6a5a, #4d5d4d)"
    : "linear-gradient(to right, #4da636, #3f8a2b)";
  return (
    <Focusable
      ref={containerRef}
      className={[appActionButtonClasses?.PlayButtonContainer, !isOffline && appActionButtonClasses?.Green]
        .filter(Boolean)
        .join(" ")}
      style={btnContainerStyle}
    >
      <DialogButton
        className={[appActionButtonClasses?.PlayButton, "romm-btn-play", isOffline && "romm-offline"]
          .filter(Boolean)
          .join(" ")}
        style={{
          ...mainBtnStyle,
          borderRadius: "2px 0 0 2px",
          background: playBg,
          backgroundPosition: "25%",
          backgroundSize: "330% 100%",
        }}
        onClick={() => {
          detach(handlePlay());
        }}
        onFocus={scrollToTop}
      >
        Play
      </DialogButton>
      <DialogButton
        className="romm-btn-dropdown"
        style={{
          ...dropdownArrowStyle,
          background: dropdownBg,
        }}
        onClick={showDropdownMenu}
        onFocus={scrollToTop}
      >
        <svg width="12" height="8" viewBox="0 0 12 8" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d="M1 1.5L6 6.5L11 1.5"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </DialogButton>
    </Focusable>
  );
};
