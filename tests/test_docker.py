"""Unit tests for Docker driver helpers."""

import pytest

from cmlxc.driver_docker import _parse_inject_tag


@pytest.mark.parametrize(
    "value, expected",
    [
        ("docker:main", "main"),
        ("docker:image:version", "image:version"),
        ("docker:registry/repo:tag", "registry/repo:tag"),
        ("docker:sha256:abc123", "sha256:abc123"),
    ],
)
def test_parse_inject_tag_accepts_valid(value, expected):
    assert _parse_inject_tag(value) == expected


@pytest.mark.parametrize(
    "value",
    ["ghcr:main", "@main", "", "main"],
)
def test_parse_inject_tag_returns_none_for_non_docker(value):
    assert _parse_inject_tag(value) is None


@pytest.mark.parametrize(
    "value",
    ["docker:", "docker: ", "docker:tag with spaces", "docker:tag!bad"],
)
def test_parse_inject_tag_rejects_invalid_tag(value):
    with pytest.raises(ValueError, match="Invalid Docker tag"):
        _parse_inject_tag(value)
