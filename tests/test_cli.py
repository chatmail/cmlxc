"""Unit tests for CLI helpers."""

import shutil
import subprocess
from pathlib import Path

import pytest

from cmlxc.container import SetupError
from cmlxc.driver_base import (
    SourceSpec,
    parse_source,
    resolve_source,
    validate_relay_name,
)
from cmlxc.driver_cmdeploy import get_ini_overrides

URL = "https://github.com/chatmail/relay.git"

# @latest lookup uses git ls-remote, skip according tests if absent.
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not on PATH")


def _git(cwd, *args):
    """Run a git subcommand in *cwd*, quietly."""
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def tagged_repo(tmp_path):
    """A real local git repo modelling the relay tag scheme."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, text=True)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    for tag in ("1.8.0", "1.9.0", "1.10.0-rc1", "withlmtp"):
        _git(repo, "tag", tag)
    return str(repo)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("@main", SourceSpec("remote", url=URL, ref="main")),
        ("@fix-dovecot", SourceSpec("remote", url=URL, ref="fix-dovecot")),
        ("@v2.1", SourceSpec("remote", url=URL, ref="v2.1")),
        ("@latest", SourceSpec("remote", url=URL, ref="latest")),
        ("/home/me/relay", SourceSpec("local", path=Path("/home/me/relay"))),
        ("./relay", SourceSpec("local", path=Path("./relay"))),
        ("../relay", SourceSpec("local", path=Path("../relay"))),
        (
            "https://github.com/fork/relay.git@my-branch",
            SourceSpec("remote", url="https://github.com/fork/relay.git", ref="my-branch"),
        ),
        ("hpk/new-lxc-test", SourceSpec("remote", url=URL, ref="hpk/new-lxc-test")),
    ],
)
def test_parse_source(value, expected):
    assert parse_source(value, URL) == expected


@pytest.mark.parametrize("value", ["main", "some-word"])
def test_parse_source_rejects_invalid(value):
    with pytest.raises(ValueError, match="Invalid SOURCE"):
        parse_source(value, URL)


@pytest.mark.parametrize("bad", [".", "..", "../relay", "/path", "a/b", "a.b"])
def test_validate_relay_name_rejects_invalid(bad):
    with pytest.raises(ValueError, match="Invalid relay name"):
        validate_relay_name(bad)


@pytest.mark.parametrize("good", ["cm0", "relay-1", "t0", "mad2-noinsecure"])
def test_validate_relay_name_accepts_valid(good):
    validate_relay_name(good)


def test_ini_overrides_lift_resource_gates():
    """Loaded CI runners must not make relays refuse new addresses."""
    overrides = get_ini_overrides("cm0.localchat")
    assert overrides["max_load_1m"] >= 1000
    assert overrides["min_available_memory"] == "1M"
    assert overrides["min_free_disk_space"] == "1M"


def test_ini_overrides_disable_ipv6():
    assert "disable_ipv6" not in get_ini_overrides("cm0.localchat")
    overrides = get_ini_overrides("cm0.localchat", disable_ipv6=True)
    assert overrides["disable_ipv6"] == "True"


@requires_git
def test_source_latest(tagged_repo):
    """@latest resolves to the newest stable tag via real git ls-remote."""
    assert resolve_source(parse_source("@latest", tagged_repo)) == SourceSpec("remote", url=tagged_repo, ref="1.9.0")


@requires_git
def test_source_main(tagged_repo):
    """@main is a branch, not a tag, passed through"""
    assert resolve_source(parse_source("@main", tagged_repo)) == SourceSpec("remote", url=tagged_repo, ref="main")


@requires_git
def test_source_latest_no_tags(tmp_path):
    """@latest against a real repo with no tags fails with SetupError."""
    empty = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(empty)], check=True, capture_output=True, text=True)
    with pytest.raises(SetupError, match="No tags found"):
        resolve_source(parse_source("@latest", str(empty)))


@requires_git
def test_source_latest_bad_repo(tmp_path):
    """@latest against a nonexistent repo fails with SetupError."""
    with pytest.raises(SetupError, match="Failed to resolve @latest"):
        resolve_source(parse_source("@latest", str(tmp_path / "does-not-exist")))
