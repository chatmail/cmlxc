"""Unit tests for CLI helpers."""

from pathlib import Path

import pytest

from cmlxc.driver_base import SourceSpec, parse_source

URL = "https://github.com/chatmail/relay.git"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("@main", SourceSpec("remote", url=URL, ref="main")),
        ("@fix-dovecot", SourceSpec("remote", url=URL, ref="fix-dovecot")),
        ("@v2.1", SourceSpec("remote", url=URL, ref="v2.1")),
        ("/home/me/relay", SourceSpec("local", path=Path("/home/me/relay"))),
        ("./relay", SourceSpec("local", path=Path("./relay"))),
        ("../relay", SourceSpec("local", path=Path("../relay"))),
        (
            "https://github.com/fork/relay.git@my-branch",
            SourceSpec(
                "remote", url="https://github.com/fork/relay.git", ref="my-branch"
            ),
        ),
    ],
)
def test_parse_source(value, expected):
    assert parse_source(value, URL) == expected


@pytest.mark.parametrize("value", ["main", "some-word", "https://example.com/repo.git"])
def test_parse_source_rejects_invalid(value):
    with pytest.raises(ValueError, match="Invalid SOURCE"):
        parse_source(value, URL)
