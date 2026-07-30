#!/usr/bin/env python3
"""列出 OpenCode 数据库中所有会话的原始字段"""

import sqlite3
import os

DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")

# ponytail: add __name__ guard to prevent top-level execution on import
if __name__ == "__main__":
    if not os.path.isfile(DB_PATH):
        print(f"数据库不存在: {DB_PATH}")
        exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 查看 session 表有哪些列
    cols = conn.execute("PRAGMA table_info(session)").fetchall()
    print("=== session 表结构 ===")
    for c in cols:
        print(f"  {c['name']:20s}  {c['type']:10s}  nullable={not c['notnull']}")

    # 查询前 5 条未归档会话
    rows = conn.execute("""
        SELECT * FROM session
        WHERE time_archived IS NULL
        ORDER BY time_updated DESC
        LIMIT 5
    """).fetchall()

    print(f"\n=== 最近 {len(rows)} 条会话 ===")
    for r in rows:
        print(f"\n{'─' * 50}")
        for key in r.keys():
            val = r[key]
            if val is None:
                val = "<NULL>"
            elif isinstance(val, str) and len(val) > 60:
                val = val[:60] + "..."
            print(f"  {key:20s} = {val}")

    # 查看 part 表结构
    print(f"\n\n=== part 表结构 ===")
    pcols = conn.execute("PRAGMA table_info(part)").fetchall()
    for c in pcols:
        print(f"  {c['name']:20s}  {c['type']:10s}  nullable={not c['notnull']}")

    conn.close()
