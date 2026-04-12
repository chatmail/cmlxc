# Changelog

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

[0.6.3]: https://github.com/chatmail/cmlxc/compare/v0.5.0..v0.6.3

