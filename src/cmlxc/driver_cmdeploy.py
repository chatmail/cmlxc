"""cmdeploy-based deployment driver for cmlxc.

All cmdeploy/chatmaild operations run inside the builder
container -- no host-side Python imports are needed.
"""

import time

from cmlxc.driver_base import Driver
from cmlxc.incus import (
    CMDEPLOY,
    DNS_CONTAINER_NAME,
    DeployConflictError,
)


class CmdeployDriver(Driver):
    """Deploys chatmail relays via the ``cmdeploy`` tool."""

    CLI_NAME = "deploy-cmdeploy"
    CLI_DOC = "Deploy a cmdeploy relay into a container."
    DEFAULT_SOURCE_URL = "https://github.com/chatmail/relay.git"
    REPO_NAME = CMDEPLOY
    IMAGE_ALIAS = "localchat-cmdeploy"
    NAME_EXAMPLES = "cm0 cm1"

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

    def init_builder(self, bld_ct, source, names):
        """Set up the cmdeploy checkout and install deps for each relay."""
        tmp_dest = f"/root/{self.REPO_NAME}-template"
        bld_ct.setup_repo(tmp_dest, self.out, source)

        for name in names:
            ct = self.ix.get_container(name)
            repo_path = ct.get_repo_path(self.REPO_NAME)
            venv_path = ct.get_venv_path(self.REPO_NAME)

            self.out.print(f"  Setting up {repo_path} ...")
            bld_ct.bash(f"rm -rf {repo_path} && cp -a {tmp_dest} {repo_path}")

            self.out.print(f"  Installing cmdeploy/chatmaild in {venv_path} ...")
            bld_ct.install_relay_deps(repo_path, venv_path)

    def run_deploy(self, names, bld_ct, *, ipv4_only=False):
        """Ensure relay containers and deploy cmdeploy."""
        try:
            for name in names:
                with self.out.section(f"Preparing container setup: {name}"):
                    self.ix.ensure_relay_containers(
                        [name],
                        ipv4_only=ipv4_only,
                        image_candidates=[self.IMAGE_ALIAS, "localchat-base"],
                    )
                ret = self.deploy([name], bld_ct)
                if ret:
                    return ret
            return 0
        except DeployConflictError as exc:
            self.out.red(f"Deploy conflict: {exc}")
            return 1

    def deploy(self, relay_names, builder_ct):
        """Deploy chatmail services via cmdeploy."""
        t_total = time.time()
        out = self.out
        ix = self.ix

        relays = [ix.get_container(n) for n in relay_names]

        # Check deploy locks before doing anything destructive
        for ct in relays:
            ct.relay_dir.mkdir(parents=True, exist_ok=True)
            ct.check_deploy_lock(CMDEPLOY)

        ix.write_ssh_config()

        # Set up SSH from builder to relay containers
        builder_ct.setup_ssh(relays)

        # Set up DNS zones (basic A/AAAA records)
        dns_ct = ix.get_container(DNS_CONTAINER_NAME)
        dns_ct.wait_ready(timeout=5)
        managed = ix.list_managed()
        relay_cnames = {ct.name for ct in relays}
        started = [c for c in managed if c["name"] in relay_cnames]

        if started:
            out.print(f"Resetting DNS zones for {len(started)} domain(s) ...")
            dns_ct.reset_dns_records(dns_ct.ipv4, started)
            sub = out.new_prefixed_out()
            for ct in relays:
                sub.print(f"Configuring DNS in {ct.name} ...")
                ct.configure_dns(dns_ct.ipv4)

        # Deploy chatmail on each relay
        for ct in relays:
            with out.section(f"cmdeploy run: {ct.sname} ({ct.domain})"):
                out.print(f"Writing {ct.ini.name} ...")
                write_ini(builder_ct, ct, disable_ipv6=ct.is_ipv6_disabled)

                # Push INI into builder for cmdeploy to use
                builder_ct.push_chatmail_ini(ct.ini)

                ret = self._run_cmdeploy(
                    builder_ct,
                    ct,
                    "run",
                    extra=["--skip-dns-check"],
                )
                if ret:
                    out.red(f"Deploy to {ct.sname} failed (exit {ret})")
                    return ret

                # cmdeploy appends 9.9.9.9 to resolv.conf; restore clean state
                out.print(f"Re-configuring DNS in {ct.name} ...")
                ct.configure_dns(dns_ct.ipv4)

            if not ix.find_image([self.IMAGE_ALIAS]):
                with out.section(f"deploy: caching {self.IMAGE_ALIAS} image"):
                    self._publish_image(ct)

        # Generate DNS zone files and load into PowerDNS
        with out.section("loading DNS zones"):
            for ct in relays:
                ret = self._run_cmdeploy(
                    builder_ct,
                    ct,
                    "dns",
                    extra=["--zonefile", "/tmp/chatmail.zone"],
                )
                if ret:
                    out.red(f"DNS zone generation for {ct.sname} failed (exit {ret})")
                    return ret

                # Pull zonefile from builder to host
                zone_content = builder_ct.bash("cat /tmp/chatmail.zone", check=False)
                if zone_content:
                    ct.zone.write_text(zone_content)

            dns_ct = ix.get_container(DNS_CONTAINER_NAME)
            for ct in relays:
                if ct.zone.exists():
                    out.print(f"Loading {ct.zone} into PowerDNS ...")
                    dns_ct.set_dns_records(ct.domain, ct.zone.read_text())

            for ct in relays:
                out.print(f"Restarting filtermail-incoming on {ct.name} ...")
                ct.bash("systemctl restart filtermail-incoming")

        # Final DNS verification
        with out.section("verifying DNS records"):
            for ct in relays:
                builder_ct.push_chatmail_ini(ct.ini)
                ret = self._run_cmdeploy(builder_ct, ct, "dns")
                if ret:
                    out.red(f"DNS verification for {ct.sname} failed (exit {ret})")
                    return ret

        # Record deploy state
        for ct in relays:
            ct.write_deploy_state(CMDEPLOY)

        elapsed = time.time() - t_total
        out.section_line(f"deploy cmdeploy complete ({elapsed:.1f}s)")
        return 0

    def _run_cmdeploy(self, builder_ct, ct, subcmd, extra=None):
        """Run ``cmdeploy`` inside the builder container."""
        repo_path = ct.get_repo_path(self.REPO_NAME)
        venv_path = ct.get_venv_path(self.REPO_NAME)
        extra_str = " ".join(extra) if extra else ""
        v_flag = " -" + "v" * self.out.verbosity if self.out.verbosity > 0 else ""
        cmd = (
            f"incus exec {builder_ct.name} --"
            f" bash -c '"
            f"source {venv_path}/bin/activate &&"
            f" cd {repo_path} &&"
            f" cmdeploy {subcmd}{v_flag}"
            f" --config /root/chatmail.ini"
            f" {extra_str}'"
        )
        return self.out.shell(cmd)

    def _publish_image(self, ct):
        """Cache the current container state as the cmdeploy image."""
        if self.ix.find_image([self.IMAGE_ALIAS]):
            return
        self.out.print(
            f"  Locally caching {ct.name!r} as {self.IMAGE_ALIAS!r} image ..."
        )
        units = " ".join(f"{s}.service" for s in self._CACHED_DISABLE_SERVICES)
        ct.bash("cp /etc/resolv.conf /tmp/resolv.conf.bak")
        ct.bash(f"systemctl disable --now {units}")
        ct.bash("rm -f /etc/resolv.conf")
        self.ix.run(["publish", ct.name, f"--alias={self.IMAGE_ALIAS}", "--force"])
        # Restore DNS and re-enable services on the running container
        ct.bash("cp /tmp/resolv.conf.bak /etc/resolv.conf")
        ct.bash(f"systemctl enable --now {units}")
        ct.wait_ready()
        self.out.print(f"  Image {self.IMAGE_ALIAS!r} ready.")


# ------------------------------------------------------------------
# Static helpers (also used by test-cmdeploy in cli.py)
# ------------------------------------------------------------------


def generate_chatmail_ini(builder_ct, ct, domain, overrides):
    """Generate chatmail.ini inside the builder and return its content."""
    overrides_str = ", ".join(
        f"'{k}': '{v}'" if isinstance(v, str) else f"'{k}': {v}"
        for k, v in overrides.items()
    )
    builder_ct.bash(f"""
        source {ct.get_venv_path(CMDEPLOY)}/bin/activate
        python3 -c "
from chatmaild.config import write_initial_config
from pathlib import Path
write_initial_config(Path('/tmp/chatmail.ini'), '{domain}', {{{overrides_str}}})
"
    """)
    return builder_ct.bash("cat /tmp/chatmail.ini")


def write_ini(builder_ct, ct, disable_ipv6=False):
    """Write a chatmail.ini for *ct* using the builder container."""
    overrides = {
        "max_user_send_per_minute": 600,
        "max_user_send_burst_size": 100,
        "mtail_address": "127.0.0.1",
    }
    if disable_ipv6:
        overrides["disable_ipv6"] = "True"

    ct.relay_dir.mkdir(parents=True, exist_ok=True)
    content = generate_chatmail_ini(builder_ct, ct, ct.domain, overrides)
    ct.ini.write_text(content)
    return ct.ini
