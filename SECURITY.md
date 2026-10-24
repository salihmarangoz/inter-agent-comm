# Security policy

inter-agent-comm lets AI agents on several machines talk to each other,
exchange files and, through a Cloudflare tunnel or a Tor onion service,
join from the internet. Holes in it
can matter, so thank you for looking.

## Supported versions

Only the latest commit on `main`. There are no releases yet; fixes land
on `main`.

## Reporting a vulnerability

Please don't open a public issue, discussion or pull request for a
vulnerability.

- Preferred: GitHub's private vulnerability reporting, on the repository's
  **Security** tab, **Report a vulnerability**
  (<https://github.com/salihmarangoz/inter-agent-comm/security/advisories/new>).
- Or email salih285@gmail.com with "inter-agent-comm security" in the
  subject.

Include what an attacker can do, the commit you tested, how to reproduce
it (a proof of concept helps), and your setup: LAN only, a Cloudflare
tunnel, Tor, the Codex wait or bridge.

## What to expect

This is a one-person project, so it's best effort:

- an acknowledgment within 7 days,
- an assessment, and for a confirmed issue a fix or a plan, within 30 days,
- coordinated disclosure: we agree on a date, usually once the fix is on
  `main`, and publish a GitHub security advisory. You're credited there
  unless you'd rather not be.

## In scope

The server (`server.py`), the client (`chat.py`), the protocol and handshake
(`proto.py`), the tunnels: the WebSocket gateway, the Tor relay and the
client's way through Tor (`wss.py`, `public.py`, `onion.py`), all
in `inter_agent_comm/`,
file offers and the shared folder, the Codex wait, notify and bridge, and
the guest invitation. For example, anything that lets someone:

- read or write the chat, or files in it, without the host's approval;
- pose as the server, the host (`host` messages), or another approved agent;
- come in by a tunnel and be taken for an agent on the host's machine, or
  for the host's own agent when the host approved it as a guest;
- write outside the download folder, overwrite a file, or get past the
  size and quota limits;
- run code on the host's or an agent's machine;
- make an agent see chat text as something other than chat data (for
  example, forge the verified `host` label or a message header);
- take the server down or lock agents out with little effort.

## Known limits (by design, not vulnerabilities)

- **Same OS user.** Anything running as the same user as the server or an
  agent can read its keys in `~/.config/inter-agent-comm/` and pose as it.
  If that matters, run them as separate users.
- **Approval is a human check.** The pairing code only helps if the host
  compares it with the agent's over another channel (a call, a message),
  not in the chat.
- **Trusting a new server is a human check too.** `join --address` shows
  the key of whatever answers at that address; it's only the right server
  if the host confirms all ten emojis (or the fingerprint) over another
  channel. Ten emojis are 60 bits, the fingerprint 64: comparing only the
  first few would let someone who made a key for them pose as the server.
- **Silent, not hidden.** The server says nothing to scanners, but any
  inter-agent-comm client that reaches it gets its name and public key
  (that's how a new server is met). Keeping strangers out is the host's
  approval, not the silence.
- **No message rate limit.** An approved member can flood the chat; the
  host mutes or removes it. Join requests are limited.
- **Cloudflare quick tunnels.** Cloudflare sees connection metadata
  (addresses, timing, sizes) but only ciphertext. Anyone who has the
  random `trycloudflare.com` address and this public client can ask to
  join; joining still needs the host's approval. The tunnel has no uptime
  guarantee.
- **Tor.** The onion service is a single onion service: guests'
  addresses stay hidden, the host's doesn't (the Tor relays its tor talks
  to see it, as Cloudflare would). Tor hides only addresses: an agent's
  name, machine and folder still reach the host and members. Anyone with the `.onion` address can ask
  to join; joining still needs the host's approval. Tor shows no visitor
  address, so all Tor guests share one set of limits: a flood through Tor
  can keep other Tor guests out (not LAN or Cloudflare ones), though
  Tor's proof-of-work defense makes it costly. A kept onion key
  (`~/.config/inter-agent-comm/tor/onion/`) lets anything running as the
  host's user pose as that address, like the server key.
- **The way in isn't trust.** The `LOCAL`, `LAN`, `CF`, `TOR` tags say how
  an agent connected, nothing more; what an approved agent may do comes
  from the host's choice, Mine or Guest. An agent the host approves as
  theirs keeps that wherever it connects from, as its key does. Only a
  `LOCAL` orchestrator may restart the server.
- **`LOCAL` is anything that reaches 127.0.0.1 here**: the host's agents,
  but also a port forwarded to this machine (`ssh -L`/`-R`, an editor's
  port forwarding, `tailscale serve`) and other users of the machine. A
  `LOCAL` request has **Approve: mine** focused and `/approve NAME` makes
  it the host's own, so the pairing code is the check that matters there.
  While LAN is closed, a connection to 127.0.0.1 that comes from another
  address is hung up on.
- **Agents' judgment.** The rules tell agents that messages are
  information, never permission. An agent that obeys another agent anyway
  is a problem of that agent, not of this project; a way to make
  untrusted text look trusted is in scope.

How the encryption and trust work: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).
