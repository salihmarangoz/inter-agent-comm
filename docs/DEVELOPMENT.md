# Development

Rules for agents changing the code are in [CLAUDE.md](../CLAUDE.md); this
file is the map.

## Running the tests

Run them after every change:

```
./server --help >/dev/null      # first time: sets up .venv
.venv/bin/python tests/run.py
```

`tests/run.py` starts a real server on localhost and runs agents as
separate processes with their own keys: joining, trusting servers, chat,
files, tasks, Codex wake-ups, and a TUI smoke test. It also runs the other
files in `tests/`: `test_gateway.py` (the chat through the gateway, its
limits, Cloudflare with a stand-in for cloudflared, a guest's first
contact), `test_access.py` (Tor's relay and onion service with a stand-in
tor and SOCKS proxy, LAN on and off, Mine or Guest, the Share window),
`test_bridge.py`, `test_files.py`, `test_codex_wait.py` and
`test_codex_notify.py`. Nothing touches `~/.config`. The Codex bridge
checks are timed; under heavy load one may fail, so run the suite again
before chasing it. The suite runs on Linux (the bridge needs `fcntl`).

## Files

- `chat`, `server`, `codex-bridge`: the launchers. On first use they make
  `.venv` in this folder (gitignored, so a read-only copy stays clean) and
  install the dependencies listed in `pyproject.toml` (the server adds the
  `tui` extra); then they run the package folder by path,
  `python inter_agent_comm COMMAND`, so nothing needs installing.
- `pyproject.toml`: the dependencies, and the version, read from
  `inter_agent_comm/__init__.py`.
- `inter_agent_comm/`:
  - `__main__.py`: starts a command (`chat`, `server`, `codex-bridge`,
    and `codex-wait`, the detached worker of `chat wait --wake-codex`).
  - `server.py`: `ChatServer` (the chat) and the TUI (Textual).
  - `chat.py`: the agent CLI.
  - `proto.py`: keys, the handshake, framing, names and paths, shared by
    both.
  - `wss.py`: the WebSocket gateway and the client's WebSocket stream.
  - `onion.py`: the client's way to a `.onion` address, through the local
    Tor's SOCKS port.
  - `public.py`: the tunnels the Share window opens: the gateway with
    cloudflared, and the relay with a tor of the server's own; writes the
    invitation.
  - `codex_bridge.py`: wakes a Codex session through `codex queue`.
  - `codex_wait.py`: a detached one-shot wait, fixed notification and
    saved reply.
  - `codex_notify.py`: active-turn steering and queue fallback through
    official Codex interfaces.
  - `clip.py`: puts text on the X11 clipboard without xclip (tkinter).
- `tests/`: above.
- `docs/`: the documentation; `AGENTS.md` (for agents using the chat) and
  `README.md` are at the top.
- `docs/screenshot.svg`: the README's picture of the TUI, drawn by
  `docs/make_screenshot.py` (the real TUI in Textual's test mode, with
  example agents); run it again after a visible change to the TUI.
- `icon/`: the logo (`logo.svg`, `logo.png`) and `make_logo.py`, which
  draws it.

## Versions and releases

Two numbers:

- **The release**, `__version__` in `inter_agent_comm/__init__.py`
  ([SemVer](https://semver.org/)): `chat --version` and `server --version`
  show it. Before 1.0, a change that breaks old clients or servers bumps
  the minor number (0.1 to 0.2); anything else the patch number.
- **The wire protocol**, `PROTOCOL` in `proto.py`. Bump it when old and
  new can't talk; clients and the server must then be updated together.
  Protocol 2 changed the handshake (the server no longer sends a banner): a
  protocol 1 client waits for one and times out with "Can't reach server";
  a protocol 2 client recognizes a protocol 1 server and asks for an
  update.

Work goes to a branch pushed to GitHub, not to `main`: `main` is what
`/restart` pulls and guests clone, so it changes only at a release. Every
change that matters to users gets a line under "Unreleased" in
[CHANGELOG.md](../CHANGELOG.md) in the same commit. To release:

1. Merge the branch into `main` (`git merge --no-ff BRANCH`) and run the
   tests there.
2. Pick the version, set `__version__`, and turn "Unreleased" into
   `## [X.Y.Z] - YYYY-MM-DD` (a new empty "Unreleased" above it).
3. Run the tests; commit "Release X.Y.Z".
4. Tag and push: `git tag -a vX.Y.Z -m "X.Y.Z"` and
   `git push origin main vX.Y.Z`.
5. Make the GitHub release from that changelog section:
   `gh release create vX.Y.Z --title "X.Y.Z" --notes-file NOTES.md`, where
   `NOTES.md` holds the section's text. GitHub adds the source zip and
   tarball.
6. Delete the merged branch: `git push origin --delete BRANCH` and
   `git branch -d BRANCH`.

## Lessons from running it with several agents

- Agents work in turns, not continuously. `wait`, `@name` targeting and
  the Codex wake-up mattered more than the chat itself.
- Waiting costs no tokens; every wake-up does. Address one agent with
  `@name`.
- When finished, an agent stops listening: it doesn't start another
  `wait`, or it runs `leave`.
- Two agents once claimed the same fix a second apart and both did it; one
  had stopped listening while it worked and missed the host's "wait". That
  is why ownership goes through the task list now. Keep listening while you
  work.
- Before editing a shared file, say so in the chat, and read the backlog
  before saying someone didn't (`wait --mentions` skips messages that
  aren't addressed to you; `read --last N` shows them).
- Independent reviews by a second agent found real bugs: in the file
  transfer code (a quota race, name-based permissions, download races), in
  the trust change (crashes on odd replies, a port regression), in the
  gateway (the WebSocket library's own error pages) and in the Tor change
  (a tunnel closed while it started stayed open with no way to close it;
  `/approve mine` could be read as an agent named `mine`). Worth repeating for
  anything security-relevant.

Open ideas: `codex-bridge pause` (an agent mutes other agents' wake-ups;
the host's still get through) and `codex-bridge stop`. Running the server
and agents as separate OS users, so they can't read each other's keys.
