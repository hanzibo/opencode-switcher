"""Static + behavioral tests for the install.sh installer script.

Per ``docs/plans/install-script-dependencies-plan.md`` the installer must:
  - declare one shared system-package array covering all six mandatory
    runtime dependencies (WebKit2GTK is resolved separately: 4.1 preferred,
    4.0 fallback, never mandatory on 4.0-only systems);
  - declare a WebKit package array (gir1.2-webkit2-4.1, gir1.2-webkit2-4.0)
    and resolve an available package before apt installation in both the
    dpkg discovery path and the non-``dpkg`` fallback;
  - declare a terminal array matching ``system/launcher.py``
    (ptyxis, gnome-terminal, kgx, blackbox) used by a detection helper;
  - check WebKit2 availability with the same 4.1/4.0 fallback the app uses
    and provide an actionable apt hint;
  - check Python module imports via ``importlib.import_module(sys.argv[1])``
    (module name passed as argv, never interpolated into ``python -c``
    source) and reject/skip invalid import names;
  - validate ``INSTALL_DIR`` early (safe charset, reject empty, ``/``,
    ``$HOME``, ``.``/``..`` components) before any install/uninstall use;
  - harden uninstall process matching: no regex expansion from INSTALL_DIR,
    verify cmdline contains the exact install path before killing;
  - add a system-dependency + runtime-binding section to ``cmd_status``;
  - run ``apt update`` in the dpkg path only when a required package (base or
    either WebKit2) is missing — a fully-present reinstall skips it — while
    the no-``dpkg`` fallback keeps its unconditional refresh;
  - install ``tiktoken`` solely via ``requirements.txt`` (no duplicate pip
    install, no misleading "optional" claim).

These checks are mostly static (no installer is executed): they parse
``install.sh`` source and run ``bash -n``. Pure helper functions
(``resolve_webkit_package``, ``py_import_ok``, ``validate_install_dir``) are
extracted and executed under bash with controlled environments.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INSTALL_SH = os.path.join(_ROOT_DIR, "install.sh")
_LAUNCHER_PY = os.path.join(_ROOT_DIR, "system", "launcher.py")

_SYSTEM_PACKAGES = [
    "gir1.2-ayatanaappindicator3-0.1",
    "python3-gi",
    "python3-gi-cairo",
    "python3-pip",
    "python3-venv",
    "wl-clipboard",
]

_WEBKIT_PACKAGES = ["gir1.2-webkit2-4.1", "gir1.2-webkit2-4.0"]

_SUPPORTED_TERMINALS = ["ptyxis", "gnome-terminal", "kgx", "blackbox"]

_BASH = shutil.which("bash") or "bash"

_TOP_LEVEL_ARRAY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=\(([^)]*)\)\s*$", re.M)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _install_sh():
    """Return the current install.sh source text."""
    return _read(_INSTALL_SH)


def _function_body(name):
    """Extract the body of a top-level bash function (empty string if absent).

    Relies on the convention that a function's closing ``}`` is a line of its
    own and no inner line starts with ``}`` (true for install.sh today).
    """
    pattern = "^" + re.escape(name) + r"\s*\(\s*\)\s*\{\n(.*?)\n\}"
    m = re.search(pattern, _install_sh(), re.M | re.S)
    return m.group(1) if m else ""


def _top_level_arrays():
    """Map top-level array names to their word-split item lists."""
    arrays = {}
    for m in _TOP_LEVEL_ARRAY_RE.finditer(_install_sh()):
        arrays[m.group(1)] = m.group(2).split()
    return arrays


def _package_array_names():
    """Names of top-level arrays that contain all six system packages."""
    return [
        name
        for name, items in _top_level_arrays().items()
        if set(_SYSTEM_PACKAGES).issubset(set(items))
    ]


def _terminal_array_names():
    """Names of top-level arrays that contain all four supported terminals."""
    return [
        name
        for name, items in _top_level_arrays().items()
        if set(_SUPPORTED_TERMINALS).issubset(set(items))
    ]


def _run_install_fn(name, argv=(), env_extra=None):
    """Execute a single install.sh function body under bash (no installer run).

    The function body is extracted from source, so only pure helpers with no
    install/uninstall side effects can be exercised this way. Returns a
    ``subprocess.CompletedProcess``; for rejections that ``exit`` the shell the
    returncode is nonzero and stdout is empty.
    """
    body = _function_body(name)
    if not body:
        raise AssertionError(f"function {name} not found in install.sh")
    env = dict(os.environ)
    env.update(env_extra or {})
    # SCRIPT_DIR is derived from $0 in install.sh; expose the real source dir so
    # extracted helpers that compare INSTALL_DIR against it behave identically.
    env.setdefault("SCRIPT_DIR", _ROOT_DIR)
    arrays = "".join(
        f"{n}=({' '.join(items)})\n" for n, items in _top_level_arrays().items()
    )
    quoted = " ".join(shlex.quote(str(a)) for a in argv)
    script = (
        'error() { printf "ERR: %s\\n" "$*" >&2; }\n'
        + arrays
        + f"{name}() {{\n{body}\n}}\n"
        + f"{name} {quoted}\n"
        + "rc=$?\n"
        + 'printf "%s" "$INSTALL_DIR"\n'
        + "exit $rc\n"
    )
    return subprocess.run(
        [_BASH, "-c", script], capture_output=True, text=True, env=env, timeout=20
    )


def _fake_dpkg(installed):
    """Source for a fake ``dpkg`` reporting ``installed`` packages as present."""
    names = "|".join(installed)
    return (
        "#!/usr/bin/env bash\n"
        'pkg="$2"\n'
        f'case "$pkg" in\n    {names})\n        exit 0 ;;\nesac\n'
        "exit 1\n"
    )


def _run_install_system_deps(installed_pkgs):
    """Execute install_system_deps with fake dpkg/apt/sudo and stub helpers.

    Pure side-effect simulation, no apt/install/uninstall on the host. The
    fake ``apt`` logs every invocation ("update -qq", "install -y -qq <pkgs>")
    to a per-run file. Returns (returncode, stdout, stderr, log_text).
    """
    body = _function_body("install_system_deps")
    resolve_body = _function_body("resolve_webkit_package")
    if not body or not resolve_body:
        raise AssertionError(
            "install_system_deps/resolve_webkit_package not found in install.sh"
        )
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = os.path.join(tmp, "bin")
        log = os.path.join(tmp, "apt.log")
        os.makedirs(bin_dir, exist_ok=True)
        files = {
            "dpkg": _fake_dpkg(installed_pkgs),
            "apt": "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$LOG\"\nexit 0\n",
            "sudo": '#!/usr/bin/env bash\nexec "$@"\n',
            "apt-cache": "#!/usr/bin/env bash\nexit 0\n",
            "python3": "#!/usr/bin/env bash\nexit 0\n",
        }
        for name, content in files.items():
            p = os.path.join(bin_dir, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(p, 0o755)
        arrays = "".join(
            f"{n}=({' '.join(items)})\n" for n, items in _top_level_arrays().items()
        )
        script = (
            'info() { printf "[INFO] %s\\n" "$*"; }\n'
            'warn() { printf "[WARN] %s\\n" "$*" >&2; }\n'
            'error() { printf "ERR: %s\\n" "$*" >&2; }\n'
            'check_webkit2() { return 0; }\n'
            'detect_terminal() { return 0; }\n'
            + arrays
            + f"resolve_webkit_package() {{\n{resolve_body}\n}}\n"
            + f"install_system_deps() {{\n{body}\n}}\n"
            + "install_system_deps\n"
        )
        env = dict(os.environ)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env["LOG"] = log
        r = subprocess.run(
            [_BASH, "-c", script], capture_output=True, text=True, env=env, timeout=20
        )
        log_text = ""
        if os.path.exists(log):
            with open(log, encoding="utf-8") as f:
                log_text = f.read()
        return r.returncode, r.stdout, r.stderr, log_text


class TestSystemPackages(unittest.TestCase):
    """install.sh must install all six mandatory deps via a shared array."""

    def test_shared_package_array_has_all_six_packages(self):
        self.assertTrue(
            _package_array_names(),
            "install.sh must define a shared array containing all six packages: "
            + ", ".join(_SYSTEM_PACKAGES),
        )

    def test_install_system_deps_uses_package_array(self):
        body = _function_body("install_system_deps")
        names = _package_array_names()
        self.assertTrue(
            names, "no shared package array with all six packages found"
        )
        self.assertNotEqual(
            body, "", "install_system_deps() must exist in install.sh"
        )
        for name in names:
            self.assertIn(
                name,
                body,
                f"install_system_deps() must install via the shared array {name}",
            )

    def test_package_array_used_beyond_definition(self):
        # The array must drive both dpkg discovery and the non-dpkg fallback,
        # so its name must appear at least twice in the file.
        names = _package_array_names()
        self.assertTrue(
            names, "no shared package array with all six packages found"
        )
        for name in names:
            self.assertGreaterEqual(
                _install_sh().count(name),
                2,
                f"{name} must be defined and used (non-dpkg fallback + discovery)",
            )


class TestWebKitPackageResolution(unittest.TestCase):
    """WebKit2GTK must be resolved (4.1 preferred, 4.0 fallback), not forced."""

    def test_webkit_array_has_both_versions_41_first(self):
        names = [
            name
            for name, items in _top_level_arrays().items()
            if set(_WEBKIT_PACKAGES).issubset(set(items))
        ]
        self.assertTrue(
            names,
            "install.sh must define an array with both "
            + " and ".join(_WEBKIT_PACKAGES),
        )
        for name in names:
            items = _top_level_arrays()[name]
            self.assertEqual(
                items[0],
                "gir1.2-webkit2-4.1",
                f"{name} must prefer gir1.2-webkit2-4.1",
            )
            self.assertEqual(
                items[1],
                "gir1.2-webkit2-4.0",
                f"{name} must fall back to gir1.2-webkit2-4.0",
            )

    def test_webkit_not_mandatory_in_sys_packages(self):
        # 4.1 must NOT be in the mandatory install array, or 4.0-only systems
        # would be forced to install an unavailable package.
        for name in _package_array_names():
            self.assertNotIn(
                "gir1.2-webkit2-4.1",
                _top_level_arrays()[name],
                f"gir1.2-webkit2-4.1 must not be mandatory in {name}",
            )

    def test_install_system_deps_resolves_webkit_in_both_paths(self):
        body = _function_body("install_system_deps")
        self.assertIn(
            "resolve_webkit_package",
            body,
            "install_system_deps must resolve the WebKit package",
        )
        # pkg_list feeds both the dpkg discovery loop and the no-dpkg fallback
        self.assertGreaterEqual(
            body.count("pkg_list"),
            2,
            "resolved WebKit package must be used in dpkg and fallback paths",
        )

    def test_resolver_uses_webkit_array(self):
        body = _function_body("resolve_webkit_package")
        self.assertNotEqual(body, "", "resolve_webkit_package() must exist")
        self.assertIn("WEBKIT_PACKAGES", body)

    def test_webkit2_package_is_declared(self):
        self.assertIn(
            "gir1.2-webkit2-4.1",
            _install_sh(),
            "install.sh must reference gir1.2-webkit2-4.1",
        )

    def test_webkit2_41_and_40_fallback(self):
        text = _install_sh()
        self.assertIn("4.1", text, "WebKit2 4.1 must be the primary binding")
        self.assertIn("4.0", text, "WebKit2 4.0 must be the fallback binding")

    def test_webkit2_apt_hint(self):
        hint_lines = [
            line
            for line in _install_sh().splitlines()
            if line.lstrip().startswith(("warn ", "info ", "error ", "echo "))
            and "apt" in line
            and ("WebKit" in line or "webkit" in line)
        ]
        self.assertTrue(
            hint_lines,
            "install.sh must print an actionable apt hint mentioning WebKit2",
        )


class TestResolveWebkitPackageBehavior(unittest.TestCase):
    """resolve_webkit_package() picks 4.1/4.0 per environment."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = cls.tmp.name

        def write(name, content):
            path = os.path.join(cls.base, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(path, 0o755)
            return path

        # 4.0-only apt repo: 4.1 has no candidate, 4.0 does.
        write(
            "bin40/dpkg",
            "#!/usr/bin/env bash\nexit 1\n",
        )
        write(
            "bin40/apt-cache",
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "policy" ]; then\n'
            '  case "$2" in\n'
            "    gir1.2-webkit2-4.1) printf 'gir1.2-webkit2-4.1:\\n  Candidate: (none)\\n' ;;\n"
            "    gir1.2-webkit2-4.0) printf 'gir1.2-webkit2-4.0:\\n  Candidate: 2.40.0-1\\n' ;;\n"
            "  esac\n"
            "fi\n",
        )
        # Both versions available.
        write("binboth/dpkg", "#!/usr/bin/env bash\nexit 1\n")
        write(
            "binboth/apt-cache",
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "policy" ]; then\n'
            '  case "$2" in\n'
            "    gir1.2-webkit2-4.1) printf 'gir1.2-webkit2-4.1:\\n  Candidate: 2.42.5-1\\n' ;;\n"
            "    gir1.2-webkit2-4.0) printf 'gir1.2-webkit2-4.0:\\n  Candidate: 2.40.0-1\\n' ;;\n"
            "  esac\n"
            "fi\n",
        )
        # dpkg says 4.0 is installed.
        write(
            "bindpkg/dpkg",
            "#!/usr/bin/env bash\n"
            '[ "$2" = "gir1.2-webkit2-4.0" ] && exit 0\nexit 1\n',
        )
        # No package tooling at all.
        os.makedirs(os.path.join(cls.base, "emptydir"), exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _resolve(self, bin_dir):
        path = os.path.join(self.base, bin_dir)
        # "emptydir" simulates a system with no dpkg/apt-cache at all: the
        # function needs no external binaries on that branch, so PATH must not
        # leak the real dpkg/apt-cache from the host.
        path_env = path if bin_dir == "emptydir" else path + ":" + os.environ["PATH"]
        r = _run_install_fn(
            "resolve_webkit_package", env_extra={"PATH": path_env}
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_4_0_only_repo_falls_back_to_4_0(self):
        self.assertEqual(self._resolve("bin40"), "gir1.2-webkit2-4.0")

    def test_both_available_prefers_4_1(self):
        self.assertEqual(self._resolve("binboth"), "gir1.2-webkit2-4.1")

    def test_4_0_installed_returns_empty(self):
        self.assertEqual(self._resolve("bindpkg"), "")

    def test_no_tooling_defaults_to_4_1(self):
        self.assertEqual(self._resolve("emptydir"), "gir1.2-webkit2-4.1")


class TestAptUpdateOrdering(unittest.TestCase):
    """apt update must run before resolve_webkit_package in both install paths.

    resolve_webkit_package consults ``apt-cache policy``; on a fresh install
    with stale apt metadata the 4.1/4.0 candidates can be missing, so apt
    update must happen first in the dpkg discovery path and the no-dpkg
    fallback alike.
    """

    def test_apt_update_precedes_resolve_webkit(self):
        body = _function_body("install_system_deps")
        self.assertNotEqual(
            body, "", "install_system_deps() must exist in install.sh"
        )
        code_lines = [
            line
            for line in body.splitlines()
            if not line.lstrip().startswith("#")
        ]
        apt_idx = next(
            (i for i, line in enumerate(code_lines) if "apt update" in line), -1
        )
        resolve_idx = next(
            (i for i, line in enumerate(code_lines) if "resolve_webkit_package" in line),
            -1,
        )
        self.assertGreaterEqual(
            apt_idx, 0, "install_system_deps must run apt update"
        )
        self.assertGreaterEqual(
            resolve_idx, 0, "install_system_deps must resolve the WebKit package"
        )
        self.assertLess(
            apt_idx,
            resolve_idx,
            "apt update must run before resolve_webkit_package: stale metadata "
            "hides 4.1/4.0 candidates on fresh installs",
        )

    def test_apt_update_runs_in_both_paths(self):
        body = _function_body("install_system_deps")
        self.assertGreaterEqual(
            body.count("apt update"),
            2,
            "apt update must run in the dpkg discovery path and the no-dpkg fallback",
        )


class TestInstallSystemDepsAptUpdatePolicy(unittest.TestCase):
    """apt update must only run when a required package is missing (dpkg path).

    Reinstall with all system packages already present must not touch the
    network: no apt update, no apt install, no resolve call. When something
    is missing, apt update must precede both resolve and install.
    """

    def test_satisfied_path_skips_apt_update(self):
        rc, out, err, log = _run_install_system_deps(
            _SYSTEM_PACKAGES + _WEBKIT_PACKAGES
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("已满足", out, "satisfied path must report packages satisfied")
        self.assertNotIn("apt update", log, "no apt update when all packages present")
        self.assertNotIn("apt install", log, "no apt install when all packages present")

    def test_missing_path_updates_before_install(self):
        installed = [p for p in _SYSTEM_PACKAGES if p != "wl-clipboard"]
        installed += _WEBKIT_PACKAGES
        rc, _out, err, log = _run_install_system_deps(installed)
        self.assertEqual(rc, 0, err)
        lines = [line for line in log.splitlines() if line.strip()]
        update_i = next(
            (i for i, line in enumerate(lines) if "update" in line), -1
        )
        install_i = next(
            (i for i, line in enumerate(lines) if "install" in line), -1
        )
        self.assertGreaterEqual(
            update_i, 0, "missing path must run apt update"
        )
        self.assertGreaterEqual(
            install_i, 0, "missing path must run apt install"
        )
        self.assertLess(
            update_i, install_i, "apt update must precede apt install"
        )
        self.assertIn(
            "wl-clipboard",
            lines[install_i],
            "only the missing package is installed",
        )

    def test_dpkg_path_apt_update_is_conditional(self):
        # Static guarantee: the dpkg-path apt update (the last occurrence) must
        # sit behind the satisfied/missing check, so a fully-present reinstall
        # never reaches it. The no-dpkg fallback's apt update is unconditional
        # by design.
        body = _function_body("install_system_deps")
        self.assertNotEqual(
            body, "", "install_system_deps() must exist in install.sh"
        )
        code_lines = [
            line
            for line in body.splitlines()
            if not line.lstrip().startswith("#")
        ]
        guard_idx = next(
            (
                i
                for i, line in enumerate(code_lines)
                if "-eq 0" in line and "missing_sys" in line
            ),
            -1,
        )
        self.assertGreaterEqual(
            guard_idx, 0, "the satisfied/missing check must exist"
        )
        update_idxs = [
            i for i, line in enumerate(code_lines) if "apt update" in line
        ]
        self.assertGreaterEqual(
            len(update_idxs), 2, "both paths must run apt update"
        )
        self.assertGreater(
            update_idxs[-1],
            guard_idx,
            "dpkg-path apt update must be gated behind the missing check",
        )


class TestTiktokenPolicy(unittest.TestCase):
    """tiktoken must come from requirements.txt only — no dead duplicate install.

    requirements.txt already declares ``tiktoken>=0.7`` (mandatory), so the
    former second ``pip install tiktoken`` in install_python_deps was dead
    code and its "optional" comment misleading.
    """

    def test_no_duplicate_tiktoken_install(self):
        body = _function_body("install_python_deps")
        self.assertNotEqual(
            body, "", "install_python_deps() must exist in install.sh"
        )
        for line in body.splitlines():
            if "pip" in line and "install" in line:
                self.assertNotIn(
                    "tiktoken",
                    line,
                    f"no pip install line may reference tiktoken: {line!r}",
                )
        self.assertNotIn(
            "tiktoken>=0.7",
            body,
            "the redundant pinned tiktoken install must be gone",
        )

    def test_python_deps_installed_once_via_requirements(self):
        body = _function_body("install_python_deps")
        self.assertEqual(
            body.count("pip"),
            1,
            "Python deps must be installed exactly once (pip -r requirements.txt)",
        )
        self.assertIn("-r", body, "install must use -r")
        self.assertIn(
            "requirements.txt",
            body,
            "requirements.txt is the single source of truth",
        )

    def test_requirements_txt_declares_tiktoken(self):
        req = _read(os.path.join(_ROOT_DIR, "requirements.txt"))
        self.assertIn("tiktoken", req, "requirements.txt must declare tiktoken")


class TestSupportedTerminals(unittest.TestCase):
    """install.sh must share one terminal array matching system/launcher.py."""

    def test_terminal_array_has_all_four_supported_terminals(self):
        names = _terminal_array_names()
        self.assertTrue(
            names,
            "install.sh must define an array with all four terminals: "
            + ", ".join(_SUPPORTED_TERMINALS),
        )

    def test_terminal_array_matches_launcher(self):
        self.assertTrue(
            os.path.isfile(_LAUNCHER_PY), "system/launcher.py not found"
        )
        launcher = _read(_LAUNCHER_PY)
        m = re.search(r"_TERMINALS\s*=\s*\[(.*?)\]", launcher, re.S)
        self.assertIsNotNone(m, "system/launcher.py must define _TERMINALS")
        launcher_terms = re.findall(r'"([^"]+)"', m.group(1))
        self.assertTrue(
            any(
                set(launcher_terms).issubset(set(items))
                for items in _top_level_arrays().values()
            ),
            f"install.sh terminal array must cover launcher _TERMINALS {launcher_terms}",
        )

    def test_terminal_detection_uses_terminal_array(self):
        # The detection helper must iterate the shared array, so the array name
        # appears beyond its definition line.
        names = _terminal_array_names()
        self.assertTrue(
            names, "no terminal array with all four terminals found"
        )
        for name in names:
            self.assertGreaterEqual(
                _install_sh().count(name),
                2,
                f"{name} must be defined and used by the terminal-detection helper",
            )


class TestWebKit2Check(unittest.TestCase):
    """install.sh must check WebKit2 with the 4.1/4.0 fallback and an apt hint."""

    def test_webkit2_check_uses_both_bindings(self):
        body = _function_body("check_webkit2")
        self.assertNotEqual(body, "", "check_webkit2() must exist")
        self.assertIn("'4.1'", body, "check_webkit2 must probe binding 4.1 first")
        self.assertIn("'4.0'", body, "check_webkit2 must fall back to binding 4.0")


class TestAppWebKitBindingFallback(unittest.TestCase):
    """App views must probe WebKit2 4.1 with a 4.0 fallback, mirroring install.sh.

    Per ``docs/plans/install-script-dependencies-plan.md`` the installer may
    select ``gir1.2-webkit2-4.0`` on 4.0-only systems. The runtime imports in
    ``views/ai_chat_panel.py`` and ``views/clipboard_panel.py`` must therefore
    guard ``gi.require_version("WebKit2", "4.1")`` with a ``ValueError``
    fallback to 4.0 instead of hard-requiring 4.1 — otherwise startup fails
    on 4.0-only systems even though the installer installed 4.0.
    """

    _APP_WEBKIT_FILES = [
        os.path.join(_ROOT_DIR, "views", "ai_chat_panel.py"),
        os.path.join(_ROOT_DIR, "views", "clipboard_panel.py"),
    ]

    # Static pattern matching the module-level guard used elsewhere in the
    # project (see dialogs/image_preview_dialog.py).
    _GUARDED_FALLBACK_RE = re.compile(
        r'try:\n'
        r'\s+gi\.require_version\("WebKit2", "4\.1"\)\n'
        r'except ValueError:\n'
        r'\s+try:\n'
        r'\s+gi\.require_version\("WebKit2", "4\.0"\)\n'
        r'\s+except ValueError:\n'
        r'\s+pass',
        re.M,
    )

    def test_view_modules_guard_webkit2_fallback(self):
        for path in self._APP_WEBKIT_FILES:
            self.assertTrue(
                os.path.isfile(path), f"{path} not found"
            )
            text = _read(path)
            self.assertRegex(
                text,
                self._GUARDED_FALLBACK_RE,
                f"{path} must probe WebKit2 4.1 with a 4.0 fallback",
            )

    def test_view_modules_have_no_bare_4_1_require(self):
        for path in self._APP_WEBKIT_FILES:
            text = _read(path)
            self.assertEqual(
                text.count('gi.require_version("WebKit2", "4.1")'),
                1,
                f"{path} must require WebKit2 4.1 exactly once (inside the guard)",
            )
            self.assertEqual(
                text.count('gi.require_version("WebKit2", "4.0")'),
                1,
                f"{path} must require WebKit2 4.0 exactly once (as the fallback)",
            )

    def test_fallback_preserves_pangocairo_require(self):
        path = os.path.join(_ROOT_DIR, "views", "clipboard_panel.py")
        text = _read(path)
        self.assertIn('gi.require_version("PangoCairo", "1.0")', text)


class TestSafeImportConstruction(unittest.TestCase):
    """Python import checks must pass the module name as argv, never inline it."""

    def test_imports_via_importlib_argv(self):
        text = _install_sh()
        self.assertIn(
            "importlib.import_module(sys.argv[1])",
            text,
            "module name must go through importlib with sys.argv[1]",
        )
        self.assertNotIn(
            'python3 -c "import $mod"', text, "check_deps must not interpolate"
        )
        self.assertNotIn(
            'python3 -c "import $import_name"',
            text,
            "status must not interpolate import names",
        )
        self.assertNotIn(
            '"$PYTHON_BIN" -c "import $import_name"',
            text,
            "status must not interpolate import names into the venv python",
        )

    def test_cmd_status_validates_import_names(self):
        body = _function_body("cmd_status")
        self.assertNotEqual(body, "", "cmd_status() must exist")
        self.assertIn(
            "[A-Za-z_][A-Za-z0-9_.]*",
            body,
            "cmd_status must whitelist valid import names",
        )
        self.assertIn("py_import_ok", body)

    def test_import_helper_has_whitelist(self):
        body = _function_body("py_import_ok")
        self.assertNotEqual(body, "", "py_import_ok() must exist")
        self.assertIn("[A-Za-z_][A-Za-z0-9_.]*", body)


class TestPyImportOkBehavior(unittest.TestCase):
    """py_import_ok() accepts valid modules and rejects invalid names."""

    def test_valid_module_imports(self):
        r = _run_install_fn("py_import_ok", ("python3", "sys"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_module_fails(self):
        r = _run_install_fn("py_import_ok", ("python3", "no_such_module_xyz"))
        self.assertNotEqual(r.returncode, 0)

    def test_malicious_import_name_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "pwned")
            payload = f"sys;open('{marker}','w').close()"
            r = _run_install_fn("py_import_ok", ("python3", payload))
            self.assertNotEqual(r.returncode, 0, "malicious name must be rejected")
            self.assertFalse(
                os.path.exists(marker),
                "malicious import name must never be executed",
            )


class TestValidateInstallDirBehavior(unittest.TestCase):
    """validate_install_dir() accepts valid paths, rejects dangerous ones."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = os.path.join(cls.tmp.name, "home")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _validate(self, install_dir, script_dir=None):
        env_extra = {"HOME": self.home, "INSTALL_DIR": install_dir}
        if script_dir is not None:
            # 覆盖默认 SCRIPT_DIR（_ROOT_DIR），用于构造共享前缀的兄弟目录场景。
            env_extra["SCRIPT_DIR"] = script_dir
        return _run_install_fn("validate_install_dir", env_extra=env_extra)

    def test_accepts_tmp_test(self):
        r = self._validate("/tmp/test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "/tmp/test")

    def test_accepts_trailing_slash(self):
        r = self._validate("/tmp/test/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "/tmp/test")

    def test_accepts_tilde_home_path(self):
        r = self._validate("~/.local/share/opencode-switcher")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout,
            os.path.join(self.home, ".local/share/opencode-switcher"),
        )

    def test_rejects_empty(self):
        self.assertNotEqual(self._validate("").returncode, 0)

    def test_rejects_root(self):
        self.assertNotEqual(self._validate("/").returncode, 0)
        self.assertNotEqual(self._validate("//").returncode, 0)

    def test_accepts_symlink_to_existing_dir(self):
        # Symlinked custom paths are preserved and resolved to the physical path,
        # so the SCRIPT_DIR equality guard cannot be bypassed via a link.
        target = os.path.join(self.tmp.name, "custom-target")
        os.makedirs(target, exist_ok=True)
        link = os.path.join(self.tmp.name, "custom-link")
        os.symlink(target, link)
        r = self._validate(link)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, os.path.realpath(target))

    def test_rejects_script_source_dir(self):
        # INSTALL_DIR equal to the source tree would let install_files' stale
        # cleanup and cmd_uninstall's rm -rf delete the whole repo.
        r = self._validate(_ROOT_DIR)
        self.assertNotEqual(
            r.returncode, 0, "INSTALL_DIR equal to SCRIPT_DIR must be rejected"
        )

    def test_rejects_script_source_dir_trailing_slash(self):
        r = self._validate(_ROOT_DIR + "/")
        self.assertNotEqual(
            r.returncode, 0, "trailing-slash variant of SCRIPT_DIR must be rejected"
        )

    def test_rejects_script_source_dir_via_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            link = os.path.join(d, "repo-link")
            os.symlink(_ROOT_DIR, link)
            r = self._validate(link)
            self.assertNotEqual(
                r.returncode,
                0,
                "symlink resolving to SCRIPT_DIR must be rejected",
            )

    def test_rejects_script_source_dir_parent(self):
        # INSTALL_DIR 是 SCRIPT_DIR 的祖先：cmd_uninstall 的 rm -rf
        # "$INSTALL_DIR" 会把整个源码目录一并删除。
        r = self._validate(os.path.dirname(_ROOT_DIR))
        self.assertNotEqual(
            r.returncode, 0, "INSTALL_DIR ancestor of SCRIPT_DIR must be rejected"
        )

    def test_rejects_script_source_dir_child(self):
        # INSTALL_DIR 位于 SCRIPT_DIR 之下（即使尚不存在）：install_files 的
        # 清理块会在源码树内部反复清理/拷贝，破坏源码布局。
        r = self._validate(os.path.join(_ROOT_DIR, "__install_script_child__"))
        self.assertNotEqual(
            r.returncode, 0, "INSTALL_DIR descendant of SCRIPT_DIR must be rejected"
        )

    def test_rejects_script_source_dir_parent_via_symlink(self):
        # 解析后成为 SCRIPT_DIR 祖先的符号链接同样被拒绝（包括解析为 / 时）。
        with tempfile.TemporaryDirectory() as d:
            link = os.path.join(d, "repo-parent-link")
            os.symlink(os.path.dirname(_ROOT_DIR), link)
            r = self._validate(link)
            self.assertNotEqual(
                r.returncode,
                0,
                "symlink resolving to SCRIPT_DIR ancestor must be rejected",
            )

    def test_rejects_script_source_dir_child_via_symlink(self):
        # 指向 SCRIPT_DIR 内子目录的符号链接解析为物理路径后被拒绝。
        with tempfile.TemporaryDirectory() as d:
            link = os.path.join(d, "repo-child-link")
            os.symlink(os.path.join(_ROOT_DIR, "views"), link)
            r = self._validate(link)
            self.assertNotEqual(
                r.returncode,
                0,
                "symlink resolving into SCRIPT_DIR must be rejected",
            )

    def test_accepts_existing_unrelated_dir(self):
        # 与源码树无关的已存在绝对路径仍然可用。
        target = os.path.join(self.tmp.name, "unrelated-target")
        os.makedirs(target, exist_ok=True)
        r = self._validate(target)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, os.path.realpath(target))

    def test_accepts_sibling_prefix_dir(self):
        # 斜杠分隔前缀：/tmp/prefixdir 与 /tmp/prefixdirx 是共享前缀的兄弟
        # 目录，不是祖先/后代关系，必须被接受。
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "prefixdir")
            sibling = os.path.join(d, "prefixdirx")
            os.makedirs(base)
            os.makedirs(sibling)
            r = self._validate(sibling, script_dir=base)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, os.path.realpath(sibling))

    def test_rejects_home_dir(self):
        self.assertNotEqual(self._validate(self.home).returncode, 0)
        self.assertNotEqual(self._validate(self.home + "/").returncode, 0)
        self.assertNotEqual(self._validate("~").returncode, 0)

    def test_rejects_relative_paths(self):
        # ~ 展开后必须是绝对路径：相对路径会被误用于 rm -rf / pgrep
        for path in ("tmp/test", "relative/path", "install", "foo", "a/b/c"):
            self.assertNotEqual(
                self._validate(path).returncode, 0, f"must reject relative {path!r}"
            )

    def test_rejects_dot_components(self):
        for path in ("/home/../x", "../x", "/tmp/./x", "/tmp/x/.", "."):
            self.assertNotEqual(
                self._validate(path).returncode, 0, f"must reject {path!r}"
            )

    def test_rejects_injection_chars(self):
        for path in (
            "/tmp/foo;rm -rf /",
            "/tmp/foo`id`x",
            "/tmp/foo$(id)x",
            "/tmp/foo|sh",
            "/tmp/foo&bar",
            "/tmp/foo bar",
        ):
            self.assertNotEqual(
                self._validate(path).returncode, 0, f"must reject {path!r}"
            )


class TestInstallDirValidationDispatch(unittest.TestCase):
    """validate_install_dir() must run before install and uninstall."""

    def test_validate_install_dir_defined(self):
        self.assertNotEqual(
            _function_body("validate_install_dir"), "", "validate_install_dir() must exist"
        )

    def test_validate_rejects_script_dir_equality(self):
        body = _function_body("validate_install_dir")
        self.assertIn(
            "SCRIPT_DIR",
            body,
            "validate_install_dir must compare INSTALL_DIR against SCRIPT_DIR",
        )
        self.assertIn(
            'INSTALL_DIR" = "$SCRIPT_DIR"',
            body,
            "validate_install_dir must reject INSTALL_DIR equal to SCRIPT_DIR",
        )

    def test_validate_before_install_and_uninstall(self):
        text = _install_sh()
        self.assertRegex(
            text,
            r"install\s*\)\s*validate_install_dir\s*;\s*cmd_install",
            "install must validate INSTALL_DIR first",
        )
        self.assertRegex(
            text,
            r"uninstall\s*\)\s*validate_install_dir\s*;\s*cmd_uninstall",
            "uninstall must validate INSTALL_DIR first",
        )


class TestSystemdGuards(unittest.TestCase):
    """Missing systemd user sessions must warn, never abort install/uninstall."""

    def test_enable_service_guards_systemctl(self):
        body = _function_body("enable_service")
        self.assertNotEqual(body, "", "enable_service() must exist in install.sh")
        self.assertIn("command -v systemctl", body, "systemctl must be probed")
        self.assertIn("daemon-reload", body)
        self.assertIn("warn", body, "absence of systemd must warn, not abort")
        self.assertIn(
            "return 0", body, "skipping service registration must not abort install"
        )

    def test_uninstall_daemon_reload_guarded(self):
        body = _function_body("cmd_uninstall")
        self.assertNotEqual(body, "", "cmd_uninstall() must exist in install.sh")
        code_lines = [
            line
            for line in body.splitlines()
            if not line.lstrip().startswith("#")
        ]
        reload_lines = [
            line
            for line in code_lines
            if "daemon-reload" in line and "systemctl" in line
        ]
        self.assertTrue(reload_lines, "cmd_uninstall must daemon-reload")
        for line in reload_lines:
            self.assertTrue(
                "command -v systemctl" in line or "||" in line,
                f"daemon-reload must be guarded: {line!r}",
            )
        # The guard must sit before the file cleanup so a missing systemd user
        # session cannot abort removal of installed files.
        rm_idx = next(
            i for i, line in enumerate(code_lines) if line.lstrip().startswith("rm ")
        )
        reload_idx = next(
            i for i, line in enumerate(code_lines) if "daemon-reload" in line
        )
        self.assertLess(reload_idx, rm_idx)


class TestInstallFilesStaleCleanup(unittest.TestCase):
    """install_files must remove stale app files before copying (reinstall)."""

    def _cleanup_block(self):
        """The rm block: from the first rm line up to the first cp line."""
        body = _function_body("install_files")
        self.assertNotEqual(body, "", "install_files() must exist in install.sh")
        lines = body.splitlines()
        start = next(
            i for i, line in enumerate(lines) if line.lstrip().startswith("rm ")
        )
        end = next(
            i for i, line in enumerate(lines) if line.lstrip().startswith("cp ")
        )
        return "\n".join(lines[start:end])

    def test_cleanup_covers_all_copied_targets(self):
        body = _function_body("install_files")
        copied = set(
            re.findall(r'cp(?: -r)? "\$SCRIPT_DIR/([^"/]+)"\s+"\$INSTALL_DIR', body)
        )
        self.assertGreaterEqual(
            len(copied), 10, "install_files must copy the application files"
        )
        block = self._cleanup_block()
        for name in copied:
            self.assertIn(
                f'"$INSTALL_DIR/{name}"',
                block,
                f"stale {name} must be removed before re-copy",
            )

    def test_cleanup_preserves_venv(self):
        self.assertNotIn(
            '"$INSTALL_DIR/venv"',
            self._cleanup_block(),
            "cleanup must never remove the venv",
        )

    def test_cleanup_never_removes_install_dir_root(self):
        self.assertNotIn(
            'rm -rf "$INSTALL_DIR"',
            self._cleanup_block(),
            "cleanup must not remove INSTALL_DIR itself",
        )

    def test_cleanup_precedes_copy(self):
        lines = _function_body("install_files").splitlines()
        rm_idx = next(
            i for i, line in enumerate(lines) if line.lstrip().startswith("rm ")
        )
        cp_idx = next(
            i for i, line in enumerate(lines) if line.lstrip().startswith("cp ")
        )
        self.assertLess(rm_idx, cp_idx, "stale files must be removed before copying")


class TestUninstallProcessHardening(unittest.TestCase):
    """cmd_uninstall must not turn INSTALL_DIR into a pgrep regex."""

    def test_uninstall_verifies_exact_cmdline(self):
        body = _function_body("cmd_uninstall")
        self.assertNotEqual(body, "", "cmd_uninstall() must exist")
        self.assertIn(
            "grep -F", body, "cmdline must be matched as a fixed string"
        )
        self.assertIn(
            "/proc/$pid/cmdline", body, "candidates must be verified via /proc"
        )
        self.assertIn(
            '"$INSTALL_DIR/main.py"',
            body,
            "the exact install path must be verified before killing",
        )

    def test_uninstall_no_regex_from_install_dir(self):
        body = _function_body("cmd_uninstall")
        self.assertNotIn(
            'pgrep -f "$INSTALL_DIR/main.py"',
            body,
            "INSTALL_DIR must never be interpolated into a pgrep -f pattern",
        )


class TestStatusSystemDependencies(unittest.TestCase):
    """cmd_status must include a system-dependency + runtime-binding section."""

    def test_status_has_system_package_checks(self):
        body = _function_body("cmd_status")
        names = _package_array_names()
        self.assertTrue(
            names, "no shared package array with all six packages found"
        )
        self.assertNotEqual(body, "", "cmd_status() must exist in install.sh")
        self.assertTrue(
            "dpkg" in body or any(name in body for name in names),
            "cmd_status must check system packages (dpkg -s on the shared array)",
        )

    def test_status_checks_webkit2_binding(self):
        body = _function_body("cmd_status")
        self.assertNotEqual(body, "", "cmd_status() must exist in install.sh")
        self.assertIn(
            "WebKit2",
            body,
            "cmd_status must check the WebKit2 runtime binding",
        )

    def test_status_reports_webkit_package_fallback(self):
        body = _function_body("cmd_status")
        self.assertIn("gir1.2-webkit2-4.1", body)
        self.assertIn("gir1.2-webkit2-4.0", body)

    def test_status_binding_missing_hint_is_dual_version(self):
        # The binding-missing hint must not advertise only 4.1: on 4.0-only
        # systems that would tell the user to install an unavailable package.
        body = _function_body("cmd_status")
        hint_lines = [
            line
            for line in body.splitlines()
            if "WebKit2 运行时绑定" in line and "缺失" in line
        ]
        self.assertTrue(
            hint_lines, "cmd_status must print a WebKit2 binding-missing hint"
        )
        for line in hint_lines:
            self.assertIn("4.1", line, f"hint must keep 4.1 as primary: {line!r}")
            self.assertIn(
                "4.0",
                line,
                f"binding-missing hint must mention the 4.0 fallback: {line!r}",
            )


class TestUninstallKeepDataPrompt(unittest.TestCase):
    """The uninstall keep-data prompt must not abort on EOF under ``set -e``.

    A bare ``read -r keep_data`` returns nonzero on EOF (empty stdin), which
    with ``set -euo pipefail`` would terminate the uninstall before the
    user-data section runs. The guard defaults to keeping user data ("y").
    """

    def test_read_guarded_for_eof(self):
        body = _function_body("cmd_uninstall")
        self.assertNotEqual(body, "", "cmd_uninstall() must exist")
        read_lines = [line for line in body.splitlines() if "read -r keep_data" in line]
        self.assertEqual(
            len(read_lines),
            1,
            "cmd_uninstall must read keep_data exactly once",
        )
        self.assertIn(
            "||", read_lines[0], f"read must be EOF-guarded: {read_lines[0]!r}"
        )
        self.assertIn(
            'keep_data="y"',
            read_lines[0],
            "EOF must default to keeping user data",
        )

    def test_eof_defaults_to_keep(self):
        r = subprocess.run(
            [
                _BASH,
                "-c",
                'set -euo pipefail\nread -r keep_data || keep_data="y"\nprintf "%s" "$keep_data"',
            ],
            input="",
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "y")

    def test_unguarded_read_aborts_on_eof(self):
        # Regression contrast: the unguarded form fails under set -e.
        r = subprocess.run(
            [_BASH, "-c", 'set -euo pipefail\nread -r keep_data\nprintf "%s" "$keep_data"'],
            input="",
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(r.returncode, 0)


class TestInstallerSyntax(unittest.TestCase):
    """Syntax guards: bash -n always, shellcheck when available."""

    def test_bash_n_parse(self):
        self.assertTrue(os.path.isfile(_INSTALL_SH), "install.sh not found")
        result = subprocess.run(
            ["bash", "-n", _INSTALL_SH], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shellcheck_when_available(self):
        self.assertTrue(os.path.isfile(_INSTALL_SH), "install.sh not found")
        if shutil.which("shellcheck") is None:
            self.skipTest("shellcheck not installed")
        result = subprocess.run(
            ["shellcheck", _INSTALL_SH], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
