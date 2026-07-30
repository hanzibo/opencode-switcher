#!/usr/bin/env python3
"""AST-based static code review tool for OpenCode Switcher.

Parses all Python source files in the project into Abstract Syntax Trees (AST)
and performs automated safety, complexity, anti-pattern, and GTK compliance checks.
"""

import ast
import os
import sys
from dataclasses import dataclass, field
from typing import List, Dict, Any, Set


@dataclass
class Finding:
    severity: str  # "HIGH", "WARN", "INFO"
    filepath: str
    line: int
    rule_id: str
    message: str


class ASTAuditor(ast.NodeVisitor):
    """AST Visitor scanning a single Python source file for code quality rules."""

    def __init__(self, filepath: str, rel_path: str):
        self.filepath = filepath
        self.rel_path = rel_path
        self.findings: List[Finding] = []
        self._current_function: List[str] = []
        self._nesting_level = 0

    def add_finding(self, severity: str, line: int, rule_id: str, message: str):
        self.findings.append(Finding(
            severity=severity,
            filepath=self.rel_path,
            line=line,
            rule_id=rule_id,
            message=message,
        ))

    def visit_Call(self, node: ast.Call):
        # Rule 1: Security — Check for dangerous eval() or exec()
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        if func_name in ("eval", "exec"):
            self.add_finding("HIGH", node.lineno, "SEC-01", f"Use of potentially dangerous function '{func_name}()'")

        # Rule 2: Performance & Safety — Subprocess calls without timeout
        if func_name in ("run", "check_output", "check_call"):
            if isinstance(node.func, ast.Attribute) and getattr(node.func.value, "id", "") in ("subprocess", "sp"):
                keywords = {kw.arg for kw in node.keywords}
                if "timeout" not in keywords:
                    self.add_finding("WARN", node.lineno, "PERF-01", f"Subprocess call '{func_name}()' without timeout parameter")

        # Rule 3: GTK Safety — add_provider_for_screen global leak warning
        if func_name == "add_provider_for_screen":
            self.add_finding("INFO", node.lineno, "GTK-01", "Use of 'add_provider_for_screen' (global CSS provider scope)")

        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._current_function.append(node.name)
        
        # Rule 4: Method Complexity — Large functions (> 80 lines)
        func_len = (node.end_lineno or node.lineno) - node.lineno + 1
        if func_len > 80:
            self.add_finding("WARN", node.lineno, "CMPLX-01", f"Function '{node.name}' is unusually large ({func_len} lines)")

        # Rule 5: Parameter Count — Too many arguments (> 8 args)
        arg_count = len(node.args.args)
        if arg_count > 8:
            self.add_finding("WARN", node.lineno, "CMPLX-02", f"Function '{node.name}' has too many parameters ({arg_count} args)")

        self.generic_visit(node)
        self._current_function.pop()

    def visit_ClassDef(self, node: ast.ClassDef):
        # Rule 6: Class Bloat — Classes with too many methods (> 30 methods)
        methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if len(methods) > 35:
            self.add_finding("WARN", node.lineno, "CMPLX-03", f"Class '{node.name}' has high method count ({len(methods)} methods)")

        self.generic_visit(node)

    def visit_If(self, node: ast.If):
        self._nesting_level += 1
        if self._nesting_level > 5:
            self.add_finding("WARN", node.lineno, "CMPLX-04", f"Control flow nesting level ({self._nesting_level}) exceeds threshold")
        self.generic_visit(node)
        self._nesting_level -= 1


def run_ast_audit(root_dir: str) -> List[Finding]:
    all_findings: List[Finding] = []
    
    exclude_dirs = {".git", "venv", "__pycache__", "katex", ".gemini"}

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            
            filepath = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(filepath, root_dir)

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                tree = ast.parse(content, filename=rel_path)
                auditor = ASTAuditor(filepath, rel_path)
                auditor.visit(tree)
                all_findings.extend(auditor.findings)
            except SyntaxError as e:
                all_findings.append(Finding("HIGH", rel_path, e.lineno or 0, "SYNTAX-01", f"Syntax error: {e.msg}"))
            except Exception as e:
                all_findings.append(Finding("WARN", rel_path, 0, "PARSE-01", f"Failed to parse AST: {e}"))

    return all_findings


def print_audit_report(findings: List[Finding]):
    print("=" * 80)
    print(" 🔍 OpenCode Switcher — AST Code Quality & Safety Report")
    print("=" * 80)

    if not findings:
        print("\n✨ Clean! No issues identified by AST audit.")
        return

    highs = [f for f in findings if f.severity == "HIGH"]
    warns = [f for f in findings if f.severity == "WARN"]
    infos = [f for f in findings if f.severity == "INFO"]

    print(f"\nSummary: Total {len(findings)} findings | 🔴 HIGH: {len(highs)} | 🟡 WARN: {len(warns)} | 🔵 INFO: {len(infos)}\n")

    for f in sorted(findings, key=lambda x: (x.severity != "HIGH", x.severity != "WARN", x.filepath, x.line)):
        badge = "🔴 [HIGH]" if f.severity == "HIGH" else "🟡 [WARN]" if f.severity == "WARN" else "🔵 [INFO]"
        print(f"{badge} {f.filepath}:{f.line} ({f.rule_id}) -> {f.message}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    results = run_ast_audit(project_root)
    print_audit_report(results)
