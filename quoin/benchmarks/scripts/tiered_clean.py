"""
tiered_clean.py - Delete what a tiered paired benchmark run left behind.

Used through `quoin bench tiered --clean`. It lists the run directory, the
superseded rows of the same run id, the default spend ledger, the harness temp
roots and (on request) the repo mirror cache, asks once, then deletes. It never
starts a process, opens a network connection or calls a model.

Every candidate passes a safety filter first, and again just before it is
deleted: it must sit inside the directory it was derived from, must not be or
contain the home directory, the checkout or a path the listing says it keeps,
must not lie inside ~/.claude or ~/.config, and a run directory must look like a
benchmark run. The filter compares file identities (device and inode), not path
strings, so a differently spelled path to the same directory (a case variant on
a case-insensitive file system, a `..` detour) is still recognised. Symlinks are
removed as links; their targets are never touched.

Mirrors are listed only from a mirror cache that itself passes the filter, and
only entries shaped like the ones repo_prep writes (`<owner>__<repo>.git` bare
repositories and their `.partial-<pid>` leftovers). A `.git` directory, a
worktree's `.git` file or any other `*.git` name is never a mirror.
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
import re
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
# The manifest is written before the first cell, so even a run stopped early has
# it; superseded rows hold only arm cell directories.
RUN_MANIFEST = "run-manifest.yaml"
# Entries repo_prep leaves in the mirror cache: `<owner>__<repo>.git` mirrors and
# `.git.partial-<pid>` clones interrupted before their rename.
MIRROR_NAME = re.compile(r"^[^/]+__[^/]+\.git$")
MIRROR_PARTIAL_NAME = re.compile(r"^[^/]+__[^/]+\.git\.partial-\d+$")

RUN_KINDS = ("run directory", "superseded rows")
MIRROR_ROOT_KIND = "mirror cache"
LEDGER_KINDS = ("default ledger", "explicit ledger")
NOT_A_LEDGER = "not a ledger file"


@dataclass
class Candidate:
    path: Path
    kind: str
    root: Path
    is_link: bool
    note: str = ""
    # The candidate this one was listed from; it is only deleted while that passes too.
    parent: Optional["Candidate"] = None


def _uid() -> int:
    return os.getuid()


def _location(path) -> Path:
    """The path with its parent resolved; the entry itself is never followed."""
    path = Path(os.path.abspath(Path(path).expanduser()))
    return Path(os.path.realpath(path.parent)) / path.name if path.name else path


def _real(path) -> Path:
    return Path(os.path.realpath(path))


def _ident(path, follow: bool = True):
    """(st_dev, st_ino) of the path, or None when it cannot be stat'ed."""
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _up(path) -> set:
    """Identities of the directory the path resolves to and every directory above it."""
    ids = set()
    p = _real(path)
    while True:
        i = _ident(p)
        if i is not None:
            ids.add(i)
        if p.parent == p:
            return ids
        p = p.parent


def _chain(path) -> set:
    """Identities of the entry itself (not followed) and every directory above it."""
    path = Path(os.path.abspath(path))
    ids = _up(path.parent)
    own = _ident(path, follow=False)
    if own is not None:
        ids.add(own)
    return ids


@dataclass
class Guard:
    protected: set      # identities of protected paths
    holds_protected: set  # identities of protected paths and every directory above them
    config: set         # identities of ~/.claude and ~/.config
    holds_kept: set     # identities of kept paths and every directory above them


def guard_for(protected, kept_paths, config_trees) -> Guard:
    prot = {i for i in (_ident(p) for p in protected) if i is not None}
    holds = set()
    for p in protected:
        holds |= _up(p)
    kept = set()
    for p in kept_paths:
        kept |= _chain(p) | _up(p)
    cfg = {i for i in (_ident(t) for t in config_trees) if i is not None}
    return Guard(prot, holds, cfg, kept)


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


def _ledger_note(c: Candidate, explicit: bool) -> Optional[str]:
    """The listing note for a ledger file, or None when the path is not a ledger."""
    from quoin.benchmarks.scripts import spend_ledger

    st = _lstat(c.path)
    if st is None or not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
        return None
    try:
        return f"recorded ${spend_ledger.recorded_total(c.path):.2f}"
    except (OSError, ValueError):
        # An unreadable default (or explicit .jsonl) ledger is still deletable; any
        # other explicit file must parse as a ledger.
        if explicit and c.path.suffix != ".jsonl":
            return None
        return "recorded total unreadable"


def _homes_above(path) -> bool:
    homes = {i for i in (_ident(h) for h in _homes()) if i is not None}
    return bool(homes & _up(path))


def _temp_roots() -> list:
    out = []
    roots = []
    for base in (tempfile.gettempdir(), SYSTEM_TMP):
        real = _real(base)
        if not real.is_dir() or any(_ident(real) == _ident(r) for r in roots):
            continue
        # A temp directory pointed at home (or inside it) would turn the prefix
        # scan loose on the user's own files.
        if _homes_above(real):
            continue
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
        else:
            note = _ledger_note(c, explicit=kind == "explicit ledger")
            if note is None:
                skipped.append((c, NOT_A_LEDGER))
            else:
                c.note = note
                delete.append(c)

    delete.extend(_temp_roots())

    mirrors = _location(repo_prep._resolve_cache_root(None))
    c = _candidate(mirrors, MIRROR_ROOT_KIND, mirrors.parent)
    if c is not None:
        if not args.clean_mirrors:
            kept.append((c, "mirror cache; pass --clean-mirrors to delete"))
        elif c.is_link:
            kept.append((c, "mirror cache is a symlink; its target is not cleaned"))
        else:
            delete.extend(_mirror_entries(c))
    return delete, skipped, kept


def _is_mirror(entry) -> bool:
    """True for a directory named and shaped like a mirror repo_prep writes."""
    name = entry.name
    full = MIRROR_NAME.match(name) is not None
    if not (full or MIRROR_PARTIAL_NAME.match(name)):
        return False
    try:
        if not entry.is_dir(follow_symlinks=False):
            return False
    except OSError:
        return False
    if not full:
        # An interrupted clone may stop before it has written anything.
        return True
    # A finished mirror is a bare repository.
    path = Path(entry.path)
    head, objects = _lstat(path / "HEAD"), _lstat(path / "objects")
    return (head is not None and stat.S_ISREG(head.st_mode)
            and objects is not None and stat.S_ISDIR(objects.st_mode))


def _mirror_entries(root: Candidate) -> list:
    """The mirrors inside the cache, then the cache itself (removed only if left empty).

    Every mirror carries the cache as its parent, so none is deleted when the cache
    itself fails the safety filter. Anything that is not a mirror is left alone.
    """
    out = []
    base = _real(root.path)
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        if not _is_mirror(entry):
            continue
        c = _candidate(base / entry.name, "mirror", base)
        if c is not None:
            c.parent = root
            out.append(c)
    root.note = "only if empty afterwards"
    out.append(root)
    return out


def _looks_like_run(path: Path) -> bool:
    from quoin.benchmarks.harness import paired_run

    st = _lstat(path / RUN_MANIFEST)
    if st is not None and stat.S_ISREG(st.st_mode):
        return True
    for arm in paired_run.PAIR_ORDER:
        st = _lstat(path / arm)
        if st is not None and stat.S_ISDIR(st.st_mode):
            return True
    return False


def check(c: Candidate, guard: Guard) -> Optional[str]:
    """The reason this candidate must not be deleted, or None."""
    if c.parent is not None:
        # The cache is checked first: no entry is listed from a cache that fails.
        reason = check(c.parent, guard)
        if reason:
            return f"inside a skipped {c.parent.kind} ({reason})"
    own = _ident(c.path, follow=False)
    if own is None:
        return None
    chain = _chain(c.path)
    root = _ident(c.root)
    if root is None or root not in chain:
        return "outside its allowed root"
    if own in guard.protected:
        return "protected path"
    if own in guard.holds_protected:
        return "contains a protected path"
    if chain & guard.config:
        return "inside ~/.claude or ~/.config"
    if own in guard.holds_kept:
        return "contains a kept path (pass the flag to delete it)"
    if c.kind in RUN_KINDS and not c.is_link and not _looks_like_run(c.path):
        return "does not look like a benchmark run"
    return None


def _chmod_entry(dfd: int, entry: str, st, mode: int) -> None:
    """chmod one directory entry without ever following a link swapped in for it."""
    if _CHMOD_NOFOLLOW:
        os.chmod(entry, mode, dir_fd=dfd, follow_symlinks=False)
        return
    # Linux has no no-follow chmod: pin the inode with O_PATH, check it is still the
    # directory that was stat'ed, and chmod it through its /proc fd link.
    o_path = getattr(os, "O_PATH", None)
    if o_path is None or not os.path.isdir("/proc/self/fd"):
        return  # left as is; rmtree then reports what it cannot delete
    fd = os.open(entry, o_path | os.O_NOFOLLOW, dir_fd=dfd)
    try:
        pinned = os.fstat(fd)
        if (stat.S_ISDIR(pinned.st_mode)
                and (pinned.st_dev, pinned.st_ino) == (st.st_dev, st.st_ino)):
            os.chmod(f"/proc/self/fd/{fd}", mode)
    finally:
        os.close(fd)


def _repair_at(pfd: int, name: str) -> None:
    """Give the owner rwx on a directory tree, top-down, without following links.

    Refuses (OSError) a tree with a directory on another file system, so the
    rmtree that follows never reaches into a mounted volume. One fd is held per
    directory level, so a tree deeper than the fd limit fails with EMFILE and is
    reported as not deleted; rmtree has the same limit.
    """
    # An explicit stack: a very deep tree must not hit the recursion limit.
    stack = [(pfd, name)]
    top_dev = None
    try:
        while stack:
            dfd, entry = stack.pop()
            if entry is None:
                os.close(dfd)
                continue
            st = os.lstat(entry, dir_fd=dfd)
            if not stat.S_ISDIR(st.st_mode):
                continue
            if top_dev is None:
                top_dev = st.st_dev
            elif st.st_dev != top_dev:
                raise OSError(f"{entry} is on another file system")
            if st.st_uid != _uid():
                continue
            if st.st_mode & 0o700 != 0o700:
                _chmod_entry(dfd, entry, st, stat.S_IMODE(st.st_mode) | 0o700)
            fd = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            try:
                opened = os.fstat(fd)
            except OSError:
                os.close(fd)
                raise
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                os.close(fd)
                continue
            stack.append((fd, None))
            with os.scandir(fd) as entries:
                children = [e.name for e in entries if e.is_dir(follow_symlinks=False)]
            stack.extend((fd, child) for child in children)
    finally:
        for dfd, entry in stack:
            if entry is None:
                os.close(dfd)


_CHMOD_NOFOLLOW = os.chmod in os.supports_follow_symlinks


def _repair(parent: Path, name: str) -> None:
    pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _repair_at(pfd, name)
    finally:
        os.close(pfd)


def _remove(c: Candidate) -> bool:
    """Delete the candidate; False when it is left in place on purpose."""
    try:
        st = os.lstat(c.path)
    except FileNotFoundError:
        # Already removed with a candidate that contained it.
        return True
    if c.kind == MIRROR_ROOT_KIND:
        if stat.S_ISDIR(st.st_mode) and not os.listdir(c.path):
            os.rmdir(c.path)
            return True
        return False
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
    return True


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
    parts = (["link"] if c.is_link else []) + ([detail] if detail else [])
    suffix = f" ({'; '.join(parts)})" if parts else ""
    return f"  {verb:<7} {c.kind}: {c.path}{suffix}"


def run_clean(args) -> int:
    from quoin.benchmarks.scripts import tiered_entry

    res = tiered_entry.resolve(args)
    if isinstance(res, int):
        return res
    delete, skipped, kept = collect(res, args)
    kept_paths = [c.path for c, _reason in kept + skipped]
    guard = guard_for(protected_paths(res.repo_root), kept_paths, _config_trees())
    accepted = []
    for c in delete:
        reason = check(c, guard)
        if reason:
            skipped.append((c, reason))
        else:
            accepted.append(c)

    print(f"clean: run {res.run_id} in {res.run_dir} (do not clean while any "
          "benchmark run is active: the harness temp directories are shared by every run)")
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
        # The answer may have come long after the listing; check again right before.
        reason = check(c, guard)
        if reason:
            print(f"clean: skipped {c.path}: {reason}")
            continue
        try:
            removed = _remove(c)
        except (OSError, RecursionError) as exc:
            print(f"clean: could not delete {c.path}: {exc}")
            failed = True
            continue
        if not removed:
            print(f"clean: kept {c.path} (not empty)")
            continue
        deleted += 1
        worktree_root = worktree_root or (c.kind == "temp root" and c.path.name in TEMP_NAMES)
    print(f"clean: deleted {deleted} of {len(accepted)}")
    if worktree_root:
        print("clean: git worktrees registered from it can be pruned with "
              "`git worktree prune` in the arm checkout")
    return 1 if failed else 0
