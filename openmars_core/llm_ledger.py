# =============================================
# OpenMars | LLM 调用记账
# 每次 LLM 调用写一行进 logs/cost.db，支持按日聚合 token 消耗
# （借鉴 91Writing BillingService 与 gpt-author print_step_costs 的记账思路）
# usage 缺失时按启发式估算：中文≈1.5 token/字，其余字符≈0.25 token/字符
# =============================================
import os
import re
import sqlite3
from datetime import datetime

from .logger import logger

# 记账库路径：与日志同目录（logs/cost.db）
LEDGER_DIR = "logs"
LEDGER_DB_PATH = os.path.join(LEDGER_DIR, "cost.db")

# 中日韩字符按 1.5 token/字 估算
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# llm_calls 表结构：step/model/prompt_tokens/completion_tokens/latency_ms/success/chapter
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    step TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 1,
    chapter INTEGER
)
"""


def estimate_tokens(text) -> int:
    """usage 缺失时的启发式估算：中文按1.5 token/字，其余字符按0.25 token/字符"""
    if not text:
        return 0
    text = str(text)
    cjk_chars = len(_CJK_RE.findall(text))
    other_chars = len(text) - cjk_chars
    return max(1, int(cjk_chars * 1.5 + other_chars * 0.25))


def _connect():
    """统一连接入口：WAL + busy_timeout=15000，面板读与后台线程写不互斥（与记忆宫殿同一套约定）"""
    os.makedirs(LEDGER_DIR, exist_ok=True)
    conn = sqlite3.connect(LEDGER_DB_PATH, timeout=15.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def record_llm_call(step, model, prompt_tokens=0, completion_tokens=0, latency_ms=0, success=True, chapter=None):
    """每次LLM调用写一行记账；写入失败向上抛出，由调用方决定是否吞掉（记账绝不阻塞生成）"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        conn.execute(_SCHEMA_SQL)
        conn.execute("""
            INSERT INTO llm_calls
            (ts, step, model, prompt_tokens, completion_tokens, latency_ms, success, chapter)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ts, str(step), str(model), int(prompt_tokens or 0), int(completion_tokens or 0),
              int(latency_ms or 0), 1 if success else 0, chapter))
        conn.commit()


def summarize_day(day=None):
    """聚合某日（默认当天）的调用次数与 token 合计，day 格式 YYYY-MM-DD"""
    day = day or datetime.now().strftime("%Y-%m-%d")
    with _connect() as conn:
        conn.execute(_SCHEMA_SQL)
        row = conn.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(prompt_tokens), 0),
                   COALESCE(SUM(completion_tokens), 0)
            FROM llm_calls
            WHERE substr(ts, 1, 10) = ?
        """, (day,)).fetchone()
    return {
        "day": day,
        "calls": row[0],
        "success_calls": row[1],
        "prompt_tokens": row[2],
        "completion_tokens": row[3],
        "total_tokens": row[2] + row[3],
    }


def summarize_chapter(chapter):
    """按章号聚合调用次数与 token 合计（每章运行报告用，读 llm_calls 表）。

    注意：同一章号的历史调用也会计入，调用方用「生成前快照-生成后快照」差集
    即可得到本次运行的真实消耗（见 cyber_printer_ultimate 的运行报告组装）。
    """
    try:
        chapter = int(chapter)
    except (TypeError, ValueError):
        chapter = -1
    with _connect() as conn:
        conn.execute(_SCHEMA_SQL)
        row = conn.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(prompt_tokens), 0),
                   COALESCE(SUM(completion_tokens), 0)
            FROM llm_calls
            WHERE chapter = ?
        """, (chapter,)).fetchone()
    return {
        "chapter": chapter,
        "calls": row[0],
        "success_calls": row[1],
        "prompt_tokens": row[2],
        "completion_tokens": row[3],
        "total_tokens": row[2] + row[3],
    }


def main():
    """命令行聚合入口：python -m openmars_core.llm_ledger 查看当日token合计"""
    info = summarize_day()
    logger.info(
        f"📊 LLM记账日报 {info['day']} | 调用{info['calls']}次（成功{info['success_calls']}次） | "
        f"prompt {info['prompt_tokens']} + completion {info['completion_tokens']} = 当日合计 {info['total_tokens']} tokens"
    )
    return info


if __name__ == "__main__":
    main()
