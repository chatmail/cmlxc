# cmlxc -- local chatmail container management and testing

Manage local [Incus](https://linuxcontainers.org/incus/) containers for
chatmail relay development and testing.
`cmlxc` spins up lightweight LXC containers,
deploys chatmail relay services into them via `cmdeploy` or `madmail`,
and runs integration tests -- all without touching the host system.
See [Architecture](#architecture) for more internal details.


## Prerequisites

[Incus](https://linuxcontainers.org/incus/) installed and configured on the host.
Usually only being part of the "incus" group is necessary,
as containers can run with user privileges.

> [!TIP]
> On Debian or Ubuntu, it is recommended to use the
> [Zabbly Incus repository](https://github.com/zabbly/incus)
> to ensure you have a recent version.

You can verify your incus installation like this:

    incus launch images:debian/12 local-my-setup

If this command fails, please check the incus documentation.
If you get an error about "Failed instance creation", 
you might be running into https://github.com/lxc/incus/issues/916
and need to ensure there is no component (mullvad) for example,
that messes up container networking. 


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

    cmlxc deploy-cmdeploy --source @main cm0
    cmlxc deploy-madmail  --source @main mad1
    cmlxc deploy-madmail  --source @main --with-webadmin mad1
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


**Deploy via Docker Compose** (builds and runs chatmail inside Docker-in-LXC):

    cmlxc docker deploy --source @main dk0
    cmlxc docker deploy --source ../relay dk0
    cmlxc docker deploy --image ./chatmail.tar.zst dk1

Pre-build images or manage the image cache in the builder:

    cmlxc docker build --source @main
    cmlxc docker build --source @main --output ./chatmail.tar.zst
    cmlxc docker list
    cmlxc docker prune
    cmlxc docker prune --all

Inspect running services and logs:

    cmlxc docker ps dk0
    cmlxc docker logs dk0
    cmlxc docker logs dk0 -f

SSH into a Docker service (auto-configured by ``cmlxc``):

    ssh chatmail@dk0.localchat


**Run integration tests** inside the builder:

    cmlxc test-mini cm0
    cmlxc test-mini cm0 cm1          # cross-relay tests (domain-based)
    cmlxc test-mini cm0 mad1         # cross-relay tests (mixed)
    cmlxc test-cmdeploy cm0 cm1
    cmlxc test-madmail mad1


**SSH into a deployed relay:**

    ssh -F ~/.config/cmlxc/ssh-config cm0


**Lifecycle commands:**

    cmlxc status                # show all containers
    cmlxc status cm0            # show only cm0
    cmlxc status cm0 mad1       # show multiple containers
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
Holds repository templates and per-relay checkouts,
Python virtualenvs for `cmdeploy` and mini-tests, and the compiled `maddy` binary.
All deployment and test operations are executed *inside* the builder --
the host only needs `cmlxc` itself.


**Relay containers** (e.g. `cm0-localchat`, `mad1-localchat`) --
ephemeral containers that receive a deployed chatmail service.
Each relay is locked to a single deployment driver (`cmdeploy`,
`madmail`, or `docker`); switching requires destroying and re-creating
the container.


### Deployment drivers

Drivers live in `driver_cmdeploy.py`, `driver_madmail.py`, and `driver_docker.py`.
Each driver module exports its CLI subcommand metadata,
builder init, and deploy orchestration.
`cli.py` generates the `deploy-*` subcommands from a `DRIVER_BY_NAME` mapping.


- **cmdeploy** -- runs `cmdeploy run` from the builder container over SSH
  into the relay.
  Generates DNS zones, loads them into PowerDNS, and verifies records.
  After the first successful deploy the relay image is cached as
  `localchat-cmdeploy` so subsequent containers start pre-populated.

- **madmail** -- builds the `maddy` Go binary inside the builder,
  pushes it via SCP and runs `madmail install --simple --ip <IP>`.
  No DNS entries are needed.

- **docker** -- builds a Docker image in the builder container,
  transfers it to the relay, and starts it with `docker compose`.
  The relay container is launched with `security.nesting=true` to
  allow Docker-in-LXC.  DNS zones are extracted from the running
  container and loaded into PowerDNS.
  Use `--image` to skip the build and load a pre-exported tarball.
  Docker is installed inside the containers automatically; no Docker
  installation is needed on the host.  The Dockerfile and compose
  files are cloned from
  [chatmail/docker](https://github.com/chatmail/docker) into the
  relay checkout automatically.
  If `zstd` is installed on the host, `--output` produces compressed
  tarballs and `--image` decompresses them; otherwise plain tar is used.

#### Docker image management

`docker build`, `docker list`, and `docker prune` operate on the
builder's Docker image cache independently of any relay deployment.

- `docker build` -- builds the chatmail Docker image from a relay source
  and caches it in the builder.  Use `--output PATH` to export a tarball (zstd-compressed if
  available).  Old images are
  auto-pruned (configurable with `--keep N`, default 3).
  Images are cached by relay git SHA.  If only the `docker/` files
  changed (Dockerfile, compose, init scripts) without a new relay
  commit, pass `--force-rebuild` to bypass the cache.

- `docker list` -- shows cached images with tag, ref, SHA, and build date.

- `docker prune` -- removes stale images and dangling Docker resources.
  Three levels: default (containers + dangling images), `--deep`
  (adds build cache + volumes), `--all` (everything unused).
  Use `--dry-run` to preview disk usage without pruning.

- `docker ps RELAY` -- lists running Docker Compose services in a relay.

- `docker logs RELAY` -- shows Docker Compose logs from a deployed relay
  container (last 100 lines).  Pass `-f` to follow output in real time.

#### SSH into Docker services

For Docker-deployed relays, `cmlxc` auto-generates SSH config entries for
each running Compose service.  After any deploy or `cmlxc status`, you can:

    ssh chatmail@dk0.localchat

This uses `ProxyCommand` to run `docker exec` inside the LXC container.
As the compose setup evolves to multiple services, each service gets its
own entry (e.g. `ssh dovecot@dk0.localchat`).


## Releasing

Versions are derived from git tags via `setuptools-git-versioning`.
The changelog is generated with [git-cliff](https://git-cliff.org/)
using the `cliff.toml` config in the repo root.


To make a new release, use the provided script:

    ./make_new_release.py

The script automates the following steps:

1. **Test** the codebase by running a full `tox` suite and functional
   tests (`pytest tests/fullrun.py`).

2. **Preview** unreleased changes with `git cliff`.

3. **Tag** the release (suggesting automatic, micro, or minor bump).

4. **Generate** the full changelog into `CHANGELOG.md`.

5. **Edit** the changelog manually (opens your `$EDITOR`).

6. **Amend** the tag commit to include the changelog update.

7. **Force-tag** the amended commit.


After the script finishes, push the changes:

    git push origin main --tags


The `release.yml` GitHub workflow triggers on pushed `v*` tags,
builds the sdist + wheel, and publishes to PyPI via trusted publishing (OIDC).
