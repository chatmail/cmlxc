"""cmrelay-based deployment driver for cmlxc.

A release tag fetches that release's installer file in the builder,
any other source builds one there with Nix.
The downloaded/built installer then runs in the relay container.
"""

import re
import time
from pathlib import Path

from cmlxc.container import SetupError
from cmlxc.driver_base import Driver, parse_source
from cmlxc.driver_cmdeploy import CmdeployDriver, get_ini_overrides
from cmlxc.driver_madmail import RELEASED_TAG_RE

CMRELAY = "cmrelay"
CMRELAY_BIN = "/opt/cmrelay/lib/bin/cmrelay"
PAYLOAD_PATH = "/root/cmrelay.run"
INSTALLED_INI = "/opt/cmrelay/chatmail.ini"
RELEASES = "https://github.com/hpk42/cmrelay/releases/download"
# single-user Nix as root
INSTALL_NIX = """
apt-get -qq update && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y curl xz-utils
mkdir -p /etc/nix
printf 'build-users-group =\\nexperimental-features = nix-command flakes\\n' >/etc/nix/nix.conf
curl -fsSL https://nixos.org/nix/install | sh -s -- --no-daemon --yes
"""


def installer_url(tag, arch):
    """Return the installer asset URL of release *tag*, or None if it has none."""
    if not RELEASED_TAG_RE.match(tag or "") or arch not in ("x86_64", "aarch64"):
        return None
    return f"{RELEASES}/{tag}/cmrelay-installer-{tag[1:]}-{arch}-linux.run"


# mtail is installed with cmdeploy, so we also do it for cmrelay
INSTALL_MTAIL = r"""
import glob, hashlib, io, json, platform, tarfile, urllib.request
[pins] = glob.glob("/opt/cmrelay/lib/store/*-cmrelay-closure/cmrelay/*/pins.json")
url, sha256 = json.load(open(pins))["MTAIL_ARTIFACTS"][platform.machine()]
data = urllib.request.urlopen(url, timeout=60).read()
binary = tarfile.open(fileobj=io.BytesIO(data)).extractfile("mtail").read()
assert hashlib.sha256(binary).hexdigest() == sha256, f"{url}: checksum mismatch"
with open("/usr/local/bin/mtail", "wb") as f:
    f.write(binary)
"""


class CmrelayDriver(Driver):
    """Installs a chatmail relay from a single cmrelay installer file."""

    CLI_NAME = "deploy-cmrelay"
    CLI_DOC = "Deploy a relay from a cmrelay single-file installer."
    DEFAULT_SOURCE_URL = "https://github.com/hpk42/cmrelay.git"
    DEFAULT_REF = "v0.6.0"
    REPO_NAME = CMRELAY
    REQUIRED_SOURCE_PATHS = ["installer/flake.nix", "host/cmrelay"]

    payload = None

    @classmethod
    def add_cli_options(cls, parser, completer=None):
        super().add_cli_options(parser, completer=completer)
        parser.add_argument(
            "--payload",
            metavar="PATH",
            help="Push this local installer file instead of downloading or building one.",
        )

    def __init__(self, ct, out):
        super().__init__(ct, out)
        self.relay = CmdeployDriver(ct, out)
        self.relay.type = self.type

    def configure_from_args(self, args):
        self.payload = getattr(args, "payload", None)

    def get_test_domain_or_ip(self):
        return self.ct.domain

    def init_builder(self, source):
        if self.payload:
            self.payload = str(Path(self.payload).expanduser().resolve())
            if not Path(self.payload).is_file():
                raise SetupError(f"--payload {self.payload} is not a file")
            return
        super().init_builder(source)

    def on_init_relay(self, repo_path, tag):
        url = installer_url(tag, self.bld_ct.bash("uname -m"))
        if url:
            self.built = f"{repo_path}/cmrelay.run"
            self.out.print(f"  Downloading {url} ...")
            curl = f"curl -fsSL -o {self.built} {url}"
            if self.bld_ct.bash(curl, check=False) is None:
                raise SetupError(f"could not download {url}")
            return
        self.ensure_nix()
        nix = "/root/.nix-profile/bin/nix build -o build/installer ./installer#payload"
        if self.out.shell(f"incus exec {self.bld_ct.name} --cwd {repo_path} -- {nix}"):
            raise SetupError("building the cmrelay installer in the builder failed")
        self.built = f"{repo_path}/build/installer/bin/cmrelay.run"

    def ensure_nix(self):
        """Nix in the builder, whose sandbox needs nested namespaces."""
        name = self.bld_ct.name
        if self.ix.run_output(["config", "get", name, "security.nesting"]) != "true":
            self.out.print(f"  Enabling security.nesting on {name} for Nix ...")
            self.ix.run(["config", "set", name, "security.nesting=true"])
            self.ix.run(["restart", name])
            self.bld_ct.wait_ready()
        if self.bld_ct.bash("test -x /root/.nix-profile/bin/nix", check=False) is None:
            self.out.print(f"  Installing Nix in {name} ...")
            self.bld_ct.bash(INSTALL_NIX)

    def run_deploy(self, *, source, ipv4_only=False):
        with self.out.section(f"Preparing container setup: {self.ct.shortname}"):
            self.ct.ensure(ipv4_only=ipv4_only)
        started = time.time()
        self.deploy(source=source)
        self.out.section_line(f"deploy cmrelay complete ({time.time() - started:.1f}s)")

    def deploy(self, source=None):
        self.ct.check_deploy_lock(CMRELAY)
        self.ix.write_ssh_config()
        self.bld_ct.write_relay_ssh_config(self.ct)

        dns_ct = self.get_dns_container()
        # A cached base image carries the nameservers of the host that built it.
        self.ct.setup_resolvconf_localchat_nameserver(dns_ct.ipv4)
        domain = self.get_test_domain_or_ip()
        dns_ct.set_dns_records(domain, f"{domain}. 3600 IN A {self.ct.ipv4}")

        with self.out.section(f"cmrelay install: {self.ct.shortname} ({domain})"):
            self.push_payload()
            self.install(domain)
            # the deploy points resolv.conf at its own unbound
            self.out.print(f"Configuring localchat DNS for {self.ct.shortname} ...")
            self.ct.setup_resolvconf_localchat_nameserver(dns_ct.ipv4)
            self.ct.setup_unbound_localchat_forwarder(dns_ct.ipv4)

        with self.out.section(f"Loading DNS zone: {self.ct.shortname}"):
            zone_path = "/root/chatmail.zone"
            self.ct.bash(f"{CMRELAY_BIN} dns --zonefile {zone_path}")
            dns_ct.set_dns_records(domain, self.ct.bash(f"cat {zone_path}"))
            self.ct.bash("systemctl restart unbound filtermail-incoming")

        with self.out.section("Verifying DNS records"):
            if self.out.shell(f"incus exec {self.ct.name} -- {CMRELAY_BIN} dns"):
                raise SetupError(f"cmrelay dns failed on {self.ct.shortname}")

        self.ct.write_deploy_state(CMRELAY, source=source, deploy_type=self.type)

    def push_payload(self):
        self.out.print(f"Pushing the installer into {self.ct.shortname} ...")
        if self.payload:
            self.ix.run(["file", "push", self.payload, f"{self.ct.name}{PAYLOAD_PATH}"])
        else:
            self.bld_ct.scp_to_relay(self.built, self.ct.ipv4, PAYLOAD_PATH)
        self.ct.bash(f"chmod 755 {PAYLOAD_PATH}")

    def install(self, domain):
        """Install or upgrade to the payload, write the test ini, install mtail, deploy."""
        installed = self.ct.bash(f"test -x {CMRELAY_BIN}", check=False) is not None
        if not installed:
            if self.out.shell(f"incus exec {self.ct.name} -- {PAYLOAD_PATH}"):
                raise SetupError(f"cmrelay install failed on {self.ct.shortname}")
        self.out.print("Writing the test chatmail.ini with cmrelay's chatmaild ...")
        overrides = get_ini_overrides(domain, disable_ipv6=self.ct.is_ipv6_disabled)
        self.ct.push_file_content(
            "/root/write-ini.py",
            "from pathlib import Path\n"
            "from chatmaild.config import write_initial_config\n"
            f"write_initial_config(Path({INSTALLED_INI!r}), {domain!r}, {overrides!r})\n",
        )
        # cmrelay's python has chatmaild
        self.ct.bash(f'"$(sed -n "1s/^#!//p" {CMRELAY_BIN})" /root/write-ini.py')
        if self.ct.bash("test -x /usr/local/bin/mtail", check=False) is None:
            self.out.print("Installing mtail ...")
            self.ct.push_file_content("/root/install-mtail.py", INSTALL_MTAIL)
            self.ct.bash(
                "python3 /root/install-mtail.py && chmod 755 /usr/local/bin/mtail"
            )
        # an installed cmrelay deploys the payload through an upgrade, signed or not
        verb = f"upgrade {PAYLOAD_PATH} --no-verify --force" if installed else "deploy"
        deploy = (
            f"incus exec {self.ct.name} -- {CMRELAY_BIN} {verb} --yes --skip-dns-check"
        )
        if self.out.shell(deploy):
            raise SetupError(f"cmrelay deploy failed on {self.ct.shortname}")

    def run_tests(self, second_domain=None):
        """Run chatmail/relay's tests at the commit this relay carries."""
        relay = self.relay
        relay.bld_ct = self.bld_ct
        marker = self.ct.bash("cat /etc/chatmail-version", check=False) or ""
        commit = marker.split()[0] if marker.split() else ""
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise SetupError(
                f"no chatmail/relay commit in {self.ct.shortname}'s /etc/chatmail-version"
            )
        head = self.bld_ct.bash(f"git -C {relay.repo_path} rev-parse HEAD", check=False)
        has_venv = self.bld_ct.bash(
            f"test -x {relay.venv_path}/bin/pytest", check=False
        )
        if (head or "").strip() != commit or has_venv is None:
            relay.init_builder(
                parse_source(f"@{commit}", CmdeployDriver.DEFAULT_SOURCE_URL)
            )
        extra_env = {
            "CHATMAIL_STATUS_CMD": f"{CMRELAY_BIN} status",
            "CHATMAIL_VERSION_COMMIT": commit,
        }
        return relay.run_tests(second_domain=second_domain, extra_env=extra_env)
