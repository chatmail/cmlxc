"""Docker driver and management commands for cmlxc.

Contains the DockerDriver (``cmlxc docker deploy``) and the
``docker logs / ps / shell / pull`` CLI subcommands.
"""

import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from cmlxc.container import SetupError, address_records
from cmlxc.driver_base import Driver, parse_source
from cmlxc.driver_cmdeploy import (
    TEST_INI_OVERRIDES,
    CmdeployDriver,
    ensure_ipv6_known,
    make_ini_script,
    run_test_cmdeploy,
    verify_dual_stack_zone,
    write_ini,
)
from cmlxc.incus import Incus

DOCKER = "docker"
DOCKER_COMPOSE_SERVICE = "chatmail"
DOCKER_IMAGE_TAG = "chatmail-relay"
GHCR_IMAGE = "ghcr.io/chatmail/docker"
DEFAULT_COMPOSE_URL = (
    "https://raw.githubusercontent.com/chatmail/docker/main/docker-compose.yaml"
)

# Validates the TAG portion of ``docker:TAG``.  Allows colons and slashes
# so users can pass ``image:version`` or ``registry/repo:tag`` forms.
_DOCKER_TAG_RE = re.compile(r"^[a-zA-Z0-9._:/-]+$")


def _add_relay_arg(parser, completer=None, *, help="Relay container name."):
    """Add the positional RELAY argument with optional tab-completion."""
    arg = parser.add_argument("relay", metavar="RELAY", help=help)
    if completer:
        arg.completer = completer


def _parse_inject_tag(source_arg):
    """Extract and validate tag from a ``docker:TAG`` source argument.

    Returns the tag string if *source_arg* starts with ``docker:`` and the
    tag passes validation, ``None`` if the prefix is absent, or raises
    ``ValueError`` if the prefix is present but the tag is empty/malformed.
    """
    if not source_arg.startswith("docker:"):
        return None
    tag = source_arg[len("docker:") :]
    if not tag or not _DOCKER_TAG_RE.match(tag):
        raise ValueError(f"Invalid Docker tag: {tag!r}")
    return tag


# -------------------------------------------------------------------
# Image helpers
# -------------------------------------------------------------------


def image_tag(sha):
    """Docker image tag for a given git SHA."""
    return f"{DOCKER_IMAGE_TAG}:{sha[:12]}"


def ensure_docker(ct):
    """Install Docker engine in container if not present."""
    if ct.bash_get("docker info >/dev/null 2>&1") is not None:
        return
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
        # Docker iptables rules conflict with LXC-managed networking.
        printf '{"iptables": false}\\n' > /etc/docker/daemon.json
        systemctl enable --now docker
    """)


def inject_image_from_host(ct, tag, out):
    """Pipe a locally-built image from the host Docker daemon into the relay."""
    out.print(f"  Injecting {tag} into {ct.shortname} ...")
    cmd = f"docker save {shlex.quote(tag)} | incus exec {ct.name} -- docker load"
    ret = out.shell(cmd)
    if ret:
        raise SetupError(f"Failed to inject {tag} into {ct.name}")
    ct.bash(f"docker tag {shlex.quote(tag)} {DOCKER_IMAGE_TAG}:latest")


def pull_image(ct, tag, out):
    """Pull a Docker image from GHCR into a container and tag locally.

    Returns the relay git SHA extracted from image labels, or None.
    """
    ref = f"{GHCR_IMAGE}:{tag}"
    ensure_docker(ct)
    out.print(f"  Pulling {ref} ...")
    try:
        ct.bash(f"docker pull {ref}")
    except subprocess.CalledProcessError:
        out.red(f"  Failed to pull {ref}")
        return None
    ct.bash(f"docker tag {ref} {DOCKER_IMAGE_TAG}:latest")
    sha = get_image_label_sha(ct, ref)
    if sha:
        local_tag = image_tag(sha)
        ct.bash(f"docker tag {ref} {local_tag}")
        out.print(f"  Tagged as {local_tag}")
        return sha
    out.print(f"  Pulled {ref} (no SHA label found)")
    return None


def get_image_label_sha(ct, tag):
    """Read the relay commit SHA from a Docker image's OCI labels."""
    sha = ct.bash_get(
        f"docker inspect {tag}"
        " --format '{{index .Config.Labels \"org.opencontainers.image.revision\"}}'"
    )
    return sha.strip() if sha and sha.strip() else None


def _require_docker_relay(ix, name, out, running=True):
    """Return a docker-deployed relay container, or None.

    Pass running=False on failure paths, where starting a stopped container
    and waiting for its services would hang instead of reporting.
    """
    ct = ix.get_running_relay(name) if running else ix.get_relay_container(name)
    state = ct.get_deploy_state()
    if not state or state.get("driver") != DOCKER:
        out.red(f"Container {ct.shortname!r} is not a Docker deployment.")
        return None
    return ct


def dump_docker_diagnostics(ct, out, tail=80):
    """Print the post-mortem log set for a failed Docker relay.

    Single source for both the deploy-time healthcheck timeout and the
    "on failure" step of lxc-test.yml, which used to carry its own
    near-identical copy of these incantations.
    """
    svc = DOCKER_COMPOSE_SERVICE
    sections = [
        (
            f"docker logs {svc} (last {tail})",
            f"docker logs {svc} --tail {tail} 2>&1",
        ),
        (
            "healthcheck state",
            f"docker inspect {svc} --format '{{{{json .State.Health}}}}' 2>/dev/null",
        ),
        (
            "dovecot journal",
            f"docker exec {svc} journalctl -u dovecot --no-pager -n 30 2>&1",
        ),
        (
            "postfix journal",
            f"docker exec {svc} journalctl -u postfix --no-pager -n 30 2>&1",
        ),
        (
            "failed systemd units",
            f"docker exec {svc} systemctl --failed --no-pager 2>&1",
        ),
        (
            "dovecot -n (effective config)",
            f"docker exec {svc} dovecot -n 2>&1 | tail -40",
        ),
        (
            "mail TLS material",
            f"docker exec {svc} ls -la /etc/ssl/certs/mailserver.pem"
            " /etc/ssl/private/mailserver.key 2>&1",
        ),
    ]
    for label, cmd in sections:
        out.red(f"  --- {label} ---")
        output = ct.bash_get(cmd)
        if output:
            for line in output.strip().splitlines():
                out.print(f"  {line}")


def logs_docker_cmd(args, out):
    """Show Docker Compose logs from a deployed relay container."""
    ix = Incus(out)
    ct = _require_docker_relay(ix, args.relay, out, running=not args.diagnostics)
    if ct is None:
        return 1

    if args.diagnostics:
        dump_docker_diagnostics(ct, out)
        return 0

    follow = "-f " if args.follow else ""
    cmd = f"incus exec {ct.name} -- docker compose -f /opt/chatmail-docker/docker-compose.yaml logs {follow}--tail=100"
    return out.shell(cmd)


def _get_docker_services(ix, name):
    """Query running Docker Compose service names from a relay container."""
    raw = ix.run_output(
        [
            "exec",
            name,
            "--",
            "docker",
            "compose",
            "-f",
            "/opt/chatmail-docker/docker-compose.yaml",
            "ps",
            "--services",
            "--status",
            "running",
        ],
        check=False,
    )
    if not raw:
        return []
    return [s.strip() for s in raw.splitlines() if s.strip()]


def ps_docker_cmd(args, out):
    """Show running Docker Compose services in a deployed relay."""
    ix = Incus(out)
    ct = _require_docker_relay(ix, args.relay, out)
    if ct is None:
        return 1
    for svc in _get_docker_services(ix, ct.name):
        out.print(svc)
    return 0


def shell_docker_cmd(args, out):
    """Open an interactive shell (or run a command) in a Docker container."""
    ix = Incus(out)
    ct = _require_docker_relay(ix, args.relay, out)
    if ct is None:
        return 1
    svc = args.service
    if args.command:
        cmd_str = " ".join(shlex.quote(c) for c in args.command)
        cmd = [
            "incus",
            "exec",
            ct.name,
            "--",
            "docker",
            "exec",
            "-i",
            svc,
            "bash",
            "-c",
            cmd_str,
        ]
    else:
        cmd = [
            "incus",
            "exec",
            ct.name,
            "--",
            "docker",
            "exec",
            "-it",
            svc,
            "bash",
            "-l",
        ]
    return subprocess.call(cmd)


def pull_docker_cmd_options(parser, completer=None):
    _add_relay_arg(
        parser, completer, help="Relay container name to pull the image into."
    )
    parser.add_argument(
        "--tag",
        default="main",
        metavar="TAG",
        help="GHCR image tag to pull (default: main).",
    )


def pull_docker_cmd(args, out):
    """Pull a chatmail Docker image from GHCR into a relay container."""
    ix = Incus(out)
    ct = ix.get_running_relay(args.relay)
    with out.section(f"Pulling {GHCR_IMAGE}:{args.tag}"):
        sha = pull_image(ct, args.tag, out)
    if sha is None:
        out.red(f"Pull failed for {GHCR_IMAGE}:{args.tag}")
        return 1
    out.green(f"Done. Image: {DOCKER_IMAGE_TAG}:{sha[:12] if sha else 'latest'}")
    return 0


# -------------------------------------------------------------------
# Deployment driver
# -------------------------------------------------------------------


class DockerDriver(Driver):
    """Deploys chatmail relays via Docker Compose in LXC containers."""

    CLI_NAME = "docker"
    CLI_DOC = "Docker relay management (deploy, pull, logs, ps, shell)."
    DEFAULT_SOURCE_URL = CmdeployDriver.DEFAULT_SOURCE_URL
    REPO_NAME = CmdeployDriver.REPO_NAME
    # --source names a prebuilt image, not a git ref, so deploy does no
    # checkout.  DEFAULT_SOURCE_URL is only used by run_tests to check out the
    # relay repo to match the deployed image.
    SOURCE_IS_GIT_REF = False

    # Overrides the relay git ref used for the run_tests checkout; set by
    # `test-cmdeploy --relay-ref`.  Default: the SHA from the image label.
    relay_ref = None

    NESTING_CONFIG = {
        "security.nesting": "true",
        "security.syscalls.intercept.mknod": "true",
        "security.syscalls.intercept.setxattr": "true",
    }
    # CI runners have AppArmor enforcing, which blocks systemd inside
    # Docker-in-LXC. These overrides must not be used outside disposable CI.
    _CI_NESTING_EXTRA = {
        "security.privileged": "true",
        "raw.lxc": "lxc.apparmor.profile=unconfined",
    }

    @classmethod
    def get_nesting_config(cls, out=None):
        cfg = dict(cls.NESTING_CONFIG)
        # Exact match, not truthiness: os.environ.get("CI") is truthy for
        # "false" and "0", so a stray env var would silently switch on
        # privileged containers and unconfined apparmor.  Matches the
        # RUNNER_DEBUG == "1" check in cli.py.
        if os.environ.get("CI", "").lower() in ("true", "1"):
            if out is not None:
                out.red(
                    "  CI=1: launching PRIVILEGED container with apparmor"
                    " unconfined. Never do this outside disposable CI."
                )
            cfg.update(cls._CI_NESTING_EXTRA)
        return cfg

    @classmethod
    def source_arg_kwargs(cls):
        """Docker deploys from a prebuilt image, not a git ref.

        The inherited text advertises @ref, @latest, /path, ./path and
        URL@ref, none of which this driver implements.
        """
        return {
            "default": "",
            "help": (
                "Image to deploy: ghcr:TAG (pull from"
                f" {GHCR_IMAGE}) or docker:TAG (inject a locally built image"
                " from the host Docker daemon). Required unless --image is given."
            ),
        }

    @classmethod
    def add_cli_options(cls, parser, completer=None):
        super().add_cli_options(parser, completer=completer)
        parser.add_argument(
            "--image",
            metavar="PATH",
            help="Load a pre-exported image tarball into the relay.",
        )
        parser.add_argument(
            "--compose",
            default=DEFAULT_COMPOSE_URL,
            help="docker-compose.yaml URL to fetch (inject path only; default: chatmail/docker main).",
        )

    def configure_from_args(self, args):
        """Resolve --source/--image into the image the deploy should use.

        Runs inside the base make_cmd template, so argparse has already
        guaranteed that source, image and compose exist on *args*.
        """
        self.image_path = args.image
        self.compose_url = args.compose
        self.ghcr_tag = None
        self.inject_tag = None

        source_str = args.source or ""
        if source_str.startswith("ghcr:"):
            if self.image_path:
                raise SetupError(
                    "--image and --source ghcr:TAG are mutually exclusive."
                )
            self.ghcr_tag = source_str[len("ghcr:") :] or "main"
            return

        # raises ValueError on a malformed tag, which main() reports
        self.inject_tag = _parse_inject_tag(source_str)
        if self.inject_tag and self.image_path:
            raise SetupError("--image and --source docker:TAG are mutually exclusive.")
        if not self.inject_tag and not self.image_path:
            raise SetupError(
                "Specify an image: --source docker:TAG, --source ghcr:TAG,"
                " or --image PATH."
            )

    @classmethod
    def logs_add_cli_options(cls, parser, completer=None):
        _add_relay_arg(parser, completer)
        parser.add_argument(
            "-f",
            "--follow",
            action="store_true",
            help="Follow log output (like tail -f).",
        )
        parser.add_argument(
            "--diagnostics",
            action="store_true",
            help="Dump the full post-mortem log set instead of compose logs.",
        )

    @classmethod
    def ps_add_cli_options(cls, parser, completer=None):
        _add_relay_arg(parser, completer)

    @classmethod
    def shell_add_cli_options(cls, parser, completer=None):
        _add_relay_arg(parser, completer)
        parser.add_argument(
            "service",
            nargs="?",
            default=DOCKER_COMPOSE_SERVICE,
            help=f"Docker Compose service (default: {DOCKER_COMPOSE_SERVICE}).",
        )
        parser.add_argument(
            "command",
            nargs="*",
            default=[],
            metavar="CMD",
            help="Command to run (default: interactive bash).",
        )

    # (name, help, func, options_func) -- options_func may accept completer kwarg;
    # logs/ps/shell use {name}_add_cli_options classmethods instead (options_func=None)
    _DOCKER_SUBCOMMANDS = [
        (
            "logs",
            "Show Docker Compose logs from a deployed relay",
            logs_docker_cmd,
            None,
        ),
        ("ps", "Show running Docker Compose services", ps_docker_cmd, None),
        ("shell", "Open a shell in a Docker container", shell_docker_cmd, None),
        (
            "pull",
            "Pull a Docker image from GHCR into a relay",
            pull_docker_cmd,
            pull_docker_cmd_options,
        ),
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
                name,
                description=func.__doc__,
                help=help_text,
                parents=[shared],
            )
            p.set_defaults(func=func)
            classmethod_opts = getattr(cls, f"{name}_add_cli_options", None)
            if classmethod_opts is not None:
                classmethod_opts(p, completer=completer)
            elif addopts is not None:
                addopts(p, completer=completer)

    def run_deploy(self, *, source, ipv4_only=False):
        """Deploy Docker Compose relay into an LXC container.

        *source* is always None here (SOURCE_IS_GIT_REF is False); the image
        to deploy was resolved by configure_from_args.
        """
        with self.out.section(f"Preparing container: {self.ct.shortname}"):
            self.ct.ensure(
                ipv4_only=ipv4_only,
                image_candidates=["localchat-docker", "localchat-base"],
                extra_config=self.get_nesting_config(self.out),
            )

        t_total = time.time()
        self.deploy()
        elapsed = time.time() - t_total
        self.out.section_line(f"deploy docker complete ({elapsed:.1f}s)")

    def deploy(self):
        """Deploy chatmail via Docker Compose."""
        self.ct.check_deploy_lock(DOCKER)
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", self.ct.domain):
            raise SetupError(f"Unsafe domain value: {self.ct.domain!r}")
        self.ix.write_ssh_config()

        dns_ct = self.get_dns_container()
        # The relay resolves *.localchat through the DNS container, same as a
        # cmdeploy relay. DHCP's resolver knows nothing about that zone.
        self.ct.setup_resolvconf_localchat_nameserver(dns_ct.ipv4)

        with self.out.section("Installing Docker in relay"):
            ensure_docker(self.ct)

        if self.image_path:
            self._load_local_image()
        elif self.ghcr_tag:
            with self.out.section(f"Pulling image from GHCR ({self.ghcr_tag})"):
                sha = pull_image(self.ct, self.ghcr_tag, self.out)
                if sha is None:
                    raise SetupError(f"Failed to pull {GHCR_IMAGE}:{self.ghcr_tag}")
        elif self.inject_tag:
            with self.out.section(f"Injecting image ({self.inject_tag})"):
                inject_image_from_host(self.ct, self.inject_tag, self.out)

        with self.out.section("Fetching compose file"):
            self._fetch_compose_file()

        # Register domain after the pull so set_dns_records()'s recursor cache
        # wipe doesn't break public DNS resolution during the image pull.
        ensure_ipv6_known(self.ct)
        dns_ct.set_dns_records(self.ct.domain, address_records(self.ct))

        with self.out.section("Preparing chatmail.ini"):
            self._write_host_ini()

        with self.out.section("Starting Docker Compose"):
            self._start_compose()

        with self.out.section("Waiting for healthcheck"):
            self._wait_healthy()

        with self.out.section("Loading DNS zone"):
            self._load_dns(dns_ct)

        desc = ""
        if self.ghcr_tag:
            desc = f"ghcr:{self.ghcr_tag}"
        elif self.inject_tag:
            desc = f"docker:{self.inject_tag}"
        elif self.image_path:
            desc = f"image:{self.image_path}"
        sha = get_image_label_sha(self.ct, f"{DOCKER_IMAGE_TAG}:latest")
        if sha:
            desc += f" (relay {sha[:12]})"
        source = SimpleNamespace(description=desc) if desc else None
        # "dns", not "ipv4": this deploy does full DNS (both set_dns_records
        # calls above plus zone extraction).  Driver.__init__ reads the label
        # back into self.type, and test_cmdeploy_cmd skips cross-relay MX
        # verification for any relay whose type is not "dns".
        self.ct.write_deploy_state(DOCKER, source=source, deploy_type="dns")

    def _load_local_image(self):
        """Load a pre-exported image tarball into the relay."""
        with self.out.section(f"Loading image from {self.image_path}"):
            path = shlex.quote(str(self.image_path))
            cmd = f"cat {path} | incus exec {self.ct.name} -- docker load"
            ret = self.out.shell(cmd)
            if ret:
                raise SetupError(f"Failed to load image from {self.image_path}")
            loaded = self.ct.bash(
                f"docker images {DOCKER_IMAGE_TAG} --format '{{{{.Tag}}}}' | head -1"
            )
            if loaded and loaded.strip() != "latest":
                self.ct.bash(
                    f"docker tag {DOCKER_IMAGE_TAG}:{loaded.strip()}"
                    f" {DOCKER_IMAGE_TAG}:latest"
                )

    def _fetch_compose_file(self):
        """Download docker-compose.yaml into the relay; raise SetupError on failure."""
        dest = "/opt/chatmail-docker/docker-compose.yaml"
        self.ct.bash("mkdir -p /opt/chatmail-docker")
        # apt-helper is always present (part of apt); avoids installing curl/wget.
        result = self.ct.bash_get(
            f"/usr/lib/apt/apt-helper download-file"
            f" {shlex.quote(self.compose_url)} {dest} 2>&1"
        )
        if result is None:
            raise SetupError(f"Failed to fetch compose file from {self.compose_url}")

    def _write_host_ini(self):
        """Write chatmail.ini into the relay via the Docker image's Python."""
        ini_path = "/srv/chatmail/chatmail.ini"
        overrides = dict(TEST_INI_OVERRIDES)
        if self.ct.is_ipv6_disabled:
            overrides["disable_ipv6"] = "True"
        script = make_ini_script(self.ct.domain, ini_path, overrides)
        self.ct.bash(f"""
            mkdir -p /srv/chatmail
            docker run --rm \\
                --entrypoint /opt/cmdeploy/bin/python3 \\
                -v /srv/chatmail:/srv/chatmail \\
                {DOCKER_IMAGE_TAG}:latest \\
                -c "
{script}
"
        """)

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
        # NOTE: do NOT add `privileged: true` here as it causes Docker to mount a
        # fresh devtmpfs and request `a *:* rwm` in the sub-cgroup, which cgroup v2's
        # hierarchical eBPF filter on the parent LXC container denies, breaking
        # /dev/null access for Dovecot.
        self.ct.bash("""
            cat > /opt/chatmail-docker/docker-compose.override.yaml <<'OVERRIDE'
services:
  chatmail:
    volumes:
      - /srv/chatmail/chatmail.ini:/etc/chatmail/chatmail.ini
OVERRIDE
        """)

        self.ct.bash("""
            cd /opt/chatmail-docker
            docker compose down -v 2>/dev/null || true
            docker compose up -d --no-build
        """)

    def _wait_healthy(self, timeout=180, interval=5):
        """Poll Docker healthcheck until healthy or timeout."""
        since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.ct.bash_get(
                f"docker inspect {DOCKER_COMPOSE_SERVICE}"
                " --format '{{.State.Health.Status}}' 2>/dev/null"
            )
            s = status.strip() if status else ""
            if s == "healthy":
                self.out.print("  Container healthy.")
                return
            if self.out.verbosity >= 1:
                new_logs = self.ct.bash_get(
                    f"docker logs {DOCKER_COMPOSE_SERVICE} --since {since} 2>&1"
                )
                if new_logs:
                    lines = new_logs.splitlines()
                    if len(lines) > 20:
                        self.out.print(
                            f"  [docker] ... ({len(lines) - 20} lines skipped)"
                        )
                        lines = lines[-20:]
                    for line in lines:
                        self.out.print(f"  [docker] {line}")
                since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            time.sleep(interval)
        dump_docker_diagnostics(self.ct, self.out, tail=80)
        raise SetupError(f"Docker container not healthy after {timeout}s")

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
        zone_content = self.ct.bash_do(
            f"docker exec {svc} cmdeploy dns --ssh-host @local --zonefile /dev/stdout"
        )
        if zone_content:
            verify_dual_stack_zone(self.ct, zone_content)
            dns_ct.set_dns_records(self.ct.domain, zone_content)
        else:
            # Minimal address record fallback
            dns_ct.set_dns_records(self.ct.domain, address_records(self.ct))

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

    def run_tests(self, second_domain=None):
        """Execute the cmdeploy test suite against the Docker relay.

        The builder checkout must match the relay image so that
        ``test_deployed_state`` (which compares local ``git rev-parse HEAD``
        against ``/etc/chatmail-version``) passes.  When the venv already
        exists from a prior deploy, re-checkout if the current SHA differs
        from the image SHA.

        Set ``self.relay_ref`` to override the relay git ref used for the
        test checkout (default: SHA from the running image).
        """
        with self.out.section("cmdeploytest"):
            self._setup_docker_ssh_forwarding()
            self.bld_ct.write_relay_ssh_config(self.ct)

            ref = (
                self.relay_ref
                or get_image_label_sha(self.ct, f"{DOCKER_IMAGE_TAG}:latest")
                or "main"
            )
            venv_exists = self.bld_ct.bash_get(f"test -d {self.venv_path}") is not None
            if not venv_exists:
                self.out.print(
                    f"  Venv missing, initializing builder for {self.ct.shortname} ..."
                )
                source = parse_source(f"@{ref}", self.DEFAULT_SOURCE_URL)
                self.init_builder(source)

            self.out.print("Preparing chatmail.ini on builder ...")
            write_ini(
                self.bld_ct,
                self.ct,
                self.ct.domain,
                disable_ipv6=self.ct.is_ipv6_disabled,
            )
            return run_test_cmdeploy(self, self.get_test_domain_or_ip(), second_domain)
