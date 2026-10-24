# Working on inter-agent-comm

- The docs live in this repo: README.md (short, for people), AGENTS.md
  (agents using the chat), docs/ (HOST, HOW_IT_WORKS, PUBLIC, GUEST, CODEX,
  DEVELOPMENT), SECURITY.md (reporting, known limits) and CHANGELOG.md.
  Write what you learn here, in the doc it belongs to: design, decisions,
  how to run and test, lessons. Keep the docs in step with the code.
- README.md is for people: starting the server, resuming, bringing agents
  in (what to tell them), guests. No agent commands there; they go in
  AGENTS.md.
- Keep it generic: no personal names, machine names, addresses, accounts
  or company context in code, docs, tests or commit messages. Examples use
  placeholders (`laptop.webshop`, `192.0.2.10`).
- Work on a branch and push it, not `main`: `main` changes only at a
  release, which merges the branch and then deletes it
  (docs/DEVELOPMENT.md, "Versions and releases").
- A visible change to the TUI: run `docs/make_screenshot.py` again, so the
  README's picture shows the TUI as it is.
- Run the tests after every change, then push:

  ```
  ./server --help >/dev/null      # first time: sets up .venv
  .venv/bin/python tests/run.py
  ```

  The Codex bridge checks are timed; under heavy load one may fail. Run
  the suite again before chasing it.
- A change users would notice gets a line under "Unreleased" in
  CHANGELOG.md, in the same commit. A protocol change bumps `PROTOCOL` in
  `proto.py`; releases and versions: docs/DEVELOPMENT.md.
- Anything security-relevant (the handshake, approvals, file transfer, the
  gateway, what agents are shown as trusted) gets a second, independent
  review before it ships. SECURITY.md lists what's in scope.
