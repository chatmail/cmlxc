import ssl

import imap_tools
import pytest
from deltachat_rpc_client import Rpc
from support import ChatmailACFactory, CMSetup, get_gencreds


def pytest_addoption(parser):
    parser.addoption(
        "--relay1", action="store", required=True, help="First relay to test"
    )
    parser.addoption(
        "--relay2", action="store", default=None, help="Second relay to test"
    )


def get_ssl_context(maildomain):
    if (
        maildomain.startswith("_")
        or maildomain.startswith("10.")
        or maildomain.startswith("172.")
        or maildomain.startswith("192.168.")
        or maildomain == "localhost"
        or maildomain == "127.0.0.1"
    ):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


@pytest.fixture(scope="session")
def maildomain(pytestconfig):
    return pytestconfig.getoption("relay1")


@pytest.fixture(scope="session")
def maildomain2(pytestconfig):
    domain = pytestconfig.getoption("relay2")
    if not domain:
        pytest.skip("set relay2 to run two-relay tests")
    return domain


@pytest.fixture(scope="session")
def ssl_context(maildomain):
    return get_ssl_context(maildomain)


@pytest.fixture(scope="session")
def ssl_context2(maildomain2):
    return get_ssl_context(maildomain2)


@pytest.fixture(scope="session")
def gencreds(maildomain):
    return get_gencreds(maildomain)


@pytest.fixture(scope="session")
def gencreds2(maildomain2):
    return get_gencreds(maildomain2)


@pytest.fixture(scope="session")
def rpc(tmp_path_factory):
    accounts_dir = str(tmp_path_factory.mktemp("dc") / "accounts")
    rpc = Rpc(accounts_dir=accounts_dir)
    rpc.start()
    yield rpc
    rpc.close()


@pytest.fixture
def cmfactory(rpc, maildomain, gencreds, ssl_context):
    return ChatmailACFactory(rpc, maildomain, gencreds, ssl_context)


@pytest.fixture
def cmfactory2(rpc, maildomain2, gencreds2, ssl_context2):
    return ChatmailACFactory(rpc, maildomain2, gencreds2, ssl_context2)


@pytest.fixture
def cmsetup(maildomain, gencreds, ssl_context):
    return CMSetup(maildomain, gencreds, ssl_context)


@pytest.fixture
def lp():
    class LP:
        def sec(self, msg):
            print(f"---- {msg} ----")

        def indent(self, msg):
            print(f"     {msg}")

    return LP()


@pytest.fixture
def imap_mailbox(cmfactory, ssl_context):
    (ac1,) = cmfactory.get_online_accounts(1)
    user = ac1.get_config("addr")
    password = ac1.get_config("mail_pw")
    host = user.split("@")[1]
    mailbox = imap_tools.MailBox(host, ssl_context=ssl_context)
    mailbox.login(user, password)
    mailbox.dc_ac = ac1
    return mailbox
