"""Functional test — exercises the complete cmlxc workflow in the live system.

Uses and destroys ``fulltest0`` and ``fulltest1`` containers.

Run with::

    pytest tests/fullrun.py -v -x -s

"""

import shutil
import subprocess

import pytest

from cmlxc.container import BASE_IMAGE_ALIAS, BuilderContainer, DNSContainer
from cmlxc.incus import Incus
from cmlxc.output import Out

CT0 = "fulltest0"
CT1 = "fulltest1"


def cmlxc(*args):
    """Run ``cmlxc <args>`` as a subprocess, assert exit code 0."""
    print(f"$ cmlxc {' '.join(args)}")
    result = subprocess.run(
        ["cmlxc", *args],
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"cmlxc {' '.join(args)} failed (exit {result.returncode})")


@pytest.fixture(scope="module", autouse=True)
def _module_setup():
    """Destroy test containers after all tests complete."""
    if not shutil.which("incus"):
        pytest.skip("incus is not installed or in the $PATH")

    yield
    subprocess.run(
        ["cmlxc", "destroy", CT0, CT1],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


# ---- init / status ---------------------------------------------------------


def test_init():
    ix = Incus(Out())
    dns_ct = DNSContainer(ix)
    bld_ct = BuilderContainer(ix)
    if dns_ct.is_running and bld_ct.is_running and ix.find_image([BASE_IMAGE_ALIAS]):
        pytest.skip("already initialized")
    cmlxc("init")
    assert ix.ssh_config_path.exists()


def test_status():
    cmlxc("status")


# ---- cmdeploy cycle -------------------------------------------------------


def test_cm_deploy():
    cmlxc("deploy-cmdeploy", "--source", "@main", CT0)
    cmlxc("deploy-cmdeploy", "--source", "@main", CT1)


def test_mini_cmdeploy():
    cmlxc("test-mini", CT0)


def test_cm_test():
    cmlxc("test-cmdeploy", CT0, CT1)


# ---- stop / destroy -------------------------------------------------------


def test_stop():
    cmlxc("stop", CT0, CT1)


def test_destroy():
    cmlxc("destroy", CT0, CT1)


# ---- madmail cycle ---------------------------------------------------------


def test_mad_deploy():
    cmlxc("deploy-madmail", "--source", "@main", "--ipv4-only", CT0)


def test_mini_madmail():
    cmlxc("test-mini", CT0)


def test_destroy_madmail():
    cmlxc("destroy", CT0)
