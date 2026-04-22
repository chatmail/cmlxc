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

With [uv](https://docs.astral.sh/uv/):

    uv tool install cmlxc


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


**Deploy via Docker Compose** (runs chatmail inside Docker-in-LXC):

    # Pull a pre-built image directly from GHCR
    cmlxc docker deploy --source ghcr:main dk0
    cmlxc docker deploy --source ghcr:sha-ce05b26 dk0

    # Load a local image tarball
    cmlxc docker deploy --image ./chatmail.tar dk0

    # Inject a locally-built image from the host Docker daemon
    cmlxc docker deploy --source docker:chatmail-relay:latest dk0

Pull a newer image into an already-deployed relay:

    cmlxc docker pull dk0
    cmlxc docker pull dk0 --tag sha-ce05b26

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

- **madmail** -- builds the `maddy` Go binary inside the builder,
  pushes it via SCP and runs `madmail install --simple --ip <IP>`.
  No DNS entries are needed.

- **docker** -- deploys chatmail via Docker Compose inside a Docker-in-LXC
relay container (`security.nesting=true`), either directly pulled from GHCR or
injected from a host docker instance. Docker is installed inside the relay
automatically; no host Docker installation is required.

#### Docker subcommands

- `docker deploy RELAY` -- deploy chatmail into a relay container via
  Docker Compose.  Three image sources are supported:
  - `--source ghcr:TAG` -- pull a pre-built image from GHCR directly
    into the relay.  No builder container is involved.
  - `--source docker:TAG` -- pipe a locally-built image from the host
    Docker daemon into the relay via `docker save | docker load`.
  - `--image PATH` -- load a pre-exported image tarball.
  A docker-compose.yaml is fetched from
  [chatmail/docker](https://github.com/chatmail/docker) unless
  `--compose URL` overrides the source.

- `docker pull RELAY` -- pull a newer image from GHCR into an already
  deployed relay without a full redeploy.  Use `--tag` to specify the
  image tag (default: `main`).

- `docker ps RELAY` -- list running Docker Compose services in a relay.

- `docker logs RELAY` -- show Docker Compose logs (last 100 lines).
  Pass `-f` to follow in real time.

- `docker shell RELAY [SERVICE]` -- open an interactive shell inside
  the named Compose service (default: `chatmail`).

#### SSH into Docker services

For Docker-deployed relays, `cmlxc` auto-generates SSH config entries for
each running Compose service.  After any deploy or `cmlxc status`, you can:

    ssh chatmail@dk0.localchat

This uses `ProxyCommand` to run `docker exec` inside the LXC container.


## Releasing

Versions are derived from git tags via `setuptools-git-versioning`.
The changelog is generated with [git-cliff](https://git-cliff.org/)
using the `cliff.toml` config in the repo root.


Releases run the Incus functional suite in CI before publishing, so
run it locally first to avoid pushing a tag that cannot be released:

    pytest tests/fullrun.py

Then run the shared release script from a checkout of
[chatmail/workflows](https://github.com/chatmail/workflows):

    python ../workflows/scripts/make_new_release.py

It runs the checks, tests the built wheel, generates the CHANGELOG.md
entry with git-cliff and opens it in your editor, then commits, tags
vX.Y.Z and pushes. The release.yml workflow re-runs the Incus suite,
builds the sdist + wheel and publishes to PyPI via trusted publishing
(OIDC); no local twine or PyPI token is involved.
