#!/usr/bin/env python3
"""telegram-bridge.py -- the phone channel, as a file protocol adapter.

    export TELEGRAM_BOT_TOKEN=123456:ABC...      # from @BotFather
    python3 tools/telegram-bridge.py

The protocol is deliberately trivial, and that is the whole design:

    agents write  $COLLAB_HOME/outbox/*.md            -> forwarded to your chat
    you send      /c <project> <message>   (Telegram) -> $COLLAB_HOME/inbox/live/
                                                         <project>/from-user-*.md

Because the bridge is only an adapter over files, the agents never learn that
Telegram exists, and swapping in Slack or Discord means writing a different
adapter against the same two directories. Telegram is optional -- with the
bridge stopped, outbox files simply queue up and the core handoff loop is
unaffected.

Zero third-party dependencies: long-polling is stdlib ``urllib`` against the
Bot API.

Safety properties, all of which matter because inbound chat is untrusted input:

* **Delivery-confirmed archival.** An outbox file moves to ``outbox/archive/``
  only after Telegram answers ``ok: true``. Crash mid-send and the message is
  re-sent, never silently dropped. At-least-once, deliberately.
* **Ordered, head-of-line queue.** Messages send oldest-first and a transient
  failure stops the drain rather than skipping ahead, so a conversation cannot
  arrive scrambled. A *permanently* rejected message is quarantined to
  ``outbox/failed/`` so it cannot wedge the queue forever.
* **Learn-and-lock chat binding.** The first chat to talk to the bot is adopted
  and persisted; every other chat is ignored from then on. For a bot that is
  publicly discoverable, pin it up front with ``TELEGRAM_CHAT_ID``.
* **Path-traversal-safe project names.** ``/c ../../etc/passwd hi`` cannot
  escape ``inbox/live/`` -- names go through the allowlist slug validator, and
  an unknown project is answered with the real list rather than written blindly.
* **The token is never logged**, including inside URLs in error messages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


def _bootstrap() -> None:
    tools = os.path.dirname(os.path.realpath(__file__))
    if tools not in sys.path:
        sys.path.insert(0, tools)


_bootstrap()

from collabkit import console, dotenv, frontmatter, slug  # noqa: E402
from collabkit.atomic import atomic_write_json, atomic_write_text, ensure_dir, read_json  # noqa: E402
from collabkit.errors import EXIT_ERROR, EXIT_OK, EXIT_USAGE, LockTimeout  # noqa: E402
from collabkit.locking import SingletonLock  # noqa: E402
from collabkit.paths import HomePaths  # noqa: E402
from collabkit.registry import Registry  # noqa: E402
from collabkit.timeutil import compact, iso  # noqa: E402

API_ROOT = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4096
LONG_POLL_SECONDS = 25
HTTP_TIMEOUT = LONG_POLL_SECONDS + 15
MAX_SEND_ATTEMPTS = 5
IDLE_SLEEP = 1.0
BACKOFF_START = 2.0
BACKOFF_MAX = 60.0
ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT = "TELEGRAM_CHAT_ID"

HELP_TEXT = (
    "collab-kit bridge\n"
    "\n"
    "/c <project> <message>  send a message into that collab's live session\n"
    "/projects               list registered collabs\n"
    "/status                 outstanding handoffs everywhere\n"
    "/whoami                 show this chat id\n"
    "/help                   this message"
)


# ==========================================================================
# transport
# ==========================================================================


class TelegramError(Exception):
    """An API call failed.

    ``permanent`` separates "this message will never be accepted" (a 400 from a
    malformed payload) from "try again later" (a network blip, a 429, a 5xx).
    Only the former may be quarantined; treating a transient error as permanent
    would throw away a message the user is waiting for.
    """

    def __init__(self, message: str, *, permanent: bool = False, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


class TelegramClient:
    """Minimal Bot API client over ``urllib``."""

    def __init__(self, token: str, *, timeout: float = HTTP_TIMEOUT) -> None:
        if not token or ":" not in token:
            raise TelegramError(
                f"{ENV_TOKEN} is missing or malformed "
                "(expected the '<id>:<secret>' string from @BotFather)",
                permanent=True,
            )
        self._token = token
        self.timeout = timeout

    def redact(self, text: str) -> str:
        """Strip the bot token out of anything about to be printed."""
        return text.replace(self._token, "<token>") if self._token else text

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        url = f"{API_ROOT}/bot{self._token}/{method}"
        payload = urllib.parse.urlencode(
            {key: value for key, value in (params or {}).items() if value is not None}
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "collab-kit-bridge/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise self._from_http_error(exc, method) from None
        except urllib.error.URLError as exc:
            raise TelegramError(self.redact(f"{method}: network error: {exc.reason}")) from None
        except socket.timeout:
            raise TelegramError(f"{method}: timed out after {timeout or self.timeout:g}s") from None
        except (ValueError, OSError) as exc:
            raise TelegramError(self.redact(f"{method}: {exc}")) from None

        if not isinstance(body, dict) or not body.get("ok"):
            description = (body or {}).get("description", "no description") if isinstance(body, dict) else "malformed response"
            raise TelegramError(self.redact(f"{method}: API said not-ok: {description}"), permanent=True)
        return body.get("result")

    def _from_http_error(self, exc: urllib.error.HTTPError, method: str) -> TelegramError:
        detail = ""
        retry_after = 0.0
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            detail = payload.get("description", "")
            retry_after = float(payload.get("parameters", {}).get("retry_after", 0) or 0)
        except (ValueError, OSError, AttributeError):
            pass
        # 429 is explicitly transient and carries its own backoff. 5xx is
        # Telegram's problem, not the message's. Everything else in 4xx means
        # this exact payload will never be accepted.
        transient = exc.code == 429 or exc.code >= 500
        return TelegramError(
            self.redact(f"{method}: HTTP {exc.code} {detail}".strip()),
            permanent=not transient,
            retry_after=retry_after,
        )

    # -- convenience -----------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe", timeout=15) or {}

    def send_message(self, chat_id: int | str, text: str, *, parse_mode: str = "") -> Any:
        return self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode or None,
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )

    def get_updates(self, offset: int, *, long_poll: int = LONG_POLL_SECONDS) -> list[dict[str, Any]]:
        result = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": long_poll,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=long_poll + 15,
        )
        return result if isinstance(result, list) else []


# ==========================================================================
# persisted state
# ==========================================================================


class BridgeState:
    """Update offset, bound chat id, and per-file send attempts.

    The offset is the important one: Telegram replays any update that was not
    acknowledged by a higher offset, so persisting it is what stops a restart
    from re-processing the last hour of messages.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        data = read_json(self.path, default={}) or {}
        self.offset: int = int(data.get("offset", 0) or 0)
        self.chat_id: int | None = data.get("chat_id")
        self.attempts: dict[str, int] = {
            str(k): int(v) for k, v in (data.get("attempts") or {}).items()
        }
        self.bot_username: str = str(data.get("bot_username", ""))

    def save(self) -> None:
        try:
            atomic_write_json(
                self.path,
                {
                    "offset": self.offset,
                    "chat_id": self.chat_id,
                    "attempts": self.attempts,
                    "bot_username": self.bot_username,
                    "updated": iso(),
                },
            )
        except OSError as exc:
            console.warn(f"could not persist bridge state: {exc}")


# ==========================================================================
# the bridge
# ==========================================================================


class Bridge:
    def __init__(
        self,
        client: TelegramClient,
        home: HomePaths,
        *,
        pinned_chat_id: str | int | None = None,
        dry_run: bool = False,
        allow_unknown_projects: bool = False,
    ) -> None:
        self.client = client
        self.home = home.ensure()
        self.state = BridgeState(home.logs / "telegram-state.json")
        self.dry_run = dry_run
        self.allow_unknown_projects = allow_unknown_projects
        self._stop = False
        self._backoff = BACKOFF_START

        if pinned_chat_id not in (None, ""):
            try:
                self.state.chat_id = int(pinned_chat_id)  # type: ignore[arg-type]
                self.pinned = True
            except (TypeError, ValueError):
                raise TelegramError(
                    f"{ENV_CHAT}={pinned_chat_id!r} is not an integer chat id",
                    permanent=True,
                ) from None
        else:
            self.pinned = False

    # -- lifecycle -------------------------------------------------------

    def install_signal_handlers(self) -> None:
        def handle(_signum: int, _frame: Any) -> None:
            self._stop = True
            console.info("shutting down after the current poll...")

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):  # pragma: no cover
                pass

    def stop(self) -> None:
        self._stop = True

    def run(self, *, once: bool = False, max_cycles: int | None = None) -> int:
        cycles = 0
        while not self._stop:
            try:
                self.pump_outbox()
                if not once:
                    self.pump_updates()
                else:
                    # In --once mode use a near-zero long poll so the command
                    # returns promptly instead of blocking for 25s.
                    self.pump_updates(long_poll=0)
                self._backoff = BACKOFF_START
            except TelegramError as exc:
                console.warn(str(exc))
                if exc.permanent and once:
                    return EXIT_ERROR
                self._sleep(max(exc.retry_after, self._backoff))
                self._backoff = min(self._backoff * 2, BACKOFF_MAX)
            except OSError as exc:
                console.warn(f"filesystem error: {exc}")
                self._sleep(self._backoff)
            finally:
                self.state.save()

            cycles += 1
            if once or (max_cycles is not None and cycles >= max_cycles):
                break
            self._sleep(IDLE_SLEEP)
        self.state.save()
        return EXIT_OK

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    # -- outbox: agents -> phone ----------------------------------------

    def pump_outbox(self) -> int:
        """Send queued messages oldest-first. Returns how many were delivered.

        Stops at the first transient failure so ordering is preserved -- a
        conversation delivered out of order is worse than one delivered late.
        """
        if self.state.chat_id is None:
            return 0  # nowhere to send yet; files stay queued

        sent = 0
        for path in self._queued_files():
            if self._stop:
                break
            try:
                meta, body = frontmatter.parse_file(path)
            except Exception as exc:
                console.warn(f"unreadable outbox file {path.name}: {exc}")
                self._quarantine(path, reason="unparseable")
                continue

            text = self._format_outbox(meta, body)
            if not text.strip():
                self._archive(path)
                continue

            try:
                self._deliver(text, parse_mode=str(meta.get("parse_mode") or ""))
            except TelegramError as exc:
                attempts = self.state.attempts.get(path.name, 0) + 1
                self.state.attempts[path.name] = attempts
                if exc.permanent or attempts >= MAX_SEND_ATTEMPTS:
                    console.warn(f"giving up on {path.name} after {attempts} attempt(s): {exc}")
                    self._quarantine(path, reason=str(exc))
                    continue
                # Transient: leave it at the head of the queue and back off.
                raise

            self.state.attempts.pop(path.name, None)
            self._archive(path)
            sent += 1
        return sent

    def _queued_files(self) -> list[Path]:
        outbox = self.home.outbox
        if not outbox.is_dir():
            return []
        return sorted(
            path
            for path in outbox.glob("*.md")
            if path.is_file() and not path.name.startswith(".")
        )

    def _format_outbox(self, meta: dict[str, Any], body: str) -> str:
        """Tag the message with its project so a multi-collab chat stays readable."""
        project = str(meta.get("project") or "").strip()
        prefix = f"[{project}] " if project else ""
        return f"{prefix}{body.strip()}"

    def _deliver(self, text: str, *, parse_mode: str = "") -> None:
        for chunk in _split_message(text):
            if self.dry_run:
                console.out(f"[dry-run] -> chat {self.state.chat_id}: {chunk[:120]}")
                continue
            self.client.send_message(self.state.chat_id, chunk, parse_mode=parse_mode)

    def _archive(self, path: Path) -> None:
        """Move a delivered file out of the queue. Only ever called after ok."""
        if self.dry_run:
            return
        destination = ensure_dir(self.home.outbox_archive) / path.name
        try:
            os.replace(path, destination)
        except OSError as exc:
            console.warn(f"delivered {path.name} but could not archive it: {exc}")

    def _quarantine(self, path: Path, *, reason: str) -> None:
        """Park an undeliverable message. Kept, never deleted."""
        if self.dry_run:
            return
        destination = ensure_dir(self.home.outbox_failed) / path.name
        try:
            os.replace(path, destination)
            atomic_write_text(
                destination.with_suffix(destination.suffix + ".reason"),
                f"{iso()}\n{reason}\n",
            )
        except OSError as exc:
            console.warn(f"could not quarantine {path.name}: {exc}")
        self.state.attempts.pop(path.name, None)

    # -- updates: phone -> agents ---------------------------------------

    def pump_updates(self, *, long_poll: int = LONG_POLL_SECONDS) -> int:
        """Long-poll for inbound messages. Returns how many were handled."""
        updates = self.client.get_updates(self.state.offset, long_poll=long_poll)
        handled = 0
        for update in updates:
            # Advance the offset even for updates we ignore, or a message from
            # a stranger would be re-fetched forever.
            self.state.offset = max(self.state.offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            if self._handle_message(message):
                handled += 1
        if updates:
            self.state.save()
        return handled

    def _handle_message(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text:
            return False

        if not self._authorized(chat_id):
            return False

        command, _, rest = text.partition(" ")
        command = command.lower().split("@", 1)[0]  # tolerate /help@yourbot

        if command in ("/start", "/help"):
            self._reply(HELP_TEXT)
            return True
        if command == "/whoami":
            self._reply(f"chat id: {chat_id}")
            return True
        if command == "/projects":
            self._reply(self._projects_text())
            return True
        if command == "/status":
            self._reply(self._status_text())
            return True
        if command == "/c":
            return self._handle_c(rest, message)

        self._reply(
            "not a command I know. Send a message with:\n"
            "  /c <project> <your message>\n\n" + HELP_TEXT
        )
        return True

    def _authorized(self, chat_id: int) -> bool:
        """Learn-and-lock: adopt the first chat, then ignore every other one."""
        if self.state.chat_id is None:
            self.state.chat_id = int(chat_id)
            self.state.save()
            console.ok(f"bound to chat {chat_id}")
            self._reply(
                "collab-kit bridge connected. This chat is now bound.\n\n" + HELP_TEXT
            )
            return False  # the binding message itself is not a command
        if int(chat_id) != int(self.state.chat_id):
            console.warn(f"ignoring message from unbound chat {chat_id}")
            return False
        return True

    def _handle_c(self, rest: str, message: dict[str, Any]) -> bool:
        raw_project, _, body = rest.strip().partition(" ")
        body = body.strip()
        if not raw_project:
            self._reply("usage: /c <project> <message>\n\n" + self._projects_text())
            return True

        project = slug.coerce_name(raw_project)
        if not project:
            # The rejected name is echoed back with repr() so a traversal
            # attempt is visible rather than mysterious.
            self._reply(f"invalid project name {raw_project!r}.\n\n" + self._projects_text())
            return True

        known = self._known_projects()
        if project not in known and not self.allow_unknown_projects:
            self._reply(
                f"no collab named {project!r}.\n\n" + self._projects_text()
            )
            return True

        if not body:
            self._reply(f"nothing to send. usage: /c {project} <message>")
            return True

        path = self._write_inbox(project, body, message)
        console.ok(f"/c {project}: {len(body)} chars -> {path.name}")
        self._reply(f"delivered to {project} ({len(body)} chars)")
        return True

    def _write_inbox(self, project: str, body: str, message: dict[str, Any]) -> Path:
        directory = ensure_dir(self.home.inbox_for(project))
        stamp = compact()
        sequence = 0
        while True:
            path = directory / f"from-user-{stamp}-{sequence:03d}.md"
            if not path.exists():
                break
            sequence += 1
            if sequence > 999:  # pragma: no cover
                path = directory / f"from-user-{stamp}-{os.getpid()}.md"
                break

        meta = {
            "from": "user",
            "to": "builder",
            "project": project,
            "created": iso(),
            "source": "telegram",
            "chat_id": self.state.chat_id,
            "message_id": message.get("message_id"),
        }
        atomic_write_text(path, frontmatter.dumps(meta, body))
        return path

    # -- replies ---------------------------------------------------------

    def _reply(self, text: str) -> None:
        if self.state.chat_id is None:
            return
        try:
            self._deliver(text)
        except TelegramError as exc:
            console.warn(f"could not reply: {exc}")

    def _known_projects(self) -> list[str]:
        try:
            return Registry(self.home).names()
        except Exception:
            return []

    def _projects_text(self) -> str:
        names = self._known_projects()
        if not names:
            return "no collabs registered yet."
        return "projects:\n" + "\n".join(f"  {name}" for name in names)

    def _status_text(self) -> str:
        """Same information as ``handoff status``, sized for a phone screen."""
        from collabkit.paths import CollabPaths
        from collabkit.store import HandoffStore

        lines: list[str] = []
        try:
            entries = list(Registry(self.home))
        except Exception as exc:
            return f"could not read the registry: {exc}"
        for entry in entries:
            if not entry.exists:
                lines.append(f"{entry.name}: MISSING at {entry.root}")
                continue
            store = HandoffStore(CollabPaths.at(entry.root, entry.name), collab=entry.name)
            counts = store.counts()
            oldest = store.oldest_pending()
            line = f"{entry.name}: {counts['pending']} pending, {counts['claimed']} claimed"
            if oldest:
                line += f"\n   oldest ({oldest.age}): {oldest.title[:60]}"
            lines.append(line)
        return "\n".join(lines) if lines else "no collabs registered yet."


def _split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> Iterable[str]:
    """Split on line boundaries where possible, hard-split where not.

    Telegram rejects anything over 4096 characters outright, and a rejected
    message is a message the user never sees -- so a long agent report has to be
    chunked rather than truncated.
    """
    text = text.rstrip()
    if len(text) <= limit:
        yield text
        return

    buffer: list[str] = []
    size = 0
    for line in text.split("\n"):
        while len(line) > limit:
            if buffer:
                yield "\n".join(buffer)
                buffer, size = [], 0
            yield line[:limit]
            line = line[limit:]
        if size + len(line) + 1 > limit and buffer:
            yield "\n".join(buffer)
            buffer, size = [], 0
        buffer.append(line)
        size += len(line) + 1
    if buffer:
        yield "\n".join(buffer)


# ==========================================================================
# entry point
# ==========================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-bridge.py",
        description="Bridge $COLLAB_HOME/outbox and inbox/live to a Telegram chat.",
        epilog=f"Requires {ENV_TOKEN}. Set {ENV_CHAT} to pin a public bot to one chat.",
    )
    parser.add_argument("--once", action="store_true", help="one drain + poll cycle, then exit")
    parser.add_argument("--max-cycles", type=int, default=None, help="stop after N cycles")
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would be sent; touch nothing"
    )
    parser.add_argument(
        "--check", action="store_true", help="verify the token and exit (calls getMe)"
    )
    parser.add_argument(
        "--allow-unknown-projects",
        action="store_true",
        help="accept /c for collabs that are not registered",
    )
    parser.add_argument("--chat-id", default="", help=f"pin the chat id (overrides {ENV_CHAT})")
    parser.add_argument(
        "--send", metavar="TEXT", help="send one message and exit (for testing the wiring)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Before any os.environ read: a git-ignored .env is where this repo's
    # secrets live. A real environment variable still wins over it.
    env_file = dotenv.load()
    for problem in env_file.problems:
        console.warn(f".env: {problem}")
    if env_file.insecure:
        console.warn(f"{env_file.path} is readable by other users -- chmod 600 it")
    if env_file.loaded:
        console.info(f"loaded {len(env_file.loaded)} key(s) from {env_file.path}")

    token = os.environ.get(ENV_TOKEN, "").strip()
    if not token:
        console.error(f"{ENV_TOKEN} is not set")
        console.hint(f"put {ENV_TOKEN}=<token> in {dotenv.FILENAME} at the project root,")
        console.hint("or export it in the shell. Create the bot with @BotFather.")
        console.hint("the phone bridge is optional -- the handoff loop works without it")
        return EXIT_USAGE

    home = HomePaths.discover()
    try:
        client = TelegramClient(token)
    except TelegramError as exc:
        console.error(str(exc))
        return EXIT_USAGE

    if args.check:
        try:
            me = client.get_me()
        except TelegramError as exc:
            console.error(str(exc))
            return EXIT_ERROR
        console.ok(f"token valid: @{me.get('username', '?')} (id {me.get('id', '?')})")
        state = BridgeState(home.logs / "telegram-state.json")
        console.info(f"bound chat: {state.chat_id if state.chat_id is not None else '(not yet bound)'}")
        return EXIT_OK

    try:
        bridge = Bridge(
            client,
            home,
            pinned_chat_id=args.chat_id or os.environ.get(ENV_CHAT) or None,
            dry_run=args.dry_run,
            allow_unknown_projects=args.allow_unknown_projects,
        )
    except TelegramError as exc:
        console.error(str(exc))
        return EXIT_USAGE

    if args.send is not None:
        if bridge.state.chat_id is None:
            console.error("no chat bound yet -- message the bot once, or pass --chat-id")
            return EXIT_USAGE
        try:
            bridge._deliver(args.send)
        except TelegramError as exc:
            console.error(str(exc))
            return EXIT_ERROR
        console.ok("sent")
        return EXIT_OK

    # One bridge per COLLAB_HOME. Two would double-deliver every outbox file
    # and fight over the same getUpdates offset.
    lock_path = home.locks / "telegram-bridge.lock"
    try:
        with SingletonLock(lock_path, name="telegram bridge"):
            bridge.install_signal_handlers()
            console.info(f"bridge up. outbox={home.outbox} inbox={home.inbox_live}")
            if bridge.state.chat_id is None:
                console.info("no chat bound yet -- send /start to your bot from your phone")
            else:
                console.info(f"bound chat: {bridge.state.chat_id}")
            return bridge.run(once=args.once, max_cycles=args.max_cycles)
    except LockTimeout as exc:
        console.error(str(exc))
        if exc.hint:
            console.hint(exc.hint)
        return EXIT_ERROR


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:  # pragma: no cover
        raise SystemExit(EXIT_OK)
