---
status: accepted
decided: 2026-10-25
updated: 2026-10-25
---

# Multiplayer runs on each emulator's own netplay, and players find a hosted game through RomM

Recorded for [#1](https://github.com/ltsjoe/romm-tender/issues/1), whose `## Decisions` D1 to D5, D7 to D10, D13, D17,
D19, D20, D22, D24 and D25 this ADR carries. The issue holds the research behind it and the smaller decisions.

## Context

Tender launches every game for one player. The wish is for a player to pick **Host multiplayer** on a game in Tender,
and for a second player, who runs Tender against the **same RomM server**, to find that game in their own Tender in
Steam and join it.

There are two families of answer. **Steam Remote Play Together** streams the host's screen to the guest, so the guest
needs no game at all. **An emulator's own netplay** runs the game on both machines and keeps them in step, so both need
the emulator, the core and the ROM, which two Tender installs on one RomM server already have.

Only two of the emulators RetroDECK ships can be put into a session from the command line or a config file, with no
press inside the emulator: **RetroArch**, for a core whose info file marks `savestate_features = "deterministic"`
(RetroArch refuses netplay for any other), and **PPSSPP**, whose ad hoc mode takes its network keys from
`--appendconfig`. RetroDECK's default PS1 core, SwanStation, is not marked deterministic; PCSX ReARMed and Beetle PSX
are. DuckStation, PCSX2, RPCS3 and Vita3K have no session Tender can start, and Dolphin, Azahar and melonDS start one
only from their own dialogs.

Two Tender installs have no channel of their own. Every way for one to tell the other "a game is hosted here" goes
through a place both can reach: the RomM server, libretro's public lobby, or Steam chat.

## Decision

1. **Multiplayer is each emulator's own netplay**, for RetroArch on a deterministic core and for PPSSPP standalone, and
   for nothing else. A PS1 game whose core refuses netplay is launched on PCSX ReARMed for that session, on both sides;
   the game's chosen core is not changed.
2. **RomM is where a guest finds a hosted game.** While a session runs, the host's Tender sends RomM activity heartbeats
   for the ROM (`POST /api/activity/heartbeat`, RomM 5.0.0 and later) and keeps a public note on it that carries what
   the guest needs: the emulator and core, the host's addresses, the relay session, the password. The note opens with a
   Tender marker and a format version. A guest reads the activity feed (`GET /api/activity`) and trusts a note only
   while its host's activity entry is live; an entry expires 90 seconds after the last heartbeat, so a host that crashed
   stops being offered within that time. The host's Tender deletes the note when the session ends, and a note left by a
   crash the next time it starts.
3. **The guest joins from a card on Main.** A Steam toast announces a session once, and Main shows a card per hosted
   game that carries **Join** (or **Download** first, for a game not on the device). Join goes through the same launch
   code as the play row. This is a deliberate exception to "One home per action" in
   [qam-panel.md](../architecture/qam-panel.md#one-home-per-action), taken for fewer presses.
4. **A RetroArch host always uses libretro's relay, and takes direct connections as well.** The note carries the host's
   direct addresses and the relay session; the guest tries a direct address first, a Tailscale one before any other, and
   falls back to the relay. Every RetroArch session carries a password made fresh for it, because the relay lists the
   game on libretro's public lobby.
5. **A RetroArch guest never reads or writes its own save file during a session.** On connecting, RetroArch copies the
   host's save RAM over the guest's (`network/netplay/netplay_frontend.c`), and would otherwise write it into the
   guest's save file, which Tender would then upload over the guest's save in RomM. The guest's launch carries
   `--sram-mode=noload-nosave`. A PPSSPP ad hoc session is different: each player runs their own game on their own save,
   so both sides save and sync as in any other game.
6. **A netplay launch is a launch path of its own.** It writes the one-off command, launches, and puts the normal
   command back when the game exits. The one-off command is never recorded in `applied_launch_options`, so the six
   recorded-state writer sites stay six.

## On Tender 0.33.0

The plan was first written against upstream's later, standalone Tender (ADR 0044 on the fork's `main`). This branch
builds it on 0.33.0, the last release that runs as a Decky Loader plugin (#1 D19), which changes four things:

- **RomM 5.0.0** is required for Host and Join, the release that added the activity feed; 0.33.0's own floor stays 4.9.0
  (D20).
- **A RomM device is registered for a session** when none is, which in 0.33.0 happens only while save sync is off, and
  save sync stays off (D22). Registering one also starts the playtime read that every registered device gets.
- **A PS1 host session on PCSX ReARMed saves where the game's own core does**: the appended config points
  `savefile_directory` at that folder, so RetroArch's sort-saves-by-core setting cannot move the memory card (D24).
- **The launch funnel moves into `src/utils/`** so the play button, Host and the Main card's Join share it (D25).

Two rules came out of building it. Every appended RetroArch config sets `config_save_on_exit = "false"`, because
RetroArch otherwise writes appended settings into `retroarch.cfg` when it exits and the relay, the public announce and
the save folder would stick for every later game. And every note field is checked with `fullmatch`: a pattern anchored
with `$` and run with `match` also accepts a value ending in a newline, which a shell reads as the start of a new
command.

## Considered options

- **Steam Remote Play Together.** Its one advantage is a guest who needs nothing installed, which this does not need.
  Nothing shows that Steam offers it for a Non-Steam shortcut, the one known workaround (RemotePlayWhatever) has given
  guests no input since about April 2026, and Linux has open bugs in it for audio, controllers and Wayland.
- **RomM's own netplay rooms** (`/api/netplay/list`, the `/netplay` socket). They belong to RomM's in-browser EmulatorJS
  player, a room lives only as long as the socket that opened it, and RomM 5.4.0 lets only a browser login open one, so
  Tender's client token would lose them at the next RomM upgrade.
- **The address in the activity entry's device label.** It keeps the note off RomM's website, but it bends a field to a
  use it was not made for, and the label is too short for a relay session and a password.
- **libretro's lobby alone.** It needs nothing from RomM, but it is visible to the whole world, it lists RetroArch hosts
  only, and a guest could match a host only by a nickname anyone can choose.
- **Copy and paste through Steam chat.** Tender can write the clipboard and the Deck's keyboard can paste, but reading
  text back into the guest's Tender is unproven, and two players on one RomM server need no manual step.
- **Join on the game page's play row,** with the Main card only opening the game. It keeps one home per action, at the
  cost of a press, and was not chosen.
- **A Host that only opens Dolphin, Azahar or melonDS** and leaves the session to their own dialogs. It looks like the
  same feature and is not one.

## Consequences

- While a session runs, its note shows on the game's page on RomM's website to every user who can see the ROM, password
  included. The password keeps strangers on libretro's lobby out, not other users of the same server.
- A RetroArch session that cannot connect directly depends on libretro's lobby and relay. The lobby answers over plain
  HTTP only, and RetroArch cannot be pointed at a private one.
- Every Tender reads RomM's activity feed every 30 seconds while Steam runs.
- PS2, PS3, Vita, GameCube, Wii, 3DS and DS games get no multiplayer through Tender, and a PS1 game is played on another
  core during a session than the one chosen for it.
- RetroArch refuses a session whose core names differ and only warns when their versions differ, so two RetroDECK
  versions can join a session that later falls out of step.
