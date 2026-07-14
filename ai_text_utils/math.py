"""LaTeX math expression processing utilities.

Pure functions for escaping/unescaping math expressions from Markdown
rendering interference, and fixing common LaTeX formatting errors
produced by LLMs.

Zero GTK dependency. Standalone module (no dependency on other
ai_text_utils submodules).
"""

import re
import html
from typing import Tuple, List


# LaTeX commands that LLMs commonly double-escape (\\frac -> \frac, etc.)
_LATEX_COMMANDS = frozenset({
    "frac", "sqrt", "sum", "int", "prod", "lim", "sin", "cos", "log", "ln",
    "det", "begin", "end", "left", "right", "text", "mathrm", "mathbf",
    "mathit", "mathtt", "mathcal", "mathbb", "mathfrak", "displaystyle",
    "partial", "nabla", "infty", "alpha", "beta", "gamma", "delta", "epsilon",
    "theta", "lambda", "pi", "sigma", "omega", "varphi", "rightarrow", "leftarrow",
    "Rightarrow", "Leftarrow", "mapsto", "implies", "iff", "cdot", "times",
    "approx", "equiv", "neq", "leq", "geq", "subset", "supset", "cup", "cap",
})


def _escape_math(text: str) -> Tuple[str, List[str]]:
    placeholders = []

    # 1. Protect block math: $$ ... $$ (multiline, not escaped)
    def replace_block(match):
        placeholder = f"<!--MATH_BLOCK_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\$\$(.*?)(?<!\\)\$\$", replace_block, text, flags=re.DOTALL)

    # 2. Protect block math: \[ ... \] (multiline, not escaped)
    def replace_bracket(match):
        placeholder = f"<!--MATH_BLOCK_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\\\[(.*?)(?<!\\)\\\]", replace_bracket, text, flags=re.DOTALL)

    # 3. Protect inline math: \( ... \) (must be before \begin{env} to avoid placeholder nesting)
    def replace_paren(match):
        placeholder = f"<!--MATH_INLINE_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\\\((.*?)(?<!\\)\\\)", replace_paren, text)

    # 4. Protect inline math: $ ... $ (single line, not escaped, no space inside delimiters)
    def replace_inline(match):
        placeholder = f"<!--MATH_INLINE_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)(?<!\\)\$", replace_inline, text)

    # 5. Protect LaTeX environments: \begin{env} ... \end{env} (multiline, not escaped)
    def replace_env(match):
        placeholder = f"<!--MATH_BLOCK_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\\begin\{([a-zA-Z*0-9]+)\}(.*?)\\end\{\1\}", replace_env, text, flags=re.DOTALL)

    return text, placeholders


def _unescape_math(html_text: str, placeholders: List[str]) -> str:
    for i, original in enumerate(placeholders):
        restored = html.escape(original)
        if original.strip().startswith("\\begin"):
            restored = f"$${restored}$$"

        html_text = html_text.replace(f"<!--MATH_BLOCK_{i}-->", restored)
        html_text = html_text.replace(f"<!--MATH_INLINE_{i}-->", restored)

        escaped_original = html.escape(original)
        html_text = html_text.replace(f"&lt;!--MATH_BLOCK_{i}--&gt;", escaped_original)
        html_text = html_text.replace(f"&lt;!--MATH_INLINE_{i}--&gt;", escaped_original)

    return html_text


def _fix_latex(content: str) -> str:
    """Fix common LaTeX formatting errors produced by LLMs.

    Applied to captured math expressions BEFORE unescape so that
    restored content is already corrected. Covers:
      - double backslash before known commands (\\frac -> \frac)
      - missing \\end{env} for unclosed \\begin{env}
      - unclosed $$ (display math)
      - unclosed $ (inline math, per-line odd count heuristic)
    """
    known = sorted(_LATEX_COMMANDS, key=len, reverse=True)
    cmd_pattern = r'\\\\(?:' + '|'.join(known) + r')\b'
    content = re.sub(cmd_pattern, lambda m: m.group(0)[1:], content)

    envs = re.findall(r'(\\(?:begin|end))\{([a-zA-Z*0-9]+)\}', content)
    open_count = {}
    for cmd, name in envs:
        if cmd == '\\begin':
            open_count[name] = open_count.get(name, 0) + 1
        elif cmd == '\\end':
            open_count[name] = open_count.get(name, 0) - 1
    for name, count in open_count.items():
        if count > 0:
            content += f"\\end{{{name}}}" * count

    dollars = re.findall(r'(?<!\\)\$\$', content)
    if len(dollars) % 2 != 0:
        content += "$$"

    lines = content.split('\n')
    any_unclosed_inline = any(
        len(re.findall(r'(?<!\\)\$(?!\$)', line)) % 2 != 0
        for line in lines
    )
    if any_unclosed_inline:
        content += "$"

    return content
