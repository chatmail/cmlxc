import imaplib
import ipaddress
import itertools
import random
import shlex
import smtplib
import ssl
import subprocess
import time

from deltachat_rpc_client import DeltaChat


class ImapConn:
    def __init__(self, host, ssl_context=None):
        self.host = host
        self.ssl_context = ssl_context

    def connect(self):
        print(f"imap-connect {self.host}")
        self.conn = imaplib.IMAP4_SSL(self.host, ssl_context=self.ssl_context)

    def login(self, user, password):
        print(f"imap-login {user!r} {password!r}")
        self.conn.login(user, password)


class RelayAdmin:
    """Perform administrative actions (firewall, journalctl) on a relay container."""

    def __init__(self, host):
        self.host = host

        res = self._ssh(
            "command -v nft >/dev/null 2>&1"
            " || apt-get install -y nftables >/dev/null 2>&1;"
            " nft flush ruleset;"
            " journalctl -n0 --show-cursor -q",
        )
        cursor_line = res.stdout.strip().splitlines()[-1]
        self._journal_cursor = cursor_line.split(":", 1)[1].strip()

    def cleanup(self):
        self._ssh("nft flush ruleset")

    def _ssh(self, cmd, check=False, **kwargs):
        return subprocess.run(
            ["ssh", f"root@{self.host}", cmd],
            capture_output=True,
            text=True,
            check=check,
            **kwargs,
        )

    def ssh_run(self, cmd, check=True):
        remote_cmd = " ".join(shlex.quote(c) for c in cmd)
        print(f"ssh {self.host} {remote_cmd}")
        return self._ssh(remote_cmd, check=check)

    def get_journal_lines(self, grep=None):
        """Returns journal lines recorded since fixture creation."""
        cmd = ["journalctl", "--no-pager", "-q"]
        if self._journal_cursor:
            cmd.extend(["--after-cursor", self._journal_cursor])
        if grep:
            cmd.extend(["-g", grep])
        res = self.ssh_run(cmd, check=False)
        return res.stdout.strip()

    def wait_for_journal_match(self, grep, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            lines = self.get_journal_lines(grep=grep)
            if lines:
                return lines
            time.sleep(1)
        raise TimeoutError(
            f"no journal match for {grep!r} on {self.host} after {timeout}s"
        )

    def block_port(self, port):
        ruleset = (
            f"add table inet filter\n"
            f"add chain inet filter input"
            f" {{ type filter hook input priority 0; policy accept; }}\n"
            f"add rule inet filter input tcp dport {port} reject\n"
        )
        self._ssh("nft -f -", check=True, input=ruleset)
        # block already established / kept-alive connections between relays
        self._ssh(f"ss -K sport = {port}")


class SmtpConn:
    def __init__(self, host, ssl_context=None):
        self.host = host
        self.ssl_context = ssl_context

    def connect(self):
        print(f"smtp-connect {self.host}")
        context = self.ssl_context or ssl.create_default_context()
        self.conn = smtplib.SMTP_SSL(self.host, context=context)

    def login(self, user, password):
        print(f"smtp-login {user!r} {password!r}")
        self.conn.login(user, password)

    def sendmail(self, from_addr, to_addrs, msg):
        print(f"smtp-sendmail from={from_addr!r} to_addrs={to_addrs!r}")
        return self.conn.sendmail(from_addr=from_addr, to_addrs=to_addrs, msg=msg)


def _is_ip(domain):
    try:
        ipaddress.ip_address(domain)
        return True
    except ValueError:
        return False


def get_gencreds(domain):
    count = itertools.count(1)

    # RFC 5321 requires IP-literal domains to be bracketed.
    addr_domain = f"[{domain}]" if _is_ip(domain) else domain

    def gen():
        while 1:
            num = next(count)
            alphanumeric = "abcdefghijklmnopqrstuvwxyz1234567890"
            user = "".join(random.choices(alphanumeric, k=10))
            user = f"ac{num}_{user}"[:9]
            password = "".join(random.choices(alphanumeric, k=12))
            yield f"{user}@{addr_domain}", f"{password}"

    _g = gen()
    return lambda: next(_g)


class ChatmailACFactory:
    def __init__(self, rpc, maildomain, gencreds, ssl_context=None):
        self.dc = DeltaChat(rpc)
        self.rpc = rpc
        self.maildomain = maildomain
        self.gencreds = gencreds
        self.ssl_context = ssl_context

    def get_online_account(self):
        return self.get_online_accounts(1)[0]

    def get_online_accounts(self, num):
        accounts = []
        for _ in range(num):
            addr, password = self.gencreds()
            account = self.dc.add_account()
            if _is_ip(self.maildomain):
                # Use DCLOGIN scheme with explicit server hosts,
                # matching how madmail presents its addresses to users.
                qr = (
                    f"dclogin:{addr}"
                    f"?p={password}&v=1"
                    f"&ih={self.maildomain}&ip=993"
                    f"&sh={self.maildomain}&sp=465"
                    f"&ic=3&ss=default"
                )
                account.add_transport_from_qr(qr)
            else:
                config = {"addr": addr, "password": password}
                if self.ssl_context:
                    config["certificateChecks"] = "acceptInvalidCertificates"
                account.add_or_update_transport(config)
            account.set_config("delete_server_after", "10")
            account.bring_online()
            accounts.append(account)
        return accounts

    def get_accepted_chat(self, ac1, ac2):
        ac2.create_chat(ac1)
        return ac1.create_chat(ac2)


class CMSetup:
    def __init__(self, maildomain, gencreds, ssl_context):
        self.maildomain = maildomain
        self.gencreds = gencreds
        self.ssl_context = ssl_context

    def gen_users(self, num):
        users = []
        for _ in range(num):
            addr, password = self.gencreds()
            user = CMUser(self.maildomain, addr, password, self.ssl_context)
            assert user.smtp
            users.append(user)
        return users


class CMUser:
    def __init__(self, maildomain, addr, password, ssl_context=None):
        self.maildomain = maildomain
        self.addr = addr
        self.password = password
        self.ssl_context = ssl_context
        self._smtp = None
        self._imap = None

    @property
    def smtp(self):
        if not self._smtp:
            handle = SmtpConn(self.maildomain, ssl_context=self.ssl_context)
            handle.connect()
            handle.login(self.addr, self.password)
            self._smtp = handle
        return self._smtp

    @property
    def imap(self):
        if not self._imap:
            imap = ImapConn(self.maildomain, ssl_context=self.ssl_context)
            imap.connect()
            imap.login(self.addr, self.password)
            self._imap = imap
        return self._imap
