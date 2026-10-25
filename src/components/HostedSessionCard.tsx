import { useState, type FC } from "react";
import { ButtonItem, Focusable, PanelSectionRow } from "@decky/ui";
import {
  isCandidateVanished,
  isCandidatesFound,
  isRenameCollisions,
  isTargetOccupied,
  isUnusableNamesake,
  prepareJoinSession,
  startDownload,
  type HostedSession,
} from "../api/backend";
import { launchThroughFunnel, type LaunchFunnelState } from "../utils/launchFunnel";
import { useDownloads } from "../utils/downloadStore";
import { showToast } from "../utils/toast";
import { detach } from "../utils/detach";
import { MODAL_LAUNCH_PROMPTS } from "./launchPrompts";

/** What the card says under the host's name, where the session's state needs words. */
const STATE_LINE: Record<HostedSession["state"], string | null> = {
  join: null,
  download: "Download the game to join.",
  missing_core: "RetroDECK on this device does not have the emulator core this session runs on.",
  not_in_library: "This game is not in Tender's library on this device.",
};

const JOIN_LABEL: Record<LaunchFunnelState, string> = {
  syncing: "Syncing saves…",
  launching: "Joining…",
  play: "Join",
  conflict: "Join",
};

/**
 * One multiplayer session another user hosts, on Main: who hosts which game,
 * and the one press that answers it.
 *
 * Join goes through the same launch funnel as the play row (#1 D25), and is the
 * one action on Main whose home is not a page: answering a session from the
 * card saves the trip to the game's page (#1 D5). Download starts the same
 * download the game page's button does; the card reads Join once the poll sees
 * the game installed. A session this device cannot join says why and offers no
 * button.
 */
export const HostedSessionCard: FC<{ session: HostedSession }> = ({ session }) => {
  const [joinState, setJoinState] = useState<LaunchFunnelState>("play");
  const [downloadPending, setDownloadPending] = useState(false);
  const downloads = useDownloads();
  const entry = downloads.find((item) => item.rom_id === session.rom_id);
  const downloading = entry !== undefined && entry.status !== "failed" && entry.status !== "cancelled";

  const join = async (): Promise<void> => {
    if (session.app_id === null || joinState === "syncing" || joinState === "launching") return;
    await launchThroughFunnel({
      appId: session.app_id,
      romId: session.rom_id,
      context: "HostedSessionCard",
      prompts: MODAL_LAUNCH_PROMPTS,
      setState: setJoinState,
      netplay: {
        role: "guest",
        prepare: () => prepareJoinSession(session.rom_id, session.host_user_id, session.started_at),
      },
    });
  };

  const download = async (): Promise<void> => {
    setDownloadPending(true);
    try {
      const result = await startDownload(session.rom_id, false, null, null, false);
      if (
        isTargetOccupied(result) ||
        isCandidatesFound(result) ||
        isUnusableNamesake(result) ||
        isCandidateVanished(result) ||
        isRenameCollisions(result)
      ) {
        // Each of these asks the user to choose what happens to files already on
        // disk, which the game page's dialogs do and this card does not.
        showToast(
          `${session.rom_name} has files on disk already. Open its page and press Download there to choose what to do.`,
        );
      } else if (!result.success) {
        showToast(result.message || "Download failed");
      }
    } catch {
      showToast("Download failed. Is the RomM server running?");
    } finally {
      setDownloadPending(false);
    }
  };

  const stateLine = STATE_LINE[session.state];
  let action: { label: string; disabled: boolean; onClick: () => void } | null = null;
  if (session.state === "join") {
    action = {
      label: JOIN_LABEL[joinState],
      disabled: joinState === "syncing" || joinState === "launching",
      onClick: () => detach(join()),
    };
  } else if (session.state === "download") {
    action = {
      label: downloading ? "Downloading…" : "Download",
      disabled: downloading || downloadPending,
      onClick: () => detach(download()),
    };
  }

  return (
    <>
      <PanelSectionRow>
        <Focusable onActivate={() => {}}>
          <div
            style={{
              padding: "8px 12px",
              backgroundColor: "rgba(89, 191, 64, 0.15)",
              borderLeft: "3px solid #59bf40",
              borderRadius: "4px",
              fontSize: "12px",
            }}
          >
            <div style={{ fontWeight: "bold", color: "#59bf40", marginBottom: "4px" }}>
              {session.host_username} is hosting
            </div>
            <div>{session.rom_name}</div>
            {stateLine && <div style={{ color: "rgba(255, 255, 255, 0.7)", marginTop: "4px" }}>{stateLine}</div>}
          </div>
        </Focusable>
      </PanelSectionRow>
      {action && (
        <PanelSectionRow>
          <ButtonItem layout="below" bottomSeparator="none" disabled={action.disabled} onClick={action.onClick}>
            {action.label}
          </ButtonItem>
        </PanelSectionRow>
      )}
    </>
  );
};
