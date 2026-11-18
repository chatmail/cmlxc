"""cmrelay-based deployment driver for cmlxc.

The installer file is built with Nix on the host
and pushed into the relay container, which runs it.
"""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from cmlxc.container import SetupError
from cmlxc.driver_base import Driver, parse_source
from cmlxc.driver_cmdeploy import CmdeployDriver, get_ini_overrides

CMRELAY = "cmrelay"
CMRELAY_BIN = "/opt/cmrelay/lib/bin/cmrelay"
PAYLOAD_PATH = "/root/cmrelay.run"
INSTALLED_INI = "/opt/cmrelay/chatmail.ini"

# mtail, as cmrelay's precheck asks, from the closure's pins
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
    REPO_NAME = CMRELAY
    REQUIRED_SOURCE_PATHS = ["installer/flake.nix", "host/cmrelay"]

    payload = None

    @classmethod
    def add_cli_options(cls, parser, completer=None):
        super().add_cli_options(parser, completer=completer)
        parser.add_argument(
            "--payload",
            metavar="PATH",
            help="Use this prebuilt .run file instead of building one with Nix.",
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
        self.payload_file = self.build_payload(source)
        size = self.payload_file.stat().st_size / (1024 * 1024)
        self.out.print(f"  payload: {self.payload_file.name} ({size:.0f} MiB)")

    def build_payload(self, source) -> Path:
        if self.payload:
            path = Path(self.payload).expanduser().resolve()
            if not path.is_file():
                raise SetupError(f"--payload {path} is not a file")
            return path
        if source.kind == "local":
            flake = f"{Path(source.path).expanduser().resolve()}/installer"
        else:
            flake = f"{source.url}?dir=installer&ref={source.ref}"
        return self.nix_build(flake)

    def nix_build(self, flake: str) -> Path:
        nix = shutil.which("nix") or str(Path.home() / ".nix-profile/bin/nix")
        if not Path(nix).exists():
            raise SetupError(
                "cmrelay installers are built with Nix, which this host lacks;"
                " install Nix, or pass --payload with a prebuilt .run file."
            )
        self.out.print(f"  nix build {flake}#payload (this takes a while) ...")
        env = dict(os.environ)
        env.setdefault("NIX_CONFIG", "experimental-features = nix-command flakes")
        result = subprocess.run(
            [nix, "build", "--no-link", "--print-out-paths", f"{flake}#payload"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if result.returncode:
            raise SetupError(f"nix build failed:\n{result.stderr.strip()[-4000:]}")
        out_path = Path(result.stdout.strip().splitlines()[-1])
        runs = sorted((out_path / "bin").glob("cmrelay-*.run"))
        if len(runs) != 1:
            raise SetupError(f"expected one cmrelay-*.run in {out_path}/bin")
        return runs[0]

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
        self.out.print(f"Pushing {self.payload_file.name} into {self.ct.shortname} ...")
        self.ix.run(
            ["file", "push", str(self.payload_file), f"{self.ct.name}{PAYLOAD_PATH}"]
        )
        self.ct.bash(f"chmod 755 {PAYLOAD_PATH}")

    def install(self, domain):
        """Run the installer once, write the test ini, install mtail, deploy."""
        if self.ct.bash(f"test -x {CMRELAY_BIN}", check=False) is None:
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
        deploy = (
            f"incus exec {self.ct.name} -- {CMRELAY_BIN} deploy --yes --skip-dns-check"
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
