"""
tiered_clean.py - Delete what a tiered paired benchmark run left behind.

Used through `quoin bench tiered --clean`. It lists the run directory, the
superseded rows of the same run id, the default spend ledger, the harness temp
roots and (on request) the repo mirror cache, asks once, then deletes. It never
starts a process, opens a network connection or calls a model.

Every candidate passes a safety filter first: it must sit inside the directory it
was derived from, must not be or contain the home directory, the checkout or a
path the listing says it keeps, must not lie inside ~/.claude or ~/.config, and a
run directory must look like a benchmark run. Symlinks are removed as links;
their targets are never touched.
"""

from __future__ import annotations

# Path bootstrap: mirrors tiered_entry.py so the script works when invoked directly.
import sys as _sys
from pathlib import Path as _Path
_repo_root = _Path(__file__).resolve().parent.parent.parent.parent  # .../quoin/
for _p in (str(_repo_root / "src"), str(_repo_root)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
try:
    import quoin as _q
    _inner = str(_repo_root / "quoin")
    if _inner not in _q.__path__:
        _q.__path__.append(_inner)
except Exception:
    pass

import os
import pwd
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TEMP_PREFIXES = ("qbench_", "qbc_")
TEMP_NAMES = ("quoin-benchmarks",)
SYSTEM_TMP = "/tmp"
# File and directory names a benchmark run directory holds; the manifest is written
# before the first cell, so even a run stopped early has one of them.
RUN_MARKERS = ("run-manifest.yaml", "summary.json", "summary.md", "simple-claude", "quoin-claude")

RUN_KINDS = ("run directory", "superseded rows")
LEDGER_KINDS = ("default ledger", "explicit ledger")
NOT_A_LEDGER = "not a ledger file"


@dataclass
class Candidate:
    path: Path
    kind: str
    root: Path
    is_link: bool
    note: str = ""


def _uid() -> int:
    return os.getuid()


def _location(path) -> Path:
    """The path with its parent resolved; the entry itself is never followed."""
    path = Path(os.path.abspath(Path(path).expanduser()))
    return Path(os.path.realpath(path.parent)) / path.name if path.name else path


def _real(path) -> Path:
    return Path(os.path.realpath(path))


def _inside(p: Path, root: Path) -> bool:
    return p == root or p.is_relative_to(root)


def _homes() -> list:
    homes = [_real(Path.home())]
    try:
        homes.append(_real(pwd.getpwuid(os.getuid()).pw_dir))
    except KeyError:
        pass
    return homes


def _config_trees() -> set:
    return {_real(home / name) for home in _homes() for name in (".claude", ".config")}


def protected_paths(repo_root: Path) -> set:
    from quoin.benchmarks.scripts import tiered_entry

    protected = set(_homes()) | _config_trees()
    protected |= {_real(repo_root), _real(tiered_entry.CHECKOUT_ROOT), Path("/")}
    return protected


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def _candidate(path: Path, kind: str, root: Path, note: str = "") -> Optional[Candidate]:
    st = _lstat(path)
    if st is None:
        return None
    return Candidate(path, kind, root, stat.S_ISLNK(st.st_mode), note)


def _is_ledger_file(c: Candidate, explicit: bool) -> bool:
    from quoin.benchmarks.scripts import spend_ledger

    st = _lstat(c.path)
    if st is None or not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
        return False
    if not explicit or c.path.suffix == ".jsonl":
        return True
    try:
        spend_ledger.recorded_total(c.path)
    except (OSError, ValueError):
        return False
    return True


def _ledger_note(path: Path) -> str:
    from quoin.benchmarks.scripts import spend_ledger

    try:
        return f"recorded ${spend_ledger.recorded_total(path):.2f}"
    except (OSError, ValueError):
        return "recorded total unreadable"


def _temp_roots() -> list:
    out = []
    roots = []
    for base in (tempfile.gettempdir(), SYSTEM_TMP):
        real = _real(base)
        if real.is_dir() and real not in roots:
            roots.append(real)
    for root in roots:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in sorted(entries, key=lambda e: e.name):
            if not (entry.name.startswith(TEMP_PREFIXES) or entry.name in TEMP_NAMES):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                continue
            if st.st_uid != _uid():
                continue
            out.append(Candidate(root / entry.name, "temp root", root, stat.S_ISLNK(st.st_mode)))
    return out


def collect(res, args) -> tuple:
    """(delete, skipped, kept): candidates, and (candidate, reason) pairs."""
    from quoin.benchmarks.harness import paired_run, repo_prep

    delete, skipped, kept = [], [], []
    run_root = _real(res.run_dir)
    for c in (
        _candidate(_location(run_root / res.run_id), "run directory", run_root),
        _candidate(_location(run_root / paired_run.SUPERSEDED_DIR / res.run_id),
                   "superseded rows", run_root),
    ):
        if c is not None:
            delete.append(c)

    default_ledger = _location(run_root / f"{res.run_id}.spend-ledger.jsonl")
    ledgers = [(default_ledger, "default ledger")]
    if args.spend_ledger is not None:
        explicit = _location(args.spend_ledger)
        if explicit != default_ledger:
            ledgers.append((explicit, "explicit ledger"))
    for path, kind in ledgers:
        c = _candidate(path, kind, path.parent)
        if c is None:
            continue
        if kind == "explicit ledger" and not args.clean_explicit_ledger:
            kept.append((c, "explicit --spend-ledger; pass --clean-explicit-ledger to delete"))
        elif not _is_ledger_file(c, explicit=kind == "explicit ledger"):
            skipped.append((c, NOT_A_LEDGER))
        else:
            c.note = _ledger_note(path)
            delete.append(c)

    delete.extend(_temp_roots())

    mirrors = _location(repo_prep._resolve_cache_root(None))
    c = _candidate(mirrors, "mirror cache", mirrors)
    if c is not None:
        if args.clean_mirrors:
            delete.append(c)
        else:
            kept.append((c, "mirror cache; pass --clean-mirrors to delete"))
    return delete, skipped, kept


def check(c: Candidate, protected: set, kept_paths: set, config_trees: set) -> Optional[str]:
    """The reason this candidate must not be deleted, or None."""
    if not _inside(c.path, c.root):
        return "outside its allowed root"
    if c.path in protected:
        return "protected path"
    if any(_inside(p, c.path) for p in protected):
        return "contains a protected path"
    if any(_inside(c.path, t) for t in config_trees):
        return "inside ~/.claude or ~/.config"
    if any(_inside(p, c.path) for p in kept_paths):
        return "contains a kept path (pass the flag to delete it)"
    if c.kind in RUN_KINDS and not c.is_link:
        if not any(_lstat(c.path / marker) is not None for marker in RUN_MARKERS):
            return "does not look like a benchmark run"
    return None


def _repair_at(pfd: int, name: str) -> None:
    """Give the owner rwx on a directory tree, top-down, without following links."""
    st = os.lstat(name, dir_fd=pfd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != _uid():
        return
    if st.st_mode & 0o700 != 0o700:
        os.chmod(name, stat.S_IMODE(st.st_mode) | 0o700, dir_fd=pfd)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pfd)
    try:
        with os.scandir(fd) as entries:
            children = [e.name for e in entries if e.is_dir(follow_symlinks=False)]
        for child in children:
            _repair_at(fd, child)
    finally:
        os.close(fd)


def _repair(parent: Path, name: str) -> None:
    pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _repair_at(pfd, name)
    finally:
        os.close(pfd)


def _remove(c: Candidate) -> None:
    try:
        st = os.lstat(c.path)
    except FileNotFoundError:
        # Already removed with a candidate that contained it.
        return
    if stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
        os.unlink(c.path)
    elif stat.S_ISDIR(st.st_mode):
        if c.kind in LEDGER_KINDS:
            raise OSError(NOT_A_LEDGER)
        # Harness run parents are left traverse-only, which rmtree cannot empty.
        _repair(c.path.parent, c.path.name)
        shutil.rmtree(c.path)
    else:
        raise OSError("not a file, directory or symlink")


def _ask(question: str, *, yes: bool, stdin=None, out=None) -> Optional[int]:
    """None to proceed, otherwise the exit code."""
    from quoin.benchmarks.scripts import tiered_entry

    stdin = sys.stdin if stdin is None else stdin
    out = sys.stdout if out is None else out
    if yes:
        print("confirmed by --yes", file=out)
        return None
    if not stdin.isatty():
        return tiered_entry._rb()._refuse("confirm", "no terminal to confirm on; pass --yes")
    out.write(f"{question} [y/N] ")
    out.flush()
    try:
        answer = stdin.readline()
    except EOFError:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return None
    print("declined", file=out)
    return 1


def _line(verb: str, c: Candidate, detail: str) -> str:
    suffix = f" ({detail})" if detail else ""
    return f"  {verb:<7} {c.kind}: {c.path}{suffix}"


def run_clean(args) -> int:
    from quoin.benchmarks.scripts import tiered_entry

    res = tiered_entry.resolve(args)
    if isinstance(res, int):
        return res
    delete, skipped, kept = collect(res, args)
    kept_paths = set()
    for c, _reason in kept + skipped:
        kept_paths |= {c.path, _real(c.path)}
    protected = protected_paths(res.repo_root)
    config_trees = _config_trees()
    accepted = []
    for c in delete:
        reason = check(c, protected, kept_paths, config_trees)
        if reason:
            skipped.append((c, reason))
        else:
            accepted.append(c)

    print(f"clean: run {res.run_id} in {res.run_dir} (do not clean while this run is active)")
    for c in accepted:
        print(_line("delete", c, c.note))
    for c, reason in kept:
        print(_line("keep", c, reason))
    for c, reason in skipped:
        print(_line("skip", c, reason))
    if not accepted:
        print("clean: nothing to delete")
        return 0

    rc = _ask(f"Delete {len(accepted)} paths?", yes=args.yes)
    if rc is not None:
        return rc
    failed = False
    deleted = 0
    worktree_root = False
    for c in accepted:
        try:
            _remove(c)
        except OSError as exc:
            print(f"clean: could not delete {c.path}: {exc}")
            failed = True
            continue
        deleted += 1
        worktree_root = worktree_root or (c.kind == "temp root" and c.path.name in TEMP_NAMES)
    print(f"clean: deleted {deleted} of {len(accepted)}")
    if worktree_root:
        print("clean: git worktrees registered from it can be pruned with "
              "`git worktree prune` in the arm checkout")
    return 1 if failed else 0
