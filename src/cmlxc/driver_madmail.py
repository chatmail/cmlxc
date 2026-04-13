"""madmail-based deployment driver for cmlxc.

The maddy binary is built inside the builder container
and transferred to relay containers via SCP.
Madmail relays run on IP addresses
and do not require DNS entries.
"""

import time

from cmlxc.driver_base import Driver
from cmlxc.incus import MADMAIL, DeployConflictError

MADMAIL_REPO_URL = "https://github.com/themadorg/madmail.git"


class MadmailDriver(Driver):
    """Deploys chatmail relays via the ``madmail`` tool."""

    CLI_NAME = "deploy-madmail"
    CLI_DOC = "Deploy a madmail relay service into a container."
    DEFAULT_SOURCE_URL = MADMAIL_REPO_URL
    REPO_NAME = MADMAIL
    NAME_EXAMPLES = "mad0 mad1"

    def init_builder(self, bld_ct, source, names):
        """Set up the madmail template and copy for each relay."""
        tmp_dest = f"/root/{self.REPO_NAME}-template"
        bld_ct.setup_repo(tmp_dest, self.out, source)

        self.out.print("  Installing build dependencies ...")
        bld_ct.bash("DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl")

        # Install Go from upstream, version parsed from template's go.mod.
        bld_ct.bash(f"""
            NEED=$(awk '/^go / {{print $2}}' {tmp_dest}/go.mod)
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

        with self.out.section("Building maddy binary (once for this source)"):
            self._build(bld_ct, tmp_dest)

        for name in names:
            ct = self.ix.get_container(name)
            repo_path = ct.get_repo_path(self.REPO_NAME)
            self.out.print(f"  Setting up {repo_path} (from {source.description}) ...")
            bld_ct.bash(f"rm -rf {repo_path} && cp -a {tmp_dest} {repo_path}")

    def run_deploy(self, names, bld_ct, *, source, ipv4_only=False):
        """Ensure relay containers and deploy madmail."""
        with self.out.section("Preparing container setup"):
            self.ix.ensure_relay_containers(
                names,
                ipv4_only=ipv4_only,
                image_candidates=None,
            )
        try:
            return self.deploy(names, bld_ct, source=source)
        except DeployConflictError as exc:
            self.out.red(f"Deploy conflict: {exc}")
            return 1

    def deploy(self, relay_names, builder_ct, source=None):
        """Deploy madmail to relay containers."""
        t_total = time.time()
        out = self.out
        ix = self.ix

        relays = [ix.get_container(n) for n in relay_names]

        # Check deploy locks
        for ct in relays:
            ct.relay_dir.mkdir(parents=True, exist_ok=True)
            ct.check_deploy_lock(MADMAIL)

        ix.write_ssh_config()

        # Set up SSH from builder to relay containers
        builder_ct.setup_ssh(relays)

        for ct in relays:
            if not ct.ipv4:
                ct.wait_ready()
            ip = ct.ipv4

            with out.section(f"madmail deploy: {ct.sname} ({ip})"):
                out.print("Pushing madmail binary via SCP ...")
                ct.bash("rm -f /tmp/madmail")
                repo_path = ct.get_repo_path(self.REPO_NAME)
                builder_ct.scp_to_relay(
                    f"{repo_path}/build/maddy",
                    ip,
                    "/tmp/madmail",
                )
                ct.bash("chmod +x /tmp/madmail")

                out.print(f"Running madmail install --simple --ip {ip} ...")
                ct.bash("systemctl stop madmail || true")
                ct.bash(f"/tmp/madmail install --simple --ip {ip}")

                out.print("Starting madmail service ...")
                ct.bash("systemctl daemon-reload")
                ct.bash("systemctl enable madmail")
                ct.bash("systemctl start madmail")
                ct.bash("rm -f /tmp/madmail")

                desc = source.description if source else None
                ct.write_deploy_state(MADMAIL, source_desc=desc)
                out.green(f"madmail deployed to {ct.sname} ({ip})")

        elapsed = time.time() - t_total
        out.section_line(f"deploy madmail complete ({elapsed:.1f}s)")
        return 0

    def _build(self, builder_ct, repo_path):
        """Compile the maddy binary inside the builder."""
        self.out.print(f"Compiling maddy in {repo_path} (make build) ...")
        ret = self.out.shell(
            f"incus exec {builder_ct.name} -- bash -c 'cd {repo_path} && make build'"
        )
        if ret:
            raise RuntimeError(f"maddy build failed in {repo_path} (exit {ret})")
