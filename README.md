# cmlxc -- local chatmail container management and testing

Manage local [Incus](https://linuxcontainers.org/incus/) containers
for chatmail relay development and testing.

`cmlxc` spins up lightweight LXC containers,
deploys chatmail relay services into them
via `cmdeploy` or `madmail`,
and runs integration tests --
all without touching the host system.


## Architecture

`cmlxc` manages four kinds of containers, each with a distinct role:

```
    cmlxc init / deploy-* / test-*
        |
        v
   +-----------------+   +------------------------+   +--------------------+
   | ns-localchat    |   | builder-localchat      |   | relay containers   |
   | (PowerDNS)      |   | (repos, venvs, builds) |   | (cm0, mad1, ...)   |
   +-----------------+   +------------------------+   +--------------------+
           ^                        |                           ^
           |      DNS zones         |        SSH / SCP          |
           +------------------------+---------------------------+
```

**Base image** (`localchat-base`) --
A Debian 12 image with SSH and Python pre-installed.
All other containers are launched from this image
(or from a cached relay image).

**DNS container** (`ns-localchat`) --
Runs PowerDNS authoritative + recursor.
Provides `.localchat` DNS resolution
so containers can reach each other by name.

**Builder container** (`builder-localchat`) --
The central workhorse.
Holds repository checkouts (`/root/relay`, `/root/madmail`),
Python virtualenvs for `cmdeploy` and mini-tests,
and the compiled `maddy` binary.
All deployment and test operations
are executed *inside* the builder --
the host only needs `cmlxc` itself.

**Relay containers** (e.g. `cm0-localchat`, `mad1-localchat`) --
Ephemeral containers that receive a deployed chatmail service.
Each relay is locked to a single deployment driver
(`cmdeploy` or `madmail`);
switching requires destroying and re-creating the container.


### Deployment drivers

Drivers live in `driver_cmdeploy.py` and `driver_madmail.py`.
Each driver has an `init_builder()` function
(called during `cmlxc init`)
and a `deploy()` function
(called during `cmlxc deploy-*`).

- **cmdeploy** --
  Runs `cmdeploy run` from the builder container
  over SSH into the relay.
  Generates DNS zones, loads them into PowerDNS,
  and verifies records.
  After the first successful deploy
  the relay image is cached as `localchat-relay`
  so subsequent containers start pre-populated.

- **madmail** --
  Builds the `maddy` Go binary on first deploy
  (the triggered `make` is idempotent on reruns),
  then pushes it via SCP
  and runs `madmail install --simple --ip <IP>`.
  No DNS entries are needed.


## Prerequisites

[Incus](https://linuxcontainers.org/incus/)
installed and configured on the host.
Usually only being part of the "incus" group is neccessary,
as containers can run with user priviledges.


## Installation

With pip:

    python -m venv venv
    source venv/bin/activate
    pip install .

Or with [uv](https://docs.astral.sh/uv/):

    uv venv venv
    source venv/bin/activate
    uv pip install .

## Usage

**Initialize the environment**
(base image, DNS container, builder container):

    cmlxc init

Use `--relay-repo` or `--madmail-repo`
to sync a local checkout instead of cloning from GitHub:

    cmlxc init --relay-repo ../relay --madmail-repo ../madmail

**Deploy chatmail relays**
(creates containers if needed, then deploys):

    cmlxc deploy-cmdeploy cm0 cm1
    cmlxc deploy-madmail mad1
    cmlxc deploy-madmail --ipv4-only mad1

**Run integration tests** inside the builder:

    cmlxc test-mini cm0
    cmlxc test-mini cm0 cm1          # cross-relay tests
    cmlxc test-cmdeploy cm0 cm1

**SSH into a deployed relay:**

    ssh -F ~/.config/cmlxc/ssh-config cm0

**Lifecycle commands:**

    cmlxc status                # show all containers
    cmlxc start cm0             # restart a stopped relay
    cmlxc stop cm0 cm1          # stop relays
    cmlxc destroy cm0           # stop + delete
    cmlxc destroy --all         # destroy relays, keep DNS/builder
    cmlxc destroy --reset       # full teardown, requires re-init

**Increase verbosity** with `-v` or `-vv`:

    cmlxc deploy-cmdeploy -vv cm1


## Shell Completion

`cmlxc` supports Bash tab-completion for subcommands, options, and container names.

Enable for the **current session**:

```bash
eval "$(register-python-argcomplete cmlxc)"
```

Enable **permanently**:

```bash
activate-global-python-argcomplete --user
```


## Releasing

Versions are derived from git tags
via `setuptools-git-versioning`.
The changelog is generated with
[git-cliff](https://git-cliff.org/)
using the `cliff.toml` config in the repo root.

### Steps

1. **Preview** unreleased changes:

       git cliff --unreleased

2. **Tag** the release
   (the tag name becomes the version):

       git tag v0.1.0

3. **Generate** the full changelog:

       git cliff -o CHANGELOG.md

4. **Amend** the tag commit
   to include the changelog:

       git add CHANGELOG.md
       git commit --amend --no-edit
       git tag -f v0.1.0

5. **Push** tag and branch:

       git push origin main --tags

The `release.yml` GitHub workflow
triggers on pushed `v*` tags,
builds the sdist + wheel,
and publishes to PyPI
via trusted publishing (OIDC).
