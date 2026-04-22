"""Base class for cmlxc deployment drivers.

Each driver subclass defines CLI metadata,
builder setup, and deploy orchestration hooks.
"""

import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

try:
    __version__ = version("cmlxc")
except PackageNotFoundError:
    __version__ = "unknown"

from cmlxc.container import (
    BASE_IMAGE_ALIAS,
    DNS_CONTAINER_NAME,
    BuilderContainer,
    DNSContainer,
)
from cmlxc.incus import Incus


@dataclass
class SourceSpec:
    """Parsed SOURCE argument for deploy --source."""

    kind: Literal["remote", "local"]
    url: str | None = None
    ref: str | None = None
    path: Path | None = None

    @property
    def description(self) -> str:
        """Return a descriptive string for this source."""
        if self.kind == "local":
            return f"local path {self.path}"
        return f"ref {self.ref!r} from {self.url}"


def parse_source(value: str, default_url: str) -> SourceSpec:
    """Turn a SOURCE string into a typed spec.

    Accepted forms:
      @ref           -- branch/tag on the default remote
      /path or ./path -- local directory
      URL@ref        -- custom remote at a given ref
    """
    if value.startswith(("/", ".")):
        return SourceSpec("local", path=Path(value))
    if value.startswith("@"):
        return SourceSpec("remote", url=default_url, ref=value[1:])
    if "://" in value:
        if "@" in value:
            url, _, ref = value.rpartition("@")
            if url:
                return SourceSpec("remote", url=url, ref=ref)
        return SourceSpec("remote", url=value, ref="main")
    if "/" in value:
        return SourceSpec("remote", url=default_url, ref=value)
    raise ValueError(f"Invalid SOURCE: {value!r}. Use @ref, /path, ./path, or URL@ref.")


_RELAY_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*$")


def validate_relay_name(name):
    """Raise ``ValueError`` if *name* is not a valid relay name."""
    if not _RELAY_NAME_RE.match(name):
        raise ValueError(
            f"Invalid relay name {name!r}."
            " Names must be alphanumeric (hyphens allowed)."
            " Did you mean '--source'?"
        )


class Driver:
    """Base for deployment drivers.

    Subclasses must set the class attributes
    and override ``init_builder`` and ``run_deploy``.
    """

    CLI_NAME: str
    CLI_DOC: str
    DEFAULT_SOURCE_URL: str
    REPO_NAME: str
    IMAGE_ALIAS: str | None = None
    REQUIRED_SOURCE_PATHS: list[str] = []

    def __init__(self, ct, out):
        self.ct = ct
        self.ix = ct.incus
        self.out = out
        self.repo_path = f"/root/relays/{self.REPO_NAME}-{ct.shortname}"
        self.venv_path = f"{self.repo_path}/venv"

    # ------------------------------------------------------------------
    # Pre-flight checks (shared by all drivers)
    # ------------------------------------------------------------------

    def check_init(self):
        """Verify that the cmlxc environment has been initialized."""
        managed = self.ix.list_managed()
        dns_running = any(
            c["name"] == DNS_CONTAINER_NAME and c["status"] == "Running"
            for c in managed
        )
        if not dns_running or not self.ix.find_image([BASE_IMAGE_ALIAS]):
            self.out.red("Error: cmlxc environment not initialized.")
            self.out.red(
                "Please run 'cmlxc init' first to set up the base image and DNS."
            )
            return False
        return True

    def get_builder(self):
        """Return the running builder container, or None.

        Stores the result as ``self.bld_ct`` for later use.
        """
        bld_ct = BuilderContainer(self.ix)
        if not bld_ct.is_running:
            self.out.red("Builder container not running.")
            self.out.red("Run 'cmlxc init' first.")
            return None
        self.bld_ct = bld_ct
        return bld_ct

    # ------------------------------------------------------------------
    # CLI registration
    # ------------------------------------------------------------------

    @classmethod
    def add_cli_options(cls, parser, completer=None):
        """Register ``deploy-*`` CLI options on *parser*."""
        parser.add_argument(
            "--source",
            default="@main",
            metavar="SOURCE",
            help="Driver source: @ref, /path, ./path, or URL@ref (default: @main).",
        )
        action = parser.add_argument(
            "name",
            metavar="NAME",
            help="Relay name.",
        )
        if completer is not None:
            action.completer = completer
        parser.add_argument(
            "--ipv4-only",
            dest="ipv4_only",
            action="store_true",
            help="Create containers without IPv6 connectivity.",
        )

    def configure_from_args(self, args):
        """Apply driver-specific CLI arguments. Override in subclasses."""

    # ------------------------------------------------------------------
    # Deploy protocol (override in subclasses)
    # ------------------------------------------------------------------

    def check_local_source(self, source):
        """Verify that a local source path looks like a valid checkout."""
        if source.kind != "local":
            return True
        path = source.path
        if not path.is_dir():
            self.out.red(f"Error: --source {path} is not a directory.")
            return False
        missing = [p for p in self.REQUIRED_SOURCE_PATHS if not (path / p).exists()]
        if missing:
            self.out.red(
                f"Error: --source {path} does not look like"
                f" a {self.REPO_NAME} checkout."
            )
            self.out.red(f"  Missing: {', '.join(missing)}")
            return False
        return True

    @classmethod
    def on_prep_builder(cls, out, bld_ct, tmp_dest):
        """Hook called by ``prep_builder`` after the git-main checkout is ready."""
        pass

    def on_init_relay(self, repo_path):
        """Hook called by ``init_builder`` after a relay checkout is ready."""
        self.out.print(f"  Running scripts/initenv.sh for {self.ct.shortname} ...")
        self.bld_ct.bash(f"cd {repo_path} && bash scripts/initenv.sh")

    def get_git_main_path(self):
        """Return path to the persistent git-main checkout on the builder."""
        return f"/root/{self.REPO_NAME}-git-main"

    @classmethod
    def prep_builder(cls, ix, out, bld_ct):
        """Hook called by ``cmlxc init`` to prepare toolchains and main checkout."""
        # Trust all repo paths inside the builder (ownership differs from host).
        bld_ct.bash("mkdir -p /root/relays")
        bld_ct.bash("git config --global --add safe.directory '*'", check=False)

        # In CI and some environments, SSH checkouts fail.
        # Ensure we always use HTTPS for GH and Codeberg.
        bld_ct.bash(
            "git config --global url.'https://github.com/'.insteadOf 'git@github.com:'",
            check=False,
        )
        bld_ct.bash(
            "git config --global"
            " url.'https://codeberg.org/'.insteadOf 'git@codeberg.org:'",
            check=False,
        )

        tmp_dest = f"/root/{cls.REPO_NAME}-git-main"
        if bld_ct.bash(f"test -d {tmp_dest}", check=False) is None:
            source = parse_source("@main", cls.DEFAULT_SOURCE_URL)
            bld_ct.setup_repo(tmp_dest, out, source)
        else:
            out.print(f"  Fetching {cls.REPO_NAME}-git-main from upstream ...")
            bld_ct.bash(f"cd {tmp_dest} && git fetch origin")

        # Install uv for faster venv/pip operations (used by initenv.sh)
        if bld_ct.bash("command -v uv", check=False) is None:
            out.print("  Installing uv ...")
            bld_ct.bash(
                "curl -LsSf https://astral.sh/uv/install.sh"
                " | env UV_INSTALL_DIR=/usr/local/bin sh",
            )

        # Driver-specific toolchain setup
        cls.on_prep_builder(out, bld_ct, tmp_dest)

    def init_builder(self, source):
        """Hook called by ``deploy-*`` to prepare a relay checkout and build."""
        self.prep_builder(self.ix, self.out, self.bld_ct)
        tmp_dest = self.get_git_main_path()
        repo_path = self.repo_path

        if source.kind == "remote":
            self.out.print(
                f"  Copying {self.REPO_NAME}-git-main to {repo_path} on builder"
            )
            self.bld_ct.bash(f"rm -rf {repo_path} && cp -a {tmp_dest} {repo_path}")
            is_sha = bool(re.fullmatch(r"[0-9a-f]{40}", source.ref or ""))
            if is_sha:
                # Shallow clone won't have arbitrary commits; fetch just this one.
                self.out.print(f"  Fetching {source.ref[:12]} ...")
                self.bld_ct.bash(
                    f"cd {repo_path} && "
                    f"git fetch --depth 1 origin {source.ref}"
                )
            elif source.ref != "main":
                self.out.print(f"  Checking out {source.ref!r} ...")
            self.bld_ct.bash(f"""
                cd {repo_path}
                git checkout -q {source.ref}
                git reset --hard -q origin/{source.ref} 2>/dev/null || true
                git clean -fdx
                if [ -f .gitmodules ]; then
                    git submodule update --init --recursive
                fi
            """)
        else:
            self.out.print(
                f"  Preparing {self.ct.shortname} checkout from local path ..."
            )
            self.bld_ct.bash(f"rm -rf {repo_path}")
            self.bld_ct.sync_to(source.path, repo_path)

        # Relay-specific preparation (e.g. build binary, init venv)
        self.on_init_relay(repo_path)

    def run_deploy(self, *, source, ipv4_only):
        """Perform the driver-specific deployment.

        Subclasses must implement.
        Raises ``SetupError`` on failure.
        """
        raise NotImplementedError

    def get_test_domain_or_ip(self):
        """Return the address used by test commands."""
        return self.ct.domain

    def configure_dns(self):
        """Configure DNS resolver and return the DNSContainer."""
        dns_ct = DNSContainer(self.ix)
        dns_ct.wait_ready(timeout=5)
        with self.out.section("Preparing DNS configuration"):
            self.out.print(f"Configuring DNS for {self.ct.shortname} ...")
            self.ct.configure_dns(dns_ct.ipv4)
        return dns_ct

    # ------------------------------------------------------------------
    # Subcommand factory
    # ------------------------------------------------------------------

    @classmethod
    def make_cmd(cls):
        """Build the CLI command function for this driver."""

        def cmd(args, out):
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

            source = parse_source(args.source, cls.DEFAULT_SOURCE_URL)
            if not driver.check_local_source(source):
                return 1

            driver.configure_from_args(args)

            out.print(f"cmlxc {__version__}")
            with out.section(f"Preparing {cls.CLI_NAME} source in builder"):
                out.print(f"  Source: {source.description}")
                driver.init_builder(source=source)

            driver.run_deploy(
                source=source,
                ipv4_only=args.ipv4_only,
            )
            return 0

        cmd.__doc__ = cls.CLI_DOC
        return cmd

    @classmethod
    def add_subcommand(cls, subparsers, shared, *, completer=None):
        """Register this driver as a subcommand on *subparsers*."""
        help_text = cls.CLI_DOC.split("\n")[0].strip(".")
        p = subparsers.add_parser(
            cls.CLI_NAME,
            description=cls.CLI_DOC,
            help=help_text,
            parents=[shared],
        )
        p.set_defaults(func=cls.make_cmd())
        cls.add_cli_options(p, completer=completer)
