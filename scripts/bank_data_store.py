# -*- coding: utf-8 -*-
"""
银行投资分析 — 本地 SQLite 数据存档

中证官网估值接口只回吐最近约20个交易日, 无法直接算历史分位,
因此每日运行时把官方 PE/股息率 落库累积, 供长期跟踪趋势。
核心的板块PB历史分位不依赖此库（由成分股个股估值史合成）。

可独立运行打印 DB 状态:
    python3 bank_data_store.py [--stats]
"""

import os
import sqlite3

WORKSPACE = os.path.expanduser(os.environ.get("BANK_WORKSPACE", "~/.bank-skill/workspace"))
DB_PATH = os.path.join(WORKSPACE, "bank_history.db")


def _connect():
    os.makedirs(WORKSPACE, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS csindex_value (
        date TEXT PRIMARY KEY,
        pe_static REAL, pe_ttm REAL,
        div_yield_1 REAL, div_yield_2 REAL)""")
    return conn


def upsert_csindex(rows):
    """rows: [(date, pe1, pe2, y1, y2), ...] — 幂等写入"""
    conn = _connect()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO csindex_value VALUES (?,?,?,?,?)", rows)
    conn.close()


def load_csindex():
    conn = _connect()
    rows = conn.execute("SELECT * FROM csindex_value ORDER BY date").fetchall()
    conn.close()
    return rows


def stats():
    conn = _connect()
    n = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM csindex_value").fetchone()
    conn.close()
    return {"csindex_rows": n[0], "first": n[1], "last": n[2], "db_path": DB_PATH}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="银行投资分析数据存档状态")
    ap.parse_args()
    s = stats()
    print(f"DB: {s['db_path']}")
    print(f"csindex_value: {s['csindex_rows']} 行 ({s['first']} ~ {s['last']})")
