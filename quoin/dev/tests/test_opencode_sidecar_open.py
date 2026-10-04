"""`read_sidecar` opens only a regular file it can read without blocking."""
from __future__ import annotations

import os

import pytest

from quoin.opencode_adapter import runstore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="FIFOs and symlink flags are POSIX-only")


def test_a_fifo_in_place_of_the_sidecar_is_unreadable_not_a_hang(tmp_path):
    path = tmp_path / "run.jsonl"
    os.mkfifo(str(path))
    with pytest.raises(runstore.RunStoreError) as caught:
        runstore.read_sidecar(path)
    assert "unreadable-sidecar" in str(caught.value)


def test_a_symlinked_sidecar_is_unreadable(tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    link = tmp_path / "run.jsonl"
    os.symlink(str(target), str(link))
    with pytest.raises(runstore.RunStoreError):
        runstore.read_sidecar(link)


def test_a_missing_sidecar_is_empty_and_a_regular_one_reads(tmp_path):
    assert runstore.read_sidecar(tmp_path / "none.jsonl").events == ()
    path = tmp_path / "run.jsonl"
    path.write_text("")
    assert runstore.read_sidecar(path).events == ()
