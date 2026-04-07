"""Lightweight unit tests for pure logic in cmlxc.incus."""

import pytest

from cmlxc.incus import (
    ContainerBuilder,
    DeployConflictError,
    DNSContainer,
    Incus,
    RelayContainer,
    _extract_ip,
    _is_ip_address,
    format_ssh_config,
)
from cmlxc.output import Out


@pytest.fixture(scope="module")
def ix():
    return Incus(Out())


def test_extract_ip():
    net = {
        "lo": {
            "addresses": [
                {"family": "inet", "address": "127.0.0.1", "scope": "global"},
            ]
        },
        "eth0": {
            "addresses": [
                {"family": "inet", "address": "10.0.0.5", "scope": "global"},
                {"family": "inet6", "address": "fd42::1", "scope": "global"},
                {"family": "inet", "address": "169.254.1.1", "scope": "link"},
            ]
        },
    }
    assert _extract_ip(net, "inet") == "10.0.0.5"
    assert _extract_ip(net, "inet6") == "fd42::1"
    # loopback is skipped even when its scope is global
    assert _extract_ip({"lo": net["lo"]}) is None
    # link-local only → None
    link_only = {"eth0": {"addresses": [net["eth0"]["addresses"][2]]}}
    assert _extract_ip(link_only) is None
    # empty dict → None
    assert _extract_ip({}) is None


def test_is_ip_address():
    assert _is_ip_address("10.0.0.1") is True
    assert _is_ip_address("fd42::1") is True
    assert _is_ip_address("_t0.localchat") is False
    assert _is_ip_address("hostname") is False


def test_get_container_name():
    assert Incus.get_container_name("cm0") == "cm0-localchat"
    assert Incus.get_container_name("cm0-localchat") == "cm0-localchat"


def test_relay_container_naming(ix):
    ct = RelayContainer(ix, "t0")
    assert ct.sname == "t0"
    assert ct.name == "t0-localchat"
    assert ct.domain == "_t0.localchat"
    assert ct.relay_dir == ix.config_dir / "t0"
    assert ct.ini == ct.relay_dir / "chatmail.ini"
    assert ct.zone == ct.relay_dir / "chatmail.zone"


def test_get_container_dispatch(ix):
    assert isinstance(ix.get_container("ns-localchat"), DNSContainer)
    assert isinstance(ix.get_container("builder-localchat"), ContainerBuilder)
    ct = ix.get_container("t0")
    assert isinstance(ct, RelayContainer)
    assert ct.sname == "t0"


def test_format_ssh_config():
    containers = [
        {"name": "t0-localchat", "ip": "10.0.0.5", "domain": "_t0.localchat"},
        {"name": "ns-localchat", "ip": None, "domain": "ns.localchat"},
    ]
    text = format_ssh_config(containers, "/tmp/id_test")
    assert "Host t0-localchat _t0.localchat _t0" in text
    assert "Hostname 10.0.0.5" in text
    assert "IdentityFile /tmp/id_test" in text
    # container without IP is skipped
    assert "ns-localchat" not in text


def test_check_deploy_lock(ix):
    ct = RelayContainer(ix, "deploylock-test")
    # no prior state — should not raise
    ct.check_deploy_lock("cmdeploy")

    # same driver — should not raise
    ct._deploy_state_override = {"driver": "cmdeploy", "timestamp": "now"}
    original = ct.get_deploy_state
    ct.get_deploy_state = lambda: ct._deploy_state_override
    ct.check_deploy_lock("cmdeploy")

    # different driver — should raise
    ct._deploy_state_override = {"driver": "madmail", "timestamp": "now"}
    with pytest.raises(DeployConflictError, match="madmail"):
        ct.check_deploy_lock("cmdeploy")

    ct.get_deploy_state = original
