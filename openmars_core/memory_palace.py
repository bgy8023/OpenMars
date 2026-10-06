import os
import sqlite3
import json
import time
from .logger import logger
from .prompts import ENDING_EXCERPT_CHARS, render_global_summary_prompts

# =============================================
# Schema 版本：v2 在 v1（仅 chapter_memory）基础上新增
#   - chapter_memory 扩展列：outline/review/ending_excerpt（大纲/排雷/结尾摘录随章节一并落库）
#   - story_state：全局键值状态（滚动全局摘要 global_summary、摘要覆盖进度等）
#   - foreshadow_ledger：伏笔台账（埋设/回收生命周期；due_chapter 只做排序键不做过滤条件，
#     借鉴 goink「只排不滤」，防 LLM 估算丢数据——超期条目保留在库并标红，绝不静默删除）
#   - character_state：角色状态表（写后回写自动维护，下一章装配时自动带回）
# =============================================
SCHEMA_VERSION = 2

# source 留痕：auto=写后抽取自动写入，user_edit=面板人工编辑（任务「编辑留痕 source=user_edit」）
SOURCE_AUTO = "auto"
SOURCE_USER_EDIT = "user_edit"

# 台账生命周期状态（MZX粒度：埋设→快到期→超期→回收）
STATUS_OPEN = "未回收"
STATUS_RESOLVED = "已回收"

# —— story_state 固定键名 ——
GLOBAL_SUMMARY_KEY = "global_summary"      # 滚动全局摘要
SUMMARY_UPTO_KEY = "summary_upto_chapter"  # 摘要已覆盖到的章号（增量压缩游标）

# —— 滚动全局摘要参数 ——
SUMMARY_EVERY_N_CHAPTERS = 5     # 每5章滚动压缩一次（伏笔台账变动时由调用方额外触发）
SUMMARY_NEW_CHAPTERS_MAX = 12    # 单次压缩最多并入的待摘要章节数
GLOBAL_SUMMARY_BUDGET = 2000     # 全局摘要硬上限（字）

# =============================================
# 生成上下文装配预算（统一注入口+显式预算账本：借鉴 SillyTavern 统一注入口、
# 51mazi 六步装配总预算 12000 字+超限显式告知）
# =============================================
RECENT_CHAPTER_LIMIT = 3         # 最近章节回看数（v1「只回看3章」保留为骨架一节）
CHAPTER_SUMMARY_BUDGET = 200     # 单章摘要预算（字）
FORESHADOW_TOP_N = 8             # 未兑现伏笔注入条数上限（台账只排不滤，注入取TopN）
FORESHADOW_ITEM_BUDGET = 120     # 单条伏笔预算（字）
CHARACTER_TOP_N = 8              # 活跃角色注入条数上限
CHARACTER_ITEM_BUDGET = 100      # 单个角色状态预算（字）
RECENT_SUMMARY_BUDGET = 700      # 最近章节摘要节预算（3章×200字+装配开销）
FORESHADOW_SECTION_BUDGET = 1200 # 未兑现伏笔节预算
CHARACTER_SECTION_BUDGET = 1000  # 活跃角色状态节预算
CONTEXT_TOTAL_BUDGET = 12000     # 生成上下文总预算（硬上限，超限显式截断标注）
CONTEXT_TRUNCATED_MARK = "（已截断，以章纲与上文为准）"
NO_HISTORY_PLACEHOLDER = "无前置剧情"

# update_foreshadow 的「未传该字段」哨兵：显式传 None 表示清空 due_chapter
_UNSET = object()


class SQLiteMemoryPalace:
    def __init__(self, novel_name="默认小说"):
        self.novel_name = novel_name
        self.base_dir = os.path.join("novel_settings", novel_name)
        os.makedirs(self.base_dir, exist_ok=True)
        self.db_path = os.path.join(self.base_dir, "memory.db")
        self.old_json_path = os.path.join(self.base_dir, "03-动态剧情记忆.json")

        self._clear_stale_lock()
        self._init_db()
        self._migrate_old_json_data()
        self._init_fixed_memory()

    def _connect(self):
        # 统一SQLite连接入口：WAL模式 + busy_timeout=15000
        # WAL允许「面板读 + 后台线程写」并发进行，互不阻塞（避免 database is locked）
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _clear_stale_lock(self):
        lock_files = [f"{self.db_path}-lock", f"{self.db_path}-journal"]
        for lock_file in lock_files:
            if os.path.exists(lock_file):
                file_age = time.time() - os.path.getmtime(lock_file)
                if file_age > 600:
                    try:
                        os.unlink(lock_file)
                        logger.warning(f"🚨 清理超时SQLite锁文件：{lock_file}")
                    except Exception as e:
                        logger.error(f"清理锁文件失败：{e}")

    def _init_db(self):
        with self._connect() as conn:
            # chapter_memory v2 全量建表：新库直接带扩展列，老库由下方 ALTER 补列
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chapter_memory (
                    chapter_num INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL,
                    word_count INTEGER NOT NULL,
                    full_content TEXT,
                    outline TEXT,
                    review TEXT,
                    ending_excerpt TEXT,
                    generate_time DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chapter_desc
                ON chapter_memory (chapter_num DESC)
            """)
            # v1→v2 迁移：老库补 chapter_memory 扩展列（大纲/排雷/结尾摘录），历史行数与摘要不动
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(chapter_memory)").fetchall()}
            for col in ("outline", "review", "ending_excerpt"):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE chapter_memory ADD COLUMN {col} TEXT")
            # 全局键值状态表（滚动摘要等）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS story_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_chapter INTEGER,
                    source TEXT NOT NULL DEFAULT 'auto'
                )
            """)
            # 伏笔台账：due_chapter 只做排序键不做过滤条件（goink只排不滤），故只建普通排序索引
            conn.execute("""
                CREATE TABLE IF NOT EXISTS foreshadow_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    planted_chapter INTEGER,
                    due_chapter INTEGER,
                    status TEXT NOT NULL DEFAULT '未回收',
                    source TEXT NOT NULL DEFAULT 'auto'
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_foreshadow_due ON foreshadow_ledger (due_chapter)"
            )
            # 角色状态表（state_json 预留结构化扩展位，当前存 {"state": 一句话状态}）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS character_state (
                    name TEXT PRIMARY KEY,
                    state_json TEXT,
                    updated_chapter INTEGER,
                    source TEXT NOT NULL DEFAULT 'auto'
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_character_recent ON character_state (updated_chapter DESC)"
            )
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        logger.info(f"✅ SQLite记忆宫殿初始化完成（Schema v{SCHEMA_VERSION}）：{self.db_path}")

    def _migrate_old_json_data(self):
        if not os.path.exists(self.old_json_path):
            return
        try:
            with open(self.old_json_path, "r", encoding="utf-8") as f:
                old_data = json.load(f)
            chapters = old_data.get("chapters", [])
            if not chapters:
                return

            with self._connect() as conn:
                for ch in chapters:
                    conn.execute("""
                        INSERT OR IGNORE INTO chapter_memory
                        (chapter_num, summary, word_count) VALUES (?, ?, ?)
                    """, (ch.get("num"), ch.get("summary"), ch.get("words", 0)))
                conn.commit()

            os.rename(self.old_json_path, f"{self.old_json_path}.bak.migrated")
            logger.info(f"✅ 旧JSON数据迁移完成，共迁移{len(chapters)}章历史数据")
        except Exception as e:
            logger.error(f"旧JSON数据迁移失败：{e}")

    def _init_fixed_memory(self):
        self.fixed_memory = {}
        for key, fname, display_name in [
            ("outline", "00-全本大纲.md", "全本大纲"),
            ("character", "01-人物档案.md", "人物档案"),
            ("worldview", "02-世界观设定.md", "世界观设定"),
            ("tomato_audit", "03-番茄审核铁则.md", "番茄审核铁则")
        ]:
            fpath = os.path.join(self.base_dir, fname)
            if os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8") as f:
                    self.fixed_memory[key] = {
                        "content": f.read(),
                        "display_name": display_name
                    }

    def get_fixed_prompt(self):
        return "\n".join([f"## {v['display_name']}\n{v['content']}" for k, v in self.fixed_memory.items() if v['content']])

    # =============================================
    # 生成上下文装配（统一注入口）：替代旧 get_dynamic_prompt 的「只回看3章」
    # =============================================
    def get_next_chapter_num(self):
        """下一章号 = 已有最大章号+1（空库为1），供面板与装配视点使用"""
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT MAX(chapter_num) FROM chapter_memory").fetchone()
            return (row[0] or 0) + 1
        except Exception as e:
            logger.error(f"获取下一章号失败：{e}")
            return 1

    def _get_recent_summaries(self, viewpoint, limit=RECENT_CHAPTER_LIMIT):
        """取视点之前最近N章摘要（重生成第N章时，不会把第N章旧记忆带回上下文）"""
        try:
            with self._connect() as conn:
                rows = conn.execute("""
                    SELECT chapter_num, summary FROM chapter_memory
                    WHERE chapter_num < ? ORDER BY chapter_num DESC LIMIT ?
                """, (viewpoint, limit)).fetchall()
            return list(reversed(rows))
        except Exception as e:
            logger.error(f"获取最近章节摘要失败：{e}")
            return []

    def _get_ending_excerpt(self, chapter_num):
        """取指定章结尾摘录：优先读 ending_excerpt 列，缺失回退 full_content 尾部，超长截到最后800字"""
        if not chapter_num or chapter_num < 1:
            return ""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT ending_excerpt, full_content FROM chapter_memory WHERE chapter_num = ?",
                    (chapter_num,)).fetchone()
        except Exception as e:
            logger.error(f"获取第{chapter_num}章结尾摘录失败：{e}")
            return ""
        if not row:
            return ""
        text = (row[0] or "").strip() or (row[1] or "").strip()
        return text[-ENDING_EXCERPT_CHARS:] if text else ""

    @staticmethod
    def _clamp_text(text, budget):
        """按预算硬截断，发生截断时在尾部追加统一标注（超限显式告知）。返回 (文本, 是否截断)"""
        text = (text or "").strip()
        if len(text) <= budget:
            return text, False
        if budget <= len(CONTEXT_TRUNCATED_MARK):
            return text[:max(budget, 0)], True
        return text[:budget - len(CONTEXT_TRUNCATED_MARK)].rstrip() + CONTEXT_TRUNCATED_MARK, True

    def build_generation_context(self, chapter_num=None):
        """装配生成上下文骨架（统一注入口，骨架永远在场、不押注检索命中）。

        固定注入五节（顺序固定、每节显式预算）：
          ①全局摘要(≤2000) ②最近3章摘要(各≤200) ③上一章结尾(≤800)
          ④未兑现伏笔TopN(条≤120，按预计回收时间排序) ⑤活跃角色状态(条≤100)
        总上限 CONTEXT_TOTAL_BUDGET=12000 字；超限节截断并标注；空库优雅降级为「无前置剧情」。
        chapter_num 为待写章号；传 None 时自动取「最大章号+1」作装配视点。
        """
        try:
            viewpoint = chapter_num if chapter_num and chapter_num > 0 else self.get_next_chapter_num()
            sections = []

            # ① 全局摘要
            global_summary = (self.get_state(GLOBAL_SUMMARY_KEY, "") or "").strip()
            if global_summary:
                sections.append(("全局摘要", global_summary, GLOBAL_SUMMARY_BUDGET))

            # ② 最近章节摘要（只回看视点前3章，各≤200字）
            recent = self._get_recent_summaries(viewpoint)
            if recent:
                lines = []
                for num, summary in recent:
                    clamped, _ = self._clamp_text(summary, CHAPTER_SUMMARY_BUDGET)
                    lines.append(f"第{num}章: {clamped or '（摘要缺失）'}")
                title = f"最近章节摘要（第{recent[0][0]}-{recent[-1][0]}章）"
                sections.append((title, "\n".join(lines), RECENT_SUMMARY_BUDGET))

            # ③ 上一章结尾（原文尾部≤800字，首章/无数据时整节省略）
            ending = self._get_ending_excerpt(viewpoint - 1)
            if ending:
                sections.append((f"上一章结尾（第{viewpoint - 1}章原文结尾）", ending, ENDING_EXCERPT_CHARS))

            # ④ 未兑现伏笔（只排不滤：库里全量保留，注入按预计回收时间取TopN）
            foreshadows = self.get_open_foreshadows(limit=FORESHADOW_TOP_N)
            if foreshadows:
                lines = []
                for f in foreshadows:
                    due_txt = f"，预计第{f['due_chapter']}章回收" if f["due_chapter"] else ""
                    clamped, _ = self._clamp_text(f["content"], FORESHADOW_ITEM_BUDGET)
                    lines.append(f"- {clamped}（埋设第{f['planted_chapter'] or '?'}章{due_txt}）")
                sections.append(("未兑现伏笔（按预计回收时间排序）", "\n".join(lines), FORESHADOW_SECTION_BUDGET))

            # ⑤ 活跃角色状态（按最近更新章号倒序取TopN）
            characters = self.get_characters(limit=CHARACTER_TOP_N)
            if characters:
                lines = []
                for c in characters:
                    clamped, _ = self._clamp_text(c["state"], CHARACTER_ITEM_BUDGET)
                    lines.append(f"- {c['name']}：{clamped}")
                sections.append(("活跃角色状态", "\n".join(lines), CHARACTER_SECTION_BUDGET))

            if not sections:
                return NO_HISTORY_PLACEHOLDER

            # 显式预算账本：逐节装配，节头不侵占正文预算；节预算与总预算双重约束
            parts = []
            used = 0
            for title, body, budget in sections:
                header = f"【{title}】\n"
                remaining = CONTEXT_TOTAL_BUDGET - used
                room = min(budget, remaining - len(header))
                if room <= len(CONTEXT_TRUNCATED_MARK) + 20:
                    logger.warning(
                        f"⚠️  生成上下文预算不足，「{title}」节未注入（已装配{used}/{CONTEXT_TOTAL_BUDGET}字）")
                    break
                clamped, truncated = self._clamp_text(body, room)
                parts.append(header + clamped)
                used += len(header) + len(clamped)
                if truncated:
                    logger.info(f"🧩 生成上下文「{title}」节超限已截断（预算{room}字）")
            logger.info(f"🧩 生成上下文装配完成：第{viewpoint}章视点 | {len(parts)}节 {used}/{CONTEXT_TOTAL_BUDGET}字")
            return "\n\n".join(parts)
        except Exception as e:
            # 装配层兜底：任何异常都降级为「无前置剧情」，绝不阻断章节生成
            logger.error(f"生成上下文装配失败，降级为无前置剧情：{e}")
            return NO_HISTORY_PLACEHOLDER

    # =============================================
    # 章节记忆写入
    # =============================================
    def safe_update(self, chapter_num, summary, word_count, full_content=None,
                    outline=None, review=None, ending_excerpt=None):
        """写入/更新章节记忆行；outline/review/ending_excerpt 为 v2 扩展列，
        传 None 时保留旧值（COALESCE），避免兼容调用方误清空。"""
        try:
            with self._connect() as conn:
                conn.execute("""
                    INSERT INTO chapter_memory (chapter_num, summary, word_count, full_content,
                                                outline, review, ending_excerpt)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chapter_num) DO UPDATE SET
                    summary=excluded.summary,
                    word_count=excluded.word_count,
                    full_content=excluded.full_content,
                    outline=COALESCE(excluded.outline, chapter_memory.outline),
                    review=COALESCE(excluded.review, chapter_memory.review),
                    ending_excerpt=COALESCE(excluded.ending_excerpt, chapter_memory.ending_excerpt),
                    generate_time=CURRENT_TIMESTAMP
                """, (chapter_num, summary, word_count, full_content, outline, review, ending_excerpt))
                conn.commit()
            logger.info(f"✅ 章节{chapter_num}记忆已极速写入SQLite")
            return True
        except Exception as e:
            logger.error(f"SQLite写入失败：{e}")
            raise e

    def get_chapter_history(self, limit=100):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT chapter_num, summary, word_count, generate_time
                    FROM chapter_memory
                    ORDER BY chapter_num DESC
                    LIMIT ?
                """, (limit,))
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"获取章节历史失败：{e}")
            return []

    # =============================================
    # 全局键值状态（story_state）
    # =============================================
    def get_state(self, key, default=None):
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT value FROM story_state WHERE key = ?", (key,)).fetchone()
            return row[0] if row and row[0] is not None else default
        except Exception as e:
            logger.error(f"读取状态「{key}」失败：{e}")
            return default

    def set_state(self, key, value, updated_chapter=None, source=SOURCE_AUTO, _conn=None):
        """写入全局状态键；_conn 传入外部连接时并入该事务（供多键原子写入复用），否则独立提交"""
        def _tx(c):
            c.execute("""
                INSERT INTO story_state (key, value, updated_chapter, source) VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_chapter=excluded.updated_chapter,
                source=excluded.source
            """, (key, value, updated_chapter, source))
        try:
            if _conn is not None:
                _tx(_conn)
            else:
                with self._connect() as conn:
                    _tx(conn)
                    conn.commit()
            return True
        except Exception as e:
            logger.error(f"写入状态「{key}」失败：{e}")
            raise e

    # =============================================
    # 伏笔台账（foreshadow_ledger）：只排不滤——只追加与排序，绝不按期次过滤丢弃
    # =============================================
    def add_foreshadow(self, content, planted_chapter, due_chapter=None, source=SOURCE_AUTO):
        """埋设伏笔入账。内容与未回收条目完全相同的不重复入账。返回是否新增"""
        content = (content or "").strip()
        if not content:
            return False
        with self._connect() as conn:
            changed = self._add_foreshadow_tx(conn, content, planted_chapter, due_chapter, source)
            conn.commit()
        return changed

    def _add_foreshadow_tx(self, conn, content, planted_chapter, due_chapter, source):
        dup = conn.execute(
            "SELECT 1 FROM foreshadow_ledger WHERE content = ? AND status != ?",
            (content, STATUS_RESOLVED)).fetchone()
        if dup:
            return False
        conn.execute("""
            INSERT INTO foreshadow_ledger (content, planted_chapter, due_chapter, status, source)
            VALUES (?, ?, ?, ?, ?)
        """, (content, planted_chapter, due_chapter, STATUS_OPEN, source))
        return True

    def resolve_foreshadow(self, content_hint, resolved_chapter=None):
        """回收伏笔：按内容互相包含匹配未回收条目（最早到期优先），命中置为已回收。返回是否命中"""
        hint = (content_hint or "").strip()
        if not hint:
            return False
        with self._connect() as conn:
            changed = self._resolve_foreshadow_tx(conn, hint)
            conn.commit()
        if changed:
            logger.info(f"✅ 伏笔「{hint[:24]}」已回收（第{resolved_chapter or '?'}章）")
        return changed

    def _resolve_foreshadow_tx(self, conn, hint):
        # LIKE 模糊匹配做宽容对账：hint ⊆ 台账内容 或 台账内容 ⊆ hint
        row = conn.execute("""
            SELECT id FROM foreshadow_ledger
            WHERE status != ? AND (content LIKE ? OR ? LIKE ('%' || content || '%'))
            ORDER BY (due_chapter IS NULL), due_chapter, id
            LIMIT 1
        """, (STATUS_RESOLVED, f"%{hint}%", hint)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE foreshadow_ledger SET status = ? WHERE id = ?",
                     (STATUS_RESOLVED, row[0]))
        return True

    def get_open_foreshadows(self, limit=None):
        """未回收伏笔：due_chapter 只排不滤（不做期次过滤），按预计回收时间升序、无期次靠后"""
        sql = """
            SELECT id, content, planted_chapter, due_chapter, status, source
            FROM foreshadow_ledger
            WHERE status != ?
            ORDER BY (due_chapter IS NULL), due_chapter, id
        """
        params = [STATUS_RESOLVED]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            logger.error(f"获取未回收伏笔失败：{e}")
            return []
        keys = ("id", "content", "planted_chapter", "due_chapter", "status", "source")
        return [dict(zip(keys, r)) for r in rows]

    def get_all_foreshadows(self):
        """全量台账（面板展示用）：已回收/超期条目一律保留（只排不滤）"""
        try:
            with self._connect() as conn:
                rows = conn.execute("""
                    SELECT id, content, planted_chapter, due_chapter, status, source
                    FROM foreshadow_ledger
                    ORDER BY (due_chapter IS NULL), due_chapter, id
                """).fetchall()
        except Exception as e:
            logger.error(f"获取伏笔台账失败：{e}")
            return []
        keys = ("id", "content", "planted_chapter", "due_chapter", "status", "source")
        return [dict(zip(keys, r)) for r in rows]

    def update_foreshadow(self, foreshadow_id, content=None, due_chapter=_UNSET,
                          status=None, source=SOURCE_USER_EDIT):
        """人工编辑台账条目（面板用）：仅更新显式传入的字段，并按 source 留痕。返回是否更新"""
        sets, params = [], []
        if content is not None:
            content = content.strip()
            if not content:
                return False
            sets.append("content = ?")
            params.append(content)
        if due_chapter is not _UNSET:
            sets.append("due_chapter = ?")
            params.append(due_chapter)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if not sets:
            return False
        sets.append("source = ?")
        params.append(source)
        params.append(foreshadow_id)
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"UPDATE foreshadow_ledger SET {', '.join(sets)} WHERE id = ?", params)
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"更新伏笔条目{foreshadow_id}失败：{e}")
            raise e

    # =============================================
    # 角色状态（character_state）
    # =============================================
    def upsert_character(self, name, state, updated_chapter, source=SOURCE_AUTO, _conn=None):
        """新增/更新角色状态；name 与 state 任一为空则忽略。返回是否写入"""
        name = (name or "").strip()
        state = (state or "").strip()
        if not name or not state:
            return False
        state_json = json.dumps({"state": state}, ensure_ascii=False)

        def _tx(c):
            c.execute("""
                INSERT INTO character_state (name, state_json, updated_chapter, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                state_json=excluded.state_json,
                updated_chapter=excluded.updated_chapter,
                source=excluded.source
            """, (name, state_json, updated_chapter, source))
        try:
            if _conn is not None:
                _tx(_conn)
            else:
                with self._connect() as conn:
                    _tx(conn)
                    conn.commit()
            return True
        except Exception as e:
            logger.error(f"更新角色「{name}」状态失败：{e}")
            raise e

    def get_characters(self, limit=None):
        """角色状态列表（按最近更新章号倒序=最近活跃优先）；state_json 解析失败回退原文"""
        sql = """
            SELECT name, state_json, updated_chapter, source
            FROM character_state
            ORDER BY updated_chapter DESC, name
        """
        params = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            logger.error(f"获取角色状态失败：{e}")
            return []
        result = []
        for name, state_json, updated_chapter, source in rows:
            state = ""
            try:
                state = (json.loads(state_json or "{}") or {}).get("state", "")
            except (TypeError, ValueError):
                state = state_json or ""
            result.append({
                "name": name,
                "state": state or (state_json or ""),
                "updated_chapter": updated_chapter,
                "source": source,
            })
        return result

    # =============================================
    # 写后回写与滚动摘要（NovelClaw/MuMuAINovel闭环：生成→结构化抽取→状态回写→下一章自动带回）
    # =============================================
    def apply_chapter_extraction(self, chapter_num, extraction):
        """把结构化抽取结果一次性写入伏笔台账/角色状态/章节摘要。

        单事务提交：任一步失败整体回滚，记忆库停留上一致状态（绝不半更新）。
        返回 (台账是否有变动, 变更说明文本)；失败抛异常由调用方显式告警。
        """
        notes = []
        try:
            with self._connect() as conn:
                ledger_changed = False
                for item in extraction.get("foreshadows_planted", []):
                    if self._add_foreshadow_tx(conn, item["content"], chapter_num,
                                               item["due_chapter"], SOURCE_AUTO):
                        ledger_changed = True
                        due_txt = f"，预计第{item['due_chapter']}章回收" if item["due_chapter"] else ""
                        notes.append(f"新埋伏笔「{item['content'][:24]}」{due_txt}")
                    else:
                        notes.append(f"重复伏笔跳过「{item['content'][:24]}」")
                for hint in extraction.get("foreshadows_resolved", []):
                    if self._resolve_foreshadow_tx(conn, hint):
                        ledger_changed = True
                        notes.append(f"回收伏笔「{hint[:24]}」")
                    else:
                        # 显式记录未命中，不静默丢弃（goink只排不滤：对不上的原文保留在台账）
                        notes.append(f"未匹配到待回收伏笔「{hint[:24]}」（原文保留待人工处理）")
                for ch in extraction.get("characters", []):
                    self.upsert_character(ch["name"], ch["state"], chapter_num,
                                          source=SOURCE_AUTO, _conn=conn)
                    notes.append(f"更新角色「{ch['name']}」")
                summary = (extraction.get("chapter_summary") or "").strip()
                if summary:
                    conn.execute("UPDATE chapter_memory SET summary = ? WHERE chapter_num = ?",
                                 (summary, chapter_num))
                    notes.append("章节摘要已按抽取结果更新")
                conn.commit()
            return ledger_changed, "；".join(notes) if notes else "抽取结果为空，无变化"
        except Exception as e:
            logger.error(f"🚨 第{chapter_num}章记忆回写失败，事务已回滚：{e}")
            raise e

    def refresh_global_summary(self, chapter_num, llm_call):
        """滚动全局摘要：把上次压缩之后的新章节摘要并入旧摘要，LLM压缩≤2000字存 story_state。

        触发节奏（每5章/台账变动）由调用方决定；提示词强制保留三要素
        （关键人物状态/主线推进/未兑现伏笔）、删过细情节。
        压缩失败保留旧摘要原样不动，返回 (False, 错误信息) 由调用方显式告警。
        返回 (是否成功, 新摘要或错误信息)。
        """
        try:
            try:
                covered = int(self.get_state(SUMMARY_UPTO_KEY, 0) or 0)
            except (TypeError, ValueError):
                covered = 0
            with self._connect() as conn:
                rows = conn.execute("""
                    SELECT chapter_num, summary FROM chapter_memory
                    WHERE chapter_num > ? AND chapter_num <= ?
                    ORDER BY chapter_num ASC LIMIT ?
                """, (covered, chapter_num, SUMMARY_NEW_CHAPTERS_MAX)).fetchall()
            if not rows:
                # 无新章节可并入：摘要保持不变，视为成功
                return True, self.get_state(GLOBAL_SUMMARY_KEY, "") or ""
            new_chapters = "\n".join(f"第{num}章: {summary}" for num, summary in rows)
            old_summary = self.get_state(GLOBAL_SUMMARY_KEY, "") or ""
            open_foreshadows = "\n".join(f"- {f['content']}" for f in self.get_open_foreshadows())
            characters = "\n".join(f"- {c['name']}：{c['state']}" for c in self.get_characters())
            system_prompt, user_prompt = render_global_summary_prompts(
                old_summary=old_summary,
                new_chapters=new_chapters,
                open_foreshadows=open_foreshadows,
                characters=characters,
            )
            compressed = str(llm_call(user_prompt, system_prompt)).strip()
            if not compressed:
                raise RuntimeError("摘要压缩调用返回空内容")
            if len(compressed) > GLOBAL_SUMMARY_BUDGET:
                compressed = compressed[:GLOBAL_SUMMARY_BUDGET]
            # 摘要与覆盖游标同一事务写入，保证「摘要-游标」一致。
            # 游标只推进到本次真正并入的最大章号：单次最多并入 SUMMARY_NEW_CHAPTERS_MAX 章，
            # 未并入的中间章节留给下次压缩——旧库批量迁移/状态重建时若直接跳到 chapter_num，中间章节将永久丢出摘要
            covered_new = max(num for num, _ in rows)
            with self._connect() as conn:
                self.set_state(GLOBAL_SUMMARY_KEY, compressed,
                               updated_chapter=covered_new, source=SOURCE_AUTO, _conn=conn)
                self.set_state(SUMMARY_UPTO_KEY, str(covered_new),
                               updated_chapter=covered_new, source=SOURCE_AUTO, _conn=conn)
                conn.commit()
            logger.info(f"✅ 全局摘要已滚动更新至第{covered_new}章（{len(compressed)}字）")
            return True, compressed
        except Exception as e:
            # 压缩失败：旧摘要原样保留（set_state 未执行），告警由调用方完成
            logger.error(f"🚨 全局摘要压缩失败，保留旧摘要：{e}")
            return False, str(e)
