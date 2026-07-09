"""
Code Analysis Tools — code metrics, project dependencies, and file AST parsing.

Extracted from tool_registry.py to reduce module size.
"""

import ast
import fnmatch
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from .common import _get_ignore_dirs
from .filesystem import _format_file_size, _resolve_safe_path


# ── Code Metrics ────────────────────────────────────────────────────────────

_METRICS_BINARY_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pyc", ".o", ".so", ".dll", ".dylib",
    ".zip", ".tar", ".gz", ".bz2", ".xz",
})

_METRICS_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    "env", "build", "dist", "target", ".cache", ".omo", ".hzb-agents",
})


def _is_binary(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in _METRICS_BINARY_EXTS


def _count_file_lines(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        total = 0
        code = 0
        comments = 0
        blank = 0
        is_python = path.endswith(".py")
        for line in source.split("\n"):
            total += 1
            stripped = line.strip()
            if not stripped:
                blank += 1
            elif stripped.startswith("#"):
                comments += 1
            else:
                code += 1
        result = {
            "file": os.path.basename(path),
            "total_lines": total,
            "code_lines": code,
            "comment_lines": comments,
            "blank_lines": blank,
        }
        if is_python:
            try:
                tree = ast.parse(source)
                funcs = sum(1 for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
                classes = sum(1 for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
                result["num_functions"] = funcs
                result["num_classes"] = classes
            except SyntaxError:
                result["num_functions"] = 0
                result["num_classes"] = 0
        return result
    except Exception:
        return None


def _format_metrics_table(results: list, total: dict, sort_by: str = "total") -> str:
    if not results:
        return "没有找到可分析的文件。"
    if sort_by == "name":
        sorted_results = sorted(results, key=lambda x: x.get("file", ""))
    elif sort_by == "code":
        sorted_results = sorted(results, key=lambda x: x["code_lines"], reverse=True)
    else:
        sorted_results = sorted(results, key=lambda x: x["total_lines"], reverse=True)
    lines = []
    if len(results) > 1:
        lines.append("📊 代码度量汇总")
        lines.append(f"{'文件':40s} {'总行数':>8s} {'代码行':>8s} {'注释行':>8s} {'空行':>8s}{' 函数':>6s}{' 类':>6s}")
        lines.append("─" * 90)
        for r in sorted_results:
            fn = r.get("file", "?")
            fn_trunc = fn[:38] + ".." if len(fn) > 38 else fn
            funcs = r.get("num_functions", "-")
            classes = r.get("num_classes", "-")
            f_str = str(funcs) if funcs != "-" else "-"
            c_str = str(classes) if classes != "-" else "-"
            lines.append(
                f"{fn_trunc:40s} {r['total_lines']:>8d} {r['code_lines']:>8d} "
                f"{r['comment_lines']:>8d} {r['blank_lines']:>8d} {f_str:>6s} {c_str:>6s}"
            )
        lines.append("─" * 90)
        lines.append(
            f"{'总计':40s} {total['total_lines']:>8d} {total['code_lines']:>8d} "
            f"{total['comment_lines']:>8d} {total['blank_lines']:>8d} "
            f"{str(total.get('num_functions', '-')):>6s} {str(total.get('num_classes', '-')):>6s}"
        )
    else:
        r = results[0]
        lines.append(f"📊 代码度量: {r['file']}")
        lines.append(f"   总行数:     {r['total_lines']}")
        lines.append(f"   代码行:     {r['code_lines']}")
        lines.append(f"   注释行:     {r['comment_lines']}")
        if r["total_lines"] > 0:
            pct = r["comment_lines"] / r["total_lines"] * 100
            lines[-1] += f"  ({pct:.1f}%)"
        lines.append(f"   空行:       {r['blank_lines']}")
        if "num_functions" in r:
            lines.append(f"   函数:       {r['num_functions']}")
            lines.append(f"   类:         {r['num_classes']}")
    return "\n".join(lines)


def execute_get_code_metrics(path: str, include: str = "",
                              exclude: str = "",
                              sort_by: str = "total") -> str:
    """Analyze code metrics for a file or directory.

    Supports Python (with ast-based function/class counting) and
    common text-based source files. Binary files are skipped automatically.

    Args:
        path: Absolute path to file or directory.
        include: Glob pattern to filter files (e.g. "*.py"). Empty = all supported types.
        exclude: Extra directory names to skip (comma-separated).

    Returns:
        Formatted metrics report with line counts and structure info.
    """
    if not path or not os.path.isabs(path):
        return "❌ 错误：必须使用绝对路径！"

    resolved = os.path.realpath(path)

    exclude_patterns = set()
    if exclude:
        for d in exclude.split(","):
            d = d.strip()
            if d:
                exclude_patterns.add(d)

    def _match_any(fname: str, patterns) -> bool:
        for p in patterns:
            if p == fname or fnmatch.fnmatch(fname, p):
                return True
        return False

    if os.path.isfile(resolved):
        if _is_binary(resolved):
            return f"❌ 跳过二进制文件: {os.path.basename(resolved)}"
        if include and not _match_any(os.path.basename(resolved), include.split(",")):
            return f"❌ 文件不匹配过滤条件: {include}"
        result = _count_file_lines(resolved)
        if result is None:
            return f"❌ 无法读取文件: {path}"
        return _format_metrics_table([result], result, sort_by)

    elif os.path.isdir(resolved):
        all_results = []
        totals = {"total_lines": 0, "code_lines": 0,
                  "comment_lines": 0, "blank_lines": 0,
                  "num_functions": 0, "num_classes": 0}
        for root, dirs, files in os.walk(resolved):
            dirs[:] = [d for d in dirs if not _match_any(d, exclude_patterns)
                       and d not in _METRICS_IGNORE_DIRS]
            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                if _is_binary(fpath):
                    continue
                if include and not _match_any(fname, include.split(",")):
                    continue
                if _match_any(fname, exclude_patterns):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in (".py", ".js", ".ts", ".rs", ".go", ".java",
                               ".c", ".cpp", ".h", ".hpp", ".cs", ".rb",
                               ".sh", ".bash", ".zsh", ".yaml", ".yml",
                               ".json", ".xml", ".html", ".css", ".scss",
                               ".md", ".rst", ".txt", ".cfg", ".ini",
                               ".conf", ".toml"):
                    continue
                result = _count_file_lines(fpath)
                if result:
                    all_results.append(result)
                    totals["total_lines"] += result["total_lines"]
                    totals["code_lines"] += result["code_lines"]
                    totals["comment_lines"] += result["comment_lines"]
                    totals["blank_lines"] += result["blank_lines"]
                    totals["num_functions"] += result.get("num_functions", 0)
                    totals["num_classes"] += result.get("num_classes", 0)
        if not all_results:
            return f"在目录中未找到可分析的代码文件: {path}"
        return _format_metrics_table(all_results, totals, sort_by)
    else:
        return f"❌ 路径不存在: {path}"


# ── Project Dependencies ────────────────────────────────────────────────────

_DEP_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    "env", "build", "dist", "target", ".cache", ".omo", ".hzb-agents",
})

# Python stdlib module names (Python 3.10+)
try:
    _STDLIB_MODULES = frozenset(sys.stdlib_module_names)
except AttributeError:
    _STDLIB_MODULES = frozenset()


def _extract_python_imports(source: str) -> List[Dict[str, str]]:
    """Extract imports from Python source using ast module."""
    imports = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "type": "import",
                    "module": alias.name,
                    "alias": alias.asname or "",
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                imports.append({
                    "type": "from_import",
                    "module": full,
                    "alias": alias.asname or "",
                    "line": node.lineno,
                })
    return imports


def _extract_js_imports(source: str) -> List[Dict[str, str]]:
    """Extract imports from JS/TS source using regex."""
    imports = []
    # ES6 static imports
    for m in re.finditer(
        r'(?:import\s+(?:(?:\{[^}]*\}|[^;{]+)\s+from\s+)?["\']([^"\']+)["\']|require\s*\(\s*["\']([^"\']+)["\']\s*\))',
        source
    ):
        module = m.group(1) or m.group(2)
        imports.append({"type": "import", "module": module, "alias": "", "line": 0})
    return imports


def _extract_go_imports(source: str) -> List[Dict[str, str]]:
    """Extract imports from Go source using regex."""
    imports = []
    # Single imports: import "fmt"
    for m in re.finditer(r'import\s+"([^"]+)"', source):
        imports.append({"type": "import", "module": m.group(1), "alias": "", "line": 0})
    # Grouped imports: import ( "fmt" "os" )
    in_group = False
    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("import ("):
            in_group = True
            continue
        if in_group and stripped.startswith(")"):
            in_group = False
            continue
        if in_group and (stripped.startswith('"') and stripped.endswith('"')):
            imports.append({"type": "import", "module": stripped.strip('"'), "alias": "", "line": 0})
    return imports


def _categorize_dependency(module_name: str, project_root: str) -> str:
    """Categorize a module as stdlib, third_party, or local."""
    top_level = module_name.split(".")[0]
    # Check stdlib
    if top_level in _STDLIB_MODULES:
        return "stdlib"
    # Check local (relative import or file exists in project)
    if module_name.startswith("."):
        return "local"
    local_path = os.path.join(project_root, top_level.replace(".", os.sep))
    if os.path.isdir(local_path) or os.path.isfile(local_path + ".py"):
        return "local"
    return "third_party"


def _find_python_files(root: str, recursive: bool = True) -> List[str]:
    """Find all Python files in a directory tree."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        if not recursive:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in _DEP_IGNORE_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                files.append(os.path.join(dirpath, f))
    return sorted(files)


def execute_find_dependencies(path: str, recursive: bool = True) -> str:
    """Analyze dependencies of a file or project directory.

    For Python files, uses ast module for precise import detection.
    For JS/TS/Go files, uses regex-based extraction.
    Categorizes dependencies as stdlib / third-party / local.
    Detects circular dependencies and potentially unused exports.

    Args:
        path: Absolute path to file or directory.
        recursive: Whether to scan subdirectories (default True).

    Returns:
        Formatted dependency report with imports, direction, layering,
        unused exports, and circular dependency analysis.
    """
    if not path or not os.path.isabs(path):
        return "❌ 错误：必须使用绝对路径！"

    resolved = os.path.realpath(path)
    project_root = resolved if os.path.isdir(resolved) else os.path.dirname(resolved)

    py_files = []
    js_files = []
    go_files = []

    if os.path.isfile(resolved):
        if resolved.endswith(".py"):
            py_files = [resolved]
        elif resolved.endswith((".js", ".ts", ".jsx", ".tsx")):
            js_files = [resolved]
        elif resolved.endswith(".go"):
            go_files = [resolved]
        else:
            return f"❌ 不支持的文件类型: {resolved}"
    elif os.path.isdir(resolved):
        for dirpath, dirnames, filenames in os.walk(resolved):
            if not recursive:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d not in _DEP_IGNORE_DIRS]
            for f in filenames:
                fpath = os.path.join(dirpath, f)
                if f.endswith(".py"):
                    py_files.append(fpath)
                elif f.endswith((".js", ".ts", ".jsx", ".tsx")):
                    js_files.append(fpath)
                elif f.endswith(".go"):
                    go_files.append(fpath)
    else:
        return f"❌ 路径不存在: {path}"

    if not (py_files or js_files or go_files):
        return "未找到可分析的代码文件（Python/JS/TS/Go）。"

    # Phase 1: extract imports + defined names from all files
    file_info = {}  # rel_path -> {"imports": [...], "defined": set(), "categories": {stdlib, 3rd, local}}

    for fpath in py_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception:
            continue
        rel = os.path.relpath(fpath, project_root)
        imports = _extract_python_imports(source)
        funcs, classes = _extract_defined_names(source)
        cats = {"stdlib": set(), "third_party": set(), "local": set()}
        for imp in imports:
            cat = _categorize_dependency(imp["module"], project_root)
            cats[cat].add(imp["module"])
        file_info[rel] = {"imports": imports, "defined": funcs | classes,
                          "categories": cats, "is_python": True}

    for fpath in js_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception:
            continue
        rel = os.path.relpath(fpath, project_root)
        imports = _extract_js_imports(source)
        cats = {"stdlib": set(), "third_party": set(), "local": set()}
        for imp in imports:
            mod = imp["module"]
            if mod.startswith(".") or mod.startswith("/"):
                cats["local"].add(mod)
            else:
                cats["third_party"].add(mod)
        file_info[rel] = {"imports": imports, "defined": set(),
                          "categories": cats, "is_python": False}

    for fpath in go_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception:
            continue
        rel = os.path.relpath(fpath, project_root)
        imports = _extract_go_imports(source)
        cats = {"stdlib": set(), "third_party": set(), "local": set()}
        for imp in imports:
            mod = imp["module"]
            parts = mod.split("/")
            if len(parts) >= 3 and "." in parts[0]:
                cats["third_party"].add(mod)
            else:
                cats["stdlib"].add(mod)
        file_info[rel] = {"imports": imports, "defined": set(),
                          "categories": cats, "is_python": False}

    if not file_info:
        return "未找到可分析的文件。"

    # Phase 2: build forward + reverse dependency graphs
    forward = {}  # rel -> {stdlib: [], 3rd: [], local: [rel2]}
    reverse = {}  # rel -> [rel_of_importer]
    for rel, info in file_info.items():
        forward[rel] = {
            "stdlib": sorted(info["categories"]["stdlib"]),
            "third_party": sorted(info["categories"]["third_party"]),
            "local": sorted(info["categories"]["local"]),
        }
    # Build reverse: for each file's local deps, add it to the reverse index of the dep
    for rel, info in file_info.items():
        local_deps = info["categories"]["local"]
        for dep_module in local_deps:
            top_mod = dep_module.split(".")[0]
            dep_rel = top_mod + ".py"
            if dep_rel not in reverse:
                reverse[dep_rel] = []
            if rel not in reverse[dep_rel]:
                reverse[dep_rel].append(rel)

    # Phase 3: module layering
    # Bottom: files that import 0-1 local modules
    # Middle: files that both import and are imported
    # Top: files that are imported by no one but import local modules
    bottom = []
    middle = []
    top = []
    for rel, info in file_info.items():
        local_imports = len(info["categories"]["local"])
        imported_by = len(reverse.get(rel, []))
        if local_imports <= 1 and imported_by == 0:
            bottom.append(rel)
        elif imported_by == 0 and local_imports > 0:
            top.append(rel)
        else:
            middle.append(rel)

    # Phase 4: unused exports detection (Python only)
    unused_exports = {}  # rel -> [name, ...]
    for rel, info in file_info.items():
        if not info.get("is_python"):
            continue
        defined = info.get("defined", set())
        if not defined:
            continue
        # Collect all names imported FROM this module
        imported_names = set()
        for other_rel, other_info in file_info.items():
            if other_rel == rel:
                continue
            for imp in other_info.get("imports", []):
                if imp.get("type") == "from_import":
                    full = imp["module"]
                    # module is "clipboard_store.ClipboardStore" -> module="clipboard_store", name="ClipboardStore"
                    parts = full.split(".")
                    if len(parts) >= 2:
                        mod_part = parts[0]
                        name_part = ".".join(parts[1:])
                        # Check if the module part matches this file's module name
                        # (e.g., clipboard_store.ClipboardStore → module=clipboard_store)
                        mod_file = mod_part + ".py"
                        if mod_file == rel:
                            imported_names.add(name_part)
                    elif full == rel.replace(".py", ""):
                        # import module directly, all defs are accessible
                        pass
        # Check for unused
        unused = set()
        for name in defined:
            if name.startswith("_"):
                continue  # private by convention
            if name not in imported_names:
                unused.add(name)
        if unused:
            unused_exports[rel] = sorted(unused)

    # Phase 5: circular dependency
    all_modules = {}
    for rel, info in file_info.items():
        all_modules[rel] = list(info["categories"]["local"])
    circles = _find_circular_deps(all_modules)

    # ── Format Output ──────────────────────────────────────────

    total_stdlib = sum(len(f["categories"]["stdlib"]) for f in file_info.values())
    total_third = sum(len(f["categories"]["third_party"]) for f in file_info.values())
    total_local = sum(len(f["categories"]["local"]) for f in file_info.values())
    total_imports = total_stdlib + total_third + total_local

    lines = [
        f"🔗 项目依赖分析: {os.path.basename(project_root)}",
        "═" * 50,
        "",
        f"📊 概览",
        f"   · 总文件数:      {len(file_info)}",
        f"   · 总导入语句:     {total_imports}",
        f"   · 标准库:        {total_stdlib}",
        f"   · 第三方包:      {total_third}",
        f"   · 本地模块依赖:   {total_local}",
        f"   · 循环依赖:      {'⚠️ 发现 ' + str(len(circles)) + ' 个' if circles else '✅ 无'}",
        "",
    ]

    # Module layering
    lines.append("📦 模块分层")
    if bottom:
        lines.append(f"   底层 (不依赖其他本地模块):")
        lines.append(f"     {', '.join(sorted(bottom))}")
    if middle:
        lines.append(f"   中间层 (互相依赖):")
        # Show top 8 middle files
        mid_show = sorted(middle)[:8]
        lines.append(f"     {', '.join(mid_show)}")
        if len(middle) > 8:
            lines.append(f"     ... 及其他 {len(middle) - 8} 个")
    if top:
        lines.append(f"   顶层 (不被其他模块依赖):")
        lines.append(f"     {', '.join(sorted(top))}")
    lines.append("")

    # Per-file detail (show ALL files, no truncation)
    # Sort by total imports descending
    sorted_rels = sorted(file_info.items(),
                         key=lambda x: len(x[1]["categories"]["stdlib"]) + len(x[1]["categories"]["third_party"]) + len(x[1]["categories"]["local"]),
                         reverse=True)

    lines.append("─" * 55)
    lines.append(f"📄 各文件依赖详情（按依赖数降序，共 {len(sorted_rels)} 个文件）")
    lines.append("")

    for idx, (rel, info) in enumerate(sorted_rels, 1):
        cats = info["categories"]
        imp_count = len(cats["stdlib"]) + len(cats["third_party"]) + len(cats["local"])
        imported_by = reverse.get(rel, [])
        lines.append(f"【{idx}】{rel} ({imp_count} 导入)")
        lines.append(f"  📥 导入:")
        # Show non-empty categories
        if cats["stdlib"]:
            stdlib_list = ", ".join(sorted(cats["stdlib"])[:12])
            extra = f" +{len(cats['stdlib']) - 12}" if len(cats['stdlib']) > 12 else ""
            lines.append(f"    stdlib: {stdlib_list}{extra}")
        if cats["third_party"]:
            third_list = ", ".join(sorted(cats["third_party"])[:8])
            extra = f" +{len(cats['third_party']) - 8}" if len(cats['third_party']) > 8 else ""
            lines.append(f"    3rd:   {third_list}{extra}")
        if cats["local"]:
            local_files_shown = set()
            for local_mod in sorted(cats["local"]):
                local_mod_clean = local_mod.split(".")[0]
                local_rel = local_mod_clean + ".py"
                display = local_mod_clean + (".py" if local_rel in file_info else "")
                if display not in local_files_shown:
                    local_files_shown.add(display)
                    lines.append(f"    → {display}")
        if imported_by:
            lines.append(f"  📤 被引用: {', '.join(sorted(imported_by)[:6])}"
                         + (f" +{len(imported_by)-6}" if len(imported_by) > 6 else ""))
        else:
            lines.append(f"  📤 被引用: (无)")
        lines.append("")

    # Circular deps
    lines.append("─" * 55)
    lines.append("⚠️  循环依赖检测")
    if circles:
        for a, b in circles:
            lines.append(f"   {a} ↔ {b}")
    else:
        lines.append("   ✅ 无")

    # Unused exports
    lines.append("")
    lines.append("─" * 55)
    lines.append("🕳️  未使用的导出（定义了但未被其他文件引用）")
    if unused_exports:
        total_unused = sum(len(v) for v in unused_exports.values())
        lines.append(f"   检测到 {total_unused} 个（可能包含误报：动态注册、`__main__` 入口、或仅内部使用的符号）")
        for rel, names in sorted(unused_exports.items()):
            for name in sorted(names):
                lines.append(f"   · {rel}: {name}()")
    else:
        lines.append("   ✅ 无 — 所有定义的函数/类均被引用")

    return "\n".join(lines)


def _extract_defined_names(source: str) -> tuple:
    """Extract top-level function and class names from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), set()
    funcs = set()
    classes = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
    return funcs, classes


def _find_circular_deps(modules: Dict[str, List[str]]) -> List[tuple]:
    """Simple pairwise circular dependency detection."""
    circles = []
    names = list(modules.keys())
    for i, name_a in enumerate(names):
        for name_b in names[i + 1:]:
            if name_a in modules.get(name_b, []) and name_b in modules.get(name_a, []):
                circles.append((name_a, name_b))
    return circles


# ── Parse File AST ──────────────────────────────────────────────────────────

_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".cpp": "cpp", ".c": "cpp", ".h": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".kt": "kotlin", ".swift": "swift",
}


def _parse_python_ast(path: str, include_body: bool = False,
                      include_imports: bool = True,
                      include_docstrings: bool = False,
                      exclude_private: bool = False) -> str:
    """Parse Python file using stdlib ast module."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except Exception as e:
        return f"❌ 无法读取文件: {e}"

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"❌ Python 语法错误: {e}"

    source_lines = source.split("\n")
    total_lines = len(source_lines)
    code_lines = sum(1 for l in source_lines if l.strip())
    blank_lines = total_lines - code_lines
    filename = os.path.basename(path)

    imports = []
    classes = []
    functions = []
    constants = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = []
            for alias in node.names:
                n = alias.name + (f" as {alias.asname}" if alias.asname else "")
                names.append(n)
            imports.append(f"from {module} import {', '.join(names)}")
        elif isinstance(node, ast.ClassDef):
            if exclude_private and node.name.startswith("_"):
                continue
            bases = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))
                except Exception:
                    bases.append("...")
            cls_info = {
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "bases": bases,
                "decorators": [],
                "methods": [],
            }
            for dec in node.decorator_list:
                try:
                    cls_info["decorators"].append(ast.unparse(dec))
                except Exception:
                    cls_info["decorators"].append("...")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if exclude_private and child.name.startswith("_"):
                        continue
                    args = []
                    for arg in child.args.args:
                        arg_str = arg.arg
                        if arg.annotation:
                            try:
                                arg_str += f": {ast.unparse(arg.annotation)}"
                            except Exception:
                                arg_str += ": ?"
                        args.append(arg_str)
                    ret = ""
                    if child.returns:
                        try:
                            ret = f" -> {ast.unparse(child.returns)}"
                        except Exception:
                            ret = " -> ?"
                    m_info = {
                        "name": child.name,
                        "line": child.lineno,
                        "end_line": getattr(child, "end_lineno", child.lineno),
                        "args": args,
                        "returns": ret,
                        "is_async": isinstance(child, ast.AsyncFunctionDef),
                        "decorators": [],
                        "docstring": ast.get_docstring(child) or "" if (include_docstrings or include_body) else "",
                    }
                    if include_body:
                        m_info["body"] = ast.get_source_segment(source, child) or ""
                    for dec in child.decorator_list:
                        try:
                            m_info["decorators"].append(ast.unparse(dec))
                        except Exception:
                            m_info["decorators"].append("...")
                    cls_info["methods"].append(m_info)
            classes.append(cls_info)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if exclude_private and node.name.startswith("_"):
                continue
            args = []
            for arg in node.args.args:
                arg_str = arg.arg
                if arg.annotation:
                    try:
                        arg_str += f": {ast.unparse(arg.annotation)}"
                    except Exception:
                        arg_str += ": ?"
                args.append(arg_str)
            ret = ""
            if node.returns:
                try:
                    ret = f" -> {ast.unparse(node.returns)}"
                except Exception:
                    ret = " -> ?"
            fn_info = {
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "args": args,
                "returns": ret,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "decorators": [],
                "docstring": ast.get_docstring(node) or "" if (include_docstrings or include_body) else "",
            }
            for dec in node.decorator_list:
                try:
                    fn_info["decorators"].append(ast.unparse(dec))
                except Exception:
                    fn_info["decorators"].append("...")
            if include_body:
                fn_info["body"] = ast.get_source_segment(source, node) or ""
            functions.append(fn_info)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                try:
                    name = ast.unparse(target)
                    if name.isupper() and name.isidentifier():
                        constants.append(name)
                except Exception:
                    pass

    # Meta summary line (P0)
    func_count = len([f for f in functions if not (exclude_private and f["name"].startswith("_"))])
    cls_count = len([c for c in classes if not (exclude_private and c["name"].startswith("_"))])
    out = [
        f"📦 {filename}  [Python | {total_lines} 行 | {func_count} 函数 | {cls_count} 类 | {len(imports)} 导入]",
        "",
    ]

    if include_imports and imports:
        out.append(f"📥 导入 ({len(imports)}):")
        for imp in imports:
            out.append(f"  · {imp}")
        out.append("")

    if classes:
        out.append(f"📚 类 ({len(classes)}):")
        for cls in classes:
            base_str = f"({', '.join(cls['bases'])})" if cls['bases'] else ""
            dec_str = f"@{' @'.join(cls['decorators'])} " if cls['decorators'] else ""
            line_range = f"L{cls['line']}-{cls['end_line']}" if cls['end_line'] != cls['line'] else f"L{cls['line']}"
            out.append(f"  📦 {dec_str}{cls['name']}{base_str}  ({line_range})")
            for m in cls["methods"]:
                if exclude_private and m["name"].startswith("_"):
                    continue
                async_str = "async " if m["is_async"] else ""
                dec_str_m = f"@{' @'.join(m['decorators'])} " if m['decorators'] else ""
                args_str = ", ".join(m["args"])
                m_line_range = f"L{m['line']}-{m['end_line']}" if m['end_line'] != m['line'] else f"L{m['line']}"
                sig = f"{dec_str_m}{async_str}def {m['name']}({args_str}){m['returns']}  ({m_line_range})"
                if include_docstrings and m["docstring"]:
                    doc_first = m["docstring"].split("\n")[0][:60]
                    sig += f"  # {doc_first}"
                out.append(f"    └ {sig}")
                if include_body and m.get("body"):
                    for body_line in m["body"].split("\n"):
                        out.append(f"       {body_line}")
        out.append("")

    if functions:
        out.append(f"📋 函数 ({len(functions)}):")
        for fn in functions:
            if exclude_private and fn["name"].startswith("_"):
                continue
            async_str = "async " if fn["is_async"] else ""
            dec_str = f"@{' @'.join(fn['decorators'])} " if fn['decorators'] else ""
            args_str = ", ".join(fn["args"])
            line_range = f"L{fn['line']}-{fn['end_line']}" if fn['end_line'] != fn['line'] else f"L{fn['line']}"
            sig = f"{dec_str}{async_str}def {fn['name']}({args_str}){fn['returns']}  ({line_range})"
            if include_docstrings and fn["docstring"]:
                doc_first = fn["docstring"].split("\n")[0][:60]
                sig += f"  # {doc_first}"
            out.append(f"  · {sig}")
            if include_body and fn.get("body"):
                for body_line in fn["body"].split("\n"):
                    out.append(f"     {body_line}")
        out.append("")

    if constants:
        out.append(f"📌 全局常量 ({len(constants)}):")
        for c in constants:
            out.append(f"  · {c}")
        out.append("")

    return "\n".join(out)


def _parse_tree_sitter(path: str, language: str, include_body: bool) -> str:
    """Parse non-Python file using Tree-sitter."""
    try:
        import tree_sitter
    except ImportError:
        msg = f"❌ 当前环境未安装 Tree-sitter，仅支持 Python 语言分析。\n\n如需分析"
        if language == "unknown":
            msg += "非 Python 文件，请安装 Tree-sitter：\n  pip install tree-sitter"
        else:
            msg += f" .{language} 文件，请安装：\n  pip install tree-sitter tree-sitter-{language}"
        msg += "\n\n解析当前文件时请使用 language=python（仅对 Python 项目）。"
        return msg
    return f"❌ Tree-sitter 解析尚未实现（language={language}）"


def execute_parse_file_ast(path: str, language: str = "auto",
                           include_body: bool = False,
                           include_imports: bool = True,
                           include_docstrings: bool = False,
                           exclude_private: bool = False) -> str:
    """Parse a code file and extract its structure: classes, functions,
    imports, constants, and other structural elements.

    Uses Python's stdlib ast module for Python files (zero extra dependencies).
    For other languages, requires tree-sitter to be installed.

    Args:
        path: Absolute path to the file to parse.
        language: Language hint ("python", "javascript", "go", etc. or "auto").
        include_body: If True, include function/method body source.
        include_imports: If True, list import statements.
        include_docstrings: If True, show docstring summary after signatures.
        exclude_private: If True, filter out _-prefixed private functions.

    Returns:
        Formatted structural overview of the file.
    """
    if not path or not os.path.isabs(path):
        return "❌ 错误：必须使用绝对路径！"
    if not os.path.isfile(path):
        return f"❌ 文件不存在: {path}"

    if language == "auto":
        ext = os.path.splitext(path)[1].lower()
        language = _EXT_TO_LANG.get(ext, "unknown")

    if language == "python":
        return _parse_python_ast(path, include_body, include_imports,
                                 include_docstrings, exclude_private)
    else:
        return _parse_tree_sitter(path, language, include_body)


# ── Tool Schemas (OpenAI function calling) ─────────────────────────────────

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_code_metrics",
            "description": "分析文件或目录的代码度量指标：总行数、代码行数、注释行数、空行数、"
                           "函数/类数量。支持任何文本文件。对于 Python 文件额外提供函数和类计数。"
                           "可用 include 过滤文件类型（如 *.py），exclude 排除文件或目录，"
                           "sort_by 控制排序方式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件或目录的绝对路径。如果是目录，汇总其中所有文件的度量。"
                    },
                    "include": {
                        "type": "string",
                        "description": "文件通配过滤（逗号分隔多值），例如「*.py」只统计 Python 文件。默认包含常见代码文件类型。",
                        "default": ""
                    },
                    "exclude": {
                        "type": "string",
                        "description": "排除的文件或目录（逗号分隔多值，支持通配），例如「*test*,node_modules」。默认已排除 .git、__pycache__、venv 等。",
                        "default": ""
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "目录模式的排序方式：total（按总行数降序）、code（按代码行降序）、name（按文件名升序）。默认 total。",
                        "default": "total"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_project_dependencies",
            "description": "分析文件或项目的依赖关系。Python 文件使用 ast 模块精确提取 import 语句，"
                           "JS/TS/Go 使用正则提取。将依赖分类为标准库、第三方包和本地模块，"
                           "并检测循环依赖和孤立模块。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件或目录的绝对路径。如果是目录，分析项目中所有文件间的依赖关系。"
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归扫描子目录（默认 true）",
                        "default": True
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "parse_file_ast",
            "description": "解析代码文件的语法结构，提取类、函数、导入语句、全局变量等结构化信息。"
                           "Python 项目使用 language=python（基于标准库 ast，推荐）。"
                           "JS/TS/Go/Rust/Java/C++ 等项目请传入对应语言名称，将使用 Tree-sitter 解析"
                           "（需安装 tree-sitter 及对应 grammar）。"
                           "不指定 language 时自动从文件后缀检测。"
                           "可通过 exclude_private 过滤私有函数，include_docstrings 显示文档摘要，"
                           "include_imports 控制是否列出导入语句。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件绝对路径"
                    },
                    "language": {
                        "type": "string",
                        "description": "语言类型：python / javascript / typescript / go / rust / java / cpp / auto（自动检测）",
                        "default": "auto"
                    },
                    "include_body": {
                        "type": "boolean",
                        "description": "是否包含函数/方法体源码（仅 Python 有效，默认 false）",
                        "default": False
                    },
                    "include_imports": {
                        "type": "boolean",
                        "description": "是否列出导入语句（默认 true）",
                        "default": True
                    },
                    "include_docstrings": {
                        "type": "boolean",
                        "description": "是否在函数/类旁显示文档字符串摘要（仅 Python，默认 false）",
                        "default": False
                    },
                    "exclude_private": {
                        "type": "boolean",
                        "description": "是否过滤以 _ 开头的私有函数/方法（默认 false）",
                        "default": False
                    }
                },
                "required": ["path"]
            }
        }
    },
]
