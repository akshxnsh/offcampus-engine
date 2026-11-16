"""SQLite storage: one `leads` table (company + contact + draft), settings, LLM usage log."""
import json
import sqlite3
import threading
import time
from pathlib import Path

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)

_conn = sqlite3.connect(DATA / "engine.db", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_lock = threading.Lock()

_conn.executescript("""
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE,            -- domain, or lowercased name when domain unknown
    name TEXT, domain TEXT, website TEXT, description TEXT, location TEXT, team_size INTEGER,
    source TEXT,                -- yc | hn | funding | jobs | social
    signal TEXT,                -- why now: "Raised $5M seed", "Hiring post on HN", ...
    signal_url TEXT,
    context TEXT,               -- raw text given to the LLM for personalisation
    fit_score INTEGER, fit_reason TEXT, role TEXT,
    contacts TEXT DEFAULT '[]', -- JSON list of {name,title,email,conf,src}
    to_name TEXT, to_title TEXT, to_email TEXT, to_conf TEXT,
    subject TEXT, body TEXT,
    status TEXT DEFAULT 'new',  -- new | scored | lowfit | nocontact | ready | drafted | pushed | sent | skipped
    created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS usage (ts REAL, model TEXT, tokens INTEGER);
""")


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.rowcount


def get_setting(key, default=None):
    rows = q("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(rows[0]["value"]) if rows else default


def set_setting(key, value):
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, json.dumps(value)))


def add_lead(lead):
    """Insert a discovered lead; returns True if it was new."""
    key = (lead.get("domain") or lead["name"]).strip().lower()
    now = time.time()
    cols = ["name", "domain", "website", "description", "location", "team_size",
            "source", "signal", "signal_url", "context"]
    vals = [lead.get(c) for c in cols]
    return x(
        f"INSERT OR IGNORE INTO leads(key,{','.join(cols)},contacts,created_at,updated_at) "
        f"VALUES(?,{','.join('?' * len(cols))},?,?,?)",
        [key, *vals, json.dumps(lead.get("contacts", [])), now, now],
    ) == 1


def update_lead(lead_id, **fields):
    if "contacts" in fields and not isinstance(fields["contacts"], str):
        fields["contacts"] = json.dumps(fields["contacts"])
    fields["updated_at"] = time.time()
    sets = ",".join(f"{k}=?" for k in fields)
    x(f"UPDATE leads SET {sets} WHERE id=?", [*fields.values(), lead_id])


def leads(where="1=1", args=()):
    rows = q(f"SELECT * FROM leads WHERE {where} ORDER BY COALESCE(fit_score,-1) DESC, id DESC", args)
    for r in rows:
        r["contacts"] = json.loads(r["contacts"] or "[]")
    return rows
