"""madmail-based deployment driver for cmlxc.

The maddy binary is built inside the builder container
and transferred to relay containers via SCP.
Madmail relays run on IP addresses
and do not require DNS entries.
"""

import time

from cmlxc.container import SetupError
from cmlxc.driver_base import Driver

MADMAIL = "madmail"


class MadmailDriver(Driver):
    """Deploys chatmail relays via the ``madmail`` tool."""

    CLI_NAME = "deploy-madmail"
    CLI_DOC = "Deploy a madmail relay service into a container."
    DEFAULT_SOURCE_URL = "https://github.com/themadorg/madmail.git"
    REPO_NAME = MADMAIL
    REQUIRED_SOURCE_PATHS = ["go.mod", "Makefile"]

    @classmethod
    def add_cli_options(cls, parser, completer=None):
        """Register madmail-specific deploy options."""
        super().add_cli_options(parser, completer=completer)
        parser.add_argument(
            "--with-webadmin",
            action="store_true",
            help=(
                "Build and enable the embedded admin web UI at /admin. "
                "Disabled by default."
            ),
        )

    def configure_from_args(self, args):
        self.with_admin = bool(args.with_webadmin)

    @classmethod
    def on_prep_builder(cls, out, bld_ct, tmp_dest):
        """Hook called by ``prep_builder`` to ensure the Go toolchain is ready."""
        out.print("  Ensuring build environment (Go) ...")
        prepare_build_container(bld_ct, tmp_dest)

    def on_init_relay(self, repo_path):
        """Hook called by ``init_builder`` to build the maddy binary."""
        mode = "with admin web UI" if self.with_admin else "without admin web UI"
        with self.out.section(
            f"Building maddy binary for {self.ct.shortname} ({mode})"
        ):
            if self.with_admin:
                # Ensure admin-web submodule is populated and dependencies installed;
                # build.sh copy_admin_web() handles the actual SPA build.
                self.bld_ct.bash(f"""
                    if [ ! -f '{repo_path}/admin-web/package.json' ]; then
                        cd '{repo_path}' && git submodule update --init admin-web
                    fi
                    cd '{repo_path}/admin-web'
                    if command -v bun >/dev/null 2>&1; then
                        bun install
                    elif command -v npm >/dev/null 2>&1; then
                        npm install
                    fi
                """)
            else:
                # Hide package.json so build.sh creates a placeholder instead.
                self.bld_ct.bash(f"""
                    PKG='{repo_path}/admin-web/package.json'
                    BAK='{repo_path}/admin-web/package.json.cmlxc-disabled'
                    if [ -f "$PKG" ]; then mv "$PKG" "$BAK"; fi
                """)

            try:
                ret = self.out.shell(
                    f"incus exec {self.bld_ct.name} -- bash -c "
                    f"'cd {repo_path} && make clean build'"
                )
            finally:
                # Restore package.json if we hid it.
                self.bld_ct.bash(f"""
                    BAK='{repo_path}/admin-web/package.json.cmlxc-disabled'
                    PKG='{repo_path}/admin-web/package.json'
                    if [ -f "$BAK" ] && [ ! -f "$PKG" ]; then mv "$BAK" "$PKG"; fi
                """)

            if ret:
                raise SetupError(f"maddy build failed in {repo_path} (exit {ret})")

            if self.with_admin:
                check = self.bld_ct.bash(
                    f"test -f {repo_path}/internal/adminweb/build/index.html",
                    check=False,
                )
                if check is None:
                    raise SetupError("admin-web build produced no index.html")

    def run_tests(self, second_domain=None, cool=False, simple=False):
        """Execute the madmail E2E test suite against relays."""
        test_src = f"{self.get_git_main_path()}/tests/deltachat-test"

        with self.out.section("test-madmail"):
            # Symlink the built maddy binary into the test directory so
            # tests that spawn a local server can find it at build/maddy.
            self.bld_ct.bash(
                f"mkdir -p {test_src}/build"
                f" && ln -sf {self.repo_path}/build/maddy {test_src}/build/maddy"
            )

            relay1 = self.get_test_domain_or_ip()
            rpc = "/root/minitest-venv/bin/deltachat-rpc-server"
            env_exports = (
                f"export REMOTE1={relay1} REMOTE2={relay1} RPC_SERVER_PATH={rpc}"
            )
            if second_domain:
                env_exports = (
                    f"export REMOTE1={relay1} REMOTE2={second_domain}"
                    f" RPC_SERVER_PATH={rpc}"
                )

            cool_flag = " --cool" if cool else ""
            if simple:
                test_flags = "--test-1 --test-2 --test-3 --test-4 --test-5 --test-6"
            else:
                test_flags = "--all"

            cmd = (
                f"incus exec {self.bld_ct.name} --"
                f" bash -c '"
                f"{env_exports} &&"
                f" source /root/minitest-venv/bin/activate &&"
                f" cd {test_src} &&"
                f" python main.py {test_flags}{cool_flag}'"
            )
            ret = self.out.shell(cmd)
            if ret:
                self.out.red(f"test-madmail failed (exit {ret})")
            return ret

    def get_test_domain_or_ip(self):
        if not self.ct.ipv4:
            self.ct.wait_ready()
        return self.ct.ipv4

    def run_deploy(self, *, source, ipv4_only=False):
        """Deploy madmail to a single relay container."""
        with self.out.section("Preparing container setup"):
            self.ct.ensure(
                ipv4_only=ipv4_only,
            )
        self.deploy(source=source)

    def deploy(self, source=None):
        """Deploy madmail services to a single relay container."""
        t_total = time.time()

        self.ct.check_deploy_lock(MADMAIL)

        self.ix.write_ssh_config()
        self.bld_ct.write_relay_ssh_config(self.ct)

        if not self.ct.ipv4:
            self.ct.wait_ready()
        ip = self.ct.ipv4

        with self.out.section(f"madmail deploy: {self.ct.shortname} ({ip})"):
            self.out.print("Pushing madmail binary via SCP ...")
            self.ct.bash("rm -f /tmp/madmail")
            self.bld_ct.scp_to_relay(
                f"{self.repo_path}/build/maddy",
                ip,
                "/tmp/madmail",
            )
            self.ct.bash("chmod +x /tmp/madmail")

            install_flags = (
                f"--simple --ip {ip}"
                " --tls-mode self_signed"
                " --enable-chatmail"
                " --non-interactive"
            )
            self.out.print(f"Running madmail install {install_flags} ...")
            self.ct.bash("systemctl stop madmail || true")
            self.ct.bash(f"/tmp/madmail install {install_flags}")

            self.out.print("Starting madmail service ...")
            self.ct.bash("systemctl daemon-reload")
            self.ct.bash("systemctl enable madmail")
            self.ct.bash("systemctl start madmail")

            if self.with_admin:
                self.out.print("Configuring admin web interface at /admin ...")
                self.ct.bash("madmail admin-web path /admin")
                self.ct.bash("madmail admin-web enable")
                # Path changes are applied at startup.
                self.ct.bash("systemctl restart madmail")
            else:
                self.out.print("Disabling admin web interface ...")
                self.ct.bash("madmail admin-web disable")

            self.ct.bash("rm -f /tmp/madmail")

            self.ct.write_deploy_state(MADMAIL, source=source)
            self.out.green(f"madmail deployed to {self.ct.shortname} ({ip})")
            print_admin_info(self.out, self.ct, ip)

        elapsed = time.time() - t_total
        self.out.section_line(f"deploy madmail complete ({elapsed:.1f}s)")


def print_admin_info(out, ct, ip):
    """Print admin API token and admin-web endpoint state."""
    try:
        token = ct.bash("madmail admin-token --raw", check=False).strip()
        if token:
            out.print(f"admin-token: {token}")

        status = ct.bash("madmail admin-web status", check=False) or ""
        enabled, path = _parse_admin_web_status(status)
        if enabled and path:
            out.print(f"admin-url: https://{ip}{path}/")
        else:
            out.print("admin-url: disabled")
    except Exception:
        pass


def _parse_admin_web_status(status):
    enabled = "Admin Web Dashboard:  enabled" in status
    path = None
    for line in status.splitlines():
        if "Admin Web Path:" in line:
            _, _, value = line.partition("Admin Web Path:")
            value = value.strip()
            if value.startswith("/"):
                path = value.rstrip("/")
            break
    return enabled, path


def prepare_build_container(bld_ct, go_mod_path):
    """Install or update Go inside the builder according to go.mod."""
    bld_ct.bash("""
        if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 22 ]; then
            apt-get -o DPkg::Lock::Timeout=60 update
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl unzip
            curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs
        fi
        if ! command -v bun >/dev/null 2>&1; then
            curl -fsSL https://bun.sh/install | bash
            ln -sf /root/.bun/bin/bun /usr/local/bin/bun
        fi
    """)
    bld_ct.bash(f"""
        NEED=$(awk '/^go / {{print $2}}' {go_mod_path}/go.mod)
        ARCH=$(dpkg --print-architecture)
        case "$ARCH" in
            amd64) GOARCH=amd64 ;;
            arm64) GOARCH=arm64 ;;
            *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
        esac
        if [ -x /usr/local/go/bin/go ]; then
            HAVE=$(/usr/local/go/bin/go version | awk '{{print $3}}' | sed 's/^go//')
            [ "$HAVE" = "$NEED" ] && exit 0
        fi
        URL="https://go.dev/dl/go${{NEED}}.linux-${{GOARCH}}.tar.gz"
        echo "Installing Go ${{NEED}} from ${{URL}} ..."
        curl -fsSL "$URL" | tar -C /usr/local -xzf -
        ln -sf /usr/local/go/bin/go /usr/local/bin/go
        ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
    """)
