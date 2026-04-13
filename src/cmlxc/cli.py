"""cmlxc -- Manage local chatmail relay containers via Incus.

Standard workflow: init -> deploy-cmdeploy/deploy-madmail -> test-cmdeploy/test-mini.
"""

import argparse
import subprocess
from pathlib import Path

import argcomplete

from cmlxc import driver_cmdeploy
from cmlxc.driver_base import (  # noqa: F401 (re-export)
    SourceSpec,
    __version__,
    parse_source,
)
from cmlxc.driver_cmdeploy import CmdeployDriver
from cmlxc.driver_madmail import MadmailDriver
from cmlxc.incus import (
    BASE_IMAGE_ALIAS,
    BUILDER_CONTAINER_NAME,
    CMDEPLOY,
    DNS_CONTAINER_NAME,
    MADMAIL,
    ContainerBuilder,
    Incus,
    RelayContainer,
    _is_ip_address,
)
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
    dns_ct = ix.get_container(DNS_CONTAINER_NAME)
    managed = ix.list_managed()
    dns_running = any(
        c["name"] == dns_ct.name and c["status"] == "Running" for c in managed
    )
    if not dns_running or not ix.find_image([BASE_IMAGE_ALIAS]):
        out.red("Error: cmlxc environment not initialized.")
        out.red("Please run 'cmlxc init' first to set up the base image and DNS.")
        return False
    return True


def _destroy_all(ix, out):
    managed = ix.list_managed()
    if not managed:
        out.print("No cmlxc-managed containers found.")
    else:
        for c in managed:
            ct = ix.get_container(c["name"])
            out.green(f"Destroying container {ct.name!r} ...")
            ct.destroy()
    ix.delete_images()


def _destroy_relays(ix, out):
    managed = ix.list_managed()
    relays = [
        c for c in managed if isinstance(ix.get_container(c["name"]), RelayContainer)
    ]
    if not relays:
        out.print("No relay containers found.")
    else:
        for c in relays:
            ct = ix.get_container(c["name"])
            out.green(f"Destroying container {ct.name!r} ...")
            ct.destroy()


def init_cmd_options(parser):
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Destroy everything (DNS, builder, images) before re-initializing.",
    )


def init_cmd(args, out):
    """Initialize the environment (base image, DNS, builder container)."""
    ix = Incus(out)

    if args.reset:
        with out.section("Full reset"):
            _destroy_all(ix, out)

    with out.section("Initializing cmlxc environment"):
        out.green(f"Ensuring {BASE_IMAGE_ALIAS} image ...")
        ix.ensure_base_image()

        out.green(f"Ensuring {DNS_CONTAINER_NAME} container ...")
        dns_ct = ix.get_container(DNS_CONTAINER_NAME)
        dns_ct.ensure()

        dns_ip = dns_ct.ipv4
        out.print(f"  {DNS_CONTAINER_NAME} IP: {dns_ip}")
        _print_dns_forwarding_status(out, dns_ip, host=False)

        out.green(f"Ensuring {BUILDER_CONTAINER_NAME} container ...")
        bld_ct = ix.get_container(BUILDER_CONTAINER_NAME)
        bld_ct.ensure()
        bld_ct.configure_dns(dns_ip)
        bld_ct.install_deps()

        out.print("  Syncing mini-test files ...")
        test_dir = Path(__file__).parents[1] / "relay_minitest"
        bld_ct.sync_mini_tests(test_dir)

        out.green(f"{BUILDER_CONTAINER_NAME} container ready.")


def _confirm_relays_running(ix, names, out):
    relays = [ix.get_container(n) for n in names]
    not_running = [ct for ct in relays if not ct.is_running]
    if not_running:
        out.red(f"Containers not running: {', '.join(c.name for c in not_running)}")
        out.print(f"Run 'cmlxc deploy-cmdeploy {' '.join(names)}' first.")
        return False
    return True


def _confirm_cmdeploy_deployed(ct, out):
    driver = ct.driver
    if driver is None:
        out.red(f"Container {ct.sname!r} has not been deployed.")
        out.print("Run 'cmlxc deploy-cmdeploy <name>' first.")
        return False
    if driver != CMDEPLOY:
        out.red(f"Container {ct.sname!r} was deployed with {driver!r}, not cmdeploy.")
        out.print("cmdeploy-test only supports cmdeploy-deployed relays.")
        return False
    return True


def _get_running_builder(ix, out):
    bld_ct = ix.get_container(BUILDER_CONTAINER_NAME)
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
    for ct in map(ix.get_container, args.names):
        state = ct.get_deploy_state()
        if state is None:
            out.red(
                f"Container {ct.sname!r} has not been deployed."
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
    for ct in map(ix.get_container, args.names):
        out.green(f"Stopping container {ct.name!r} ...")
        ct.stop(force=True)
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
        for ct in map(ix.get_container, args.names):
            out.green(f"Destroying container {ct.name!r} ...")
            ct.destroy()
    else:
        out.red("Error: specify container name(s) or --all.")
        return 1

    ix.write_ssh_config()
    out.green("LXC containers destroyed.")
    return 0


# -------------------------------------------------------------------
# cmdeploytest
# -------------------------------------------------------------------


def test_cmdeploy_cmd_options(parser):
    parser.add_argument(
        "names",
        nargs="+",
        metavar="NAME",
        help="One or more relay names (e.g. t0 t1).",
    ).completer = _container_completer


def test_cmdeploy_cmd(args, out):
    """Run cmdeploy integration tests inside the builder container."""
    out.print(f"cmlxc {__version__}")
    ix = Incus(out)
    if not _check_init(ix, out):
        return 1

    relay_names = list(args.names)
    if not _confirm_relays_running(ix, relay_names, out):
        return 1

    first_ct = ix.get_container(relay_names[0])
    if not _confirm_cmdeploy_deployed(first_ct, out):
        return 1

    bld_ct = _get_running_builder(ix, out)
    if not bld_ct:
        return 1

    second_relay = None
    if len(relay_names) > 1:
        second_relay = _resolve_relay_addr(relay_names[1], ix, out)
        if second_relay is None:
            return 1

    with out.section("cmdeploytest"):
        out.print("Preparing chatmail.ini on builder ...")
        driver_cmdeploy.write_ini(
            bld_ct, first_ct, disable_ipv6=first_ct.is_ipv6_disabled
        )

        relays = [ix.get_container(n) for n in relay_names]
        out.print("Setting up SSH access to relay containers ...")
        bld_ct.setup_ssh(relays)

        pytest_args = []
        env = {}
        if second_relay:
            pytest_args.extend(["--override-ini", "filterwarnings="])
            env["CHATMAIL_DOMAIN2"] = second_relay

        # Verify cross-relay DNS before running tests
        if len(relays) > 1:
            for ct in relays:
                for other in relays:
                    if ct is other:
                        continue
                    mx = ct.bash(
                        f"dig {other.domain} MX +short",
                        check=False,
                    )
                    if not mx or not mx.strip():
                        out.red(
                            f"DNS check failed: {ct.name} cannot"
                            f" resolve MX for {other.domain}"
                        )
                        return 1

        out.print(f"Running cmdeploy tests against {first_ct.domain} ...")
        ret = bld_ct.run_cmdeploy_tests(
            first_ct.get_repo_path(),
            first_ct.get_venv_path(),
            pytest_args,
            out,
            env=env,
        )
        if ret:
            out.red(f"test-cmdeploy failed (exit {ret})")
            return ret

    return 0


# -------------------------------------------------------------------
# minitest
# -------------------------------------------------------------------


def _resolve_relay_addr(name, ix, out):
    """Turn a relay name or raw IP into its test address."""
    if _is_ip_address(name):
        return name

    ct = ix.get_container(name)
    if not ct.is_running:
        out.red(f"Container {ct.name!r} is not running.")
        return None

    driver = ct.driver
    if driver is None:
        out.red(f"Container {ct.sname!r} has not been deployed.")
        return None

    if driver == MADMAIL:
        if not ct.ipv4:
            ct.wait_ready()
        return ct.ipv4
    return ct.domain


def test_mini_cmd_options(parser):
    parser.add_argument(
        "relays",
        nargs="+",
        metavar="RELAY",
        help="Relay names (e.g. t0 t1) or IP addresses.",
    ).completer = _container_completer


def test_mini_cmd(args, out):
    """Run mini-test integration tests inside the builder container."""
    out.print(f"cmlxc {__version__}")
    ix = Incus(out)
    if not _check_init(ix, out):
        return 1

    bld_ct = _get_running_builder(ix, out)
    if not bld_ct:
        return 1

    relay1 = _resolve_relay_addr(args.relays[0], ix, out)
    if relay1 is None:
        return 1

    relay2 = None
    if len(args.relays) > 1:
        relay2 = _resolve_relay_addr(args.relays[1], ix, out)
        if relay2 is None:
            return 1

    with out.section("test-mini"):
        test_dir = Path(__file__).parents[1] / "relay_minitest"
        out.print("Syncing test files ...")
        bld_ct.sync_mini_tests(test_dir)

        pytest_args = ["--relay1", relay1]
        if relay2:
            pytest_args.extend(["--relay2", relay2])

        out.print(f"Running tests against {relay1} ...")
        ret = bld_ct.run_mini_tests(pytest_args, out)
        if ret:
            out.red(f"test-mini failed (exit {ret})")
            return ret

    return 0


# -------------------------------------------------------------------
# status
# -------------------------------------------------------------------


def status_cmd_options(parser):
    parser.add_argument(
        "name",
        nargs="?",
        help="Optional container name (short or long) to show.",
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
    if args.name:
        long_name = ix.get_container_name(args.name)
        containers = [c for c in containers if c["name"] in (args.name, long_name)]

    if not containers:
        if args.name:
            out.red(f"Container {args.name!r} not found.")
            return 1
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

    if args.name:
        return 0

    out.section_line("Host ssh and DNS configuration")
    need_ssh = _print_ssh_status(out, ix, host=args.host)
    need_dns = _print_dns_forwarding_status(out, dns_ip, host=args.host)
    if (need_ssh or need_dns) and not args.host:
        out.print("use 'cmlxc status --host' for setup instructions")
    return 0


def _deploy_state_label(ct):
    if not isinstance(ct, RelayContainer):
        return ""
    driver = ct.driver
    return driver if driver else "undeployed"


def _print_container_status(out, c, ix):
    cname = c["name"]
    is_running = c.get("status") == "Running"

    ct = ix.get_container(cname)
    tag = "running" if is_running else "STOPPED"
    deploy_label = _deploy_state_label(ct)
    if deploy_label:
        deploy_label = f"  [{deploy_label}]"
    out.print(f"{cname:20s} {tag}{deploy_label}")

    domain = c.get("domain", "")
    ip = c.get("ip") or "?"
    ipv6 = c.get("ipv6")
    out.print(f"{domain:20s} IPv4 {ip}  IPv6 {ipv6}")

    detail_out = out.new_prefixed_out(" " * 21)
    if isinstance(ct, RelayContainer):
        driver_name = ct.driver
        if driver_name and is_running:
            bld_ct = ix.get_container(BUILDER_CONTAINER_NAME)
            if bld_ct.is_running:
                repo_path = ct.get_repo_path(driver_name)
                status = bld_ct.get_repo_status(repo_path)
                if status:
                    state = ct.get_deploy_state()
                    source_ref = state.get("source") or "?"
                    detail_out.print(f"source: {source_ref}")
                    detail_out.print(f"        {status}")
                    detail_out.print(f"builder: {repo_path}")

    elif isinstance(ct, ContainerBuilder) and is_running:
        _print_builder_repos(detail_out, ct)
    out.print()


def _print_builder_repos(out, ct):
    try:
        # Templates
        for name in ["cmdeploy", "madmail"]:
            path = f"/root/{name}-template"
            status = ct.get_repo_status(path)
            if status:
                out.print(f"{name} template: {status}")

        # Binary
        has_binary = (
            ct.bash("test -f /root/madmail-template/build/maddy", check=False)
            is not None
        )
        if has_binary:
            out.green("maddy: built")
    except Exception:
        out.print("repos: (unavailable)")


def _print_ssh_status(out, ix, *, host=False):
    """Returns True when SSH is not configured and host details were omitted."""
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
    """Returns True when DNS is not configured and host details were omitted."""
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

    if host:
        out.red("DNS: .localchat forwarding NOT configured")
        sub.print("Run:")
        sub.print(f"    sudo resolvectl dns incusbr0 {dns_ip}")
        sub.print("    sudo resolvectl domain incusbr0 ~localchat")
        return False

    out.print("DNS: .localchat forwarding NOT configured")
    return True


# -------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------

SUBCOMMANDS = [
    ("init", init_cmd, init_cmd_options),
    ("test-cmdeploy", test_cmdeploy_cmd, test_cmdeploy_cmd_options),
    ("test-mini", test_mini_cmd, test_mini_cmd_options),
    ("status", status_cmd, status_cmd_options),
    ("start", start_cmd, start_cmd_options),
    ("stop", stop_cmd, stop_cmd_options),
    ("destroy", destroy_cmd, destroy_cmd_options),
]

DEPLOY_DRIVERS = [CmdeployDriver, MadmailDriver]


def _add_subcommand(subparsers, name, func, addopts, shared):
    """Register a single subcommand on *subparsers*."""
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
    for drv in DEPLOY_DRIVERS:
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

    out = Out(verbosity=args.verbose)
    try:
        res = args.func(args, out)
        if res is None:
            res = 0
        return res
    except KeyboardInterrupt:
        out.red("\nKeyboardInterrupt: Operation cancelled.")
        out.print("Some containers may be left in a partial state.")
        out.print("Use 'cmlxc status' to check or 'cmlxc destroy' to clean up.")
        raise SystemExit(130)
