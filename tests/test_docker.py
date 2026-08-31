"""Unit tests for Docker driver helpers."""

from inspect import signature

import pytest

from cmlxc.cli import DRIVER_BY_NAME, get_parser
from cmlxc.container import SetupError
from cmlxc.driver_base import Driver
from cmlxc.driver_docker import DockerDriver, _parse_inject_tag


@pytest.mark.parametrize("hook", ["on_init_relay", "run_deploy", "init_builder"])
def test_driver_hooks_match_base(hook):
    """Every driver's hooks must be callable the way the base class calls them."""
    base = signature(getattr(Driver, hook))
    for name, cls in DRIVER_BY_NAME.items():
        assert signature(getattr(cls, hook)) == base, f"{name}.{hook}"


@pytest.mark.parametrize(
    "sub, argv",
    [
        ("deploy", ["docker", "deploy", "dk0", "--source", "ghcr:main"]),
        ("pull", ["docker", "pull", "dk0", "--tag", "main"]),
        ("logs", ["docker", "logs", "dk0", "-f"]),
        ("ps", ["docker", "ps", "dk0"]),
        ("shell", ["docker", "shell", "dk0", "chatmail", "ls"]),
    ],
)
def test_docker_subcommand_tree_builds(sub, argv):
    args = get_parser().parse_args(argv)
    assert callable(args.func)


def test_docker_deploy_without_source_is_rejected():
    """A bare `docker deploy NAME` must not silently deploy nothing.

    As argparse accepts it the rejection must happens in configure_from_args
    """
    args = get_parser().parse_args(["docker", "deploy", "dk0"])
    assert args.source == ""
    driver = DockerDriver.__new__(DockerDriver)
    with pytest.raises(SetupError, match="Specify an image"):
        driver.configure_from_args(args)


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "deploy", "dk0", "--source", "ghcr:main", "--image", "/tmp/i.tar"],
        ["docker", "deploy", "dk0", "--source", "docker:tag", "--image", "/tmp/i.tar"],
    ],
)
def test_docker_deploy_image_and_source_are_exclusive(argv):
    args = get_parser().parse_args(argv)
    driver = DockerDriver.__new__(DockerDriver)
    with pytest.raises(SetupError, match="mutually exclusive"):
        driver.configure_from_args(args)


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
