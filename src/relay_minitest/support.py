import imaplib
import itertools
import random
import smtplib
import ssl

from deltachat_rpc_client import DeltaChat


class ImapConn:
    AuthError = imaplib.IMAP4.error

    def __init__(self, host, ssl_context=None):
        self.host = host
        self.ssl_context = ssl_context

    def connect(self):
        print(f"imap-connect {self.host}")
        self.conn = imaplib.IMAP4_SSL(self.host, ssl_context=self.ssl_context)

    def login(self, user, password):
        print(f"imap-login {user!r} {password!r}")
        self.conn.login(user, password)

    def fetch_all(self):
        status, res = self.conn.select()
        if int(res[0]) == 0:
            return []
        status, results = self.conn.fetch("1:*", "(RFC822)")
        assert status == "OK"
        return results

    def fetch_all_messages(self):
        results = self.fetch_all()
        messages = []
        for item in results:
            if len(item) == 2:
                messages.append(item[1].decode())
        return messages


class SmtpConn:
    AuthError = smtplib.SMTPAuthenticationError

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


def get_gencreds(domain):
    count = itertools.count(1)

    def gen():
        while 1:
            num = next(count)
            alphanumeric = "abcdefghijklmnopqrstuvwxyz1234567890"
            user = "".join(random.choices(alphanumeric, k=10))
            user = f"ac{num}_{user}"[:9]
            password = "".join(random.choices(alphanumeric, k=12))
            yield f"{user}@{domain}", f"{password}"

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
        for i in range(num):
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
