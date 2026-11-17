"""Single-server persistent store. No account or permission subsystem."""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('REVIEW_DATA_DIR', ROOT / 'data')).resolve()
for sub in ['', 'files', 'chunks', 'previews', 'reports']:
    (DATA / sub).mkdir(parents=True, exist_ok=True)

def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')

def uid():
    return uuid.uuid4().hex

def dumps(value):
    return json.dumps(value, ensure_ascii=False)

@contextmanager
def db():
    con = sqlite3.connect(DATA / 'review.sqlite3', timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def all_rows(sql, args=()):
    with db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]

def one(sql, args=()):
    with db() as c:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None

def execute(sql, args=()):
    with db() as c:
        c.execute(sql, args)

def event(project_id, actor, action, detail):
    execute('INSERT INTO events VALUES(?,?,?,?,?,?)',
            (uid(), project_id, actor, action, dumps(detail), now()))

def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS projects(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT, mode TEXT,
          baseline_date TEXT, facts TEXT, actor TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS files(
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
          name TEXT, role TEXT, version TEXT, path TEXT, hash TEXT, size INTEGER,
          status TEXT, page_count INTEGER DEFAULT 0, done INTEGER DEFAULT 0,
          error TEXT DEFAULT '', created TEXT);
        CREATE TABLE IF NOT EXISTS pages(
          file_id TEXT REFERENCES files(id), number INTEGER, label TEXT,
          text TEXT, quality TEXT, note TEXT, PRIMARY KEY(file_id,number));
        CREATE TABLE IF NOT EXISTS uploads(
          id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id), name TEXT,
          role TEXT, version TEXT, size INTEGER, offset INTEGER, created TEXT,
          file_id TEXT);
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id), main_file TEXT,
          file_ids TEXT, baseline_id TEXT, actor TEXT, facts TEXT, state TEXT,
          stage TEXT, done INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
          error TEXT DEFAULT '', snapshot TEXT, result TEXT, created TEXT, updated TEXT,
          reviewer TEXT DEFAULT '', conclusion TEXT DEFAULT '', review_note TEXT DEFAULT '',
          confirmed TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS issues(
          id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), code TEXT, rule_id TEXT,
          title TEXT, nature TEXT, risk TEXT, description TEXT, evidence TEXT,
          citations TEXT, recommendation TEXT, decision TEXT DEFAULT 'pending',
          review_note TEXT DEFAULT '', owner TEXT DEFAULT '', deadline TEXT DEFAULT '',
          actor TEXT DEFAULT '', updated TEXT, fingerprint TEXT,
          UNIQUE(task_id,fingerprint));
        CREATE TABLE IF NOT EXISTS rechecks(
          id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), issue_id TEXT REFERENCES issues(id),
          suggestion TEXT, reason TEXT, evidence TEXT, confirmed TEXT DEFAULT '',
          actor TEXT DEFAULT '', note TEXT DEFAULT '', updated TEXT,
          UNIQUE(task_id,issue_id));
        CREATE TABLE IF NOT EXISTS standards(
          id TEXT PRIMARY KEY, name TEXT, code TEXT, version TEXT, category TEXT,
          source TEXT, effective TEXT, expires TEXT, conditions TEXT, content TEXT,
          state TEXT, actor TEXT, created TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id), actor TEXT,
          action TEXT, detail TEXT, created TEXT);
        ''')
        c.execute("UPDATE files SET status='failed',error='服务重启中断了解析，请重试' WHERE status IN ('queued','parsing')")
        c.execute("UPDATE tasks SET state='failed',error='服务重启中断了审核，可重试；已完成批次已保留' WHERE state IN ('queued','running')")
