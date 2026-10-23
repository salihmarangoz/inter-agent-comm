# Changelog

What changed in each release of inter-agent-comm. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/) (before 1.0, a minor
version may break old clients or servers).

## [Unreleased]

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

[Unreleased]: https://github.com/salihmarangoz/inter-agent-comm/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/salihmarangoz/inter-agent-comm/releases/tag/v0.1.0
