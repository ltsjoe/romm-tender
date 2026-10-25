import type { LaunchPrompts } from "../utils/launchInterceptor";
import { showCoreChangeModal } from "./CoreChangeModal";
import { handleConflicts } from "./SyncConflictModal";
import { showOfflineDriftModal } from "./OfflineDriftModal";
import { showFallbackLaunchModal } from "./FallbackLaunchModal";

/**
 * The questions a launch may put to the user, answered by the shared modals.
 * Handed to the launch funnel by every surface that launches a game, and to
 * the launch watcher at plugin load.
 */
export const MODAL_LAUNCH_PROMPTS: LaunchPrompts = {
  confirmCoreChange: showCoreChangeModal,
  resolveConflicts: handleConflicts,
  askOfflineDrift: showOfflineDriftModal,
  confirmFallbackLaunch: showFallbackLaunchModal,
};
