"""Base class for cmlxc deployment drivers.

Each driver subclass defines CLI metadata, builder setup,
and deploy orchestration.  The ``cli`` module discovers
drivers via the ``DEPLOY_DRIVERS`` list and generates
subcommands automatically.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cmlxc.incus import (
    BASE_IMAGE_ALIAS,
    BUILDER_CONTAINER_NAME,
    DNS_CONTAINER_NAME,
    Incus,
)


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
        url, _, ref = value.rpartition("@")
        if not url:
            raise ValueError(f"Invalid SOURCE: {value!r}")
        return SourceSpec("remote", url=url, ref=ref)
    raise ValueError(f"Invalid SOURCE: {value!r}. Use @ref, /path, ./path, or URL@ref.")


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
    NAME_EXAMPLES: str = "r0 r1"

    def __init__(self, ix, out):
        self.ix = ix
        self.out = out

    # ------------------------------------------------------------------
    # Pre-flight checks (shared by all drivers)
    # ------------------------------------------------------------------

    def check_init(self):
        """Verify that the cmlxc environment has been initialized."""
        dns_ct = self.ix.get_container(DNS_CONTAINER_NAME)
        managed = self.ix.list_managed()
        dns_running = any(
            c["name"] == dns_ct.name and c["status"] == "Running" for c in managed
        )
        if not dns_running or not self.ix.find_image([BASE_IMAGE_ALIAS]):
            self.out.red("Error: cmlxc environment not initialized.")
            self.out.red(
                "Please run 'cmlxc init' first to set up the base image and DNS."
            )
            return False
        return True

    def get_builder(self):
        """Return the running builder container, or None."""
        bld_ct = self.ix.get_container(BUILDER_CONTAINER_NAME)
        if not bld_ct.is_running:
            self.out.red("Builder container not running.")
            self.out.red("Run 'cmlxc init' first.")
            return None
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
            "names",
            nargs="+",
            metavar="NAME",
            help=f"One or more relay names (e.g. {cls.NAME_EXAMPLES}).",
        )
        if completer is not None:
            action.completer = completer
        parser.add_argument(
            "--ipv4-only",
            dest="ipv4_only",
            action="store_true",
            help="Create containers without IPv6 connectivity.",
        )

    # ------------------------------------------------------------------
    # Deploy protocol (override in subclasses)
    # ------------------------------------------------------------------

    def init_builder(self, bld_ct, source, names):
        """Prepare the builder container for this driver and these relays."""
        raise NotImplementedError

    def run_deploy(self, names, bld_ct, *, source, ipv4_only):
        """Ensure relay containers and run the deployment."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Subcommand factory
    # ------------------------------------------------------------------

    @classmethod
    def make_cmd(cls):
        """Build the CLI command function for this driver."""

        def cmd(args, out):
            driver = cls(Incus(out), out)
            if not driver.check_init():
                return 1

            bld_ct = driver.get_builder()
            if not bld_ct:
                return 1

            source = parse_source(args.source, cls.DEFAULT_SOURCE_URL)
            with out.section(f"Preparing {cls.CLI_NAME} source in builder"):
                out.print(f"  Source: {source.description}")
                driver.init_builder(bld_ct, source=source, names=args.names)

            return driver.run_deploy(
                args.names,
                bld_ct,
                source=source,
                ipv4_only=args.ipv4_only,
            )

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
