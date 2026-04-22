"""cmlxc -- Manage local chatmail relay containers via Incus.

Standard workflow:
init -> deploy-cmdeploy/deploy-madmail -> test-cmdeploy/test-madmail/test-mini.
"""

import argparse
import os
import subprocess
from pathlib import Path

import argcomplete

from cmlxc.container import (
    BASE_IMAGE_ALIAS,
    BUILDER_CONTAINER_NAME,
    DNS_CONTAINER_NAME,
    BuilderContainer,
    Container,
    DNSContainer,
    SetupError,
)
from cmlxc.driver_base import __version__
from cmlxc.driver_cmdeploy import CmdeployDriver
from cmlxc.driver_madmail import MadmailDriver, print_admin_info
from cmlxc.incus import Incus, _is_ip_address, check_cgroup_compat
from cmlxc.output import Out


def _container_completer(prefix, **kwargs):
    ix = Incus(Out())
    managed = ix.list_managed()
    names = [c["name"] for c in managed]
    # Also provide short names
    names += [c["name"].removesuffix(".localchat") for c in managed]
    return [n for n in names if n.startswith(prefix)]


# -------------------------------------------------------------------
# init
# -------------------------------------------------------------------


def _check_init(ix, out):
    return ix.check_init()


def _destroy_all(ix, out):
    managed = ix.list_managed()
    if not managed:
        out.print("No cmlxc-managed containers found.")
    else:
        for c in managed:
            name = c["name"]
            out.green(f"Destroying container {name!r} ...")
            if name == DNS_CONTAINER_NAME:
                DNSContainer(ix).destroy()
            else:
                Container(ix, name).destroy()
    ix.delete_images()


def _destroy_relays(ix, out):
    managed = ix.list_managed()
    relays = [
        c
        for c in managed
        if c["name"] not in (DNS_CONTAINER_NAME, BUILDER_CONTAINER_NAME)
    ]
    if not relays:
        out.print("No relay containers found.")
    else:
        for c in relays:
            name = c["name"]
            out.green(f"Destroying container {name!r} ...")
            ix.get_relay_container(name).destroy()

        bld_ct = BuilderContainer(ix)
        if bld_ct.is_running:
            bld_ct.bash("rm -rf /root/relays/*")


def init_cmd_options(parser):
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Destroy everything (DNS, builder, images) before re-initializing.",
    )
    parser.add_argument(
        "--ipv4-only",
        action="store_true",
        help="Setup the environment with IPv4 only (disable IPv6 in NS and builder).",
    )


def init_cmd(args, out):
    """Initialize the environment (base image, DNS, builder container)."""
    check_cgroup_compat()
    ix = Incus(out)

    if args.reset:
        with out.section("Full reset"):
            _destroy_all(ix, out)

    with out.section("Initializing cmlxc environment"):
        out.green(f"Ensuring {BASE_IMAGE_ALIAS} image ...")
        ix.ensure_base_image()

        out.green("Ensuring DNS container ...")
        dns_ct = DNSContainer(ix)
        dns_ct.ensure(ipv4_only=args.ipv4_only)

        dns_ip = dns_ct.ipv4
        out.print(f"  {dns_ct.name} IP: {dns_ip}")

        out.green("Ensuring builder container ...")
        bld_ct = BuilderContainer(ix)
        bld_ct.ensure(ipv4_only=args.ipv4_only)
        bld_ct.configure_dns(dns_ip)
        bld_ct.install_deps()
        bld_ct.init_ssh()

        out.print("  Syncing mini-test files ...")
        test_dir = Path(__file__).parents[1] / "relay_minitest"
        bld_ct.sync_mini_tests(test_dir)

        for drv_cls in DRIVER_BY_NAME.values():
            with out.section(f"Preparing {drv_cls.REPO_NAME} in builder"):
                drv_cls.prep_builder(ix, out, bld_ct)

        ix.write_ssh_config()
        bld_ct.cleanup()
        out.green("Builder container ready.")


def _get_running_builder(ix, out):
    bld_ct = BuilderContainer(ix)
    if not bld_ct.is_running:
        out.red("Builder container not running.")
        out.red("Run 'cmlxc init' first.")
        return None
    return bld_ct


# -------------------------------------------------------------------
# start (restart stopped containers)
# -------------------------------------------------------------------


def start_cmd_options(parser):
    parser.add_argument(
        "names",
        nargs="+",
        metavar="NAME",
        help="One or more container names to start.",
    ).completer = _container_completer


def start_cmd(args, out):
    """Start previously deployed relay containers."""
    ix = Incus(out)
    for ct in map(ix.get_relay_container, args.names):
        state = ct.get_deploy_state()
        if state is None:
            out.red(
                f"Container {ct.shortname!r} has not been deployed."
                f" Use 'deploy-cmdeploy' or 'deploy-madmail' first."
            )
            return 1
        out.green(f"Starting container {ct.name!r} ...")
        ct.start()
    ix.write_ssh_config()
    out.green("LXC containers started.")


# -------------------------------------------------------------------
# stop
# -------------------------------------------------------------------


def stop_cmd_options(parser):
    parser.add_argument(
        "names",
        nargs="+",
        metavar="NAME",
        help="One or more container names to stop.",
    ).completer = _container_completer


def stop_cmd(args, out):
    """Stop relay containers."""
    ix = Incus(out)
    for name in args.names:
        full = Incus.get_container_name(name)
        out.green(f"Stopping container {full!r} ...")
        Container(ix, full).stop(force=True)
    out.green("LXC containers stopped.")


# -------------------------------------------------------------------
# destroy
# -------------------------------------------------------------------


def destroy_cmd_options(parser):
    parser.add_argument(
        "names",
        nargs="*",
        metavar="NAME",
        help="Container name(s) to destroy.",
    ).completer = _container_completer
    parser.add_argument(
        "--all",
        dest="destroy_all",
        action="store_true",
        help="Destroy all relay containers (keeps DNS, builder, images).",
    )


def destroy_cmd(args, out):
    """Stop and delete containers."""
    ix = Incus(out)

    if args.destroy_all:
        _destroy_relays(ix, out)
    elif args.names:
        for name in args.names:
            full = ix.get_container_name(name)
            out.green(f"Destroying container {full!r} ...")
            ix.get_relay_container(name).destroy()
    else:
        out.red("Error: specify container name(s) or --all.")
        return 1

    ix.write_ssh_config()
    out.green("LXC containers destroyed.")
    return 0


# -------------------------------------------------------------------
# test helpers
# -------------------------------------------------------------------


def _add_test_relay_args(parser):
    parser.add_argument(
        "relay",
        metavar="RELAY",
        help="Relay name (e.g. cm0).",
    ).completer = _container_completer
    parser.add_argument(
        "relay2",
        nargs="?",
        default=None,
        metavar="RELAY2",
        help="Optional second relay for cross-relay tests.",
    ).completer = _container_completer


def test_cmdeploy_cmd_options(parser):
    _add_test_relay_args(parser)
    parser.add_argument(
        "--no-dns",
        dest="no_dns",
        action="store_true",
        help="Deploy the relay with only an IPv4",
    )


def test_cmdeploy_cmd(args, out):
    """Run cmdeploy integration tests inside the builder container."""
    ix = Incus(out)
    ct = ix.get_running_relay(args.relay)
    driver = CmdeployDriver(ct, out)
    driver.no_dns = bool(args.no_dns)
    if not driver.check_init():
        return 1

    if not driver.get_builder():
        return 1

    relays = [ct]
    second_domain = None
    if args.relay2:
        ct2 = ix.get_running_relay(args.relay2)
        relays.append(ct2)

        # Cross-relay DNS check
        for a, b in [(ct, ct2), (ct2, ct)]:
            drv_b = DRIVER_BY_NAME[b.driver_name](b, out)
            if _is_ip_address(drv_b.get_test_domain_or_ip()):
                continue

            if not args.no_dns:
                mx = a.bash(f"dig {b.domain} MX +short", check=False)
                if not mx or not mx.strip():
                    out.red(f"DNS check failed: {a.name} cannot resolve MX for {b.domain}")
                    return 1

        drv_cls = DRIVER_BY_NAME.get(ct2.driver_name)
        second_domain = drv_cls(ct2, out).get_test_domain_or_ip()

    return driver.run_tests(second_domain=second_domain)


# -------------------------------------------------------------------
# test-madmail
# -------------------------------------------------------------------


def test_madmail_cmd_options(parser):
    _add_test_relay_args(parser)
    parser.add_argument(
        "--cool",
        action="store_true",
        help="Minimal colored output (pass --cool to madmail test suite).",
    )
    parser.add_argument(
        "--simple",
        action="store_true",
        default=True,
        help="Run only simpler tests (1-6). Enabled by default.",
    )
    parser.add_argument(
        "--all",
        action="store_false",
        dest="simple",
        help="Run all tests (disables --simple).",
    )


def test_madmail_cmd(args, out):
    """Run madmail E2E tests inside the builder container."""
    ix = Incus(out)
    ct = ix.get_running_relay(args.relay)
    driver = MadmailDriver(ct, out)
    if not driver.check_init():
        return 1

    if not driver.get_builder():
        return 1

    second_domain = None
    if args.relay2:
        ct2 = ix.get_running_relay(args.relay2)
        drv2 = MadmailDriver(ct2, out)
        second_domain = drv2.get_test_domain_or_ip()

    return driver.run_tests(
        second_domain=second_domain,
        cool=args.cool,
        simple=args.simple,
    )


# -------------------------------------------------------------------
# minitest
# -------------------------------------------------------------------


def _resolve_relay_addr(name, ix, out):
    if _is_ip_address(name):
        return name

    ct = ix.get_running_relay(name)
    drv_cls = DRIVER_BY_NAME.get(ct.driver_name)
    if drv_cls is None:
        out.red(f"Container {ct.shortname!r} has not been deployed.")
        return None
    return drv_cls(ct, out).get_test_domain_or_ip()


def test_mini_cmd_options(parser):
    _add_test_relay_args(parser)


def test_mini_cmd(args, out):
    """Run mini-test integration tests inside the builder container."""
    out.print(f"cmlxc {__version__}")
    ix = Incus(out)
    if not _check_init(ix, out):
        return 1

    bld_ct = _get_running_builder(ix, out)
    if not bld_ct:
        return 1

    relay1 = _resolve_relay_addr(args.relay, ix, out)
    if relay1 is None:
        return 1

    relay2 = None
    if args.relay2:
        relay2 = _resolve_relay_addr(args.relay2, ix, out)
        if relay2 is None:
            return 1

    with out.section("test-mini"):
        test_dir = Path(__file__).parents[1] / "relay_minitest"
        out.print("Syncing test files ...")
        bld_ct.sync_mini_tests(test_dir)

        out.print(f"Running tests against {relay1} ...")
        ret = bld_ct.run_mini_tests(relay1, relay2, out)
        if ret:
            out.red(f"test-mini failed (exit {ret})")
            return ret

    return 0


# -------------------------------------------------------------------
# status
# -------------------------------------------------------------------


def status_cmd_options(parser):
    parser.add_argument(
        "names",
        nargs="*",
        metavar="NAME",
        help="Optional container name(s) (short or long) to show.",
    ).completer = _container_completer
    parser.add_argument(
        "--host",
        action="store_true",
        help="Show detailed host configuration (DNS forwarding, resolvectl).",
    )


def status_cmd(args, out):
    """Show status of managed containers and host configuration."""
    ix = Incus(out)
    containers = ix.list_managed()

    if args.names:
        matched = []
        seen = set()
        for name in args.names:
            long_name = ix.get_container_name(name)
            found = [c for c in containers if c["name"] in (name, long_name)]
            if found:
                for c in found:
                    if c["name"] not in seen:
                        seen.add(c["name"])
                        matched.append(c)
            else:
                out.red(f"Container {name!r} not found.")
        containers = matched
        if not containers:
            return 1

    if not containers:
        out.print("No cmlxc-managed containers found.")
        return 0

    # Get storage pool path for display
    storage_path = None
    data = ix.run_json(
        ["storage", "show", "default"],
        check=False,
    )
    if data:
        storage_path = data.get("config", {}).get("source")
    msg = "Container status"
    if storage_path:
        msg += f": {storage_path}"
    out.section_line(msg)

    dns_ip = None
    for c in containers:
        _print_container_status(out, c, ix)
        if c["name"] == DNS_CONTAINER_NAME:
            dns_ip = c["ip"]

    if args.names and not args.host:
        return 0

    out.section_line("Host ssh and DNS configuration (not required for cmlxc itself)")
    need_ssh = _print_ssh_status(out, ix, host=args.host)
    need_dns = _print_dns_forwarding_status(out, dns_ip, host=args.host)
    if (need_ssh or need_dns) and not args.host:
        out.print("use 'cmlxc status --host' for setup instructions")
    return 0


def _print_container_status(out, c, ix):
    cname = c["name"]
    is_running = c.get("status") == "Running"

    tag = "running" if is_running else "STOPPED"
    driver = c.get("driver")
    deploy_label = f"  [{driver}]" if driver else ""
    out.print(f"{cname:20s} {tag}{deploy_label}")

    domain = c.get("domain", "")
    ip = c.get("ip") or "?"
    ipv6 = c.get("ipv6")
    out.print(f"{domain:20s} IPv4 {ip}  IPv6 {ipv6}")

    detail_out = out.new_prefixed_out(" " * 21)

    if driver and is_running:
        bld_ct = BuilderContainer(ix)
        if bld_ct.is_running:
            ct = ix.get_relay_container(cname)
            repo_path = ct.get_repo_path(driver)
            status = bld_ct.get_repo_status(repo_path)
            if status:
                source_ref = c.get("source") or "?"
                detail_out.print(f"source: {source_ref}")
                detail_out.print(f"        {status}")
                detail_out.print(f"builder: {repo_path}")
        if driver == "madmail":
            ct = ix.get_relay_container(cname)
            print_admin_info(detail_out, ct, ip)

    elif cname == BUILDER_CONTAINER_NAME and is_running:
        _print_builder_repos(detail_out, BuilderContainer(ix))
    out.print()


def _print_builder_repos(out, ct):
    try:
        for name in DRIVER_BY_NAME:
            path = f"/root/{name}-git-main"
            status = ct.get_repo_status(path)
            if status:
                out.print(f"{name}: {status}")
    except Exception:
        out.print("repos: (unavailable)")


def _print_ssh_status(out, ix, *, host=False):
    ssh_cfg = ix.ssh_config_path
    if ix.check_ssh_include():
        out.green(f"SSH: ~/.ssh/config includes {ix.ssh_config_path} ✓")
        return False

    msg = f"SSH: ~/.ssh/config does NOT include {ix.ssh_config_path}"
    if host:
        out.red(msg)
        sub = out.new_prefixed_out()
        sub.print("Add to ~/.ssh/config:")
        sub.print(f"    Include {ssh_cfg}")
        return False

    out.print(msg)
    return True


def _print_dns_forwarding_status(out, dns_ip, *, host=False):
    sub = out.new_prefixed_out()
    if not dns_ip:
        out.red("DNS: ns-localchat container not found")
        return False

    try:
        rv = subprocess.run(
            ["resolvectl", "status", "incusbr0"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        sub.print("DNS: cannot check forwarding (resolvectl not found)")
        return False
    except OSError as exc:
        sub.red(f"DNS: failed to check forwarding ({exc})")
        return False

    if rv.returncode != 0:
        sub.red("DNS: failed to query resolvectl status for incusbr0")
        if rv.stderr.strip():
            sub.print(rv.stderr.strip())
        return False

    dns_ok = dns_ip in rv.stdout and "localchat" in rv.stdout
    if dns_ok:
        out.green(f"DNS: .localchat forwarding to {dns_ip} ✓")
        return False

    msg = "DNS: .localchat forwarding NOT configured"
    if host:
        out.red(msg)
        sub.print("Run:")
        sub.print(f"    sudo resolvectl dns incusbr0 {dns_ip}")
        sub.print("    sudo resolvectl domain incusbr0 ~localchat")
        return False

    out.print(msg)
    return True


# -------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------

SUBCOMMANDS = [
    ("init", init_cmd, init_cmd_options),
    ("test-cmdeploy", test_cmdeploy_cmd, test_cmdeploy_cmd_options),
    ("test-madmail", test_madmail_cmd, test_madmail_cmd_options),
    ("test-mini", test_mini_cmd, test_mini_cmd_options),
    ("status", status_cmd, status_cmd_options),
    ("start", start_cmd, start_cmd_options),
    ("stop", stop_cmd, stop_cmd_options),
    ("destroy", destroy_cmd, destroy_cmd_options),
]

DRIVER_BY_NAME = {"cmdeploy": CmdeployDriver, "madmail": MadmailDriver}


def _add_subcommand(subparsers, name, func, addopts, shared):
    doc = func.__doc__.strip()
    help_text = doc.split("\n")[0].strip(".")
    p = subparsers.add_parser(
        name,
        description=doc,
        help=help_text,
        parents=[shared],
    )
    p.set_defaults(func=func)
    if addopts is not None:
        addopts(p)


def get_parser():
    """Build the argument parser for the ``cmlxc`` CLI."""
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="count",
        default=0,
        help="Increase verbosity (can be repeated: -v, -vv).",
    )

    parser = argparse.ArgumentParser(
        prog="cmlxc",
        description=f"cmlxc {__version__} -- Manage local Incus/LXC containers for chatmail relay testing.",
        parents=[shared],
    )
    parser.add_argument("--version", action="version", version=f"{__version__}")
    parser.set_defaults(func=None)

    subparsers = parser.add_subparsers(title="subcommands")

    # init
    _add_subcommand(subparsers, *SUBCOMMANDS[0], shared)

    # Deploy subcommands (self-registered by driver classes)
    for drv in DRIVER_BY_NAME.values():
        drv.add_subcommand(
            subparsers,
            shared,
            completer=_container_completer,
        )

    # Remaining static subcommands (test, status, lifecycle)
    for name, func, addopts in SUBCOMMANDS[1:]:
        _add_subcommand(subparsers, name, func, addopts, shared)

    return parser


def main(args=None):
    """Provide main entry point for 'cmlxc' CLI invocation."""
    parser = get_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args(args=args)
    if args.func is None:
        return parser.parse_args(["-h"])

    # GitHub Actions: auto-enable max verbosity when debug logging is on
    if not args.verbose and os.environ.get("RUNNER_DEBUG") == "1":
        args.verbose = 3

    out = Out(verbosity=args.verbose)
    try:
        res = args.func(args, out)
        if res is None:
            res = 0
        return res
    except SetupError as exc:
        out.red(str(exc))
        return 1
    except KeyboardInterrupt:
        out.red("\nKeyboardInterrupt: Operation cancelled.")
        out.print("Some containers may be left in a partial state.")
        out.print("Use 'cmlxc status' to check or 'cmlxc destroy' to clean up.")
        raise SystemExit(130)
