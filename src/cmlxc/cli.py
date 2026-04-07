"""cmlxc -- Manage local chatmail relay containers via Incus.

Standard workflow: init -> deploy-cmdeploy/deploy-madmail -> test-cmdeploy/test-mini.
"""

import argparse
import subprocess
from pathlib import Path

import argcomplete

from cmlxc import driver_cmdeploy, driver_madmail
from cmlxc.incus import (
    BASE_IMAGE_ALIAS,
    BUILDER_CONTAINER_NAME,
    CMDEPLOY,
    DNS_CONTAINER_NAME,
    MADMAIL,
    ContainerBuilder,
    DeployConflictError,
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
        "--relay-repo",
        dest="relay_repo",
        help="Path to a local relay checkout to sync into the builder "
        "(instead of cloning from GitHub).",
    )
    parser.add_argument(
        "--madmail-repo",
        dest="madmail_repo",
        help="Path to a local madmail checkout to sync into the builder "
        "(instead of cloning from GitHub).",
    )


def init_cmd(args, out):
    """Initialize the environment (base image, DNS, builder container)."""
    ix = Incus(out)
    with out.section("Initializing cmlxc environment"):
        out.green(f"Ensuring {BASE_IMAGE_ALIAS} image ...")
        ix.ensure_base_image()

        out.green(f"Ensuring {DNS_CONTAINER_NAME} container ...")
        dns_ct = ix.get_container(DNS_CONTAINER_NAME)
        dns_ct.ensure()

        dns_ip = dns_ct.ipv4
        out.print(f"  {DNS_CONTAINER_NAME} IP: {dns_ip}")
        _print_dns_forwarding_status(out, dns_ip)

        out.green(f"Ensuring {BUILDER_CONTAINER_NAME} container ...")
        bld_ct = ix.get_container(BUILDER_CONTAINER_NAME)
        bld_ct.ensure()
        bld_ct.configure_dns(dns_ip)
        bld_ct.install_deps()

        out.print("  Syncing mini-test files ...")
        test_dir = Path(__file__).parents[1] / "relay_minitest"
        bld_ct.sync_mini_tests(test_dir)

        driver_cmdeploy.init_builder(bld_ct, out, local_repo=args.relay_repo)

        driver_madmail.init_builder(bld_ct, out, local_repo=args.madmail_repo)

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
    state = ct.get_deploy_state()
    if state is None:
        out.red(f"Container {ct.sname!r} has not been deployed.")
        out.print("Run 'cmlxc deploy-cmdeploy <name>' first.")
        return False
    if state["driver"] != CMDEPLOY:
        out.red(
            f"Container {ct.sname!r} was deployed with"
            f" {state['driver']!r}, not cmdeploy."
        )
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
# shared container-start helper
# -------------------------------------------------------------------


def _ensure_relay_containers(names, ix, out, ipv4_only=False, image_preference=None):
    dns_ct = ix.get_container(DNS_CONTAINER_NAME)
    dns_ct.wait_ready(timeout=5)
    sub = out.new_prefixed_out()
    sub.print(f"DNS container IP: {dns_ct.ipv4}")

    relays = [ix.get_container(n) for n in names]
    for ct in relays:
        out.green(f"Ensuring container {ct.name!r} ({ct.domain}) ...")
        ct.ensure(
            ipv4_only=ipv4_only,
            image_preference=image_preference,
        )

        sub.print("Configuring container hostname ...")
        ct.bash(f"echo '{ct.name}' > /etc/hostname")
        sub.print(f"IPv4 {ct.ipv4}, IPv6 {ct.ipv6}")

        sub.green(f"Container {ct.name!r} ready: {ct.domain} -> {ct.ipv4}")
        out.print()

    # Generate the unified SSH config
    out.green("Writing ssh-config ...")
    ssh_cfg = ix.write_ssh_config()
    sub.print(f"{ssh_cfg}")

    # Verify SSH via the generated config
    for ct in relays:
        sub.print(f"Verifying SSH to {ct.name} via ssh-config ...")
        if ct.verify_ssh(ssh_cfg):
            sub.print(f"SSH OK: ssh -F {ix.ssh_config_path} {ct.domain}")
        else:
            sub.red(f"WARNING: SSH verification failed for {ct.name}")

    # Print integration suggestions
    ssh_cfg = ix.ssh_config_path
    if not ix.check_ssh_include():
        sub.green(
            "\n(Optional) To use containers from any SSH client, add to ~/.ssh/config:"
        )
        sub.green(f"    Include {ssh_cfg}")

    return relays


# -------------------------------------------------------------------
# deploy-cmdeploy
# -------------------------------------------------------------------


def deploy_cmdeploy_cmd_options(parser):
    parser.add_argument(
        "names",
        nargs="+",
        metavar="NAME",
        help="One or more relay names (e.g. cm0 cm1).",
    ).completer = _container_completer
    parser.add_argument(
        "--ipv4-only",
        dest="ipv4_only",
        action="store_true",
        help="Create containers without IPv6 connectivity.",
    )


def deploy_cmdeploy_cmd(args, out):
    """Deploy a relay into a container using cmdeploy."""
    ix = Incus(out)
    if not _check_init(ix, out):
        return 1

    bld_ct = _get_running_builder(ix, out)
    if not bld_ct:
        return 1

    try:
        for name in args.names:
            with out.section(f"Preparing container setup: {name}"):
                _ensure_relay_containers(
                    [name],
                    ix,
                    out,
                    ipv4_only=args.ipv4_only,
                )
            ret = driver_cmdeploy.deploy([name], ix, out, bld_ct)
            if ret:
                return ret
        return 0
    except DeployConflictError as exc:
        out.red(f"Deploy conflict: {exc}")
        return 1


# -------------------------------------------------------------------
# deploy-madmail
# -------------------------------------------------------------------


def deploy_madmail_cmd_options(parser):
    parser.add_argument(
        "names",
        nargs="+",
        metavar="NAME",
        help="One or more relay names (e.g. mad0 mad1).",
    ).completer = _container_completer
    parser.add_argument(
        "--ipv4-only",
        dest="ipv4_only",
        action="store_true",
        help="Create containers without IPv6 connectivity.",
    )


def deploy_madmail_cmd(args, out):
    """Deploy a madmail relay service into a container."""
    ix = Incus(out)
    if not _check_init(ix, out):
        return 1

    bld_ct = _get_running_builder(ix, out)
    if not bld_ct:
        return 1

    with out.section("Preparing container setup"):
        _ensure_relay_containers(
            args.names,
            ix,
            out,
            ipv4_only=args.ipv4_only,
            image_preference="base",
        )

    try:
        return driver_madmail.deploy(
            args.names,
            ix,
            out,
            bld_ct,
        )
    except DeployConflictError as exc:
        out.red(f"Deploy conflict: {exc}")
        return 1


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
    parser.add_argument(
        "--reset",
        dest="reset",
        action="store_true",
        help="Destroy everything including DNS, builder, and images. "
        "Requires 'cmlxc init' afterwards.",
    )


def destroy_cmd(args, out):
    """Stop and delete containers."""
    ix = Incus(out)

    if args.reset:
        _destroy_all(ix, out)
        ix.write_ssh_config()
        out.green("Full reset complete. Run 'cmlxc init' to reinitialize.")
        return 0
    elif args.destroy_all:
        _destroy_relays(ix, out)
    elif args.names:
        for ct in map(ix.get_container, args.names):
            out.green(f"Destroying container {ct.name!r} ...")
            ct.destroy()
    else:
        out.red("Error: specify container name(s), --all, or --reset.")
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
        out.print(f"Writing {first_ct.ini.name} ...")
        driver_cmdeploy.write_ini(
            bld_ct, first_ct, disable_ipv6=first_ct.is_ipv6_disabled
        )

        bld_ct.push_chatmail_ini(first_ct.ini)

        relays = [ix.get_container(n) for n in relay_names]
        out.print("Setting up SSH access to relay containers ...")
        bld_ct.setup_ssh(relays)

        pytest_args = []
        env = {}
        if second_relay:
            pytest_args.extend(["--override-ini", "filterwarnings="])
            env["CHATMAIL_DOMAIN2"] = second_relay

        out.print(f"Running cmdeploy tests against {first_ct.domain} ...")

        ret = bld_ct.run_cmdeploy_tests(pytest_args, out, env=env)
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

    state = ct.get_deploy_state()
    if state is None:
        out.red(f"Container {ct.sname!r} has not been deployed.")
        return None

    if state["driver"] == MADMAIL:
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
    _print_ssh_status(out, ix)
    _print_dns_forwarding_status(out, dns_ip)
    return 0


def _deploy_state_label(ct):
    if not isinstance(ct, RelayContainer):
        return ""
    state = ct.get_deploy_state()
    if state is None:
        return "undeployed"
    return state["driver"]


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
    out.print(f"{domain:20s} IPv4 {ip}, IPv6 {ipv6}")

    detail_out = out.new_prefixed_out(" " * 21)
    if isinstance(ct, RelayContainer):
        detail_out.print(f"config: {ct.relay_dir.resolve()}")
    elif isinstance(ct, ContainerBuilder) and is_running:
        _print_builder_repos(detail_out, ct)
    out.print()


def _print_builder_repos(out, ct):
    try:
        for name, path in [("relay", "/root/relay"), ("madmail", "/root/madmail")]:
            if ct.bash(f"test -d {path}", check=False) is None:
                out.print(f"{name}: not installed")
                continue
            commit = ct.bash(
                f"cd {path} && git log --oneline -1 --no-decorate",
                check=False,
            )
            if commit:
                out.print(f"{name}: {commit.strip()}")
            else:
                out.print(f"{name}: synced from host")

        has_binary = (
            ct.bash("test -x /root/madmail/build/maddy", check=False) is not None
        )
        if has_binary:
            out.green("maddy: built")
        else:
            out.red("maddy: not built")
    except Exception:
        out.print("repos: (unavailable)")


def _print_ssh_status(out, ix):
    ssh_cfg = ix.ssh_config_path
    if ix.check_ssh_include():
        out.green(f"SSH: ~/.ssh/config includes {ix.ssh_config_path} ✓")
    else:
        out.red(f"SSH: ~/.ssh/config does NOT include {ix.ssh_config_path}")
        sub = out.new_prefixed_out()
        sub.print("Add to ~/.ssh/config:")
        sub.print(f"    Include {ssh_cfg}")


def _print_dns_forwarding_status(out, dns_ip):
    sub = out.new_prefixed_out()
    if not dns_ip:
        out.red("DNS: ns-localchat container not found")
        return
    try:
        rv = subprocess.run(
            ["resolvectl", "status", "incusbr0"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        sub.print("DNS: cannot check forwarding (resolvectl not found)")
        return
    except OSError as exc:
        sub.red(f"DNS: failed to check forwarding ({exc})")
        return

    if rv.returncode != 0:
        sub.red("DNS: failed to query resolvectl status for incusbr0")
        if rv.stderr.strip():
            sub.print(rv.stderr.strip())
        return

    dns_ok = dns_ip in rv.stdout and "localchat" in rv.stdout
    if dns_ok is True:
        out.green(f"DNS: .localchat forwarding to {dns_ip} ✓")
    else:
        out.red("DNS: .localchat forwarding NOT configured")
        sub.print("Run:")
        sub.print(f"    sudo resolvectl dns incusbr0 {dns_ip}")
        sub.print("    sudo resolvectl domain incusbr0 ~localchat")


# -------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------

SUBCOMMANDS = [
    ("init", init_cmd, init_cmd_options),
    ("deploy-cmdeploy", deploy_cmdeploy_cmd, deploy_cmdeploy_cmd_options),
    ("deploy-madmail", deploy_madmail_cmd, deploy_madmail_cmd_options),
    ("test-cmdeploy", test_cmdeploy_cmd, test_cmdeploy_cmd_options),
    ("test-mini", test_mini_cmd, test_mini_cmd_options),
    ("status", status_cmd, status_cmd_options),
    ("start", start_cmd, start_cmd_options),
    ("stop", stop_cmd, stop_cmd_options),
    ("destroy", destroy_cmd, destroy_cmd_options),
]


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
        description=("Manage local Incus/LXC containers for chatmail relay testing."),
        parents=[shared],
    )
    parser.set_defaults(func=None)

    subparsers = parser.add_subparsers(title="subcommands")
    for name, func, addopts in SUBCOMMANDS:
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
