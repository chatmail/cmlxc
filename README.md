# cmlxc -- local chatmail container management and testing

Manage local [Incus](https://linuxcontainers.org/incus/) containers for
chatmail relay development and testing.
`cmlxc` spins up lightweight LXC containers, deploys chatmail relay
services into them via `cmdeploy` or `madmail`, and runs integration
tests -- all without touching the host system.  
See [Architecture](#architecture) for more internal details.


## Prerequisites

[Incus](https://linuxcontainers.org/incus/) installed and configured on the host.
Usually only being part of the "incus" group is necessary, as containers
can run with user privileges.


## Installation

With pip:

    python -m venv venv
    source venv/bin/activate
    pip install cmlxc

Or with [uv](https://docs.astral.sh/uv/):

    uv venv venv
    source venv/bin/activate
    uv pip install cmlxc


## Usage

**Initialize the environment** (base image, DNS container, builder container):

    cmlxc init

Re-initialize from scratch (destroys everything first):

    cmlxc init --reset


**Deploy chatmail relays** (creates containers if needed, then deploys).
The `--source` argument controls where the code comes from:

    cmlxc deploy-cmdeploy --source @main cm0 cm1
    cmlxc deploy-madmail  --source @main mad1
    cmlxc deploy-madmail  --source @main --ipv4-only mad1

| Form | Meaning |
|---------|---------|
| `@ref` | Clone default remote at branch/tag `ref` |
| `/path` or `./path` | Sync from a local checkout |
| `URL@ref` | Clone a custom remote at `ref` |

Examples with local checkouts or feature branches:

    cmlxc deploy-cmdeploy --source ../relay cm0
    cmlxc deploy-madmail  --source @lmtp-rework mad0
    cmlxc deploy-cmdeploy --source @fix-dovecot cm1

Each `deploy-*` invocation initialises the driver's source in the
builder (wipe-and-reclone).


**Run integration tests** inside the builder:

    cmlxc test-mini cm0
    cmlxc test-mini cm0 cm1          # cross-relay tests
    cmlxc test-cmdeploy cm0 cm1


**SSH into a deployed relay:**

    ssh -F ~/.config/cmlxc/ssh-config cm0


**Lifecycle commands:**

    cmlxc status                # show all containers
    cmlxc status --host         # show DNS/SSH setup instructions
    cmlxc start cm0             # restart a stopped relay
    cmlxc stop cm0 cm1          # stop relays
    cmlxc destroy cm0           # stop + delete
    cmlxc destroy --all         # destroy relays, keep DNS/builder


**Increase verbosity** with `-v` or `-vv`:

    cmlxc deploy-cmdeploy --source @main -vv cm1


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


**Base image** (`localchat-base`) -- a Debian 12 image with SSH and
Python pre-installed.
All other containers are launched from this image (or from a cached
relay image).


**DNS container** (`ns-localchat`) -- runs PowerDNS authoritative + recursor.
Provides `.localchat` DNS resolution so containers can reach each other by name.


**Builder container** (`builder-localchat`) -- the central workhorse.
Holds repository checkouts (`/root/relay`, `/root/madmail`), Python
virtualenvs for `cmdeploy` and mini-tests, and the compiled `maddy` binary.
All deployment and test operations are executed *inside* the builder --
the host only needs `cmlxc` itself.


**Relay containers** (e.g. `cm0-localchat`, `mad1-localchat`) --
ephemeral containers that receive a deployed chatmail service.
Each relay is locked to a single deployment driver (`cmdeploy` or
`madmail`); switching requires destroying and re-creating the container.


### Deployment drivers

Drivers live in `driver_cmdeploy.py` and `driver_madmail.py`.
Each driver module is exports its CLI subcommand metadata,
builder init, and deploy orchestration.
`cli.py` generates the `deploy-*` subcommands from a `DEPLOY_DRIVERS` list.


- **cmdeploy** -- runs `cmdeploy run` from the builder container over SSH
  into the relay.
  Generates DNS zones, loads them into PowerDNS, and verifies records.
  After the first successful deploy the relay image is cached as
  `localchat-cmdeploy` so subsequent containers start pre-populated.


- **madmail** -- builds the `maddy` Go binary inside the builder,
  pushes it via SCP and runs `madmail install --simple --ip <IP>`.
  No DNS entries are needed.


## Releasing

Versions are derived from git tags via `setuptools-git-versioning`.
The changelog is generated with [git-cliff](https://git-cliff.org/)
using the `cliff.toml` config in the repo root.


To make a new release, use the provided script:

    ./make_new_release.py

The script automates the following steps:

1. **Preview** unreleased changes with `git cliff`.

2. **Tag** the release (suggesting automatic, micro, or minor bump).

3. **Generate** the full changelog into `CHANGELOG.md`.

4. **Edit** the changelog manually (opens your `$EDITOR`).

5. **Amend** the tag commit to include the changelog update.

6. **Force-tag** the amended commit.


After the script finishes, push the changes:

    git push origin main --tags


The `release.yml` GitHub workflow triggers on pushed `v*` tags,
builds the sdist + wheel, and publishes to PyPI via trusted publishing (OIDC).
