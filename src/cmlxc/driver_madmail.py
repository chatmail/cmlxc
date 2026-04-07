"""madmail-based deployment driver for cmlxc.

The maddy binary is built inside the builder container
and transferred to relay containers via SCP.
Madmail relays run on IP addresses
and do not require DNS entries.
"""

import time

from cmlxc.incus import MADMAIL

MADMAIL_REPO_URL = "https://github.com/themadorg/madmail.git"


def init_builder(bld_ct, out, local_repo=None):
    """Set up the madmail checkout in the builder."""
    bld_ct.setup_repo("/root/madmail", out, url=MADMAIL_REPO_URL, local_path=local_repo)


def build_madmail(builder_ct):
    """Build the maddy binary inside the builder."""
    builder_ct.bash("""
        apt-get -o DPkg::Lock::Timeout=60 update
        DEBIAN_FRONTEND=noninteractive apt-get install -y curl
    """)
    # Install Go from upstream, version parsed from go.mod.
    builder_ct.bash("""
        NEED=$(awk '/^go / {print $2}' /root/madmail/go.mod)
        ARCH=$(dpkg --print-architecture)
        case "$ARCH" in
            amd64) GOARCH=amd64 ;;
            arm64) GOARCH=arm64 ;;
            *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
        esac
        URL="https://go.dev/dl/go${NEED}.linux-${GOARCH}.tar.gz"
        echo "Installing Go ${NEED} from ${URL} ..."
        curl -fsSL "$URL" | tar -C /usr/local -xzf -
        ln -sf /usr/local/go/bin/go /usr/local/bin/go
        ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
    """)
    builder_ct.bash("cd /root/madmail && make build")


def deploy(relay_names, ix, out, builder_ct):
    """Deploy madmail to relay containers."""
    t_total = time.time()

    relays = [ix.get_container(n) for n in relay_names]

    # Check deploy locks
    for ct in relays:
        ct.relay_dir.mkdir(parents=True, exist_ok=True)
        ct.check_deploy_lock(MADMAIL)

    ix.write_ssh_config()

    with out.section("Building maddy binary"):
        build_madmail(builder_ct)

    # Set up SSH from builder to relay containers
    builder_ct.setup_ssh(relays)

    for ct in relays:
        if not ct.ipv4:
            ct.wait_ready()
        ip = ct.ipv4

        with out.section(f"madmail deploy: {ct.sname} ({ip})"):
            out.print("Pushing madmail binary via SCP ...")
            ct.bash("rm -f /tmp/madmail")
            builder_ct.scp_to_relay("/root/madmail/build/maddy", ip, "/tmp/madmail")
            ct.bash("chmod +x /tmp/madmail")

            out.print(f"Running madmail install --simple --ip {ip} ...")
            ct.bash("systemctl stop madmail || true")
            ct.bash(f"/tmp/madmail install --simple --ip {ip}")

            out.print("Starting madmail service ...")
            ct.bash("systemctl daemon-reload")
            ct.bash("systemctl enable madmail")
            ct.bash("systemctl start madmail")
            ct.bash("rm -f /tmp/madmail")

            ct.write_deploy_state(MADMAIL)
            out.green(f"madmail deployed to {ct.sname} ({ip})")

    elapsed = time.time() - t_total
    out.section_line(f"deploy madmail complete ({elapsed:.1f}s)")
    return 0
