# Netplay

Two players who run Tender against the same RomM server can play one game together over the network. One of them hosts:
Tender starts the game with the emulator's own netplay switched on and tells RomM a session is running. The other sees
the session in their own Tender and joins it. Why it is built this way, and what was not chosen, is
[ADR 0034](../adr/0034-multiplayer-runs-on-each-emulators-netplay-and-is-found-through-romm.md); the user's side is
[Multiplayer](../user-guide/multiplayer.md).

## Where the code lives

| Module                                 | Responsibility                                                                     |
| -------------------------------------- | ---------------------------------------------------------------------------------- |
| `domain/netplay.py`                    | Which launch can host, the PS1 core switch, command and config text, address order |
| `domain/netplay_note.py`               | The session note: its format, render, parse, and the strict check of every field   |
| `services/netplay.py`                  | Capability, host and guest sessions, the heartbeat and relay tasks, the guest list |
| `adapters/romm/romm_api.py`            | RomM's activity feed and ROM notes                                                 |
| `adapters/netplay_config.py`           | The appended config files, in a folder the RetroDECK sandbox can read              |
| `adapters/local_network.py`            | This machine's IPv4 addresses, and a TCP probe of another's                        |
| `adapters/libretro_lobby.py`           | libretro's public netplay lobby                                                    |
| `src/utils/launchFunnel.ts`            | The one launch funnel: the pre-launch gate, then a normal or a netplay launch      |
| `src/utils/netplayLaunch.ts`           | The active netplay launch, its end when the game exits, and cleanup at load        |
| `src/utils/hostedSessions.ts`          | The 30-second poller and the store of hosted sessions Main reads                   |
| `src/components/HostedSessionCard.tsx` | A hosted session's card on Main                                                    |
| `src/components/CustomPlayButton.tsx`  | Host multiplayer in the play row's chevron menu                                    |

## Which games can be hosted

Only two emulators RetroDECK ships can be put into a session from the command line, with no press inside the emulator:

- **RetroArch**, for a core whose `.info` file says `savestate_features = "deterministic"`. RetroArch refuses netplay
  for any other core, so Tender does not offer it. RetroDECK's default PS1 core, SwanStation, is not deterministic; a
  PS1 game on it is hosted and joined on PCSX ReARMed (`pcsx_rearmed_libretro`) for that session, if that core is among
  the system's options. The game's chosen core is never changed or pinned. A PS1 game on DuckStation standalone is not
  switched: a RetroArch session would not find DuckStation's memory cards.
- **PPSSPP** standalone, for PSP games, through the game's own ad hoc multiplayer.

Every other emulator answers `emulator_not_supported`, and a RetroArch core that is not deterministic answers
`core_not_supported`. Host and Join also need RomM 5.0.0 or newer, the release that added the activity feed
(`ACTIVITY_MIN_VERSION`); an older server answers `unsupported`, and Tender's own floor stays 4.9.0.

## The launch command

A session's launch is the game's normal RetroDECK command with flags put in front of `%ROM%` inside the `-e` string
(`invocation_with_launch_args` in `domain/shortcut_data.py`):

| Role             | Flags                                                                                             |
| ---------------- | ------------------------------------------------------------------------------------------------- |
| RetroArch host   | `--host --appendconfig=/var/config/tender-netplay/retroarch-host.cfg`                             |
| RetroArch guest  | `--connect=<addr> --port=<port> [--mitm-session=<id>] --sram-mode=noload-nosave --appendconfig=…` |
| PPSSPP, any role | `--appendconfig=/var/config/tender-netplay/ppsspp-host.ini` or `ppsspp-guest.ini`                 |

RetroDECK's `run_game.sh` runs the `-e` string through `eval`. So nothing that can hold a shell character (a nickname, a
password) goes on the command line: both go in the appended config file. Every value that does go on the command line is
checked against a whole-value pattern first (`_SAFE_ARG_RE`), and RetroArch's `addr|port|session` form of `--connect` is
never used, since `|` is a shell pipe.

`--sram-mode=noload-nosave` is there because a RetroArch guest plays on the host's save: on connecting, RetroArch copies
the host's save RAM over the guest's, and would otherwise write it into the guest's own save file, which the next save
sync would upload over the guest's save in RomM. A PPSSPP ad hoc player runs their own game on their own save, so both
sides save and sync as usual.

The one-off command is written to the shortcut for that launch only. It is never recorded in `applied_launch_options`;
the six recorded-state writer sites stay six. When the game exits, the normal command goes back through the usual
re-confirm, and after a crash the startup heal puts it back.

## The appended config

The files live in `~/.var/app/net.retrodeck.retrodeck/config/tender-netplay/` on the host, which the sandbox sees as
`/var/config/tender-netplay/`, readable by the owner only.

- **RetroArch host:** `netplay_password`, `netplay_nickname`, `netplay_ip_port = "55435"`, the relay on
  (`netplay_use_mitm_server`, `netplay_mitm_server = "nyc"`), `netplay_public_announce = "true"` (the relay session id
  is published nowhere but libretro's lobby), and for a PS1 session switched to PCSX ReARMed, `savefile_directory`
  pointed at the folder the game's own core saves in with both sort-savefiles flags off, so the memory card is read and
  written where it always is.
- **RetroArch guest:** the session's password and the guest's own nickname.
- **Both RetroArch files** set `config_save_on_exit = "false"`. RetroArch writes appended settings into `retroarch.cfg`
  when it exits; without this, the relay, the public announce and the save folder would stick for every later game.
- **PPSSPP:** `[Network]` with `EnableWlan = True`; the host runs PPSSPP's built-in ad hoc server
  (`EnableAdhocServer = True`), and both point `proAdhocServer` at the host's address.

The nickname is the RomM username, read from `/api/users/me` when a session starts and cleaned of quotes, backslashes
and control characters. The password is 16 hex characters from the injected UUID generator, made fresh per session.

## Finding a session: RomM activity and a public note

While a session runs, the host's backend task sends `POST /api/activity/heartbeat {rom_id, device_id}` every 30 seconds.
RomM keeps an entry for 90 seconds after the last one, and every user who can see the ROM reads it through
`GET /api/activity`. A device id is needed, so starting a session registers this machine as a RomM device when none is
registered, without turning save sync on (`ensure_registered` in `services/saves/sync_engine/devices.py`).

The connection details go in a **public note** on the ROM titled `Tender multiplayer session`. Its first line says the
game is hosted with Tender and joined from Tender; then a fenced block with the info string `tender-netplay` holds JSON
with `"v": 1`: the kind, the core (`.so` name only), the host's addresses, the port, the relay once known, the password,
the nickname, and when the session started. RomM allows one note per ROM, user and title, so the host removes its own
earlier note first.

For RetroArch, the relay session is known only once libretro's lobby (`http://lobby.libretro.com/list`, plain HTTP)
lists the room. The host looks every 5 seconds, 24 times, for the newest relayed room under its nickname on its core,
created no earlier than 30 seconds before the session started, and writes the relay into the note with `PUT`.

The host records the note in `kv_config` (`netplay_host_session`). Ending the session clears the heartbeat
(`DELETE /api/activity/heartbeat`), deletes the note and the config file, and clears the record. A note left by a crash
is deleted at the next start; until then guests ignore it, because its host has no live activity entry.

**RomM permissions.** The heartbeat and the note need `roms.user.write`, the activity list `roms.user.read`. A token
from a username-and-password sign-in carries both; a pairing code or a pasted token carries whatever its user picked in
RomM. A 403 on either answers `reauth_required`.

## The guest's side

`list_hosted_sessions` reads the activity feed, drops this user's own entries, and for each other ROM keeps a Tender
note written by the same user whose activity is live. Each session gets a state for this device: `not_in_library` (no
shortcut here), `missing_core` (the host's core is not among this system's options, or its `.so` is not under
RetroDECK's `config/retroarch/cores`), `download`, or `join`.

**Never trust the note.** It was written by another user, and parts of it reach a command a shell evaluates. Every field
is checked with a whole-value pattern (`fullmatch`, never `match` with `$`, which also accepts a trailing newline):
addresses are a dotted quad or a plain hostname, ports 1 to 65535 and never a `bool`, the relay session base64
characters, the password letters and digits. The core is resolved from this device's own options by its `.so` name and
never used as a path. A note that fails any check parses to nothing.

`prepare_join_session` re-reads the note, requires the host's activity to still be live, and probes the host's direct
addresses in order (Tailscale's `100.64.0.0/10` first, then private LAN addresses), 1.5 seconds each, on port 55435 for
RetroArch or 27312 for PPSSPP's ad hoc server. The first that answers is used; otherwise RetroArch falls back to the
relay, and PPSSPP, which has none, answers `host_unreachable`.

## In the panel

- **Host multiplayer** sits in the play row's chevron menu, above Uninstall, shown only when `get_netplay_capability`
  says the game can be hosted. It is read again each time the play button settles on Play.
- **One launch funnel** (`launchFunnel.ts`) serves Play, Host and Join. A netplay launch runs the same pre-launch gate,
  and only once the gate allows it asks the backend for the session, so a cancelled sync leaves nothing to undo. It then
  records the launch, writes the one-off command under the `netplay_launch` lease (handed back on every path), and calls
  `RunGame` with no re-confirm in between, which would put the normal command back. A start whose answer was lost is
  ended anyway, and a command that could not be written ends the session and restores the normal one instead of
  launching.
- **The end:** when the netplay game exits, the session manager calls `end_host_session` or `end_join_session`, puts the
  normal command back through the re-confirm, and clears the record. The post-exit save sync runs as for any game.
- **Reload and crash:** the active launch is kept in `localStorage` (`romm-tender:netplay-launch`). At plugin load a
  recorded launch is taken over and ended if its game is gone; with no record, `end_host_session` clears any session the
  backend still holds.
- **The guest:** a poller started when the plugin loads reads `list_hosted_sessions` every 30 seconds, a running game
  included, and again at once when a download a card waits on completes. A failed or refused poll clears the cards,
  since a stale card could only offer a Join that fails. Each session raises one Steam toast
  (`<host> is hosting <game>`) the first time its key is seen. Main shows a card per session after the status block:
  **Join**, **Download** (then Join once installed), or a line saying the core is missing or the game is not in this
  library. Join on Main is a deliberate exception to one home per action ([qam-panel.md](qam-panel.md)).

## What has not been checked on a device

- Whether RetroDECK's RetroArch or PPSSPP launcher passes an `--appendconfig` of its own; RetroArch keeps the last one.
- PPSSPP may write appended keys into `ppsspp.ini`, so `EnableWlan` and `proAdhocServer` could outlast a session.
- The address PPSSPP's ad hoc server records for a host that connects to its own server.
