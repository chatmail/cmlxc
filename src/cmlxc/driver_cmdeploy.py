"""cmdeploy-based deployment driver for cmlxc.

All cmdeploy/chatmaild operations run inside the builder
container -- no host-side Python imports are needed.
"""

import time
from pathlib import Path

from cmlxc.container import SetupError
from cmlxc.driver_base import Driver

CMDEPLOY = "cmdeploy"


class CmdeployDriver(Driver):
    """Deploys chatmail relays via the ``cmdeploy`` tool."""

    CLI_NAME = "deploy-cmdeploy"
    CLI_DOC = "Deploy a cmdeploy relay into a container."
    DEFAULT_SOURCE_URL = "https://github.com/chatmail/relay.git"
    REPO_NAME = CMDEPLOY
    REQUIRED_SOURCE_PATHS = ["chatmaild", "cmdeploy"]

    filtermail_bin = None

    @classmethod
    def add_cli_options(cls, parser, completer=None):
        """Register cmdeploy-specific deploy options."""
        super().add_cli_options(parser, completer=completer)
        parser.add_argument(
            "--type",
            dest="type",
            choices=["dns", "ipv4", "ipv6"],
            default="dns",
            help="Deploy the relay using dns (default), ipv4, or ipv6.",
        )
        parser.add_argument(
            "--filtermail",
            metavar="PATH",
            type=Path,
            help="Path to a local filtermail binary to use for deployment.",
        )

    def configure_from_args(self, args):
        self.type = args.type
        self.filtermail_bin = getattr(args, "filtermail", None)

    def get_test_domain_or_ip(self):
        """Return the IP when deployed without DNS, else the domain."""
        if not self.ct.ipv4:
            self.ct.wait_ready()
        match self.type:
            case "ipv6":
                if not self.ct.ipv6:
                    raise SetupError(f"{self.ct.name} has no IPv6 address.")
                return self.ct.ipv6
            case "ipv4":
                return self.ct.ipv4
            case _:
                return self.ct.domain

    def on_init_relay(self, repo_path):
        """Hook called by ``init_builder`` to run initenv.sh for the relay."""
        self.out.print(f"  Running scripts/initenv.sh for {self.ct.shortname} ...")
        self.bld_ct.bash(f"cd {repo_path} && bash scripts/initenv.sh")

    def init_builder(self, source):
        super().init_builder(source)
        if self.filtermail_bin:
            local_path = self.filtermail_bin.resolve()
            if not local_path.is_file():
                raise SetupError(f"filtermail path {local_path} is not a file.")

            remote_path = f"{self.repo_path}/filtermail"
            self.out.print(f"  Syncing {local_path.name} to builder ...")
            self.ix.run(
                ["file", "push", str(local_path), f"{self.bld_ct.name}{remote_path}"]
            )
            self.custom_env["CHATMAIL_FILTERMAIL_BINARY"] = remote_path

    def run_deploy(self, *, source, ipv4_only=False):
        """Deploy cmdeploy to a single relay container."""
        with self.out.section(f"Preparing container setup: {self.ct.shortname}"):
            self.ct.ensure(ipv4_only=ipv4_only)
        t_total = time.time()
        self.deploy(source=source)
        elapsed = time.time() - t_total
        self.out.section_line(f"deploy cmdeploy complete ({elapsed:.1f}s)")

    def run_tests(self, second_domain=None):
        """Execute the cmdeploy test suite against the relay."""
        with self.out.section("cmdeploytest"):
            self.out.print("Preparing chatmail.ini on builder ...")
            domain = self.get_test_domain_or_ip()
            write_ini(
                self.bld_ct, self.ct, domain, disable_ipv6=self.ct.is_ipv6_disabled
            )

            ini_path = f"{self.repo_path}/chatmail.ini"
            env = {"CHATMAIL_INI": ini_path}
            if second_domain:
                env["CHATMAIL_DOMAIN2"] = second_domain

            self.out.print(f"Running cmdeploy tests against {domain} ...")

            env_args = "".join(f" --env {k}={v}" for k, v in env.items())
            cmd = (
                f"incus exec {self.bld_ct.name}{env_args} --"
                f" bash -c '"
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

        dns_ct = self.get_dns_container()

        domain = self.get_test_domain_or_ip()
        if self.type == "dns":
            dns_ct.set_dns_records(
                domain,
                f"{domain}. 3600 IN A {self.ct.ipv4}",
            )

        with self.out.section(f"cmdeploy run: {self.ct.shortname} ({domain})"):
            self.out.print("Preparing chatmail.ini on builder ...")
            write_ini(
                self.bld_ct, self.ct, domain, disable_ipv6=self.ct.is_ipv6_disabled
            )
            self._run_cmdeploy("run", "--skip-dns-check")

            # Reconfigure DNS for localchat after cmdeploy overwrote resolv.conf.
            self.out.print(f"Configuring localchat DNS for {self.ct.shortname} ...")
            self.ct.setup_resolvconf_localchat_nameserver(dns_ct.ipv4)
            self.ct.setup_unbound_localchat_forwarder(dns_ct.ipv4)

        if self.type == "dns":
            with self.out.section(f"Loading DNS zone: {self.ct.shortname}"):
                zone_path = f"{self.repo_path}/chatmail.zone"
                self._run_cmdeploy("dns", "--zonefile", zone_path)

                zone_content = self.bld_ct.bash(f"cat {zone_path}")
                self.out.print("  Loading zone content into PowerDNS ...")
                dns_ct.set_dns_records(self.ct.domain, zone_content)
                # Flush stale NXDOMAIN entries cached during initial checks
                self.ct.bash("systemctl restart unbound || true")

        self.out.print(f"Restarting filtermail-incoming on {self.ct.shortname} ...")
        self.ct.bash("systemctl restart filtermail-incoming")

        with self.out.section("Verifying DNS records"):
            self._run_cmdeploy("dns")

        self.ct.write_deploy_state(CMDEPLOY, source=source, deploy_type=self.type)

    def _run_cmdeploy(self, subcmd, *extra):
        extra_str = " ".join(extra)
        v_flag = " -" + "v" * self.out.verbosity if self.out.verbosity > 0 else ""
        ini_path = f"{self.repo_path}/chatmail.ini"
        env_args = "".join(f" --env {k}={v}" for k, v in self.custom_env.items())
        cmd = (
            f"incus exec {self.bld_ct.name}{env_args} --"
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


# ------------------------------------------------------------------
# Static helpers
# ------------------------------------------------------------------


def get_ini_overrides(domain, disable_ipv6=False):
    """Return chatmail.ini settings suited for throwaway test relays."""
    overrides = {
        "max_user_send_per_minute": 600,
        "max_user_send_burst_size": 100,
        # Relays reject new address creation while the machine looks
        # busy, which a CI runner hosting several containers always
        # does.  Test relays are throwaway, so lift the gates instead
        # of losing accounts to unrelated load.
        "max_load_per_cpu_1m": 1000,
        "min_available_memory": "1M",
        "min_free_disk_space": "1M",
        "mtail_address": "127.0.0.1",
        "ssh_host": domain,
    }
    if disable_ipv6:
        overrides["disable_ipv6"] = "True"
    return overrides


def write_ini(builder_ct, ct, domain, disable_ipv6=False):
    """Write a chatmail.ini for *ct* using the builder container."""
    overrides = get_ini_overrides(domain, disable_ipv6=disable_ipv6)
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
write_initial_config(Path('{ini_path}'), '{domain}', {{{overrides_str}}})
"
    """)
