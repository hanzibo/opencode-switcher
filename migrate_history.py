#!/usr/bin/env python3
import os
import json
import re

CONFIG_DIR = os.path.expanduser("~/.config/opencode-switcher")
CLIPBOARD_PATH = os.path.join(CONFIG_DIR, "clipboard_history.json")

# Heuristic Code Classification Regexes
HTML_START_RE = re.compile(r'^\s*<(html|head|body|div|span|p|a|ul|ol|li|table|tr|td|script|style|link|meta|xml)\b', re.IGNORECASE)
HTML_END_RE = re.compile(r'</[a-zA-Z][a-zA-Z0-9]*>')
SHEBANG_RE = re.compile(r'^#!\s*/(bin|usr)/')
CLI_CMD_RE = re.compile(r'^\s*(sudo\s+)?(apt-get|yum|docker|systemctl|pip install|npm install|git clone|yarn add|pnpm add|chmod\s+\+x)\b')
CURLY_NEWLINE_RE = re.compile(r'[\{\}]\s*[\r\n]|[\r\n]\s*[\{\}]')

PY_DEF_RE = re.compile(r'^\s*def\s+[a-zA-Z_]\w*\s*\(.*\)\s*:', re.MULTILINE)
PY_CLASS_RE = re.compile(r'^\s*class\s+[a-zA-Z_]\w*(\s*\(.*\))?\s*:', re.MULTILINE)
PY_IMPORT_RE = re.compile(r'^\s*(import\s+\w+|from\s+\w+\s+import)\b', re.MULTILINE)

CPP_INCLUDE_RE = re.compile(r'^\s*#include\s*[<" ]', re.MULTILINE)
CPP_DEFINE_RE = re.compile(r'^\s*#define\s+[a-zA-Z_]\w*', re.MULTILINE)
CPP_USING_RE = re.compile(r'\busing namespace std\b')

TYPED_DECL_RE = re.compile(r'\b(public|private|protected)\s+(class|interface|void|int|double|float|char|bool|boolean|string)\b')

JS_CONSOLE_RE = re.compile(r'\bconsole\.log\(')
JS_VAR_RE = re.compile(r'\b(const|let|var)\s+[a-zA-Z_]\w*\s*=')
JS_FUNC_RE = re.compile(r'\b(export|export default)\b')
JS_FUNCTION_KW_RE = re.compile(r'\bfunction\b')
LANG_KEYWORDS_RE = re.compile(r'\b(fn|pub|struct|impl|package)\b')

C_COMMENT_RE = re.compile(r'^\s*(\/\/|\/\*).*$', re.MULTILINE)

SQL_SELECT_RE = re.compile(r'\bSELECT\s+[\w\s,*()\-]+FROM\b', re.IGNORECASE)
SQL_MOD_RE = re.compile(r'\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b', re.IGNORECASE)

GENERIC_KW_RE = re.compile(r'\b(interface|void|printf|println|nullptr|undefined|typeof|elseif|elif|sizeof|struct|enum|static|class)\b')

SEMICOLON_RE = re.compile(r'(?<!&\w)(?<!&#\d);\s*$', re.MULTILINE)
CURLY_BRACE_RE = re.compile(r'[\{\}]')

def classify_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "text"
        
    if stripped.startswith("http"):
        return "link"
        
    # 1. JSON Check
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, (dict, list)) and len(stripped) > 4:
            return "code"
    except Exception:
        pass

    # 2. HTML/XML Check
    if HTML_START_RE.match(stripped):
        return "code"
    if HTML_END_RE.search(stripped):
        return "code"

    # 3. Shebang / CLI Check
    if SHEBANG_RE.match(stripped):
        return "code"
    if CLI_CMD_RE.match(stripped):
        return "code"

    # 3.5 Curly Braces with Newline Check (typical of block-structured code)
    if CURLY_NEWLINE_RE.search(text):
        return "code"

    # 4. Code Heuristics Scorer
    score = 0

    # Python
    if PY_DEF_RE.search(stripped):
        score += 5
    if PY_CLASS_RE.search(stripped):
        score += 4
    if "if __name__ ==" in stripped:
        score += 5
    if PY_IMPORT_RE.search(stripped):
        score += 3

    # C/C++
    if CPP_INCLUDE_RE.search(stripped):
        score += 5
    if CPP_DEFINE_RE.search(stripped):
        score += 3
    if "std::cout" in stripped or "std::endl" in stripped:
        score += 4
    if CPP_USING_RE.search(stripped):
        score += 5

    # Java/C#/C++
    if TYPED_DECL_RE.search(stripped):
        score += 5
    if "System.out.print" in stripped:
        score += 4

    # JS/TS/Go/Rust
    if JS_CONSOLE_RE.search(stripped):
        score += 4
    if JS_VAR_RE.search(stripped):
        score += 3
    if JS_FUNC_RE.search(stripped):
        score += 2
    if JS_FUNCTION_KW_RE.search(stripped):
        score += 3
    if LANG_KEYWORDS_RE.search(stripped):
        score += 2

    # C-style comments
    if C_COMMENT_RE.search(stripped):
        score += 3

    # SQL
    if SQL_SELECT_RE.search(stripped):
        score += 5
    if SQL_MOD_RE.search(stripped):
        score += 5

    # Generic specific keywords
    if GENERIC_KW_RE.search(stripped):
        score += 2

    # Line ending semicolons
    semicolons = SEMICOLON_RE.findall(stripped)
    score += min(3, len(semicolons))

    # Curly braces match
    left_braces = stripped.count('{')
    right_braces = stripped.count('}')
    if left_braces > 0 and left_braces == right_braces:
        score += min(3, left_braces)

    if score >= 4:
        return "code"
    return "text"

def run_migration():
    if not os.path.isfile(CLIPBOARD_PATH):
        return
        
    try:
        with open(CLIPBOARD_PATH, "r") as f:
            items = json.load(f)
    except Exception as e:
        print(f"Error loading history during migration: {e}")
        return
        
    if not isinstance(items, list):
        return
        
    updated = False
    for item in items:
        if not isinstance(item, dict):
            continue
        # If type is missing, or is "text" (which needs classification check)
        current_type = item.get("type", "text")
        text = item.get("text", "")
        # Preserve "image" type
        if current_type == "image":
            continue
        if text == "[Image]":
            item["type"] = "image"
            updated = True
            continue
            
        new_type = classify_text(text)
        if new_type != current_type:
            item["type"] = new_type
            updated = True
                
    if updated:
        try:
            with open(CLIPBOARD_PATH, "w") as f:
                json.dump(items, f, indent=2)
            print("Successfully migrated clipboard history item types.")
        except Exception as e:
            print(f"Error writing migrated history: {e}")

if __name__ == "__main__":
    run_migration()
