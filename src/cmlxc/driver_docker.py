"""Docker driver, image builder, and management commands for cmlxc.

Contains the DockerDriver (``cmlxc docker deploy``), shared image helpers
(build, transfer, export, prune), and the ``docker build / list / prune``
CLI subcommands.
"""

import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from cmlxc.container import BuilderContainer, SetupError
from cmlxc.driver_base import Driver, __version__, parse_source, validate_relay_name
from cmlxc.driver_cmdeploy import (
    TEST_INI_OVERRIDES,
    CmdeployDriver,
    run_test_cmdeploy,
    write_ini,
)
from cmlxc.incus import Incus

DOCKER = "docker"
DOCKER_COMPOSE_SERVICE = "chatmail"
DOCKER_IMAGE_TAG = "chatmail-relay"
DOCKER_REPO_URL = "https://github.com/chatmail/docker.git"
GHCR_IMAGE = "ghcr.io/chatmail/docker"


def _has_zstd():
    return shutil.which("zstd") is not None


# -------------------------------------------------------------------
# Image helpers
# -------------------------------------------------------------------


def image_tag(sha):
    """Docker image tag for a given git SHA."""
    return f"{DOCKER_IMAGE_TAG}:{sha[:12]}"


def ensure_docker(ct):
    """Install Docker engine in container if not present.

    Enables security.nesting (required for Docker-in-LXC)
    and restarts the container if needed.
    """
    if ct.bash("docker info >/dev/null 2>&1", check=False) is not None:
        return
    ct.incus.run(
        [
            "config",
            "set",
            ct.name,
            "security.nesting=true",
            "security.syscalls.intercept.mknod=true",
        ]
    )
    ct.incus.run(["restart", ct.name])
    ct.wait_ready()
    ct.bash("""
        mkdir -p /etc/apt/keyrings
        /usr/lib/apt/apt-helper download-file \
            https://download.docker.com/linux/debian/gpg \
            /etc/apt/keyrings/docker.asc
        echo "deb [arch=$(dpkg --print-architecture) \
            signed-by=/etc/apt/keyrings/docker.asc] \
            https://download.docker.com/linux/debian \
            $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -qq
        apt-get install -y -qq \
            docker-ce docker-ce-cli containerd.io docker-compose-plugin
        mkdir -p /etc/docker
        printf '{"iptables": false}\\n' > /etc/docker/daemon.json
        systemctl enable --now docker
    """)


def ensure_docker_checkout(bld_ct, repo_path, out):
    """Clone or update chatmail/docker into <repo_path>/docker/."""
    docker_dir = f"{repo_path}/docker"
    if (
        bld_ct.bash(f"test -f {docker_dir}/docker-compose.yaml", check=False)
        is not None
    ):
        out.print("  docker/ checkout already present, pulling latest ...")
        bld_ct.bash(f"git -C {docker_dir} pull --ff-only", check=False)
        return
    out.print(f"  Cloning chatmail/docker into {docker_dir} ...")
    bld_ct.bash(f"git clone {DOCKER_REPO_URL} {docker_dir}")


def prepare_source_in_builder(bld_ct, out, source, ix):
    """Checkout relay source in builder and return the repo path.

    For @main: reuses the persistent git-main checkout.
    For other refs: copies git-main to /root/docker-build, checks out ref.
    For local paths: syncs to /root/docker-build.
    """
    CmdeployDriver.prep_builder(ix, out, bld_ct)
    git_main = f"/root/{CmdeployDriver.REPO_NAME}-git-main"

    if source.kind == "remote" and source.ref == "main":
        bld_ct.bash(f"cd {git_main} && git pull --ff-only origin main")
        ensure_docker_checkout(bld_ct, git_main, out)
        return git_main

    checkout = "/root/docker-build"
    if source.kind == "remote":
        bld_ct.bash(f"rm -rf {checkout} && cp -a {git_main} {checkout}")
        bld_ct.bash(f"""
            cd {checkout}
            git fetch origin
            git checkout -q {source.ref}
            git reset --hard -q origin/{source.ref} 2>/dev/null || true
            git clean -fdx
            if [ -f .gitmodules ]; then
                git submodule update --init --recursive
            fi
        """)
    else:
        bld_ct.bash(f"rm -rf {checkout}")
        bld_ct.sync_to(source.path, checkout)

    ensure_docker_checkout(bld_ct, checkout, out)
    return checkout


def get_relay_sha(bld_ct, repo_path):
    """Return git SHA of relay checkout in builder."""
    return bld_ct.bash(f"git -C {repo_path} rev-parse HEAD").strip()


def container_has_image(ct, sha):
    """Check if a container's Docker daemon has an image for this sha."""
    tag = image_tag(sha)
    return (
        ct.bash(f"docker image inspect {tag} >/dev/null 2>&1", check=False)
        is not None
    )


def build_image(bld_ct, repo_path, source, out, force_rebuild=False):
    """Build chatmail Docker image in builder, tag with git SHA."""
    sha = get_relay_sha(bld_ct, repo_path)
    tag = image_tag(sha)
    if not force_rebuild and container_has_image(bld_ct, sha):
        out.print(f"  Docker image {tag} already cached in builder.")
        return sha

    source_ref = source.ref or str(source.path)
    build_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out.print(f"  Building Docker image {tag} ...")
    bld_ct.bash(f"""
        cd {repo_path}
        docker compose -f docker/docker-compose.yaml build \
            --build-arg GIT_HASH={sha} \
            --build-arg SOURCE_REF={shlex.quote(source_ref)} \
            --build-arg BUILD_DATE={build_date}
        docker tag {DOCKER_IMAGE_TAG}:latest {tag}
    """)
    return sha


def transfer_image_to_relay(bld_ct, ct, sha, out):
    """Save image from builder Docker daemon, load into relay."""
    tag = image_tag(sha)
    out.print(f"  Transferring {tag} to {ct.shortname} ...")
    # Host-side pipe bridging two incus exec calls
    cmd = (
        f"incus exec {bld_ct.name} -- docker save {tag} | "
        f"incus exec {ct.name} -- docker load"
    )
    ret = out.shell(cmd)
    if ret:
        raise SetupError(f"Failed to transfer image {tag} to {ct.name}")
    ct.bash(f"docker tag {tag} {DOCKER_IMAGE_TAG}:latest")


def export_image(bld_ct, sha, output_path, out):
    """Export image tarball from builder, zstd-compressed if available."""
    tag = image_tag(sha)
    path = shlex.quote(str(output_path))
    compress = f"| zstd -o {path}" if _has_zstd() else f"> {path}"
    out.print(f"  Exporting {tag} to {output_path} ...")
    ret = out.shell(f"incus exec {bld_ct.name} -- docker save {tag} {compress}")
    if ret:
        raise SetupError(f"Failed to export image {tag}")


def pull_image(ct, tag, out):
    """Pull a Docker image from GHCR into a container and tag locally.

    Returns the relay git SHA extracted from image labels, or None.
    """
    ref = f"{GHCR_IMAGE}:{tag}"
    ensure_docker(ct)
    out.print(f"  Pulling {ref} ...")
    result = ct.bash(f"docker pull {ref}", check=False)
    if result is None:
        out.red(f"  Failed to pull {ref}")
        return None
    ct.bash(f"docker tag {ref} {DOCKER_IMAGE_TAG}:latest")
    sha = ct.bash(
        f"docker inspect {ref}"
        " --format '{{index .Config.Labels \"org.opencontainers.image.revision\"}}'",
        check=False,
    )
    if sha and sha.strip():
        sha = sha.strip()
        local_tag = image_tag(sha)
        ct.bash(f"docker tag {ref} {local_tag}")
        out.print(f"  Tagged as {local_tag}")
        return sha
    out.print(f"  Pulled {ref} (no SHA label found)")
    return None


def auto_prune_images(bld_ct, out, keep=3):
    """Keep newest ``keep`` chatmail-relay images, delete the rest."""
    raw = bld_ct.bash(
        f"docker images {DOCKER_IMAGE_TAG}"
        " --format '{{.Tag}} {{.CreatedAt}}' --no-trunc",
        check=False,
    )
    if not raw:
        return
    entries = []
    for line in raw.splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and parts[0] != "latest":
            entries.append((parts[0], parts[1]))
    if len(entries) <= keep:
        return
    entries.sort(key=lambda x: x[1], reverse=True)
    for tag, _ in entries[keep:]:
        out.print(f"  Pruning {DOCKER_IMAGE_TAG}:{tag} ...")
        bld_ct.bash(f"docker rmi {DOCKER_IMAGE_TAG}:{tag}", check=False)


def show_docker_df(bld_ct, out):
    """Display docker disk usage summary from builder."""
    raw = bld_ct.bash("docker system df", check=False)
    if raw:
        for line in raw.strip().splitlines():
            out.print(f"  {line}")


def prune_relay_containers(ix, level, out):
    """Prune Docker resources inside running docker-driver relay containers."""
    managed = ix.list_managed()
    relays = [
        c for c in managed
        if c.get("driver") == DOCKER and c.get("status") == "Running"
    ]
    if not relays:
        return
    flag = "-af" if level == "all" else "-f"
    for c in relays:
        name = c["name"]
        out.print(f"  Pruning Docker in {name} ...")
        ix.run_output(
            ["exec", name, "--", "docker", "system", "prune", flag],
            check=False,
        )


_PRUNE_COMMANDS = {
    "default": (
        "Removing stopped containers and dangling images ...",
        ["docker container prune -f", "docker image prune -f"],
    ),
    "deep": (
        "Removing build cache, unused volumes ...",
        [
            "docker container prune -f", "docker image prune -f",
            "docker builder prune -af", "docker volume prune -f",
        ],
    ),
    "all": (
        "Removing all unused images, build cache, and volumes ...",
        [
            "docker system prune -af", "docker builder prune -af",
            "docker volume prune -af",
        ],
    ),
}


def prune_docker_system(bld_ct, out, level="default"):
    """Prune Docker resources at the specified level."""
    msg, cmds = _PRUNE_COMMANDS[level]
    out.print(f"  {msg}")
    for cmd in cmds:
        bld_ct.bash(cmd, check=False)


def list_images(bld_ct):
    """Return list of dicts with tag, ref, sha, created for cached images."""
    raw = bld_ct.bash(
        f"docker images {DOCKER_IMAGE_TAG} --format '{{{{.Tag}}}}'",
        check=False,
    )
    if not raw:
        return []

    tags = [t for line in raw.splitlines() if (t := line.strip()) and t != "latest"]
    if not tags:
        return []

    fmt = (
        "'{{index .Config.Labels"
        ' "com.chatmail.source.ref"}}|'
        "{{index .Config.Labels"
        ' "org.opencontainers.image.revision"}}|'
        "{{index .Config.Labels"
        ' "org.opencontainers.image.created"}}\''
    )
    refs = " ".join(f"{DOCKER_IMAGE_TAG}:{t}" for t in tags)
    labels_raw = bld_ct.bash(
        f"docker inspect {refs} --format {fmt}",
        check=False,
    )
    label_lines = labels_raw.strip().splitlines() if labels_raw else []

    images = []
    for i, tag in enumerate(tags):
        ref, sha, created = "", "", ""
        if i < len(label_lines):
            parts = label_lines[i].strip().split("|", 2)
            if len(parts) == 3:
                ref, sha, created = parts
        images.append(
            {
                "tag": tag,
                "ref": ref or "?",
                "sha": sha[:12] if sha else "?",
                "created": created[:16] if created else "?",
            }
        )
    return images


# -------------------------------------------------------------------
# CLI subcommands: docker build, docker list, docker prune
# -------------------------------------------------------------------


def _get_builder(out):
    """Return (Incus, BuilderContainer) or exit with error code 1."""
    ix = Incus(out)
    if not ix.check_init():
        return None, None
    bld_ct = BuilderContainer(ix)
    if not bld_ct.is_running:
        out.red("Builder not running. Run 'cmlxc init' first.")
        return None, None
    return ix, bld_ct


def build_docker_cmd_options(parser):
    parser.add_argument(
        "--source",
        default="@main",
        metavar="SOURCE",
        help="Relay source: @ref, ./path, or URL@ref (default: @main).",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="Export image tarball to this host path.",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Rebuild even if an image for the current SHA exists.",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=3,
        metavar="N",
        help="Keep N newest images during auto-prune (default: 3, 0=disable).",
    )


def build_docker_cmd(args, out):
    """Build chatmail Docker image in the builder container."""
    ix, bld_ct = _get_builder(out)
    if bld_ct is None:
        return 1

    source = parse_source(args.source, CmdeployDriver.DEFAULT_SOURCE_URL)

    with out.section("Preparing relay source in builder"):
        out.print(f"  Source: {source.description}")
        repo_path = prepare_source_in_builder(bld_ct, out, source, ix)

    with out.section("Building Docker image"):
        ensure_docker(bld_ct)
        sha = build_image(
            bld_ct, repo_path, source, out, force_rebuild=args.force_rebuild
        )

    if args.output:
        with out.section(f"Exporting to {args.output}"):
            export_image(bld_ct, sha, Path(args.output), out)
        out.green(f"Image exported: {args.output}")

    if args.keep > 0:
        auto_prune_images(bld_ct, out, keep=args.keep)

    out.green(f"Done. Image: chatmail-relay:{sha[:12]}")
    return 0


def list_docker_cmd(args, out):
    """List cached Docker images in the builder."""
    _ix, bld_ct = _get_builder(out)
    if bld_ct is None:
        return 1

    if bld_ct.bash("docker info >/dev/null 2>&1", check=False) is None:
        out.print("No Docker installed in builder.")
        return 0

    images = list_images(bld_ct)
    if not images:
        out.print("No cached images found.")
        return 0

    out.print(f"{'TAG':<15s} {'REF':<25s} {'SHA':<14s} {'BUILT'}")
    for img in images:
        out.print(
            f"{img['tag']:<15s} {img['ref']:<25s} {img['sha']:<14s} {img['created']}"
        )
    return 0


def logs_docker_cmd_options(parser, completer=None):
    relay_arg = parser.add_argument(
        "relay",
        metavar="RELAY",
        help="Relay container name (e.g. cm0).",
    )
    if completer:
        relay_arg.completer = completer
    parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Follow log output (like tail -f).",
    )


def logs_docker_cmd(args, out):
    """Show Docker Compose logs from a deployed relay container."""
    ix = Incus(out)
    ct = ix.get_running_relay(args.relay)
    state = ct.get_deploy_state()
    if state is None or state.get("driver") != DOCKER:
        out.red(f"Container {ct.shortname!r} is not a Docker deployment.")
        return 1

    follow = "-f " if args.follow else ""
    cmd = f"incus exec {ct.name} -- docker compose -f /opt/chatmail-docker/docker-compose.yaml logs {follow}--tail=100"
    return out.shell(cmd)


def ps_docker_cmd_options(parser, completer=None):
    relay_arg = parser.add_argument(
        "relay",
        metavar="RELAY",
        help="Relay container name (e.g. dk0).",
    )
    if completer:
        relay_arg.completer = completer


def ps_docker_cmd(args, out):
    """Show running Docker Compose services in a deployed relay."""
    ix = Incus(out)
    ct = ix.get_running_relay(args.relay)
    for svc in ix._get_docker_services(ct.name):
        out.print(svc)


def shell_docker_cmd_options(parser, completer=None):
    relay_arg = parser.add_argument(
        "relay",
        metavar="RELAY",
        help="Relay container name (e.g. dock0).",
    )
    if completer:
        relay_arg.completer = completer
    parser.add_argument(
        "service",
        nargs="?",
        default=DOCKER_COMPOSE_SERVICE,
        metavar="SERVICE",
        help=f"Docker Compose service (default: {DOCKER_COMPOSE_SERVICE}).",
    )
    parser.add_argument(
        "command",
        nargs="*",
        default=[],
        metavar="CMD",
        help="Command to run (default: interactive bash).",
    )


def shell_docker_cmd(args, out):
    """Open an interactive shell (or run a command) in a Docker container."""
    ix = Incus(out)
    ct = ix.get_running_relay(args.relay)
    svc = args.service
    if args.command:
        cmd_str = " ".join(shlex.quote(c) for c in args.command)
        cmd = [
            "incus", "exec", ct.name, "--",
            "docker", "exec", "-i", svc, "bash", "-c", cmd_str,
        ]
    else:
        cmd = [
            "incus", "exec", ct.name, "--",
            "docker", "exec", "-it", svc, "bash", "-l",
        ]
    return subprocess.call(cmd)


def pull_docker_cmd_options(parser, completer=None):
    parser.add_argument(
        "--tag",
        default="main",
        metavar="TAG",
        help="GHCR image tag to pull (default: main).",
    )
    relay_arg = parser.add_argument(
        "--relay",
        metavar="RELAY",
        help="Transfer pulled image to this relay container.",
    )
    if completer:
        relay_arg.completer = completer


def pull_docker_cmd(args, out):
    """Pull a chatmail Docker image from GHCR into the builder."""
    ix, bld_ct = _get_builder(out)
    if bld_ct is None:
        return 1

    with out.section(f"Pulling {GHCR_IMAGE}:{args.tag}"):
        sha = pull_image(bld_ct, tag=args.tag, out=out)

    if sha is None:
        out.red(f"Pull failed for {GHCR_IMAGE}:{args.tag}")
        return 1

    if args.relay:
        ct = ix.get_running_relay(args.relay)
        with out.section(f"Transferring to {ct.shortname}"):
            ensure_docker(ct)
            transfer_image_to_relay(bld_ct, ct, sha, out)

    out.green(f"Done. Image: {DOCKER_IMAGE_TAG}:{sha[:12]}")
    return 0


def prune_docker_cmd_options(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--deep",
        action="store_true",
        help="Also prune dangling build cache, unused volumes, and relay containers.",
    )
    group.add_argument(
        "--all",
        dest="prune_all",
        action="store_true",
        help="Remove ALL unused images, build cache, volumes, and relay resources.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show disk usage only, do not prune.",
    )


def prune_docker_cmd(args, out):
    """Remove cached Docker images and build artifacts from the builder."""
    ix, bld_ct = _get_builder(out)
    if bld_ct is None:
        return 1

    if bld_ct.bash("docker info >/dev/null 2>&1", check=False) is None:
        out.print("No Docker installed in builder -- nothing to prune.")
        return 0

    out.print("Docker disk usage (builder):")
    show_docker_df(bld_ct, out)

    if args.dry_run:
        return 0

    if args.prune_all:
        level = "all"
    elif args.deep:
        level = "deep"
    else:
        level = "default"

    images = list_images(bld_ct)
    if images:
        out.print(f"Found {len(images)} cached image(s).")

    keep = 1 if level in ("deep", "all") else 3
    auto_prune_images(bld_ct, out, keep=keep)
    prune_docker_system(bld_ct, out, level=level)

    if level in ("deep", "all"):
        prune_relay_containers(ix, level, out)

    out.print()
    out.print("Docker disk usage after prune:")
    show_docker_df(bld_ct, out)

    if level == "all":
        out.green("All images, build cache, and volumes removed.")
    elif level == "deep":
        out.green("Deep prune complete.")
    else:
        out.green("Pruning complete.")

    return 0


# -------------------------------------------------------------------
# Deployment driver
# -------------------------------------------------------------------


class DockerDriver(Driver):
    """Deploys chatmail relays via Docker Compose in LXC containers."""

    CLI_NAME = "docker"
    CLI_DOC = "Docker relay management (deploy, build, pull, list, logs, shell, prune)."
    DEFAULT_SOURCE_URL = "https://github.com/chatmail/relay.git"
    REPO_NAME = "cmdeploy"
    REQUIRED_SOURCE_PATHS = ["cmdeploy"]

    NESTING_CONFIG = {
        "security.nesting": "true",
        "security.syscalls.intercept.mknod": "true",
        "security.syscalls.intercept.setxattr": "true",
    }
    # CI runners have AppArmor enforcing, which blocks systemd inside
    # Docker-in-LXC. On a real host the admin controls AppArmor themselves.
    _CI_NESTING_EXTRA = {
        "security.privileged": "true",
        "raw.lxc": "lxc.apparmor.profile=unconfined",
    }

    @classmethod
    def get_nesting_config(cls):
        cfg = dict(cls.NESTING_CONFIG)
        if os.environ.get("CI"):
            cfg.update(cls._CI_NESTING_EXTRA)
        return cfg

    @classmethod
    def add_cli_options(cls, parser, completer=None):
        super().add_cli_options(parser, completer=completer)
        parser.add_argument(
            "--image",
            metavar="PATH",
            help="Load a pre-exported image tarball instead of building.",
        )
        parser.add_argument(
            "--force-rebuild",
            action="store_true",
            help="Rebuild even if an image for the current SHA exists.",
        )

    # (name, help, func, options_func) -- options_func may accept completer kwarg
    _DOCKER_SUBCOMMANDS = [
        ("build", "Build chatmail Docker image in the builder container",
         build_docker_cmd, build_docker_cmd_options),
        ("list", "List cached Docker images in the builder",
         list_docker_cmd, None),
        ("logs", "Show Docker Compose logs from a deployed relay",
         logs_docker_cmd, logs_docker_cmd_options),
        ("ps", "Show running Docker Compose services",
         ps_docker_cmd, ps_docker_cmd_options),
        ("shell", "Open a shell in a Docker container",
         shell_docker_cmd, shell_docker_cmd_options),
        ("pull", "Pull a Docker image from GHCR",
         pull_docker_cmd, pull_docker_cmd_options),
        ("prune", "Remove cached Docker images from the builder",
         prune_docker_cmd, prune_docker_cmd_options),
    ]

    @classmethod
    def add_subcommand(cls, subparsers, shared, *, completer=None):
        """Register 'docker' with deploy/build/list/prune sub-subcommands."""
        docker_parser = subparsers.add_parser(
            cls.CLI_NAME,
            description=cls.CLI_DOC,
            help=cls.CLI_DOC.split(".")[0],
            parents=[shared],
        )
        docker_parser.set_defaults(func=lambda args, out: docker_parser.print_help())
        docker_subs = docker_parser.add_subparsers(title="docker subcommands")

        # docker deploy (special: uses driver make_cmd + add_cli_options)
        deploy_p = docker_subs.add_parser(
            "deploy",
            description="Deploy a chatmail relay via Docker Compose.",
            help="Deploy a chatmail relay via Docker Compose",
            parents=[shared],
        )
        deploy_p.set_defaults(func=cls.make_cmd())
        cls.add_cli_options(deploy_p, completer=completer)

        for name, help_text, func, addopts in cls._DOCKER_SUBCOMMANDS:
            p = docker_subs.add_parser(
                name, description=func.__doc__, help=help_text, parents=[shared],
            )
            p.set_defaults(func=func)
            if addopts is not None:
                try:
                    addopts(p, completer=completer)
                except TypeError:
                    addopts(p)

    @classmethod
    def make_cmd(cls):
        """Build the CLI command, with GHCR pull support via --source ghcr:TAG."""
        base_cmd = super().make_cmd()

        def cmd(args, out):
            source_str = getattr(args, "source", "")
            if source_str.startswith("ghcr:"):
                return cls._ghcr_deploy_cmd(args, out)
            return base_cmd(args, out)

        cmd.__doc__ = cls.CLI_DOC
        return cmd

    @classmethod
    def _ghcr_deploy_cmd(cls, args, out):
        """Deploy using a pre-built GHCR image (--source ghcr:TAG)."""
        try:
            validate_relay_name(args.name)
        except ValueError as exc:
            out.red(str(exc))
            return 1

        ix = Incus(out)
        ct = ix.get_relay_container(args.name)
        driver = cls(ct, out)
        if not driver.check_init():
            return 1
        if not driver.get_builder():
            return 1

        driver.configure_from_args(args)
        out.print(f"cmlxc {__version__}")
        driver.run_deploy(source=None, ipv4_only=args.ipv4_only)
        return 0

    def configure_from_args(self, args):
        self.image_path = args.image
        self.force_rebuild = args.force_rebuild
        self.ghcr_tag = None
        if args.source.startswith("ghcr:"):
            self.ghcr_tag = args.source[5:] or "main"

    def run_deploy(self, *, source, ipv4_only=False):
        """Deploy Docker Compose relay into an LXC container."""
        with self.out.section(f"Preparing container: {self.ct.shortname}"):
            self.ct.ensure(
                ipv4_only=ipv4_only,
                image_candidates=["localchat-docker", "localchat-base"],
                extra_config=self.get_nesting_config(),
            )

        t_total = time.time()
        self.deploy(source=source)
        elapsed = time.time() - t_total
        self.out.section_line(f"deploy docker complete ({elapsed:.1f}s)")

    def deploy(self, source=None):
        """Deploy chatmail via Docker Compose."""
        self.ct.check_deploy_lock(DOCKER)
        self.ix.write_ssh_config()

        dns_ct = self.configure_dns()

        dns_ct.set_dns_records(
            self.ct.domain,
            f"{self.ct.domain}. 3600 IN A {self.ct.ipv4}",
        )

        with self.out.section("Installing Docker in relay"):
            ensure_docker(self.ct)

        if self.image_path:
            self._load_local_image()
        elif self.ghcr_tag:
            self._pull_ghcr_image()
        else:
            self._build_and_transfer(source)

        with self.out.section("Starting Docker Compose"):
            self._start_compose()

        with self.out.section("Waiting for healthcheck"):
            self._wait_healthy()

        with self.out.section("Patching rate limits"):
            self._patch_container_ini()

        with self.out.section("Loading DNS zone"):
            self._load_dns(dns_ct)

        self.ct.write_deploy_state(DOCKER, source=source)

    def _load_local_image(self):
        """Load a pre-exported image tarball into the relay."""
        with self.out.section(f"Loading image from {self.image_path}"):
            path = shlex.quote(str(self.image_path))
            decompress = f"zstd -d < {path}" if _has_zstd() else f"cat {path}"
            cmd = f"{decompress} | incus exec {self.ct.name} -- docker load"
            ret = self.out.shell(cmd)
            if ret:
                raise SetupError(f"Failed to load image from {self.image_path}")
            loaded = self.ct.bash(
                f"docker images {DOCKER_IMAGE_TAG} --format '{{{{.Tag}}}}'"
                " | head -1"
            )
            if loaded and loaded.strip() != "latest":
                self.ct.bash(
                    f"docker tag {DOCKER_IMAGE_TAG}:{loaded.strip()}"
                    f" {DOCKER_IMAGE_TAG}:latest"
                )

    def _pull_ghcr_image(self):
        """Pull a pre-built image from GHCR directly into the relay."""
        with self.out.section(f"Pulling image from GHCR ({self.ghcr_tag})"):
            sha = pull_image(self.ct, self.ghcr_tag, self.out)
            if sha is None:
                raise SetupError(f"Failed to pull {GHCR_IMAGE}:{self.ghcr_tag}")

        with self.out.section("Preparing compose files"):
            git_main = self.get_git_main_path()
            ensure_docker_checkout(self.bld_ct, git_main, self.out)
            self.repo_path = git_main

    def _build_and_transfer(self, source):
        """Build the image in the builder and transfer to the relay."""
        with self.out.section("Preparing Docker build"):
            ensure_docker(self.bld_ct)
            ensure_docker_checkout(self.bld_ct, self.repo_path, self.out)

        with self.out.section("Building Docker image"):
            sha = build_image(
                self.bld_ct, self.repo_path, source, self.out,
                force_rebuild=self.force_rebuild,
            )

        with self.out.section("Transferring image to relay"):
            if container_has_image(self.ct, sha):
                self.out.print(f"  Image {image_tag(sha)} already on relay, skipping.")
            else:
                transfer_image_to_relay(self.bld_ct, self.ct, sha, self.out)

    def _start_compose(self):
        """Write .env, compose override, copy compose file, and start."""
        self.ct.bash(f"""
            mkdir -p /opt/chatmail-docker
            cd /opt/chatmail-docker
            cat > .env <<'DOTENV'
MAIL_DOMAIN={self.ct.domain}
CHATMAIL_IMAGE=chatmail-relay:latest
DOTENV
        """)
        # `cgroup: host` works on bare-metal Docker but not inside LXC --
        # systemd fails with "Failed to allocate notification socket".
        # Write a privileged override unless the user has their own.
        if self.ct.bash(
            "test -f /opt/chatmail-docker/docker-compose.override.yaml",
            check=False,
        ) is None:
            self.ct.bash("""
                cat > /opt/chatmail-docker/docker-compose.override.yaml <<'OVERRIDE'
services:
  chatmail:
    privileged: true
OVERRIDE
            """)

        if not self.image_path:
            cmd = (
                f"incus exec {self.bld_ct.name} --"
                f" cat {self.repo_path}/docker/docker-compose.yaml |"
                f" incus exec {self.ct.name} --"
                f" tee /opt/chatmail-docker/docker-compose.yaml > /dev/null"
            )
            self.out.shell(cmd, quiet=True)

        self.ct.bash("""
            cd /opt/chatmail-docker
            docker compose up -d --no-build
        """)

    def _wait_healthy(self, timeout=180, interval=5):
        """Poll Docker healthcheck until healthy or timeout."""
        verbose = self.out.verbosity >= 2
        since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.ct.bash(
                f"docker inspect {DOCKER_COMPOSE_SERVICE}"
                " --format '{{.State.Health.Status}}' 2>/dev/null",
                check=False,
            )
            s = status.strip() if status else ""
            if s == "healthy":
                self.out.print("  Container healthy.")
                return
            if verbose:
                new_logs = self.ct.bash(
                    f"docker logs {DOCKER_COMPOSE_SERVICE}"
                    f" --since {since} 2>&1",
                    check=False,
                )
                if new_logs:
                    for line in new_logs.splitlines():
                        self.out.print(f"  [docker] {line}")
                since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif self.out.verbosity >= 1 and s:
                self.out.print(f"  status: {s}")
            time.sleep(interval)
        self._dump_docker_logs(tail=80)
        raise SetupError(f"Docker container not healthy after {timeout}s")

    def _dump_docker_logs(self, tail=80):
        """Print recent Docker container logs for debugging."""
        svc = DOCKER_COMPOSE_SERVICE
        sections = [
            (f"docker logs {svc} (last {tail})",
             f"docker logs {svc} --tail {tail} 2>&1"),
            ("healthcheck state",
             f"docker inspect {svc} --format '{{{{json .State.Health}}}}' 2>/dev/null"),
            ("dovecot journal",
             f"docker exec {svc} journalctl -u dovecot --no-pager -n 30 2>&1"),
            ("postfix journal",
             f"docker exec {svc} journalctl -u postfix --no-pager -n 30 2>&1"),
            ("failed systemd units",
             f"docker exec {svc} systemctl --failed --no-pager 2>&1"),
        ]
        for label, cmd in sections:
            self.out.red(f"  --- {label} ---")
            output = self.ct.bash(cmd, check=False)
            if output:
                _print_indented(self.out, output)

    def _patch_container_ini(self):
        """Apply test rate-limit overrides inside the Docker container.

        Uses TEST_INI_OVERRIDES (shared with write_ini) to patch both the
        source ini and the deployed copy that filtermail reads.
        """
        svc = DOCKER_COMPOSE_SERVICE
        ini_paths = [
            "/etc/chatmail/chatmail.ini",
            "/usr/local/lib/chatmaild/chatmail.ini",
        ]
        sed_cmds = " && ".join(
            f"sed -i 's/^{k} = .*/{k} = {v}/' {path}"
            for path in ini_paths
            for k, v in TEST_INI_OVERRIDES.items()
        )
        self.ct.bash(
            f"docker exec {svc} bash -c \"{sed_cmds}\""
            f" && docker exec {svc} systemctl restart filtermail filtermail-incoming"
        )

    def _load_dns(self, dns_ct):
        """Extract DNS zone from Docker container and load into PowerDNS."""
        tmp = "/tmp/localchat-forward.conf"
        self.ct.push_file_content(
            tmp,
            f"""
            server:
              domain-insecure: "localchat"

            forward-zone:
              name: "localchat"
              forward-addr: {dns_ct.ipv4}
            """,
        )
        svc = DOCKER_COMPOSE_SERVICE
        self.ct.bash(
            f"docker cp {tmp} {svc}:/etc/unbound/unbound.conf.d/localchat-forward.conf"
            f" && docker exec {svc} systemctl restart unbound"
        )
        zone_content = self.ct.bash(
            f"docker exec {svc} cmdeploy dns --ssh-host @local --zonefile /dev/stdout",
            check=False,
        )
        if zone_content:
            dns_ct.set_dns_records(self.ct.domain, zone_content)
        else:
            # Minimal A record fallback
            dns_ct.set_dns_records(
                self.ct.domain,
                f"{self.ct.domain}. 3600 IN A {self.ct.ipv4}",
            )

    def _setup_docker_ssh_forwarding(self):
        """Rewrite authorized_keys on the LXC host to forward SSH into Docker.

        Tests use SSHExec (execnet over SSH) which lands on the LXC host.
        Services (dovecot, opendkim, postfix) run inside the Docker container.
        By wrapping the builder key with command="docker exec ...", every SSH
        session transparently enters the container.  The LXC host itself is
        managed via incus exec, so losing direct SSH access is fine.

        A wrapper script is needed because $SSH_ORIGINAL_COMMAND contains
        shell metacharacters (quotes, parens) from execnet's python bootstrap.
        Bare $SSH_ORIGINAL_COMMAND expansion would mangle them; bash -c with
        double-quoted expansion preserves the command correctly.
        """
        self.ct.push_file_content(
            "/usr/local/bin/docker-ssh-forward",
            f'#!/bin/bash\nexec docker exec -i {DOCKER_COMPOSE_SERVICE} bash -c "$SSH_ORIGINAL_COMMAND"',
            mode="755",
        )
        pub_key = self.ct.incus.ssh_key_path.with_suffix(".pub").read_text().strip()
        self.ct.bash("mkdir -p /root/.ssh && chmod 700 /root/.ssh")
        self.ct.push_file_content(
            "/root/.ssh/authorized_keys",
            f'command="/usr/local/bin/docker-ssh-forward" {pub_key}',
            mode="600",
        )

    def _get_image_relay_sha(self):
        """Read the relay commit SHA from the running Docker image's OCI labels."""
        sha = self.ct.bash(
            f"docker inspect {DOCKER_IMAGE_TAG}:latest"
            " --format '{{index .Config.Labels \"org.opencontainers.image.revision\"}}'",
            check=False,
        )
        return sha.strip() if sha and sha.strip() else None

    def run_tests(self, second_domain=None):
        """Execute the cmdeploy test suite against the Docker relay.

        The builder checkout must match the relay image so that
        ``test_deployed_state`` (which compares local ``git rev-parse HEAD``
        against ``/etc/chatmail-version``) passes.  When the venv already
        exists from a prior deploy, re-checkout if the current SHA differs
        from the image SHA.

        Set ``RELAY_REF`` in the environment to override the relay git ref
        used for the test checkout (default: SHA from the running image).
        """
        with self.out.section("cmdeploytest"):
            self._setup_docker_ssh_forwarding()
            self.bld_ct.write_relay_ssh_config(self.ct)

            ref = os.environ.get("RELAY_REF") or self._get_image_relay_sha() or "main"
            venv_exists = self.bld_ct.bash(
                f"test -d {self.venv_path}", check=False,
            ) is not None
            if not venv_exists:
                self.out.print(
                    f"  Venv missing, initializing builder for {self.ct.shortname} ..."
                )
                source = parse_source(f"@{ref}", self.DEFAULT_SOURCE_URL)
                self.init_builder(source)
            else:
                current_sha = get_relay_sha(self.bld_ct, self.repo_path)
                if current_sha != ref and not ref.startswith(current_sha):
                    self.out.print(
                        f"  Updating builder checkout to {ref} ..."
                    )
                    source = parse_source(f"@{ref}", self.DEFAULT_SOURCE_URL)
                    self.init_builder(source)

            self.out.print("Preparing chatmail.ini on builder ...")
            write_ini(self.bld_ct, self.ct, self.ct.domain, disable_ipv6=self.ct.is_ipv6_disabled)
            return run_test_cmdeploy(self, second_domain)
