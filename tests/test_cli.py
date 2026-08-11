"""Unit tests for CLI helpers."""

from pathlib import Path

import pytest

from cmlxc.driver_base import (
    SourceSpec,
    latest_release_tag,
    parse_source,
    validate_relay_name,
)
from cmlxc.driver_cmdeploy import get_ini_overrides
from cmlxc.driver_madmail import release_asset_url

URL = "https://github.com/chatmail/relay.git"


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
            SourceSpec(
                "remote", url="https://github.com/fork/relay.git", ref="my-branch"
            ),
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


@pytest.mark.parametrize(
    "tag_output, expected",
    [
        # `git tag -l --sort=-v:refname` output, newest first
        ("v2.23.0\nv2.22.1\nv2.2.2\n", "v2.23.0"),
        # pre-releases and non-semver tags are skipped
        ("v2.24.0-rc1\nv2.23.0\n", "v2.23.0"),
        ("test\nlatest\nv1.0.0\n", "v1.0.0"),
        # tags without the v prefix still count
        ("2.23.0\n", "2.23.0"),
        ("", None),
        (None, None),
    ],
)
def test_latest_release_tag(tag_output, expected):
    assert latest_release_tag(tag_output) == expected


@pytest.mark.parametrize(
    "tag, arch, expected_asset",
    [
        ("v2.23.0", "amd64", "madmail-linux-amd64-musl"),
        ("v2.23.0", "arm64", "madmail-linux-arm64-musl"),
        # not an exact release commit -> build from source
        ("v2.23.0-dirty", "amd64", None),
        ("v2.23.0-4-gabc1234", "amd64", None),
        (None, "amd64", None),
        ("", "amd64", None),
        ("v2.23.0", "riscv64", None),
    ],
)
def test_release_asset_url(tag, arch, expected_asset):
    url = release_asset_url(tag, arch)
    if expected_asset is None:
        assert url is None
    else:
        assert url.endswith(f"/v2.23.0/{expected_asset}")
