"""Static tests for the install.sh installer script (RED phase).

Per ``docs/plans/install-script-dependencies-plan.md`` the installer must:
  - declare one shared system-package array covering all seven runtime
    dependencies (including ``python3-gi-cairo`` and ``gir1.2-webkit2-4.1``)
    used for both Debian discovery and the non-``dpkg`` fallback;
  - declare a terminal array matching ``system/launcher.py``
    (ptyxis, gnome-terminal, kgx, blackbox) used by a detection helper;
  - check WebKit2 availability with the same 4.1/4.0 fallback the app uses
    and provide an actionable apt hint;
  - add a system-dependency + runtime-binding section to ``cmd_status``.

These checks are static (no installer is executed): they parse ``install.sh``
source and run ``bash -n``. The package/terminal/WebKit2/status tests FAIL
against the current installer and pass once the planned changes land.
"""

import os
import re
import shutil
import subprocess
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
    "gir1.2-webkit2-4.1",
]

_SUPPORTED_TERMINALS = ["ptyxis", "gnome-terminal", "kgx", "blackbox"]

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
    """Names of top-level arrays that contain all seven system packages."""
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


class TestSystemPackages(unittest.TestCase):
    """install.sh must install all seven runtime dependencies via a shared array."""

    def test_shared_package_array_has_all_seven_packages(self):
        self.assertTrue(
            _package_array_names(),
            "install.sh must define a shared array containing all seven packages: "
            + ", ".join(_SYSTEM_PACKAGES),
        )

    def test_install_system_deps_uses_package_array(self):
        body = _function_body("install_system_deps")
        names = _package_array_names()
        self.assertTrue(
            names, "no shared package array with all seven packages found"
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
            names, "no shared package array with all seven packages found"
        )
        for name in names:
            self.assertGreaterEqual(
                _install_sh().count(name),
                2,
                f"{name} must be defined and used (non-dpkg fallback + discovery)",
            )


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


class TestStatusSystemDependencies(unittest.TestCase):
    """cmd_status must include a system-dependency + runtime-binding section."""

    def test_status_has_system_package_checks(self):
        body = _function_body("cmd_status")
        names = _package_array_names()
        self.assertTrue(
            names, "no shared package array with all seven packages found"
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
