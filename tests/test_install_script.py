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
  - add a system-dependency + runtime-binding section to ``cmd_status``.

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

    def _validate(self, install_dir):
        return _run_install_fn(
            "validate_install_dir",
            env_extra={"HOME": self.home, "INSTALL_DIR": install_dir},
        )

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

    def test_rejects_home_dir(self):
        self.assertNotEqual(self._validate(self.home).returncode, 0)
        self.assertNotEqual(self._validate(self.home + "/").returncode, 0)
        self.assertNotEqual(self._validate("~").returncode, 0)

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
