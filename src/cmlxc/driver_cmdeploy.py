"""cmdeploy-based deployment driver for cmlxc.

All cmdeploy/chatmaild operations run inside the builder
container -- no host-side Python imports are needed.
"""

import time

from cmlxc.incus import (
    CMDEPLOY,
    DNS_CONTAINER_NAME,
    RELAY_IMAGE_ALIAS,
)

RELAY_REPO_URL = "https://github.com/chatmail/relay.git"


def init_builder(bld_ct, out, local_repo=None):
    """Set up the relay checkout and install cmdeploy deps."""
    bld_ct.setup_repo("/root/relay", out, url=RELAY_REPO_URL, local_path=local_repo)
    out.print("  Installing cmdeploy/chatmaild (editable) ...")
    bld_ct.install_relay_deps()


def generate_chatmail_ini(builder_ct, domain, overrides):
    """Generate chatmail.ini inside the builder and return its content."""
    overrides_str = ", ".join(
        f"'{k}': '{v}'" if isinstance(v, str) else f"'{k}': {v}"
        for k, v in overrides.items()
    )
    builder_ct.bash(f"""
        source /root/cmdeploy-venv/bin/activate
        python3 -c "
from chatmaild.config import write_initial_config
from pathlib import Path
write_initial_config(Path('/tmp/chatmail.ini'), '{domain}', {{{overrides_str}}})
"
    """)
    return builder_ct.bash("cat /tmp/chatmail.ini")


def run_cmdeploy(builder_ct, subcmd, out, extra=None):
    """Run ``cmdeploy`` inside the builder container."""
    extra_str = " ".join(extra) if extra else ""
    v_flag = " -" + "v" * out.verbosity if out.verbosity > 0 else ""
    cmd = (
        f"incus exec {builder_ct.name} --"
        f" bash -c '"
        f"source /root/cmdeploy-venv/bin/activate &&"
        f" cd /root/relay &&"
        f" cmdeploy {subcmd}{v_flag}"
        f" --config /root/chatmail.ini"
        f" {extra_str}'"
    )
    return out.shell(cmd)


def write_ini(builder_ct, ct, disable_ipv6=False):
    overrides = {
        "max_user_send_per_minute": 600,
        "max_user_send_burst_size": 100,
        "mtail_address": "127.0.0.1",
    }
    if disable_ipv6:
        overrides["disable_ipv6"] = "True"

    ct.relay_dir.mkdir(parents=True, exist_ok=True)
    content = generate_chatmail_ini(builder_ct, ct.domain, overrides)
    ct.ini.write_text(content)
    return ct.ini


def deploy(relay_names, ix, out, builder_ct):
    """Deploy chatmail services via cmdeploy."""
    t_total = time.time()

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

            ret = run_cmdeploy(
                builder_ct,
                "run",
                out,
                extra=["--skip-dns-check"],
            )
            if ret:
                out.red(f"Deploy to {ct.sname} failed (exit {ret})")
                return ret

            # cmdeploy may overwrite unbound config; force re-inject .localchat forwarding
            ct.configure_dns(dns_ct.ipv4)

        if not ix.find_image([RELAY_IMAGE_ALIAS]):
            with out.section("deploy: caching relay image"):
                ct.publish_as_relay_image()

    # Generate DNS zone files and load into PowerDNS
    with out.section("loading DNS zones"):
        for ct in relays:
            ret = run_cmdeploy(
                builder_ct,
                "dns",
                out,
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
            ret = run_cmdeploy(builder_ct, "dns", out)
            if ret:
                out.red(f"DNS verification for {ct.sname} failed (exit {ret})")
                return ret

    # Record deploy state
    for ct in relays:
        ct.write_deploy_state(CMDEPLOY)

    elapsed = time.time() - t_total
    out.section_line(f"deploy cmdeploy complete ({elapsed:.1f}s)")
    return 0
