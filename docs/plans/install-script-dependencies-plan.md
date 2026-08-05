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

WebKit2GTK is resolved dynamically (NOT mandatory): `gir1.2-webkit2-4.1`
preferred, `gir1.2-webkit2-4.0` fallback — 4.0-only systems must not be
forced to install 4.1.

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

## Review-blocker hardening

- **WebKit package resolution**: `WEBKIT_PACKAGES=(gir1.2-webkit2-4.1
  gir1.2-webkit2-4.0)`; `resolve_webkit_package()` prefers an installed or
  apt-cache-available 4.1 and falls back to 4.0 (defaults to 4.1 when no
  package tooling is detectable). The resolved package feeds both the dpkg
  discovery loop and the no-`dpkg` fallback via `pkg_list`. `status` reports
  the installed 4.1/4.0 package in addition to the runtime binding.
- **No dynamic-command injection in status**: Python module checks route the
  module name through `importlib.import_module(sys.argv[1])` as argv
  (`py_import_ok <python> <module>`); requirements.txt-derived import names
  must match the whitelist `[A-Za-z_][A-Za-z0-9_.]*` or the check is skipped
  with a warning. `requirements.txt` content is never interpolated into
  `python -c` source.
- **INSTALL_DIR validation**: `validate_install_dir()` runs before both
  install and uninstall. It expands a leading `~`, strips trailing slashes,
  and rejects empty values, `/`, `$HOME`, `.`/`..` path components, and any
  character outside `[A-Za-z0-9._/+_-]`. Valid custom paths such as
  `~/.local/share/opencode-switcher` and `/tmp/test` are preserved.
- **Uninstall process matching**: candidate PIDs come from a fixed safe
  pgrep pattern, then each `/proc/<pid>/cmdline` is matched with `grep -F`
  against the exact `$INSTALL_DIR/main.py` — INSTALL_DIR is never expanded
  into a pgrep regex and only processes whose cmdline contains the exact
  install path are killed.
- **apt update only on missing packages (dpkg path)**: `install_system_deps()`
  probes every `SYS_PACKAGES` entry plus either `WEBKIT_PACKAGES` member with
  `dpkg -s` *before* any apt invocation. If all are present, reinstall is
  fully offline: no `apt update`, no `resolve_webkit_package`, no `apt
  install`. Only when something is missing does the dpkg path run `sudo apt
  update -qq` *before* `resolve_webkit_package()` — `apt-cache policy` needs
  fresh metadata to pick the 4.1/4.0 candidate on fresh installs — then it
  installs only the missing packages. The no-`dpkg` fallback keeps its
  unconditional `apt update` (it cannot probe installed state without dpkg).
- **tiktoken via requirements.txt only**: `requirements.txt` already declares
  `tiktoken>=0.7` (mandatory). `install_python_deps()` therefore installs
  exactly once via `pip -r requirements.txt`; the redundant second
  `pip install tiktoken` and its misleading "optional" comment are removed.
  `requirements.txt` is the single source of truth for Python dependencies.
- **Absolute INSTALL_DIR required**: after `~` expansion and trailing-slash
  stripping, `validate_install_dir()` rejects any path not starting with `/`
  (relative paths would be misinterpreted by `rm -rf`/pgrep).
- **systemd absence tolerated**: `enable_service()` probes `systemctl` and
  warns instead of aborting when no systemd user session exists (install
  still completes); `cmd_uninstall`'s `daemon-reload` is likewise guarded so
  a missing session cannot abort the file cleanup that follows.
- **Reinstall stale-file cleanup**: `install_files()` removes only the
  application files/directories it copies (main.py, package dirs, run.sh,
  toggle, icon, katex) before re-copying, so `cp -r` reinstall leaves no
  ghost files. The `venv` and user data (`~/.config`, `~/.cache`) are never
  touched; `rm -rf "$INSTALL_DIR"` only happens in `cmd_uninstall`.
- **INSTALL_DIR ∉ {ancestors, descendants} of SCRIPT_DIR**: `validate_install_dir()`
  canonicalizes both `INSTALL_DIR` (when it already exists, via `cd -P &&
  pwd -P`, resolving symlinks) and `SCRIPT_DIR`, then rejects (a) equality,
  (b) `SCRIPT_DIR` under `$INSTALL_DIR/`, and (c) `INSTALL_DIR` under
  `$SCRIPT_DIR/` via slash-delimited prefix checks before any
  install/uninstall use. Without these guards, pointing `INSTALL_DIR` at the
  source tree, at an ancestor of it (e.g. `INSTALL_DIR=/parent` with the
  checkout at `/parent/opencode-switcher`), or at a subdirectory of it lets
  `install_files()`' stale cleanup and `cmd_uninstall`'s `rm -rf` delete the
  entire repo. Valid absolute custom paths (including symlinks to existing
  directories) are preserved and resolved to their physical path; sibling
  paths sharing a textual prefix (e.g. `/tmp/prefixdir` vs `/tmp/prefixdirx`)
  are not misclassified.
- **Dual-version WebKit status hint**: the `cmd_status` binding-missing hint
  no longer advertises `gir1.2-webkit2-4.1` alone; it now names both 4.1
  (primary) and 4.0 (fallback for 4.0-only systems), mirroring
  `check_webkit2`'s apt hint so a 4.0-only machine is not told to install an
  unavailable package.
- **EOF-safe keep-data prompt**: `cmd_uninstall` reads `keep_data` via
  `read -r keep_data || keep_data="y"`. A bare `read` returns nonzero on EOF
  (empty stdin) and, under `set -euo pipefail`, would abort the uninstall
  before the user-data section runs; the guard makes EOF default to keeping
  user data ("y").

The uninstall path is otherwise intentionally unchanged; system packages are
not removed during application uninstall.

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
