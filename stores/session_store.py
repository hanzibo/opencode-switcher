import sqlite3
import json
import os
import shutil
import time
import subprocess
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict


@dataclass
class Session:
    id: str
    title: str
    directory: str
    project_name: str
    status: str
    snippet: str
    started_at: int
    updated_at: int


DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")
SNIPPET_MAX_LEN = 120


def _detect_live_sessions() -> Tuple[set, set]:
    live_dirs: set = set()
    live_session_ids: set = set()
    # ponytail: removed manual /proc scanning fallback, rely on system standard pgrep
    try:
        out = subprocess.check_output(["pgrep", "-f", "opencode"], stderr=subprocess.DEVNULL)
        pids = out.decode("utf-8", errors="ignore").strip().split()
    except Exception:
        pids = []

    for pid in pids:
        try:
            cmdline_path = f"/proc/{pid}/cmdline"
            if not os.path.isfile(cmdline_path):
                continue
            with open(cmdline_path, "rb") as f:
                raw = f.read(4096)
            if b"opencode" not in raw:
                continue
            if b"python3" in raw and b"opencode-switcher" in raw:
                continue
            cwd = os.readlink(f"/proc/{pid}/cwd")
            if cwd:
                live_dirs.add(cwd)
            parts = raw.split(b"\0")
            for i, part in enumerate(parts):
                if part == b"--session" and i + 1 < len(parts):
                    sid = parts[i + 1].decode("utf-8", errors="replace").strip()
                    if sid:
                        live_session_ids.add(sid)
        except (OSError, IOError):
            continue
    return live_dirs, live_session_ids


def _extract_snippet_text(data_json: str) -> Optional[str]:
    try:
        d = json.loads(data_json)
    except (json.JSONDecodeError, TypeError):
        return None
    t = d.get("type")
    if t == "text":
        text = d.get("text", "")
    elif t == "reasoning":
        text = d.get("text", "")
    elif t == "tool":
        inp = d.get("state", {}).get("input", "")
        out = d.get("state", {}).get("output", "")
        if isinstance(out, str) and out.strip():
            text = out
        elif isinstance(inp, str):
            text = inp
        elif isinstance(inp, dict):
            text = inp.get("command", inp.get("pattern", json.dumps(inp)))
        else:
            text = str(inp)
    else:
        return None
    if isinstance(text, str) and text.strip():
        text = " ".join(text.split())[:SNIPPET_MAX_LEN]
        if len(text) >= SNIPPET_MAX_LEN:
            text = text[:SNIPPET_MAX_LEN] + "..."
        return text
    return None


def get_sessions(limit: int = 100) -> List[Session]:
    if not os.path.isfile(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            SELECT id, title, directory, time_created, time_updated
            FROM session
            WHERE time_archived IS NULL
              AND title NOT LIKE '%(@%subagent)%'
            ORDER BY time_updated DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        if not rows:
            return []

        session_ids = [r["id"] for r in rows]

        placeholders = ",".join("?" * len(session_ids))
        part_cur = conn.execute(
            f"""
            SELECT p1.session_id, p1.data
            FROM part p1
            INNER JOIN (
                SELECT session_id, MAX(time_created) as max_tc
                FROM part
                WHERE session_id IN ({placeholders})
                GROUP BY session_id
            ) p2 ON p1.session_id = p2.session_id AND p1.time_created = p2.max_tc
            GROUP BY p1.session_id
            """,
            session_ids,
        )

        snippet_map: Dict[str, str] = {}
        for part_row in part_cur.fetchall():
            text = _extract_snippet_text(part_row["data"])
            if text:
                snippet_map[part_row["session_id"]] = text

        now = time.time() * 1000
        live_dirs, live_session_ids = _detect_live_sessions()
        results = []
        for r in rows:
            if r["directory"] and not os.path.isdir(r["directory"]):
                continue
            sid = r["id"]
            project_name = r["directory"].split("/")[-1] if r["directory"] else ""
            updated = r["time_updated"] or 0
            created = r["time_created"] or 0
            delta = now - updated
            is_recent = delta < 86400_000
            id_match = sid in live_session_ids
            dir_match = r["directory"] in live_dirs if r["directory"] else False
            is_live = id_match or (dir_match and is_recent)
            status = "live" if is_live else ("recent" if is_recent else "closed")
            results.append(Session(
                id=sid,
                title=r["title"] or "Untitled",
                directory=r["directory"] or "",
                project_name=project_name,
                status=status,
                snippet=snippet_map.get(sid, ""),
                started_at=created,
                updated_at=updated,
            ))

        return results
    finally:
        conn.close()


def _session_exists(session_id: str) -> bool:
    """交叉验证：CLI 声称成功后确认行是否真的消失。"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM session WHERE id=?", (session_id,)
            ).fetchone()[0]
            return n > 0
        finally:
            conn.close()
    except Exception:
        return True  # 查不到视为存在，走兜底（保守）


def _soft_delete(session_id: str) -> Optional[str]:
    """软删除兜底：置 time_archived（原 delete_session 逻辑，抽为单点）。

    返回 None = 软删成功；返回 str = 错误。
    """
    if not os.path.isfile(DB_PATH):
        return "Database not found"
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            now = int(time.time() * 1000)
            cur = conn.execute(
                "UPDATE session SET time_archived=? WHERE id=?", (now, session_id)
            )
            conn.commit()
            if cur.rowcount == 0:
                return "session 不存在或已被删除（UPDATE 影响 0 行）"
            return None
        finally:
            conn.close()
    except Exception as e:
        return str(e)


def delete_session(session_id: str) -> Optional[str]:
    """删除 session：硬删除优先（opencode 官方命令实际删库），失败回退软删除兜底。

    返回 None = 已从数据库彻底删除；
    返回 str = 警告（已软删除隐藏，数据可能残留）或错误（完全未删）。
    """
    if not os.path.isfile(DB_PATH):
        return "Database not found"
    opencode = shutil.which("opencode")
    if opencode:
        try:
            proc = subprocess.run(
                [opencode, "session", "delete", session_id],
                capture_output=True, text=True, errors="replace", timeout=30,
            )
            if proc.returncode == 0:
                if not _session_exists(session_id):
                    return None  # 交叉验证通过：彻底删除
                hard_err = "CLI 返回成功但 session 仍存在"
            else:
                hard_err = (proc.stderr or proc.stdout or "").strip()[:200]
        except (subprocess.TimeoutExpired, OSError) as e:
            hard_err = str(e)
    else:
        hard_err = "opencode 不在 PATH"
    soft_err = _soft_delete(session_id)
    if soft_err:
        return f"硬删除失败（{hard_err}），且软删除也失败：{soft_err}"
    return f"警告：opencode 删除失败（{hard_err}），已软删除隐藏（数据可能残留）"


def rename_session(session_id: str, new_title: str) -> Optional[str]:
    if not os.path.isfile(DB_PATH):
        return "Database not found"
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            now = int(time.time() * 1000)
            conn.execute(
                "UPDATE session SET title=?, time_updated=? WHERE id=?",
                (new_title, now, session_id),
            )
            conn.commit()
            return None
        finally:
            conn.close()
    except Exception as e:
        return str(e)
