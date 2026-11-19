#!/usr/bin/env python3
"""board - a message board made of plain files.

One message is one file, in the stream of whoever wrote it:

    board/crypto/20261118-143204-does-the-seam-need-a-txid.md

A stream is a directory. There are no roles to register, no schema, no
server: any name that is a valid directory name is a stream, and any number
of agents can have one.

Why one file per message rather than one appended log: two writers, one
file, one moment, and the last one wins. Creating a file is atomic;
appending is not. This board never loses a message because two agents
posted in the same second.

Quick start, in the project that wants a board:

    python3 board.py init --streams human,crypto,wires,room
    python3 board.py post crypto ask "does the seam need a txid?"
    python3 board.py post wires note -            # body from stdin
    python3 board.py read                          # newest from every stream
    python3 board.py read wires -n 10
    python3 board.py ls
    python3 board.py watch crypto --for 900        # background notifier
    python3 board.py watch crypto --stay           # follow until interrupted
    python3 board.py watch crypto --streams wires,room --notify
    python3 board.py watch crypto --once --json    # machine-readable check

`watch` is the notifier. Start it in the background; it prints what arrived
since the last time this viewer ran, then waits. Exit code 2 means it
printed something, 0 means it timed out with nothing new, 1 means you typed
the command wrong.

Ten agents, no config: give each one a stream at `init`, and let each run
`watch <own-name> --for N` in the background. Filtering, backlog and cursor
state are per viewer, so nobody reads anyone else's mail twice.

Environment:

    BOARD_DIR   where the board lives (default: ./board next to this script)

The files are the board. This script is a convenience: anything that can
read a file can read the board, and nothing breaks if this is deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATUSES = ("note", "ask", "answer", "blocked", "done")
RESERVED = {"README.md", "RULES.md", ".gitignore"}
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


# --- locations ---------------------------------------------------------


def board_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("BOARD_DIR")
    if env:
        return Path(env).expanduser()
    cwd = Path.cwd()
    # Run from a project root and the board is ./board. Run from inside the
    # board folder itself (board.py next to the streams) and the folder is
    # the board. No guessing beyond those two cases.
    if (cwd / "board").is_dir() or not (cwd / "board.py").exists():
        return cwd / "board"
    return cwd


def check_name(kind: str, value: str) -> str:
    if not NAME.match(value):
        raise SystemExit(
            f"board: {kind} must be a short lowercase name "
            f"(letters, digits, -, _; max 32): {value!r}"
        )
    return value


def streams(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


# --- writing -----------------------------------------------------------


def slug(text: str, limit: int = 48, words: int = 7) -> str:
    out = ""
    for word in re.findall(r"[a-z0-9]+", text.lower())[:words]:
        candidate = f"{out}-{word}" if out else word
        if len(candidate) > limit:
            break
        out = candidate
    return out or "message"


def post(
    root: Path,
    stream: str,
    status: str,
    body: str,
    reply_to: str | None,
    tags: list[str],
    extra: dict[str, str],
    to: list[str],
    not_to: list[str],
) -> Path:
    now = datetime.now(timezone.utc)
    target = root / stream
    target.mkdir(parents=True, exist_ok=True)

    stem = f"{now:%Y%m%d-%H%M%S}-{slug(body)}"
    header = [f"from: {stream}", f"at: {now:%Y-%m-%dT%H:%M:%SZ}", f"status: {status}"]
    header.append(f"to: {', '.join(to) if to else 'all'}")
    if not_to:
        header.append(f"not: {', '.join(not_to)}")
    if reply_to:
        header.append(f"re: {reply_to}")
    if tags:
        header.append(f"tags: {', '.join(tags)}")
    for key, value in extra.items():
        header.append(f"{key}: {value}")
    text = "\n".join(header) + "\n\n" + body.strip() + "\n"

    # Write to a temp name, then hard-link it into place. A link either
    # exists or fails, so a reader never sees half a message and two writers
    # never share one file.
    tmp = target / f".tmp-{os.getpid()}-{time.time_ns()}"
    tmp.write_text(text, encoding="utf-8")
    try:
        for n in range(1, 1000):
            name = f"{stem}.md" if n == 1 else f"{stem}-{n}.md"
            path = target / name
            try:
                os.link(tmp, path)
                return path
            except FileExistsError:
                continue
        raise SystemExit(f"board: could not create a file under {target}")
    finally:
        tmp.unlink(missing_ok=True)


# --- reading -----------------------------------------------------------


def parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    head: dict[str, str] = {}
    body = text
    if text.startswith("from:"):
        block, sep, rest = text.partition("\n\n")
        if sep:
            for line in block.splitlines():
                key, colon, value = line.partition(":")
                if colon:
                    head[key.strip()] = value.strip()
            body = rest
    return {
        "id": f"{path.parent.name}/{path.stem}",
        "stream": path.parent.name,
        "file": path.name,
        "path": str(path),
        "head": head,
        "body": body.strip(),
    }


def messages(root: Path, stream: str | None = None, since: str | None = None) -> list[Path]:
    wanted = [stream] if stream else streams(root)
    found: list[Path] = []
    for name in wanted:
        directory = root / name
        if not directory.is_dir():
            continue
        files = [p for p in directory.glob("*.md") if p.name not in RESERVED]
        if since:
            files = [p for p in files if f"{name}/{p.stem}" > since]
        found.extend(files)
    return sorted(found, key=lambda p: (p.parent.name, p.name))


def format_message(path: Path) -> str:
    msg = parse(path)
    head = msg["head"]
    keys = ("at", "status", "to", "re", "tags")
    meta = "  ".join(f"{k}: {head[k]}" for k in keys if k in head)
    lines = [f"--- {msg['id']}"]
    if meta:
        lines.append(f"    {meta}")
    lines.append("")
    lines.extend(f"    {line}" for line in msg["body"].splitlines())
    return "\n".join(lines)


def as_json(path: Path) -> dict:
    msg = parse(path)
    return {
        "id": msg["id"],
        "stream": msg["stream"],
        "file": msg["file"],
        "at": msg["head"].get("at"),
        "status": msg["head"].get("status"),
        "to": recipients(msg)[0],
        "not": recipients(msg)[1],
        "re": msg["head"].get("re"),
        "tags": [t.strip() for t in msg["head"].get("tags", "").split(",") if t.strip()],
        "body": msg["body"],
    }


def recipients(msg: dict) -> tuple[list[str], list[str]]:
    """Include and exclude lists. An empty include list means broadcast."""
    raw = msg["head"].get("to", "all").strip()
    include = [] if not raw or raw in ("all", "*", "everyone") else [
        part.strip() for part in raw.split(",") if part.strip()
    ]
    raw_not = msg["head"].get("not", "").strip()
    exclude = [part.strip() for part in raw_not.split(",") if part.strip()]
    return include, exclude


def addressed_to(msg: dict, viewer: str) -> bool:
    include, exclude = recipients(msg)
    if viewer in exclude:
        return False
    return viewer in include or not include


# --- cursors -----------------------------------------------------------


def cursor_path(root: Path, viewer: str) -> Path:
    return root / ".watch" / f"{viewer}.json"


def load_cursor(root: Path, viewer: str) -> dict | None:
    path = cursor_path(root, viewer)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def save_cursor(root: Path, viewer: str, cursor: dict) -> None:
    path = cursor_path(root, viewer)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cursor, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def select(root: Path, viewer: str, include: list[str], exclude: list[str], include_own: bool) -> list[str]:
    chosen = streams(root)
    if include:
        chosen = [s for s in chosen if s in include]
    if exclude:
        chosen = [s for s in chosen if s not in exclude]
    if not include_own:
        chosen = [s for s in chosen if s != viewer]
    return chosen


def scan(
    root: Path,
    viewer: str,
    cursor: dict,
    include: list[str],
    exclude: list[str],
    include_own: bool,
    backlog: int,
    limit: int,
    see_all: bool = False,
) -> tuple[list[Path], dict]:
    """What this viewer has not seen, and the cursor to remember."""
    found: list[Path] = []
    next_cursor = dict(cursor)
    for name in select(root, viewer, include, exclude, include_own):
        files = sorted(
            p for p in (root / name).glob("*.md") if p.name not in RESERVED
        )
        if not files:
            continue
        mark = cursor.get(name)
        unseen = files[-backlog:] if mark is None else [p for p in files if p.name > mark]
        if not see_all:
            unseen = [p for p in unseen if addressed_to(parse(p), viewer)]
        found.extend(unseen)
        next_cursor[name] = files[-1].name
    if limit > 0:
        found = found[-limit:]
    return found, next_cursor


# --- commands ----------------------------------------------------------


def cmd_init(root: Path, names: list[str], here: bool) -> int:
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        check_name("stream", name)
        (root / name).mkdir(exist_ok=True)
    (root / ".watch").mkdir(exist_ok=True)

    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text(
            "# board state, not board content\n.watch/\n.tmp-*\n*.tmp\n\n"
            "# everything else in a stream is a message and belongs in git\n",
            encoding="utf-8",
        )

    readme = root / "README.md"
    if not readme.exists():
        listed = ", ".join(f"`{n}`" for n in names) or "(none yet)"
        readme.write_text(
            "# board\n\n"
            "Messages are files. One file per message, one directory per "
            "stream.\n\n"
            f"Streams: {listed}\n\n"
            "## The loop\n\n"
            "```\n"
            "post  ->  watch in the background  ->  work\n"
            "      ->  it exits and reports what arrived  ->  read, answer\n"
            "      ->  watch again\n"
            "```\n\n"
            "Re-arm as you read, before or while you act. A watcher that "
            "exited on delivery is dead until someone remembers it.\n\n"
            "## Quick start\n\n"
            "```bash\n"
            "python3 board.py post <your-stream> note \"what happened\"\n"
            "python3 board.py post <your-stream> ask \"what you need\"\n"
            "python3 board.py read -n 5\n"
            "python3 board.py ls\n"
            "python3 board.py watch <your-name> --for 900 --notify\n"
            "```\n\n"
            "Exit codes: `0` nothing new, `2` something new, `1` usage error.\n"
            "Nothing else uses `2`, so `if [ $? -eq 2 ]` always means "
            "\"something arrived\".\n\n"
            "See `board.py --help` and `board.py watch --help` for the rest.\n",
            encoding="utf-8",
        )

    print(f"board: initialised at {root}")
    for name in names:
        print(f"  {name}/")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    root = board_dir(args.dir)
    check_name("stream", args.stream)
    if not NAME.match(args.status):
        raise SystemExit(f"board: bad status {args.status!r}")
    tags = []
    for tag in args.tag:
        for piece in tag.split(","):
            piece = piece.strip()
            if piece and not TAG.match(piece):
                raise SystemExit(f"board: bad tag {piece!r}")
            if piece:
                tags.append(piece)
    extra: dict[str, str] = {}
    for item in args.field:
        key, colon, value = item.partition("=")
        if not colon or not key.strip():
            raise SystemExit(f"board: --field expects key=value, got {item!r}")
        extra[key.strip()] = value.strip()

    to = [part.strip() for part in args.to.split(",") if part.strip()]
    for name in to:
        check_name("recipient", name)
    if len(to) == 1 and to[0] in ("all", "*", "everyone"):
        to = []
    not_to = [part.strip() for part in (args.not_to or "").split(",") if part.strip()]
    for name in not_to:
        check_name("recipient", name)
    if to and set(to) & set(not_to):
        raise SystemExit("board: a recipient cannot be both included and excluded")

    body = sys.stdin.read() if args.body in (None, "-") else args.body
    if args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    if not body.strip():
        raise SystemExit("board: empty message, nothing written")

    path = post(root, args.stream, args.status, body, args.re, tags, extra, to, not_to)
    msg = parse(path)
    if args.json:
        print(json.dumps(as_json(path)))
    else:
        print(msg["id"])
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    root = board_dir(args.dir)
    found = messages(root, args.stream, args.since)
    if not found:
        print("board: nothing yet" if not root.is_dir() else "board: no messages")
        return 0
    if args.paths:
        for path in found[-args.count :] if args.count else found:
            print(path.relative_to(root))
        return 0
    grouped: dict[str, list[Path]] = {}
    for path in found:
        grouped.setdefault(path.parent.name, []).append(path)
    chosen: list[Path] = []
    for name in sorted(grouped):
        chosen.extend(grouped[name][-args.count :] if args.count else grouped[name])
    if args.json:
        print(json.dumps([as_json(p) for p in chosen], indent=2))
        return 0
    for path in chosen:
        print(format_message(path))
        print()
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    root = board_dir(args.dir)
    found = messages(root)
    if args.json:
        print(json.dumps([as_json(p) for p in found], indent=2))
        return 0
    if args.long:
        for name in streams(root):
            count = len([p for p in (root / name).glob("*.md") if p.name not in RESERVED])
            print(f"{name:<16} {count:>5}")
        return 0
    for path in found:
        print(path.relative_to(root))
    return 0


def cmd_streams(args: argparse.Namespace) -> int:
    root = board_dir(args.dir)
    names = streams(root)
    if args.json:
        print(json.dumps(names))
    else:
        for name in names:
            print(name)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    root = board_dir(args.dir)
    viewer = check_name("viewer", args.viewer)
    wanted = args.include or args.streams
    include = [s.strip() for s in wanted.split(",") if s.strip()] if wanted else []
    exclude = [s.strip() for s in args.exclude.split(",") if s.strip()] if args.exclude else []
    for name in include + exclude:
        check_name("stream", name)

    def stop(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)

    cursor = load_cursor(root, viewer)

    def emit(paths: list[Path]) -> None:
        if args.json:
            for path in paths:
                print(json.dumps(as_json(path)))
        else:
            for path in paths:
                print(format_message(path))
                print()

    if args.once:
        new, next_cursor = scan(
            root, viewer, cursor or {}, include, exclude, args.all,
            max(1, args.backlog), args.limit, see_all=args.all,
        )
        emit(new)
        if next_cursor != (cursor or {}):
            save_cursor(root, viewer, next_cursor)
        return 2 if new else 0

    if cursor is None:
        cursor = {}
        first = "first run, showing the last few from each stream"
    else:
        first = "showing what arrived since last time"

    if not args.quiet:
        limit = f"{args.seconds}s" if args.seconds > 0 else "no limit"
        where = ",".join(include) if include else ("all streams" if args.all else "every stream but your own")
        print(f"[board] watching as {viewer} ({where}) for {limit}; {first}")
        print("[board] exit 2 = something new, exit 0 = timed out with nothing")
        sys.stdout.flush()

    deadline = time.time() + args.seconds if args.seconds > 0 else None
    last_beat = time.time()

    try:
        while True:
            new, next_cursor = scan(
                root, viewer, cursor, include, exclude, args.all,
                max(1, args.backlog), args.limit, see_all=args.all,
            )
            if new:
                if args.notify:
                    plural = "s" if len(new) > 1 else ""
                    print(f"[board] {len(new)} new message{plural}")
                emit(new)
                sys.stdout.flush()
            if next_cursor != cursor:
                save_cursor(root, viewer, next_cursor)
                cursor = next_cursor
            if new and not args.stay:
                return 2
            if deadline is not None and time.time() >= deadline:
                return 0
            if args.heartbeat > 0 and time.time() - last_beat >= args.heartbeat:
                if not args.quiet:
                    print(f"[board] still watching ({int(time.time())})")
                    sys.stdout.flush()
                last_beat = time.time()
            time.sleep(max(1, args.every))
    except KeyboardInterrupt:
        if not args.quiet:
            print("[board] stopped")
        return 0


class Parser(argparse.ArgumentParser):
    """Usage errors exit 1, so exit 2 always means 'watch found something'."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise SystemExit(f"board: {message}")


def build_parser() -> Parser:
    parser = Parser(prog="board.py", description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=None, help="board directory (overrides BOARD_DIR)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create the board, its streams and its README")
    p_init.add_argument("--streams", default="human", help="comma-separated stream names")
    p_init.add_argument("--here", action="store_true", help="the folder holding board.py is the board")

    p_post = sub.add_parser("post", help="write a message to a stream")
    p_post.add_argument("stream", help="which stream to write to")
    p_post.add_argument("status", nargs="?", default="note", help="note, ask, answer, blocked, done, or anything short")
    p_post.add_argument("body", nargs="?", default="-", help='the message; "-" or omitted reads stdin')
    p_post.add_argument("-f", "--file", default=None, help="read the body from a file")
    p_post.add_argument("--re", dest="re", default=None, help="id of the message this replies to, stream/name")
    p_post.add_argument("--to", default="all",
                        help="include list, comma-separated, or all (default all)")
    p_post.add_argument("--not", dest="not_to", default=None,
                        help="exclude list, comma-separated, even when the message is broadcast")
    p_post.add_argument("--tag", action="append", default=[], help="tag, repeatable or comma-separated")
    p_post.add_argument("--field", action="append", default=[], help="extra header key=value, repeatable")
    p_post.add_argument("--json", action="store_true", help="print the message as JSON")

    p_read = sub.add_parser("read", help="print messages, oldest first")
    p_read.add_argument("stream", nargs="?", default=None, help="one stream, or every stream")
    p_read.add_argument("-n", "--count", type=int, default=5, help="per stream (default 5)")
    p_read.add_argument("--since", default=None, help="only messages with id greater than this")
    p_read.add_argument("--paths", action="store_true", help="print paths, not bodies")
    p_read.add_argument("--json", action="store_true", help="print a JSON array")

    p_watch = sub.add_parser("watch", help="background notifier: print new messages, then exit or wait")
    p_watch.add_argument("viewer", help="who you are; your own stream is skipped unless --all")
    p_watch.add_argument("--include", dest="include", default=None,
                         help="only these streams, comma-separated")
    p_watch.add_argument("--streams", default=None,
                         help="alias of --include, kept for older commands")
    p_watch.add_argument("--exclude", default=None, help="ignore these streams, comma-separated")
    p_watch.add_argument("--for", dest="seconds", type=int, default=900, help="give up after N seconds; 0 means forever (default 900)")
    p_watch.add_argument("--every", type=int, default=2, help="seconds between checks (default 2)")
    p_watch.add_argument("--stay", action="store_true", help="keep following after printing")
    p_watch.add_argument("--once", action="store_true", help="check once and return immediately")
    p_watch.add_argument("--all", action="store_true",
                         help="ignore addressing and include your own stream: the full feed")
    p_watch.add_argument("--notify", action="store_true", help="print one summary line before the bodies")
    p_watch.add_argument("--backlog", type=int, default=3, help="on a first run, how many per stream (default 3)")
    p_watch.add_argument("--limit", type=int, default=0, help="print at most N messages per wakeup (default all)")
    p_watch.add_argument("--heartbeat", type=int, default=0, help="print a keep-alive line every N seconds")
    p_watch.add_argument("--quiet", action="store_true", help="no banner, no heartbeat, no summary")
    p_watch.add_argument("--json", action="store_true", help="one JSON object per message")

    p_ls = sub.add_parser("ls", help="list every message path, or per-stream counts")
    p_ls.add_argument("--long", action="store_true", help="stream names and message counts")
    p_ls.add_argument("--json", action="store_true", help="JSON array of messages")

    p_streams = sub.add_parser("streams", help="list stream names")
    p_streams.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        root = board_dir(args.dir)
        if args.here:
            root = Path(__file__).resolve().parent
        names = [s.strip() for s in args.streams.split(",") if s.strip()]
        for name in names:
            check_name("stream", name)
        return cmd_init(root, names, args.here)
    if args.cmd == "post":
        return cmd_post(args)
    if args.cmd == "read":
        return cmd_read(args)
    if args.cmd == "watch":
        return cmd_watch(args)
    if args.cmd == "ls":
        return cmd_ls(args)
    if args.cmd == "streams":
        return cmd_streams(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
