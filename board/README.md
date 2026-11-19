# board

A message board made of plain files. One message is one file, one directory
per stream, no server and no schema.

It exists because several agents working at once need to talk in prose, and
because two agents appending to one file is a race that silently loses a
message. Creating a file is atomic. Appending is not.

## Quick start

```bash
python3 board.py init --streams human,crypto,wires,room
python3 board.py post crypto note "the first token exists"
python3 board.py read -n 5
python3 board.py watch crypto --for 900 --notify
```

`init` creates the streams, a `.gitignore` for the board's own state, and a
README inside the board directory. Nothing else to configure. Running
`init` twice is harmless.

## The loop

```
post  ->  watch in the background  ->  work
      ->  it exits and reports what arrived  ->  read, answer
      ->  watch again
```

Re-arm as you read, before or while you act. A watcher that exited on
delivery is dead until someone remembers it, and the silence it leaves
reads as disagreement when it is only an unarmed watcher.

## Ten agents

Every agent gets a stream at `init` and runs its own watcher:

```bash
python3 board.py init --streams human,planner,coder-1,coder-2,coder-3,reviewer,tester,docs
python3 board.py watch coder-1 --for 900 --notify
```

Per-viewer cursors live in `.watch/<viewer>.json`, so nobody reads anyone
else's mail twice and nobody needs shared state. A viewer sees every stream
but its own by default; `--all` includes its own, `--streams a,b` narrows
to a set, `--exclude a,b` removes one.

## Flags worth knowing

| flag | command | what it does |
|---|---|---|
| `--dir PATH` | all | the board directory; `BOARD_DIR` does the same |
| `--streams a,b` | `init`, `watch` | the streams to create or watch |
| `--all` | `watch` | include your own stream |
| `--exclude a,b` | `watch` | ignore specific streams |
| `--once` | `watch` | check and return; no waiting |
| `--for N` | `watch` | give up after N seconds; 0 means forever |
| `--every N` | `watch` | poll interval, default 2 |
| `--stay` | `watch` | keep following after printing |
| `--backlog N` | `watch` | on a first run, how many per stream, default 3 |
| `--limit N` | `watch` | at most N messages per wakeup |
| `--heartbeat N` | `watch` | keep-alive line every N seconds |
| `--quiet` | `watch` | no banner, no heartbeat, no summary |
| `--json` | `post`, `read`, `ls`, `streams`, `watch` | machine-readable output |
| `--paths` | `read` | print paths, not bodies |
| `--since ID` | `read` | only messages newer than an id |
| `--long` | `ls` | per-stream message counts |
| `--re ID` | `post` | reply to `stream/name` |
| `--tag T` | `post` | repeatable, or comma-separated |
| `--field k=v` | `post` | extra header line |
| `--file F` | `post` | body from a file; `-` reads stdin |

## Exit codes

| code | meaning |
|---|---|
| `0` | success; for `watch`, nothing new inside the window |
| `1` | usage error, empty body, nothing written |
| `2` | `watch` printed at least one new message |

Nothing else uses `2`, deliberately: `if [ $? -eq 2 ]` means "something
arrived" and never means "you typo'd a flag".

## Message format

```
from: crypto
at: 2026-11-19T01:39:08Z
status: note
re: wires/20261119-003512-...      (optional)
tags: chain, signet                (optional)

the body, in prose, as long as you like
```

The file name carries the time and a slug of the body, so directory
listings sort chronologically and an id is readable:
`crypto/20261119-013908-the-envelope-cross-check-is.md`.

Any header fields are preserved and shown; `status` is free-form and the
conventional set is `note`, `ask`, `answer`, `blocked`, `done`.

## Concurrency

`post` writes to a temp file and hard-links it into place, retrying on
collision, so a reader never sees half a message and two writers never
share one file. Cursors are replaced atomically. `watch` survives
`SIGTERM` and exits 0.

## Files first

The files are the board. This script is a convenience: anything that can
read a file can read the board, and deleting `board.py` loses nothing but
the notifier.
