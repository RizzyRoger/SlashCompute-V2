"""Terminal commands for a Code tab: how risky each one is, and running it inside the project folder.

``classify`` is a conservative reading of the command line, not a sandbox. Anything it can't follow
(nested commands, inline code, paths outside the folder) counts as powerful, so Auto mode asks first.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import psutil

from compute.app.code.workspace import is_sensitive

READ_ONLY, NORMAL, POWERFUL = "read_only", "normal", "powerful"
RANK = {READ_ONLY: 0, NORMAL: 1, POWERFUL: 2}
COMMAND_TIMEOUT_S = 120.0
OUTPUT_KEEP_BYTES = 2 * 1024 * 1024
OUTPUT_SHOW_CHARS = 10_000
LOGIN_PATH_TIMEOUT_S = 5.0

SEPARATORS = frozenset({"&&", "||", ";", "|", "&", ";;", "|&"})
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WINDOWS_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
SAFE_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "nul", "NUL"})

READ_ONLY_NAMES = frozenset({
    "ls", "cat", "head", "tail", "wc", "pwd", "grep", "egrep", "fgrep", "rg", "ag", "find", "fd",
    "echo", "printf", "which", "whereis", "file", "stat", "du", "tree", "diff", "cmp", "sort", "uniq",
    "cut", "tr", "basename", "dirname", "realpath", "readlink", "date", "true", "false", "less", "more",
    "column", "nl", "md5", "md5sum", "shasum", "sha256sum", "whoami", "uname", "jq", "cd", "pushd",
    "popd", "test", "[", "sleep",
    # Windows (cmd)
    "dir", "type", "findstr", "where",
})
POWERFUL_NAMES = {
    **dict.fromkeys(("rm", "rmdir", "del", "erase", "rd", "unlink", "shred", "srm", "trash"), "deletes files"),
    **dict.fromkeys(("sudo", "su", "doas", "runas"), "runs as administrator"),
    **dict.fromkeys(("chmod", "chown", "chgrp", "chflags", "takeown", "icacls", "attrib", "xattr"),
                    "changes file permissions"),
    **dict.fromkeys(("kill", "killall", "pkill", "taskkill"), "stops processes"),
    **dict.fromkeys(("dd", "mkfs", "fdisk", "diskutil", "format", "mount", "umount", "hdiutil"), "touches disks"),
    **dict.fromkeys(("shutdown", "reboot", "halt", "poweroff"), "restarts the computer"),
    **dict.fromkeys(("curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "telnet", "ftp",
                     "invoke-webrequest", "iwr", "bitsadmin"), "uses the network"),
    **dict.fromkeys(("docker", "podman", "kubectl", "helm", "terraform", "vagrant"), "controls containers or infrastructure"),
    **dict.fromkeys(("osascript", "open", "xdg-open", "start", "launchctl", "systemctl", "service", "crontab",
                     "defaults", "security", "reg", "sc", "schtasks", "setx", "net", "wmic", "csrutil", "spctl"),
                    "changes system settings or opens apps"),
    **dict.fromkeys(("eval", "exec", "source", ".", "xargs", "nohup", "env", "command", "builtin", "watch",
                     "parallel", "at", "batch", "script", "expect"), "runs another command indirectly"),
    **dict.fromkeys(("npx", "pnpx", "bunx", "uvx", "pipx"), "downloads and runs a package"),
    **dict.fromkeys(("apt", "apt-get", "yum", "dnf", "pacman", "zypper", "apk", "port", "choco", "winget",
                     "scoop", "snap", "flatpak"), "installs system packages"),
    **dict.fromkeys(("powershell", "pwsh", "cmd"), "starts another shell"),
}
INTERPRETERS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "python", "python3",
                          "node", "ruby", "perl", "php", "deno", "bun", "lua", "Rscript"})
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval", "-p", "--print", "-command", "-encodedcommand", "-r"})
PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv", "poetry", "pdm", "pipenv",
                              "conda", "mamba", "gem", "bundle", "cargo", "go", "composer", "brew", "dotnet",
                              "nuget", "swift", "mix", "dart", "flutter"})
PACKAGE_CHANGES = frozenset({"install", "i", "add", "remove", "rm", "uninstall", "un", "update", "upgrade",
                             "up", "publish", "link", "unlink", "ci", "global", "dlx", "exec", "x", "sync",
                             "get", "tap", "untap", "cleanup", "autoremove", "lock", "self", "yank", "login"})
RUNNERS = frozenset({"uv", "poetry", "pdm", "pipenv", "bundle", "conda", "mamba"})
GIT_READ = frozenset({"status", "diff", "log", "show", "blame", "ls-files", "rev-parse", "describe",
                      "shortlog", "grep", "cat-file", "ls-tree", "reflog", "whatchanged", "version", "help"})
# Options that make an otherwise read-only program start another program.
EXEC_OPTIONS = {
    "rg": ("--pre", "--hostname-bin"),
    "fd": ("-x", "--exec", "-X", "--exec-batch"),
    "sort": ("--compress-program",),
    "ag": ("--pager",),
}
# git global options that can make any git command run an arbitrary program.
GIT_EXEC_OPTIONS = ("-c", "--config-env", "--exec-path")
GIT_NETWORK = frozenset({"fetch", "pull", "clone", "ls-remote", "archive"})
GIT_POWERFUL = frozenset({"push", "clean", "rebase", "filter-branch", "filter-repo", "rm", "restore",
                          "update-ref", "gc", "prune", "replace", "lfs"})
FIND_POWERFUL = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"})


@dataclass(frozen=True)
class Risk:
    level: str
    reason: str


def worst(risks: list[Risk]) -> Risk:
    return max(risks, key=lambda r: RANK[r.level]) if risks else Risk(READ_ONLY, "does nothing")


# ------------------------------------------------------------- tokenizing


def newlines_to_separators(command: str) -> str:
    """Newlines outside quotes end a command, like ``;``; inside quotes they stay part of the word."""
    out, quote, escaped = [], "", False
    for ch in command.replace("\r", ""):
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif ch in "'\"" and (not quote or quote == ch):
            quote = "" if quote else ch
        elif ch == "\n" and not quote:
            ch = " ; "
        out.append(ch)
    return "".join(out)


def tokenize(command: str) -> list[str]:
    """Shell words and operators (``&&``, ``|``, ``>`` ...)."""
    lexer = shlex.shlex(newlines_to_separators(command), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def segments(tokens: list[str]) -> list[tuple[list[str], bool, str]]:
    """(words, fed by a pipe, separator after) for each simple command."""
    out, current, piped = [], [], False
    for token in tokens:
        if token in SEPARATORS:
            out.append((current, piped, token))
            piped = token in ("|", "|&")
            current = []
        else:
            current.append(token)
    out.append((current, piped, ""))
    return [s for s in out if s[0]]


def is_redirect(token: str) -> bool:
    return bool(token) and set(token) <= set("<>&") and any(c in token for c in "<>")


# ------------------------------------------------------------- path checks


def outside(arg: str, root: Path) -> bool:
    """True when an argument names a path outside the project folder."""
    value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
    if not value or "://" in value or value in SAFE_TARGETS:
        return False
    if value.startswith("~"):
        return True
    is_abs = value.startswith("/") or bool(WINDOWS_ABS.match(value))
    if not is_abs and ".." not in Path(value).parts:
        return False
    try:
        target = (Path(value) if is_abs else root / value).resolve()
    except (OSError, ValueError):
        return True
    return target != root and root not in target.parents


def words_outside(words: list[str], root: Path) -> bool:
    return any(outside(w, root) for w in words)


# ------------------------------------------------------------- one command


def strip_redirects(words: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(argv, files written, files read) with redirections taken out."""
    argv, writes, reads = [], [], []
    i = 0
    while i < len(words):
        word = words[i]
        if is_redirect(word):
            target = words[i + 1] if i + 1 < len(words) else ""
            if word.endswith("&") or target.isdigit() or target.startswith("&"):
                i += 2 if target.isdigit() or target.startswith("&") else 1
                continue
            (writes if ">" in word else reads).append(target)
            i += 2
            continue
        argv.append(word)
        i += 1
    return argv, writes, reads


def first_sub(argv: list[str], skip_with_value: frozenset[str] = frozenset()) -> tuple[str, list[str]]:
    """The first non-option word after the program name, and the words after it."""
    i = 1
    while i < len(argv):
        word = argv[i]
        if word in skip_with_value:
            i += 2
            continue
        if word.startswith("-"):
            i += 1
            continue
        return word.lower(), argv[i + 1:]
    return "", []


def has_option(args: list[str], options: tuple[str, ...]) -> Optional[str]:
    """The first of ``options`` present in ``args`` (as ``--opt`` or ``--opt=value``), else None."""
    for a in args:
        for o in options:
            if a == o or a.startswith(o + "="):
                return o
    return None


def git_global_options(argv: list[str]) -> list[str]:
    """The options between ``git`` and its subcommand (``-c key=value`` counts as two words)."""
    out, i = [], 1
    while i < len(argv) and argv[i].startswith("-"):
        takes_value = argv[i] in ("-C", "-c", "--git-dir", "--work-tree", "--config-env", "--exec-path")
        out += argv[i:i + 2] if takes_value else argv[i:i + 1]
        i += 2 if takes_value else 1
    return out


def git_risk(argv: list[str]) -> Risk:
    if opt := has_option(git_global_options(argv), GIT_EXEC_OPTIONS):
        return Risk(POWERFUL, f"git {opt} can make git run any program")
    sub, rest = first_sub(argv, frozenset({"-C", "-c", "--git-dir", "--work-tree", "--config-env",
                                          "--exec-path"}))
    flags = set(rest)
    if sub in GIT_NETWORK:
        return Risk(POWERFUL, f"git {sub} uses the network")
    if sub == "grep" and (has_option(rest, ("--open-files-in-pager",)) or any(a.startswith("-O") for a in rest)):
        return Risk(POWERFUL, "git grep -O opens files in another program")
    if sub in GIT_POWERFUL:
        return Risk(POWERFUL, f"git {sub} can lose work or publish it")
    if sub == "reset" and ("--hard" in flags or "--merge" in flags or "--keep" in flags):
        return Risk(POWERFUL, "git reset --hard discards changes")
    if sub in ("checkout", "switch") and ({"--", "-f", "--force", "--discard-changes", "."} & flags):
        return Risk(POWERFUL, f"git {sub} can discard changes")
    if sub == "branch" and ({"-d", "-D", "--delete", "-f", "--force", "-m", "-M"} & flags):
        return Risk(POWERFUL, "git branch can delete branches")
    if sub == "stash":
        if rest[:1] in (["drop"], ["clear"]):
            return Risk(POWERFUL, "git stash drop discards saved work")
        if rest[:1] in (["list"], ["show"]):
            return Risk(READ_ONLY, "git stash list only reads")
        return Risk(NORMAL, "git stash changes the working tree")
    if sub in ("tag", "remote", "config", "submodule", "worktree") and rest and not rest[0].startswith("-l"):
        if sub == "config" and set(rest) & {"--get", "--list", "-l", "--get-all"}:
            return Risk(READ_ONLY, "reads git settings")
        if sub == "remote" and rest == ["-v"]:
            return Risk(READ_ONLY, "lists git remotes")
        return Risk(POWERFUL, f"git {sub} changes repository settings")
    if sub in GIT_READ or (sub in ("branch", "tag", "remote") and (not rest or rest[0] in ("-a", "-r", "-v", "-l", "--list"))):
        return Risk(READ_ONLY, f"git {sub or 'help'} only reads")
    return Risk(NORMAL, f"git {sub} changes the repository")


def package_risk(name: str, argv: list[str], root: Path) -> Risk:
    sub, rest = first_sub(argv)
    if name == "brew" and sub not in ("list", "info", "search", "--version", "config", "doctor", "outdated", "deps"):
        return Risk(POWERFUL, "brew changes installed software")
    if name in ("pip", "pip3") and sub in ("list", "show", "freeze", "check", "--version"):
        return Risk(READ_ONLY, f"{name} {sub} only reads")
    if sub in PACKAGE_CHANGES:
        return Risk(POWERFUL, f"{name} {sub} installs, removes or publishes packages")
    if sub == "run" and name in RUNNERS:
        inner = list(rest)
        while inner and inner[0].startswith("-"):
            inner = inner[1:]
        return worst([Risk(NORMAL, f"{name} run starts a program"), command_risk(inner, False, root)])
    return Risk(NORMAL, f"{name} {sub} runs project tooling".replace("  ", " "))


def program_name(word: str) -> str:
    name = os.path.basename(word.replace("\\", "/")).lower()
    return name[:-4] if name.endswith((".exe", ".cmd", ".bat")) else name


def command_risk(words: list[str], piped: bool, root: Path) -> Risk:
    argv, writes, reads = strip_redirects(words)
    risks = []
    if argv and ASSIGNMENT.match(argv[0]):
        risks.append(Risk(POWERFUL, "sets environment variables for the command (they can change what it runs)"))
    while argv and ASSIGNMENT.match(argv[0]):
        argv = argv[1:]
    while argv and program_name(argv[0]) == "time":
        argv = argv[1:]
    if any(is_sensitive(Path(w)) for w in argv[1:] + writes + reads if w and not w.startswith("-")):
        risks.append(Risk(POWERFUL, "touches a file that may hold secrets (its text would be sent to the pool)"))
    if words_outside(argv[1:] + writes + reads, root):
        risks.append(Risk(POWERFUL, "uses a path outside the project folder"))
    if any(t not in SAFE_TARGETS for t in writes):
        risks.append(Risk(NORMAL, "writes output to a file"))
    if not argv:
        return worst(risks)
    name = program_name(argv[0])
    args = argv[1:]
    lowered = {a.lower() for a in args}
    if name == "printenv" or (name == "env" and not args):
        risks.append(Risk(POWERFUL, f"{name} prints environment variables, which may hold secrets "
                                    "(the output is sent to the pool)"))
    elif name in POWERFUL_NAMES:
        risks.append(Risk(POWERFUL, f"{name} {POWERFUL_NAMES[name]}"))
    elif name in EXEC_OPTIONS and (opt := has_option(args, EXEC_OPTIONS[name])):
        risks.append(Risk(POWERFUL, f"{name} {opt} runs another program"))
    elif name in INTERPRETERS and piped:
        risks.append(Risk(POWERFUL, f"pipes text into {name} to run it"))
    elif name in INTERPRETERS and lowered & INLINE_CODE_FLAGS:
        risks.append(Risk(POWERFUL, f"{name} runs inline code"))
    elif name in INTERPRETERS and args and args[0] in ("--version", "-V"):
        risks.append(Risk(READ_ONLY, f"{name} --version"))
    elif name in INTERPRETERS and "-m" in args[:-1] and program_name(args[args.index("-m") + 1]) in PACKAGE_MANAGERS:
        module = program_name(args[args.index("-m") + 1])
        risks.append(package_risk(module, args[args.index("-m") + 1:], root))
    elif name == "git":
        risks.append(git_risk(argv))
    elif name in PACKAGE_MANAGERS:
        risks.append(package_risk(name, argv, root))
    elif name == "find" and lowered & FIND_POWERFUL:
        risks.append(Risk(POWERFUL, "find can delete files or run commands"))
    elif name in ("sed", "perl") and any(a.startswith("-i") for a in args):
        risks.append(Risk(NORMAL, f"{name} -i edits files in place"))
    elif name == "sort" and any(a.startswith("-o") for a in args):
        risks.append(Risk(NORMAL, "sort -o writes a file"))
    elif name in READ_ONLY_NAMES:
        risks.append(Risk(READ_ONLY, f"{name} only reads"))
    else:
        risks.append(Risk(NORMAL, f"{name} changes files or runs a program"))
    return worst(risks)


def classify(command: str, root: Path) -> Risk:
    text = (command or "").strip()
    if not text:
        return Risk(NORMAL, "empty command")
    if "`" in text or "$(" in text or "<(" in text or ">(" in text:
        return Risk(POWERFUL, "runs a nested command ($(...) or backticks)")
    try:
        tokens = tokenize(text)
    except ValueError:
        return Risk(POWERFUL, "could not read the command (unbalanced quotes)")
    parts = segments(tokens)
    risks = [command_risk(words, piped, root) for words, piped, _ in parts]
    if any(sep == "&" for _, _, sep in parts):
        risks.append(Risk(POWERFUL, "starts a background process"))
    return worst(risks)


# ----------------------------------------------------------------- running


@dataclass(frozen=True)
class CommandResult:
    exit_code: Optional[int]
    output: str
    timed_out: bool = False

    def text(self) -> str:
        status = "timed out and was stopped" if self.timed_out else f"exit code {self.exit_code}"
        body = self.output if self.output.strip() else "(no output)"
        return f"[{status}]\n{body}"


@functools.lru_cache(maxsize=1)
def login_path() -> Optional[str]:
    """PATH from the user's login shell. An app opened from Finder gets a bare PATH without
    Homebrew, nvm or uv; commands should see the same tools as the user's terminal."""
    if sys.platform == "win32":
        return None
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        out = subprocess.run([shell, "-ilc", 'printf "__PATH__%s__END__" "$PATH"'], capture_output=True,
                             text=True, timeout=LOGIN_PATH_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"__PATH__(.*?)__END__", out.stdout or "")
    return match.group(1) if match and match.group(1) else None


def command_env(base: Optional[Mapping[str, str]] = None, path: Optional[str] = None) -> dict[str, str]:
    """The environment for a project command: no /compute secrets, no PyInstaller leftovers."""
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if not k.startswith(("COMPUTE_", "_PYI", "_MEIPASS"))}
    for key in [k for k in env if k.endswith("_ORIG")]:
        env[key[:-5]] = env.pop(key)
    if path:
        env["PATH"] = path
    return env


def spawn_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000}
    return {"start_new_session": True}


def kill_tree(pid: int) -> None:
    if sys.platform != "win32":
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            child.kill()
        proc.kill()
    except psutil.Error:
        return


def clip(text: str, limit: int = OUTPUT_SHOW_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... ({len(text) - limit} characters cut) ...\n{text[-half:]}"


async def collect(stream: asyncio.StreamReader, buf: bytearray) -> None:
    while chunk := await stream.read(65536):
        room = OUTPUT_KEEP_BYTES - len(buf)
        if room > 0:
            buf.extend(chunk[:room])


async def run(command: str, root: Path, timeout_s: float = COMMAND_TIMEOUT_S,
              env: Optional[dict[str, str]] = None) -> CommandResult:
    """Run one shell command in the project folder; Stop (task cancel) and timeouts kill its whole tree."""
    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(root), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env if env is not None else command_env(path=login_path()),
        **spawn_kwargs())
    buf = bytearray()
    reader = asyncio.ensure_future(collect(proc.stdout, buf))
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        kill_tree(proc.pid)
        await proc.wait()
    except asyncio.CancelledError:
        kill_tree(proc.pid)
        reader.cancel()
        raise
    try:
        await asyncio.wait_for(reader, 2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        reader.cancel()
    output = clip(buf.decode("utf-8", errors="replace"))
    return CommandResult(proc.returncode, output, timed_out)
