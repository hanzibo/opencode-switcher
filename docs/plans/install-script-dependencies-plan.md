# Install Script Dependency Corrections

## Scope

Update `install.sh` so fresh Debian/Ubuntu installations include the runtime
dependencies required by the GTK/WebKit AI panel and so installer diagnostics
match the supported terminal list.

Required system packages:

- `gir1.2-ayatanaappindicator3-0.1`
- `python3-gi`
- `python3-gi-cairo`
- `python3-pip`
- `python3-venv`
- `wl-clipboard`
- `gir1.2-webkit2-4.1`

Supported terminals must match `system/launcher.py`: `ptyxis`,
`gnome-terminal`, `kgx`, and `blackbox`.

## Implementation

1. Add shared system-package and terminal arrays plus a terminal-detection
   helper.
2. Use the package array for both Debian package discovery and installation,
   including the non-`dpkg` fallback.
3. Check WebKit2 availability with the same 4.1/4.0 fallback used by the
   application and provide an actionable apt hint.
4. Add system dependency and runtime binding checks to `install.sh status`.
5. Synchronize the README apt command with the installer and AGENTS.md.
6. Add static/syntax tests for package, terminal, WebKit2, and status coverage.

The uninstall path is intentionally unchanged; system packages are not
removed during application uninstall.

## Validation

- `bash -n install.sh`
- `venv/bin/python3 -m unittest tests.test_install_script`
- `venv/bin/python3 -m unittest discover tests`
- `./install.sh status` and `./install.sh help` smoke checks
- `shellcheck install.sh` when available

## Commit and Rollback

Use focused commits for tests, installer corrections, and README alignment.
Each commit is independently revertible with `git revert <sha>`. No database,
dependency-lock, or user-data migration is introduced.

Do not merge or push this branch.
