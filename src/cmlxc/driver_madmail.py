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
    def on_prep_builder(cls, out, bld_ct, tmp_dest):
        """Hook called by ``prep_builder`` to ensure the Go toolchain is ready."""
        out.print("  Ensuring build environment (Go) ...")
        prepare_build_container(bld_ct, tmp_dest)

    def on_init_relay(self, repo_path):
        """Hook called by ``init_builder`` to build the maddy binary."""
        with self.out.section(f"Building maddy binary for {self.ct.shortname}"):
            self.bld_ct.bash(f"""
                if [ -f '{repo_path}/admin-web/package.json' ]; then
                    cd '{repo_path}/admin-web' && bun install
                fi
            """)
            self.out.print(f"Compiling maddy in {repo_path} (make build) ...")
            ret = self.out.shell(
                f"incus exec {self.bld_ct.name} -- bash -c 'cd {repo_path} && make build'"
            )
            if ret:
                raise SetupError(f"maddy build failed in {repo_path} (exit {ret})")

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

            self.out.print(f"Running madmail install --simple --ip {ip} ...")
            self.ct.bash("systemctl stop madmail || true")
            self.ct.bash(f"/tmp/madmail install --simple --ip {ip}")

            self.out.print("Starting madmail service ...")
            self.ct.bash("systemctl daemon-reload")
            self.ct.bash("systemctl enable madmail")
            self.ct.bash("systemctl start madmail")
            self.ct.bash("rm -f /tmp/madmail")

            self.ct.write_deploy_state(MADMAIL, source=source)
            self.out.green(f"madmail deployed to {self.ct.shortname} ({ip})")

        elapsed = time.time() - t_total
        self.out.section_line(f"deploy madmail complete ({elapsed:.1f}s)")



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
