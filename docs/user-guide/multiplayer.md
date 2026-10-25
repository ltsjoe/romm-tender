# Multiplayer

Two players who both run Tender, signed in to the **same RomM server**, can play a game together over the network. One
player hosts the game; the other sees it in their own Tender and joins with one press.

Each player runs the game on their own device, with the emulator's own netplay keeping the two in step. That is why both
need the game, and why it feels like playing locally rather than watching a stream.

## What you need

- Tender on both devices, connected to the same RomM server.
- RomM **5.0.0 or newer** on that server. Tender finds hosted games through RomM's activity feed, which 5.0.0 added.
- The game in both players' libraries. The guest can download it from the session card if it isn't on their device yet.
- A way for the two devices to reach each other (see [How the players connect](#how-the-players-connect)).

## Which games

| System                                | Emulator                     | How it plays                                      |
| ------------------------------------- | ---------------------------- | ------------------------------------------------- |
| NES, SNES, Genesis, arcade and others | RetroArch, on a netplay core | Both players control the same game                |
| PlayStation (PS1)                     | RetroArch (see below)        | Both players control the same game                |
| PSP                                   | PPSSPP                       | The game's own wireless multiplayer (ad hoc mode) |

**Host multiplayer** only appears for games that can do it. A RetroArch core has to support netplay; when one doesn't,
the menu item is missing for that game.

**PS1 games** usually run on SwanStation, which RetroArch can't use for netplay. For a multiplayer session Tender runs
the game on PCSX ReARMed instead, on both devices, and points it at the same memory card the game normally uses. Your
chosen core doesn't change. A PS1 game set to the DuckStation emulator can't be hosted.

**Not supported:** PS2, PS3, Vita, GameCube, Wii, 3DS and DS games. Their emulators either have no netplay, or can only
start it from their own menus, which Tender can't drive.

## Hosting a game

1. Open the game's page and press the arrow beside **Play**.
2. Choose **Host multiplayer**.

Tender starts the game ready for a second player and tells your RomM server you're hosting. While the game runs, other
players on your server see it. When you quit the game, Tender takes the notice down and puts the game's normal launch
back.

While you host, a note titled **Tender multiplayer session** appears on the game's page on your RomM website. It holds
what the other player's Tender needs to connect, including a password made fresh for each session. Every user of your
RomM server who can see the game can see the note, and it's removed when the session ends.

## Joining a game

When someone on your server starts hosting, Tender shows a Steam notification, and a card for the session appears on the
**Main** page of Tender's Quick Access panel:

- **Join** starts the game connected to the host.
- **Download** appears when the game isn't on your device yet. Once it's downloaded, the card offers Join.
- "RetroDECK on this device does not have the emulator core this session runs on" means you can't join until RetroDECK
  has that core. Updating RetroDECK so both devices match usually fixes it.
- "This game is not in Tender's library on this device" means the game's platform isn't synced here. Turn the platform
  on in Tender's Library and sync.

Tender checks for hosted games every 30 seconds, also while you're playing something else.

## How the players connect

When you join, Tender tries the host's addresses in this order and uses the first one that answers:

1. **Tailscale.** If both devices are on the same Tailscale network, this works from anywhere, with no router setup.
2. **The local network,** when both devices are at the same place.
3. **libretro's public relay,** for RetroArch games only. It gets past home routers without any setup. A relayed game is
   listed on libretro's public lobby, where anyone can see the game name and the host's RomM username; the session's
   password keeps everyone else out.

A PSP game has no relay, so the guest has to reach the host over Tailscale or the local network.

## Your saves during multiplayer

- **RetroArch games:** the guest plays on the **host's** save. Tender starts the guest's game so it never reads or
  writes the guest's own save file, so a multiplayer session can't overwrite the guest's progress. The host's save is
  saved and synced as usual.
- **PSP games:** each player plays on their own save, which saves and syncs as usual.

## If something goes wrong

| Message                                                       | What to do                                                                                                                              |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Multiplayer needs RomM 5.0.0 or newer on the server           | Update the RomM server.                                                                                                                 |
| Your RomM sign-in does not allow multiplayer                  | Sign in to RomM again in Tender's settings with your username and password, which grants every permission.                              |
| This device has no network address another player could reach | Connect the device to a network, or to Tailscale.                                                                                       |
| Tender could not reach the host of this session               | For a PSP game, both devices need Tailscale or the same local network. For RetroArch, try again; the relay can take a minute to appear. |
| RetroDECK on this device does not have the emulator core      | Update RetroDECK so both devices have the same cores.                                                                                   |
| A multiplayer session is already running                      | Quit the game you're hosting before starting another.                                                                                   |

If the game falls out of step during play, check that both devices run the same RetroDECK version: RetroArch joins
players on different core versions, but they can drift apart.
