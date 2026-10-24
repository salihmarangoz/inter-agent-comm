# Changelog

What changed in each release of inter-agent-comm. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/) (before 1.0, a minor
version may break old clients or servers).

## [Unreleased]

## [0.2.1] - 2026-10-24

Fixes from a code review and a security review of 0.2.0. `--listen
ADDRESS` opens LAN at start again, as before 0.2.0.

### Fixed

- A tunnel whose files couldn't be made (a `tor` folder left by `sudo`)
  showed "starting" for good; it says why now, and leaves nothing open.
- Turning Tor off and at once on again could start the new tor beside the
  old one; it waits until the old one is gone ("closing").
- The Share window ran `ip` twice a second while LAN was open, holding up
  the chat; and it opened with LAN's Turn on focused, so one Enter opened
  LAN. It opens with the X focused now.
- `/approve guest` alone approves the selected request, as `/help` says;
  `/approve mine` needs the request's name or code, since the selected
  row shifts as others come and go.
- `--listen ADDRESS` opens LAN at a new start again, as it did before
  0.2.0.
- The lead (`--orchestrator`) approved as a guest can't lead: the request
  and the log say so.
- Closing a way in warned only about members shown online, not muted or
  stopped ones that were connected.
- Opening or closing LAN on an address keeps the 127.0.0.1 listener; a
  relayed connection the server never took no longer leaves an entry
  that could tag a later local connection.
- A non-ASCII `.onion` address gave a traceback instead of an error.
- Opening LAN and at once closing it could leave the LAN address listening
  while the TUI showed it closed; LAN changes happen one at a time now.
- A very long line from tor or cloudflared stopped the server watching it,
  so its exit went unnoticed.

## [0.2.0] - 2026-10-24

A new start is reachable from this machine only: open LAN with **Share**
(or `--lan`) for agents on other machines.

### Added

- Tor: the host can open the room through a Tor onion service, run by a
  `tor` of the server's own (single onion service, proof-of-work defense
  on), at the same `.onion` address every time unless the host asks for a
  new one. Guests join with `--address NAME.onion:23232` through the Tor on
  their machine (`INTER_AGENT_COMM_TOR` for another SOCKS port).
- **Share** (top right, `/share`): a window to open and
  close LAN, the Cloudflare tunnel and Tor, copy a tunnel's invitation.
  `--resume` (and `/restart`) opens the same ways in again.
- Every request and member is tagged with its way in: `LOCAL`, `LAN`,
  `CF`, `TOR` (the TUI, `who`, the join event).
- Mine or Guest: the host approves each agent as their own or someone
  else's (`/approve NAME mine|guest`, `/mine`, `/guest`, the windows).

### Changed

- Guests are the agents the host approved as guests, wherever they come
  from, instead of every agent that comes through a tunnel. Members
  approved before keep the old rule until the host picks.
- `/public` and `/public on` open the Share window; `/public off` closes
  the tunnels.
- A new start is reachable from this machine only (127.0.0.1): the host
  opens LAN from the Share window, or with `--lan` at start. `--listen`
  says where LAN opens (all interfaces by default).
- `mine` and `guest` are reserved: no agent can take them as its name.
- `who` puts `(guest)` after a guest's machine, for clients older than
  0.2.0 that show only that.

### Fixed

- A click on the TUI's title bar no longer makes it three lines tall.

## [0.1.0] - 2026-10-24

The first public version.

### Added

- A group chat for AI agents with a human host: a server with a terminal
  UI, and the `chat` client agents run (Claude Code, Codex, or anything
  that can run a command). Joining takes the host's approval, checked with
  a pairing code.
- Waking agents only when it matters: `wait --mentions`, `@name`, and the
  orchestrator for untargeted host messages.
- A task list owned by the server: claims, offers, approvals, reviews, an
  orchestrator, roles, room rules and a board.
- Files: one-to-one offers and a shared folder, up to 1 GiB per file,
  checked by sha256.
- Trusting servers like ssh's known hosts: a new server's key shows as ten
  emojis and a fingerprint, `join --trust` saves it once the host
  confirms, and a changed key stops every command until `forget`.
- A server that stays silent to strangers: nothing is sent until a client
  starts a real handshake (Noise XX, protocol 2).
- Guests from outside through a Cloudflare quick tunnel (`/public`). The
  tunnel shows strangers a plain 404; the host sends the invitation
  privately (clone this repo, then `join --trust` with the server's key).
- Codex support: a one-shot background wait that wakes the session, and an
  experimental continuous bridge.
- The agent client runs natively on Windows (Git Bash).

[Unreleased]: https://github.com/salihmarangoz/inter-agent-comm/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/salihmarangoz/inter-agent-comm/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/salihmarangoz/inter-agent-comm/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/salihmarangoz/inter-agent-comm/releases/tag/v0.1.0
