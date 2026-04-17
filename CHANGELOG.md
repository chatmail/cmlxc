# Changelog

## [0.11.0] - 2026-04-17

### Features / Changes

- add --ipv4-only option to init CLI, and refactor ipv6 handling.

## [0.10.0] - 2026-04-17

### Features / Changes

- ci: run each cmlxc command as a separate step in GitHub UI.
- ci: split lxc-test into plan and run jobs for better UI visibility.

### Fixes

- resolve cross-relay DNS failures and improve CI command grouping.
- cli: support branches with slashes in --source and restore tests.

### Miscellaneous Tasks

- release v0.10.0.

### Other

- drop all previous cached images, to try to debug hanging cmdeploy/madmail tests.

## [0.9.0] - 2026-04-16

### Features / Changes

- "cmlxc status" can now take one or multiple hosts.

### Fixes

- support mixed cmdeploy/madmail testing.

### Miscellaneous Tasks

- release v0.9.0.

### Other

- wait for relay services to become ready.

### Refactor

- streamline wait/timeout from the various call sites.

## [0.8.1] - 2026-04-15

### Miscellaneous Tasks

- release v0.8.1.

### Other

- minimize cached builder images.

## [0.8.0] - 2026-04-15

### Features / Changes

- madmail: add mandatory install flags for IP-based deployment.
- madmail: add test-madmail command and E2E tests, with some selected madmail tests run by default.

### Miscellaneous Tasks

- release v0.8.0.

### Other

- fix log print out and consistent capitalization.

### Refactor

- [**breaking**] extract container module and standardise driver hooks.

## [0.7.5] - 2026-04-14

### Documentation

- note Zabbly Incus and refactor init prep.

### Fixes

- create venv per relay instead of in template.
- validate relay names to reject path-like arguments, and validate --source local paths.

### Miscellaneous Tasks

- release v0.7.5.

### Other

- remove superflous indirection of madmail repo url.

### Refactor

- use relay's scripts/initenv.sh for venv setup instead of custom venv and install commands.

## [0.7.4] - 2026-04-13

### Fixes

- incus: make launching of base setup image more robust and force labeling.
- doc: add note about possible incus failures.

### Miscellaneous Tasks

- release v0.7.4.

### Other

- README: some fixes.

## [0.7.3] - 2026-04-13

### Features / Changes

- cli: add --version option and show version in help string.
- cli: show version at top of deploy and test command output.

### Miscellaneous Tasks

- release v0.7.3.

## [0.7.2] - 2026-04-13

### CI

- releases run tests, and PRs runs tests..

### Miscellaneous Tasks

- release v0.7.2.

## [0.7.1] - 2026-04-13

### Miscellaneous Tasks

- revise automatic releasing to avoid amending local commits.
- release v0.7.1.
- release v0.7.1.

## [0.7.0] - 2026-04-13

### CI

- skip image export on cache hit in lxc-test.

### Documentation

- reformat README and add release automation script.

### Features / Changes

- better per-relay status, and generally more exact references in the output.
- remove all per-container state, and put it into the build container instead.
- move host setup instructions behind 'status --host', revise output.
- make_new_release.py checks for proper git state.
- make_new_release.py checks for proper git state.

### Fixes

- gate release on test suite, add skip-existing to PyPI publish.
- reusable workflow caching.
- add missing driver_base.py file.
- allow concurrent cmlxc runs, and prevent conflicts on ssh-config manipulations, allow drivers to participate in builder container preparation.

### Miscellaneous Tasks

- upgrade action dependencies for Node 24.
- upgrade action dependencies for Node 24.
- improve release script to also first run tests.
- reduce number of printed references, fix DNS issues.

### Other

- reorder README.

### Refactor

- [**breaking**] cli: shift driver specific CLI handling to driver modules.

## [0.6.4] - 2026-04-12

### Fixes

- add skip-existing to PyPI publish to tolerate re-tagged releases.

### Miscellaneous Tasks

- slightly better phrasing for "cmdeploy" relays.

## [0.6.3] - 2026-04-12

### Features / Changes

- commit a "lxc-test" workflow provider for helping other repositories to use lxc testing.
- require explicit --cmdeploy/--madmail SOURCE for init.
- require explicit --cmdeploy/--madmail SOURCE for init.

### Fixes

- make github actions deal more explicitely with ssh-key identities, and run from "cmlxc" directory to conftest.py gets picked up properly, and output is proper.
- disable services before caching relay image, and fix re-inject DNS config after cmdeploy, add some cross-relay DNS diagnostics. Also fix various workflow run issues, and fix image export step to handle incus adding .tar.gz extension automatically..
- optimize init and CI by avoiding redundant clones and exports.

### Miscellaneous Tasks

- ignore dist/ directory.
- add release workflow using OIDC/environments based on tagging, update workflow deps.
- add git-cliff configuration adapted from core.
- disable cancel-in-progress for Test workflow.
- define concurrency group for reusable lxc-test workflow.
- ensure cancel-in-progress is false.

### Other

- only install the debian dependencies needed for crypt_r compilation like cmdeploy does, also up image cache key for good measure.

### Refactor

- move Go toolchain install into builder init phase.

## [0.5.0] - 2026-04-12

### Features / Changes

- initial commit of cmlxc tool.

[0.11.0]: https://github.com/chatmail/cmlxc/compare/v0.10.0..v0.11.0
[0.10.0]: https://github.com/chatmail/cmlxc/compare/v0.9.0..v0.10.0
[0.9.0]: https://github.com/chatmail/cmlxc/compare/v0.8.1..v0.9.0
[0.8.1]: https://github.com/chatmail/cmlxc/compare/v0.8.0..v0.8.1
[0.8.0]: https://github.com/chatmail/cmlxc/compare/v0.7.5..v0.8.0
[0.7.5]: https://github.com/chatmail/cmlxc/compare/v0.7.4..v0.7.5
[0.7.4]: https://github.com/chatmail/cmlxc/compare/v0.7.3..v0.7.4
[0.7.3]: https://github.com/chatmail/cmlxc/compare/v0.7.2..v0.7.3
[0.7.2]: https://github.com/chatmail/cmlxc/compare/v0.7.1..v0.7.2
[0.7.1]: https://github.com/chatmail/cmlxc/compare/v0.7.0..v0.7.1
[0.7.0]: https://github.com/chatmail/cmlxc/compare/v0.6.4..v0.7.0
[0.6.4]: https://github.com/chatmail/cmlxc/compare/v0.6.3..v0.6.4
[0.6.3]: https://github.com/chatmail/cmlxc/compare/v0.5.0..v0.6.3

