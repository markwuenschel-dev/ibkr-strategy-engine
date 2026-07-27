"""Getting a human's attention.

Two independent channels, because ARCHITECTURE.md's requirement is that
"nothing waits unseen":

``desktop``  a local OS notification, best-effort per platform.
``outbox``   a file in ``$COLLAB_HOME/outbox/`` that the Telegram bridge
             forwards to your phone. This one is durable -- it survives the
             notifier being closed, the laptop being asleep, and the bridge not
             running yet, because it is just a file waiting to be picked up.

Everything here is best-effort and non-blocking. A notifier that hangs would
stall the watcher that called it, and a watcher that stalls is a handoff nobody
sees -- strictly worse than a missed toast.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .atomic import atomic_write_text, ensure_dir
from .paths import HomePaths
from .slug import slugify
from .timeutil import compact, iso

NOTIFY_TIMEOUT = 5.0
ENV_NOTIFY_CMD = "COLLAB_NOTIFY_CMD"
ENV_NO_DESKTOP = "COLLAB_KIT_NO_DESKTOP_NOTIFY"


def notify(title: str, message: str, *, urgent: bool = False) -> bool:
    """Fire a desktop notification. Returns whether one was dispatched.

    Order of preference: an explicit ``COLLAB_NOTIFY_CMD`` override, then the
    platform's native tool. A terminal bell is always emitted regardless, since
    it is the one channel that works over SSH and in a bare TTY.
    """
    _bell()
    if os.environ.get(ENV_NO_DESKTOP):
        return False

    override = os.environ.get(ENV_NOTIFY_CMD)
    if override:
        # shlex.split honours quoting in the user's template, then the
        # placeholders are substituted into the *already-split* argv. That
        # ordering is the security property: a handoff title written by an agent
        # lands in exactly one argv slot and can never add arguments, and since
        # no shell is involved, `$(...)` in it is inert text.
        import shlex

        try:
            parts = shlex.split(override)
        except ValueError:
            warn = f"{ENV_NOTIFY_CMD} is not parseable (unbalanced quotes); ignoring it"
            print(warn, file=sys.stderr)
            return False
        command = [
            part.replace("{title}", title).replace("{message}", message) for part in parts
        ]
        return _run(command)

    for builder in (_linux_command, _macos_command, _windows_command):
        command = builder(title, message, urgent)
        if command:
            return _run(command)
    return False


def _bell() -> None:
    try:
        if sys.stderr.isatty():
            sys.stderr.write("\a")
            sys.stderr.flush()
    except (AttributeError, ValueError, OSError):
        pass


def _run(command: list[str]) -> bool:
    if not command:
        return False
    try:
        subprocess.run(
            command,
            check=False,
            timeout=NOTIFY_TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _linux_command(title: str, message: str, urgent: bool) -> list[str] | None:
    if not sys.platform.startswith("linux") or not shutil.which("notify-send"):
        return None
    return [
        "notify-send",
        "--app-name=collab-kit",
        f"--urgency={'critical' if urgent else 'normal'}",
        title,
        message,
    ]


def _macos_command(title: str, message: str, _urgent: bool) -> list[str] | None:
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return None
    # AppleScript string literals: escape backslashes first, then quotes.
    script = (
        f'display notification "{_applescript(message)}" '
        f'with title "collab-kit" subtitle "{_applescript(title)}"'
    )
    return ["osascript", "-e", script]


def _windows_command(title: str, message: str, _urgent: bool) -> list[str] | None:
    if not sys.platform.startswith("win"):
        return None
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return None
    # Balloon tip via WinForms: present on every Windows box, no install, no
    # third-party module. Single-quoted PowerShell strings need '' doubling.
    script = (
        "[void][reflection.assembly]::LoadWithPartialName('System.Windows.Forms');"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle='collab-kit';"
        f"$n.BalloonTipText='{_pwsh(title)}: {_pwsh(message)}';"
        "$n.Visible=$true;$n.ShowBalloonTip(5000);Start-Sleep -Milliseconds 5200;$n.Dispose()"
    )
    return [shell, "-NoProfile", "-NonInteractive", "-Command", script]


def _applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:200]


def _pwsh(text: str) -> str:
    return text.replace("'", "''").replace("\n", " ")[:200]


# --------------------------------------------------------------------------
# outbox -- the durable channel
# --------------------------------------------------------------------------


def write_outbox(
    text: str,
    *,
    home: HomePaths | None = None,
    project: str = "",
    source: str = "collab-kit",
    priority: str = "normal",
) -> Path:
    """Queue a message for the phone bridge. Returns the file written.

    Filenames are ``<compact-utc>-<seq>-<slug>.md`` so the bridge can send them
    in the order they were produced with a plain sorted glob -- ordering that
    survives the bridge being restarted, which an in-memory queue would not.
    """
    home = home or HomePaths.discover()
    directory = ensure_dir(home.outbox)

    stamp = compact()
    slug = slugify(project or source, fallback="msg", max_length=24)
    # Same-second messages need a tiebreaker that still sorts correctly.
    sequence = 0
    while True:
        name = f"{stamp}-{sequence:03d}-{slug}.md"
        path = directory / name
        if not path.exists():
            break
        sequence += 1
        if sequence > 999:  # pragma: no cover - 1000 messages in one second
            path = directory / f"{stamp}-{os.getpid()}-{slug}.md"
            break

    from . import frontmatter

    meta = {"created": iso(), "source": source, "priority": priority}
    if project:
        meta["project"] = project
    atomic_write_text(path, frontmatter.dumps(meta, text))
    return path


def announce(
    title: str,
    message: str,
    *,
    project: str = "",
    home: HomePaths | None = None,
    desktop: bool = True,
    phone: bool = False,
    urgent: bool = False,
) -> None:
    """Both channels at once, each independently best-effort.

    A failure in one must not suppress the other: the whole point of having two
    channels is that they fail differently.
    """
    if desktop:
        try:
            notify(title, message, urgent=urgent)
        except Exception:  # pragma: no cover - defensive
            pass
    if phone:
        try:
            write_outbox(
                f"*{title}*\n{message}",
                home=home,
                project=project,
                source="watcher",
                priority="high" if urgent else "normal",
            )
        except OSError:
            pass
