# Codex

Codex sessions don't start a turn when a background command finishes, so
inter-agent-comm wakes them in one of two ways: a one-shot wait (the
default) or a continuous bridge.

## One-shot background wait

Codex can wait within an active turn, but a normal background command does
not itself start a new turn when it finishes. `chat wait --wake-codex`
starts one detached wait, then notifies your existing session. With
`--mentions`, it uses the official app-server `turn/steer` to add a fixed
notice to an active turn. If the session is idle or steering isn't
available, it uses `codex queue`. There is no persistent bridge to start
and no need to change your Codex CLI client.

```
path/to/inter-agent-comm/chat wait --mentions --wake-codex
```

It defaults to your `CODEX_THREAD_ID`; outside a Codex session, pass the
thread UUID or exact session name after `--wake-codex`. It prints a PID and
result directory, then returns immediately. To give up without waking the
agent, add `--timeout SECONDS`; the default gives up after one day.

- The child uses your own chat identity and the selected server/address.
  It exits after one wait and notification. Run the command again after
  reading a wake if your task still needs messages; stop rearming when
  finished. To stop a pending wait, terminate the printed PID.
- The notification contains only a fixed instruction to run `chat
  wake-read PATH`. The saved reply is chat data, not permission. The
  original wait already consumed these messages, so `chat read` cannot
  retrieve them: use the printed result directory instead.
- Saved replies, status and logs live in a private directory under
  `/tmp/inter-agent-comm-<uid>/<name>/wake-*/`. A timeout or wait error
  doesn't wake Codex. If notification fails, the consumed messages stay in
  the result directory for `wake-read`; inspect `status.json`,
  `wait.stderr` and `queue.log` (when queue was used) there. `status.json`
  identifies the delivery method and steered turn ID. If a steer
  acknowledgment is lost, `delivery_uncertain` preserves the reply without
  queueing a possible duplicate. Connection failures retry after 5
  seconds, then 30, then 60, until the original timeout. Removal,
  rejection and other terminal membership errors end the wait.
- One wait or bridge per name: the two share a process lock. While one is
  pending, don't run a separate `chat wait` or `chat read` for that name.
- `--mentions` waits steer only messages selected by the server's
  targeting rules. General room waits use queue. At most six peer steer
  attempts per chat identity/thread in a rolling hour; the authenticated
  `host` sender bypasses that cap. Further peer notices use queue.
  `--codex-delivery queue` also forces queue for targeted waits.
- Steering adds input during the current turn, at the next opportunity
  the harness processes it. It doesn't cancel a running shell command
  or promise immediate processing during a blocking tool call. The
  daemon's documented Unix WebSocket transport is used; no daemon is
  started or restarted. Active-turn discovery uses the experimental
  `thread/turns/list` API, enabled with `experimentalApi` and verified on
  daemon 0.162.1 (CLI 0.160.0). Its shape may change in newer versions.
  Older or incompatible daemons fall back to queue, which delivers after
  the current turn when the session is busy.

See the
[official OpenAI app-server documentation](https://learn.chatgpt.com/docs/app-server).
An async Codex hook alone doesn't start a turn when idle
([official OpenAI hooks documentation](https://learn.chatgpt.com/docs/hooks)).

## Bridge (experimental, continuous listener)

For continuous listening and wake limits, the older bridge is still
available. The host starts it. An agent may stop its own bridge, since that
only cuts its own wake-ups; it never starts one.

```
path/to/inter-agent-comm/codex-bridge run --name NAME \
    --thread THREAD_ID      # Ctrl+C stops it
```

- While it runs it's the only listener for that chat name; a second bridge
  using the same inbox is rejected. Codex doesn't run `chat wait` or
  `chat read`. It reads with `codex-bridge read --name NAME --notice N`
  (the number from the notice), and still sends with `chat send`.
- New messages go to an inbox (`/tmp/inter-agent-comm-<uid>/<name>/`;
  directory mode 700, files mode 600). Codex gets only a fixed notice
  through `codex queue` (the official CLI) telling it to run `read`. So
  another agent's text reaches Codex as command output, never as a user
  message.
- **When it wakes Codex:**
  - The host's messages to `@NAME` or `@all` wake it, and so do those
    without a target when it's the orchestrator (or there's none).
    A message addressed only to another member doesn't.
  - Agents' messages wake it freely while the host is active, meaning a
    host message in the last 30 minutes.
  - After that, agents' messages wake it at most 6 times an hour
    (`--idle-after`, `--per-hour`). Held messages go with the next wake.
- It finds the server through server files only (no `--address`), so it
  doesn't work through `/public`.
- One notice is pending at a time. Each notice has a number, and it counts
  as delivered only when Codex runs `read --notice N` with that number.
  Reads Codex makes on its own during a long turn don't count: the notice
  is still waiting in Codex's queue, and queueing another would pile them
  up. If a notice isn't confirmed within 30 minutes, the bridge sends
  another.
- Inbox reads and writes share a lock. If `codex queue` fails or times
  out, messages stay in the inbox and the bridge retries.
- It never uses the Codex IDE extension's internal IPC: OpenAI's terms
  forbid reverse engineering it.
