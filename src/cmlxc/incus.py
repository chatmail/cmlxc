"""Incus orchestration for chatmail relay testing.

Manages container lifecycles, image caching,
and the DNS name server container.

Deployment drivers (``cmdeploy`` and ``madmail``) are tracked
via labels to prevent conflicting configurations --
a container must be destroyed before switching drivers.
"""

import ipaddress
import json
import subprocess
from pathlib import Path

from xdg_base_dirs import xdg_config_home

from cmlxc.container import (
    BASE_IMAGE_ALIAS,
    DOMAIN_SUFFIX,
    LABEL_DEPLOY_DRIVER,
    LABEL_DEPLOY_SOURCE,
    LABEL_DOMAIN,
    LABEL_KEY,
    SSH_KEY_NAME,
    UPSTREAM_IMAGE,
    Container,
    RelayContainer,
    SetupError,
    _extract_ip,
    format_ssh_config,
)

BASE_SETUP_NAME = "localchat-base-setup"


def _is_ip_address(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def check_cgroup_compat():
    """Raise ``SetupError`` if legacy cgroup v1 mounts break containers."""
    v1_mounts = []
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "cgroup":
                v1_mounts.append(parts[1])
    except OSError:
        return
    if v1_mounts:
        paths = ", ".join(v1_mounts)
        raise SetupError(
            f"Legacy cgroup v1 mounts detected ({paths}).\n"
            "This hybrid cgroup layout prevents Incus from"
            " starting containers.\n"
            "Usually caused by Mullvad VPN split-tunneling.\n"
            "Fix:\n"
            "  sudo systemctl stop mullvad-daemon\n"
            f"  sudo umount {v1_mounts[0]}"
        )


class Incus:
    """Helper for Incus container operations."""

    def __init__(self, out):
        self.out = out
        self.config_dir = xdg_config_home() / "cmlxc"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.ssh_key_path = self.config_dir / SSH_KEY_NAME
        if not self.ssh_key_path.exists():
            cmd = ["ssh-keygen", "-t", "ed25519"]
            cmd += ["-f", str(self.ssh_key_path), "-N", "", "-C", "localchat"]
            subprocess.run(
                cmd,
                check=True,
            )
        self.ssh_config_path = self.config_dir / "ssh-config"
        self._bridge_subnet = NotImplemented

    @property
    def bridge_subnet(self):
        """Return the IPv4 subnet of incusbr0 as an IPv4Network, or None."""
        if self._bridge_subnet is NotImplemented:
            self._bridge_subnet = None
            result = self.run(
                ["network", "get", "incusbr0", "ipv4.address"], check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    self._bridge_subnet = ipaddress.ip_network(
                        result.stdout.strip(), strict=False
                    )
                except ValueError:
                    pass
        return self._bridge_subnet

    def write_ssh_config(self):
        """Write ``ssh-config`` mapping all containers to their IPs."""
        containers = self.list_managed()
        text = format_ssh_config(containers, self.ssh_key_path)
        self.ssh_config_path.write_text(text)
        return self.ssh_config_path

    def check_ssh_include(self):
        """Check if ~/.ssh/config includes our ssh-config."""
        user_ssh_config = Path.home() / ".ssh" / "config"
        if not user_ssh_config.exists():
            return False
        lines = user_ssh_config.read_text().splitlines()
        target = f"include {self.ssh_config_path}".lower()
        return any(line.strip().lower() == target for line in lines)

    def get_host_nameservers(self):
        """Return upstream nameservers found on the host."""
        ns = []
        for path in ["/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"]:
            p = Path(path)
            if p.exists():
                for line in p.read_text().splitlines():
                    if line.strip().startswith("nameserver "):
                        addr = line.split()[1]
                        if addr not in ("127.0.0.1", "127.0.0.53", "::1"):
                            if addr not in ns:
                                ns.append(addr)
                if ns:
                    break
        return ns

    def run(self, args, check=True, input=None):
        """Run an incus command and return result."""
        cmd = ["incus", "--quiet"]
        cmd += list(args)
        sub = self.out.new_prefixed_out("  ")

        if sub.verbosity >= 1:
            sub.print(f"$ {' '.join(cmd)}")

        proc = subprocess.Popen(
            cmd,
            text=True,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            if input is not None:
                stdout, stderr = proc.communicate(input=input)
            else:
                stdout, stderr = proc.communicate()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
        ret = proc.returncode
        if sub.verbosity >= 2:
            for line in stdout.splitlines():
                sub.print(f"  > {line}")
        if check and ret != 0:
            full_output = stdout + stderr
            for line in full_output.splitlines():
                sub.red(line)
            raise subprocess.CalledProcessError(ret, cmd, output=stdout, stderr=stderr)

        return subprocess.CompletedProcess(cmd, ret, stdout=stdout, stderr=stderr)

    def run_json(self, args, check=True):
        """Run incus command and return parsed JSON."""
        result = self.run(
            list(args) + ["--format=json"],
            check=check,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)

    def run_output(self, args, check=True):
        """Run incus command and return stripped stdout."""
        result = self.run(args, check=check)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def find_image(self, aliases):
        """Return the first alias from *aliases* that exists, else None."""
        images = self.run_json(["image", "list"], check=False) or []
        existing = {a.get("name") for img in images for a in img.get("aliases", [])}
        for alias in aliases:
            if alias in existing:
                return alias
        return None

    def delete_images(self):
        """Delete all localchat-related cached images."""
        for img in self.run_json(["image", "list"]):
            aliases = [a["name"] for a in img.get("aliases", [])]
            is_localchat = any(a.startswith("localchat-") for a in aliases)
            if is_localchat:
                fp = img["fingerprint"]
                self.run(["image", "delete", fp], check=False)

    @staticmethod
    def get_container_name(name):
        """Return the full container name for a given short name."""
        if name.endswith("-localchat"):
            return name
        return f"{name}-localchat"

    def list_managed(self):
        """Return list of dicts with name, ip, ipv6, domain, status."""
        containers = []
        for ct in self.run_json(["list"]):
            config = ct.get("config", {})
            if config.get(LABEL_KEY) != "true":
                continue
            name = ct["name"]
            state = ct.get("state", {})
            net = state.get("network") or {}
            containers.append(
                {
                    "name": name,
                    "ip": _extract_ip(net, "inet", subnet=self.bridge_subnet),
                    "ipv6": _extract_ip(net, "inet6"),
                    "domain": config.get(LABEL_DOMAIN, f"{name}{DOMAIN_SUFFIX}"),
                    "status": ct.get("status", "Unknown"),
                    "driver": config.get(LABEL_DEPLOY_DRIVER),
                    "source": config.get(LABEL_DEPLOY_SOURCE),
                }
            )
        return containers

    def ensure_base_image(self):
        """Build and cache base image with openssh."""
        if self.find_image([BASE_IMAGE_ALIAS]):
            self.out.print(f"  Base image '{BASE_IMAGE_ALIAS}' already cached.")
            return BASE_IMAGE_ALIAS

        self.out.print("  Building base image (one-time setup) ...")

        self.run(["delete", BASE_SETUP_NAME, "--force"], check=False)
        self.run(["image", "delete", BASE_IMAGE_ALIAS], check=False)
        self.run(
            [
                "launch",
                UPSTREAM_IMAGE,
                BASE_SETUP_NAME,
                "-c",
                f"{LABEL_KEY}=true",
                "-c",
                f"{LABEL_DOMAIN}=setup{DOMAIN_SUFFIX}",
            ]
        )

        ct = Container(self, BASE_SETUP_NAME)
        ct.wait_ready()

        key_path = self.ssh_key_path
        pub_key = key_path.with_suffix(".pub").read_text().strip()
        host_ns = self.get_host_nameservers()
        ns_lines = "\n".join(f"nameserver {n}" for n in host_ns)
        ct.bash(f"""
            printf '{ns_lines}\\n' > /etc/resolv.conf
            apt-get -o DPkg::Lock::Timeout=60 update
            DEBIAN_FRONTEND=noninteractive apt-get purge -y unattended-upgrades
            DEBIAN_FRONTEND=noninteractive apt-get install -y \
                openssh-server python3 gcc python3-dev
            systemctl enable ssh
            apt-get clean
            mkdir -p /root/.ssh
            chmod 700 /root/.ssh
            echo '{pub_key}' > /root/.ssh/authorized_keys
            chmod 600 /root/.ssh/authorized_keys
            # Incus containers only get ULA IPv6 addresses which cannot
            # route to the internet.  Prefer IPv4 for outbound connections
            # so that curl/wget/apt don't hang trying IPv6 first.
            echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf
        """)

        self.run(["stop", BASE_SETUP_NAME])
        self.run(["publish", BASE_SETUP_NAME, f"--alias={BASE_IMAGE_ALIAS}"])
        self.run(["delete", BASE_SETUP_NAME, "--force"])
        self.out.print(f"  Base image '{BASE_IMAGE_ALIAS}' ready.")
        return BASE_IMAGE_ALIAS

    def get_relay_container(self, name):
        """Return a RelayContainer handle for the given short name."""
        return RelayContainer(self, name.removesuffix("-localchat"))

    def get_running_relay(self, name):
        """Return a running relay container, starting it if stopped."""
        ct = self.get_relay_container(name)
        data = self.run_json(["list", ct.name], check=False) or []
        if not data:
            raise SetupError(
                f"Container {name!r} does not exist."
                f" Deploy it first with 'deploy-cmdeploy' or 'deploy-madmail'."
            )
        if data[0].get("status") != "Running":
            self.out.print(f"Starting container {ct.name!r} ...")
            ct.start()
            ct.wait_ready()
            ct.wait_services()
        return ct
