"""cmdeploy-based deployment driver for cmlxc.

All cmdeploy/chatmaild operations run inside the builder
container -- no host-side Python imports are needed.
"""

import time

from cmlxc.container import DNSContainer, SetupError
from cmlxc.driver_base import Driver

CMDEPLOY = "cmdeploy"


class CmdeployDriver(Driver):
    """Deploys chatmail relays via the ``cmdeploy`` tool."""

    CLI_NAME = "deploy-cmdeploy"
    CLI_DOC = "Deploy a cmdeploy relay into a container."
    DEFAULT_SOURCE_URL = "https://github.com/chatmail/relay.git"
    REPO_NAME = CMDEPLOY
    IMAGE_ALIAS = "localchat-cmdeploy"
    REQUIRED_SOURCE_PATHS = ["chatmaild", "cmdeploy"]

    _CACHED_DISABLE_SERVICES = [
        "postfix",
        "dovecot",
        "unbound",
        "opendkim",
        "nginx",
        "filtermail",
        "filtermail-incoming",
        "fcgiwrap",
    ]

    def on_init_relay(self, repo_path):
        """Hook called by ``init_builder`` to run initenv.sh for the relay."""
        self.out.print(f"  Running scripts/initenv.sh for {self.ct.shortname} ...")
        self.bld_ct.bash(f"cd {repo_path} && bash scripts/initenv.sh")

    def run_deploy(self, *, source, ipv4_only=False):
        """Deploy cmdeploy to a single relay container."""
        with self.out.section(f"Preparing container setup: {self.ct.shortname}"):
            self.ct.ensure(
                ipv4_only=ipv4_only,
                image_candidates=[self.IMAGE_ALIAS, "localchat-base"],
            )
        t_total = time.time()
        self.deploy(source=source)
        elapsed = time.time() - t_total
        self.out.section_line(f"deploy cmdeploy complete ({elapsed:.1f}s)")

    def run_tests(self, second_domain=None):
        """Execute the cmdeploy test suite against the relay."""
        with self.out.section("cmdeploytest"):
            self.out.print("Preparing chatmail.ini on builder ...")
            write_ini(self.bld_ct, self.ct, disable_ipv6=self.ct.is_ipv6_disabled)

            env = {}
            if second_domain:
                env["CHATMAIL_DOMAIN2"] = second_domain

            test_addr = self.get_test_domain_or_ip()
            self.out.print(f"Running cmdeploy tests against {test_addr} ...")

            ini_path = f"{self.repo_path}/chatmail.ini"
            env_exports = f"export CHATMAIL_INI={ini_path}"
            for k, v in env.items():
                env_exports += f" && export {k}={v}"
            cmd = (
                f"incus exec {self.bld_ct.name} --"
                f" bash -c '"
                f"{env_exports} &&"
                f" source {self.venv_path}/bin/activate &&"
                f" cd {self.repo_path} &&"
                f" pytest cmdeploy/src/ -n4 -rs -x -v --durations=5'"
            )
            ret = self.out.shell(cmd)
            if ret:
                self.out.red(f"test-cmdeploy failed (exit {ret})")
            return ret

    def deploy(self, source=None):
        """Deploy chatmail services to a single relay via cmdeploy."""
        self.ct.check_deploy_lock(CMDEPLOY)

        self.ix.write_ssh_config()
        self.bld_ct.write_relay_ssh_config(self.ct)

        dns_ct = DNSContainer(self.ix)
        dns_ct.wait_ready(timeout=5)

        with self.out.section("Preparing DNS configuration"):
            sub = self.out.new_prefixed_out()
            sub.print(f"Configuring DNS for {self.ct.shortname} ...")
            self.ct.configure_dns(dns_ct.ipv4)

        # Bootstrap minimal A record so cmdeploy can find the relay
        dns_ct.set_dns_records(
            self.ct.domain,
            f"{self.ct.domain}. 3600 IN A {self.ct.ipv4}",
        )

        with self.out.section(f"cmdeploy run: {self.ct.shortname} ({self.ct.domain})"):
            self.out.print("Preparing chatmail.ini on builder ...")
            write_ini(self.bld_ct, self.ct, disable_ipv6=self.ct.is_ipv6_disabled)
            self._run_cmdeploy("run", "--skip-dns-check")

            # cmdeploy appends 9.9.9.9 to resolv.conf; restore clean state
            self.out.print(f"Re-configuring DNS for {self.ct.shortname} ...")
            self.ct.configure_dns(dns_ct.ipv4)

        with self.out.section(f"Loading DNS zone: {self.ct.shortname}"):
            zone_path = f"{self.repo_path}/chatmail.zone"
            self._run_cmdeploy("dns", "--zonefile", zone_path)

            zone_content = self.bld_ct.bash(f"cat {zone_path}")
            self.out.print("  Loading zone content into PowerDNS ...")
            dns_ct.set_dns_records(self.ct.domain, zone_content)

        self.out.print(f"Restarting filtermail-incoming on {self.ct.shortname} ...")
        self.ct.bash("systemctl restart filtermail-incoming")

        if not self.ix.find_image([self.IMAGE_ALIAS]):
            with self.out.section(f"Caching {self.IMAGE_ALIAS} image"):
                self._publish_image()

        with self.out.section("Verifying DNS records"):
            self._run_cmdeploy("dns")

        self.ct.write_deploy_state(CMDEPLOY, source=source)

    def _run_cmdeploy(self, subcmd, *extra):
        extra_str = " ".join(extra)
        v_flag = " -" + "v" * self.out.verbosity if self.out.verbosity > 0 else ""
        ini_path = f"{self.repo_path}/chatmail.ini"
        cmd = (
            f"incus exec {self.bld_ct.name} --"
            f" bash -c '"
            f"source {self.venv_path}/bin/activate &&"
            f" cd {self.repo_path} &&"
            f" cmdeploy {subcmd}{v_flag}"
            f" --config {ini_path}"
            f" {extra_str}'"
        )
        ret = self.out.shell(cmd)
        if ret:
            raise SetupError(
                f"cmdeploy {subcmd} failed on {self.ct.shortname} (exit {ret})"
            )

    def _publish_image(self):
        if self.ix.find_image([self.IMAGE_ALIAS]):
            return
        self.out.print(
            f"  Locally caching {self.ct.name!r} as {self.IMAGE_ALIAS!r} image ..."
        )
        units = " ".join(f"{s}.service" for s in self._CACHED_DISABLE_SERVICES)
        self.ct.bash("cp /etc/resolv.conf /tmp/resolv.conf.bak")
        self.ct.bash(f"systemctl disable --now {units}")
        self.ct.bash("rm -f /etc/resolv.conf")
        self.ix.run(["publish", self.ct.name, f"--alias={self.IMAGE_ALIAS}", "--force"])
        self.ct.bash("cp /tmp/resolv.conf.bak /etc/resolv.conf")
        self.ct.bash(f"systemctl enable --now {units}")
        self.ct.wait_ready()
        self.out.print(f"  Image {self.IMAGE_ALIAS!r} ready.")


# ------------------------------------------------------------------
# Static helpers
# ------------------------------------------------------------------


def write_ini(builder_ct, ct, disable_ipv6=False):
    """Write a chatmail.ini for *ct* using the builder container."""
    overrides = {
        "max_user_send_per_minute": 600,
        "max_user_send_burst_size": 100,
        "mtail_address": "127.0.0.1",
    }
    if disable_ipv6:
        overrides["disable_ipv6"] = "True"
    overrides_str = ", ".join(
        f"'{k}': '{v}'" if isinstance(v, str) else f"'{k}': {v}"
        for k, v in overrides.items()
    )
    repo_path = ct.get_repo_path(CMDEPLOY)
    ini_path = f"{repo_path}/chatmail.ini"
    builder_ct.bash(f"""
        source {repo_path}/venv/bin/activate
        python3 -c "
from chatmaild.config import write_initial_config
from pathlib import Path
write_initial_config(Path('{ini_path}'), '{ct.domain}', {{{overrides_str}}})
"
    """)
