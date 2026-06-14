import sqlite3
import json
import os
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
    try:
        out = subprocess.check_output(["pgrep", "-f", "opencode"], stderr=subprocess.DEVNULL)
        pids = out.decode("utf-8", errors="ignore").strip().split()
    except subprocess.CalledProcessError as e:
        if e.returncode == 1:
            pids = []
        else:
            try:
                pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
            except Exception:
                pids = []
    except Exception:
        try:
            pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
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
            SELECT session_id, data
            FROM part
            WHERE session_id IN ({placeholders})
            ORDER BY session_id, time_created DESC
            """,
            session_ids,
        )

        snippet_map: Dict[str, str] = {}
        for part_row in part_cur.fetchall():
            sid = part_row["session_id"]
            if sid in snippet_map:
                continue
            text = _extract_snippet_text(part_row["data"])
            if text:
                snippet_map[sid] = text

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


def delete_session(session_id: str) -> Optional[str]:
    if not os.path.isfile(DB_PATH):
        return "Database not found"
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            now = int(time.time() * 1000)
            conn.execute("UPDATE session SET time_archived=? WHERE id=?", (now, session_id))
            conn.commit()
            return None
        finally:
            conn.close()
    except Exception as e:
        return str(e)


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
