import imaplib
import ipaddress
import smtplib
import ssl
from email.mime.text import MIMEText

import imap_tools
import pytest
import requests


def test_login_basic_functioning(cmsetup, lp):
    """Test that an initial login creates a user automatically"""
    lp.sec("creating user and checking auto-creation")
    user1 = cmsetup.gen_users(1)[0]
    assert user1.addr
    assert user1.smtp
    assert user1.imap
    lp.indent(f"successfully logged in as {user1.addr}")


class TestEndToEndDeltaChat:
    "Tests that use Delta Chat accounts on the chat mail instance."

    def test_one_on_one(self, cmfactory, lp):
        """Test that a DC account can send a message to a second DC account"""
        ac1, ac2 = cmfactory.get_online_accounts(2)
        chat = cmfactory.get_accepted_chat(ac1, ac2)
        chat.send_text("message0")

        lp.sec("wait for ac2 to receive message")
        msg2 = ac2.wait_for_incoming_msg()
        assert msg2.get_snapshot().text == "message0"

    def test_send_dot(self, cmfactory, lp):
        """Test that a single dot is properly escaped in SMTP protocol"""
        ac1, ac2 = cmfactory.get_online_accounts(2)
        chat = cmfactory.get_accepted_chat(ac1, ac2)

        lp.sec("ac1: sending single dot message")
        chat.send_text(".")

        lp.sec("ac2: wait for receive")
        msg2 = ac2.wait_for_incoming_msg()
        assert msg2.get_snapshot().text == "."


class TestMultiRelay:
    """Tests that use two different chatmail relays."""

    def test_one_on_one_between_relays(self, cmfactory, cmfactory2, lp):
        """Test that a DC account can send a message to a second DC account
        on a different chatmail instance."""
        ac1 = cmfactory.get_online_account()
        ac2 = cmfactory2.get_online_account()
        chat = cmfactory.get_accepted_chat(ac1, ac2)
        chat.send_text("hello from relay1")

        lp.sec("wait for ac2 to receive message from relay1")
        msg2 = ac2.wait_for_incoming_msg()
        assert msg2.get_snapshot().text == "hello from relay1"

    def test_securejoin(self, cmfactory, cmfactory2, lp):
        """Test that SecureJoin protocol works between two instances."""
        ac1 = cmfactory.get_online_account()
        ac2 = cmfactory2.get_online_account()

        lp.sec("ac1: create QR code and let ac2 scan it, starting the securejoin")
        qr = ac1.get_qr_code()

        lp.sec("ac2: start QR-code based setup contact protocol")
        ac2.secure_join(qr)
        ac1.wait_for_securejoin_inviter_success()


def test_hide_senders_ip_address(cmfactory, ssl_context):
    public_ip = requests.get("http://icanhazip.com").content.decode().strip()
    assert ipaddress.ip_address(public_ip)

    user1, user2 = cmfactory.get_online_accounts(2)
    chat = cmfactory.get_accepted_chat(user1, user2)

    chat.send_text("testing submission header cleanup")
    user2.wait_for_incoming_msg()
    addr = user2.get_config("addr")
    host = addr.split("@")[1]
    pw = user2.get_config("mail_pw")
    mailbox = imap_tools.MailBox(host, ssl_context=ssl_context)
    mailbox.login(addr, pw)
    msgs = list(mailbox.fetch(mark_seen=False))
    assert msgs, "expected at least one message"
    assert public_ip not in msgs[0].obj.as_string()


def test_unencrypted_rejection(cmsetup, lp):
    """Test that unencrypted messages are rejected by the relay."""
    lp.sec("creating users")
    u1, u2 = cmsetup.gen_users(2)

    lp.sec("sending unencrypted mail via SMTP")
    msg = MIMEText("unencrypted")
    msg["Subject"] = "test"
    msg["From"] = u1.addr
    msg["To"] = u2.addr

    try:
        u1.smtp.sendmail(u1.addr, [u2.addr], msg.as_string())
        pytest.fail("Unencrypted message was accepted!")
    except smtplib.SMTPDataError as e:
        assert e.smtp_code == 523
    except smtplib.SMTPRecipientsRefused as e:
        for addr, (code, msg) in e.recipients.items():
            assert code == 523


def test_login_domain_validation(maildomain, lp):
    """Test that IMAP LOGIN validates the domain part for JIT creation."""
    lp.sec("attempting login with invalid domain")
    # We use raw imaplib because cmsetup.gen_users() would fail at credential generation
    # or handle the error differently.
    user = f"invalid@{maildomain}.invalid"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with imaplib.IMAP4_SSL(maildomain, ssl_context=ctx) as conn:
        try:
            conn.login(user, "password")
            pytest.fail("Login with invalid domain was accepted!")
        except imaplib.IMAP4.error as e:
            # Most relays return [AUTHENTICATIONFAILED] or similar
            assert "FAILED" in str(e) or "Invalid" in str(e)
