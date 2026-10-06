import os
import json
import sqlite3
import time
import threading
from datetime import datetime

import requests
from openmars_core.config import get_config
from openmars_core.llm_ledger import summarize_chapter
from openmars_core.query_engine import get_engine, StepError, PipelineCancelled, LLMCallError
from openmars_core.memory_palace import SQLiteMemoryPalace as SimpleMemoryPalace, SUMMARY_EVERY_N_CHAPTERS
from openmars_core.prompts import (ENDING_EXCERPT_CHARS, render_memory_extract_prompts,
                                   parse_memory_extract, render_gate_repair_prompts)
from openmars_core.validators import (validate_chapter, run_consistency_review,
                                      format_gate_violations, format_gate_annotation)
from openmars_core.logger import logger

# 全局互斥锁，确保同一时间只允许一个生成任务执行
generate_global_lock = threading.Lock()

# 流水线步骤失败 → 移动端告警文案分流（大纲/排雷/正文），替代笼统的「流水线严重崩溃」
STEP_ALERT_TITLES = {
    "outline": "大纲步失败",
    "review": "排雷步失败",
    "content": "正文步失败",
}

# 质量门禁定向重写轮数上限（chinese-novelist-skill：失败自动重写设上限；超限标注不阻塞）
GATE_MAX_REWRITES = 2

# =============================================
# 告警留痕（denova/无双「全程可见」）：send_mobile_alert 每次推送追加一条记录，
# 运行报告按「生成前游标-生成后切片」收录本章告警，过程可回放、告警不黑洞
# =============================================
_ALERT_LOG = []
_ALERT_LOG_LOCK = threading.Lock()


def _alert_log_mark():
    """取当前告警游标（列表长度），供本次生成结束时切片本章告警"""
    with _ALERT_LOG_LOCK:
        return len(_ALERT_LOG)


def _alerts_since(mark):
    """取游标之后的告警记录切片（浅拷贝，避免运行报告序列化时被并发修改）"""
    with _ALERT_LOG_LOCK:
        return [dict(r) for r in _ALERT_LOG[mark:]]

# 写后记忆抽取的便宜模型（NovelClaw/MuMuAINovel闭环：抽取用小模型省钱）：
# 环境变量 LLM_EXTRACT_MODEL_NAME 指定，缺省为空时回落主模型（抽取照常执行，只是不省钱）


def _get_extract_model():
    return os.getenv("LLM_EXTRACT_MODEL_NAME", "").strip() or None

# 检查是否使用免费模型（智谱 GLM-4-Flash）
def is_free_model():
    config = get_config()
    model_name = config.llm_model_name.lower()
    base_url = config.llm_base_url.lower()
    return 'glm-4-flash' in model_name or 'bigmodel.cn' in base_url

def send_mobile_alert(title, content, is_error=False):
    # 先留痕再推送：即使未配置 Webhook，告警也进运行报告（绝不黑洞）
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": title,
        "content": content,
        "is_error": bool(is_error),
        "sent": False,
    }
    with _ALERT_LOG_LOCK:
        _ALERT_LOG.append(record)

    webhook_url = get_config().webhook_url
    if not webhook_url:
        return record

    try:
        icon = "🚨" if is_error else "🎉"
        full_title = f"{icon}{title}"
        full_content = f"【赛博印钞机Pro】{content}"

        if "bark" in webhook_url.lower():
            requests.get(f"{webhook_url}/{full_title}/{full_content}", timeout=5)
        elif "ftqq" in webhook_url.lower() or "sct" in webhook_url.lower():
            requests.post(webhook_url, data={"title": full_title, "desp": full_content}, timeout=5)
        else:
            requests.post(webhook_url, json={"title": full_title, "content": full_content}, timeout=5)
        record["sent"] = True

        logger.info(f"📱 告警已推送：{title}")
    except Exception as e:
        logger.warning(f"⚠️  告警推送失败: {e}")
        pass
    return record

def _writeback_memory(memory, engine, chapter_num, content):
    """写后回写闭环：章节成功后追加一次结构化抽取调用（便宜模型），更新伏笔台账（埋设/回收）、
    角色状态、章节摘要，并按「每5章或伏笔台账变动」滚动压缩全局摘要。

    抽取/压缩失败一律显式告警并保留旧状态，绝不阻断章节交付（NovelClaw反面清单：禁止except-pass静默丢记忆）。
    """
    extract_model = _get_extract_model()
    ledger_changed = False
    try:
        existing_foreshadows = memory.get_open_foreshadows(limit=20)
        existing_characters = memory.get_characters(limit=20)
        system_prompt, user_prompt = render_memory_extract_prompts(
            chapter_num, content, existing_foreshadows, existing_characters)
        raw = engine.call_llm_sync(user_prompt, system_prompt,
                                   step="memory_extract", chapter=chapter_num, model=extract_model)
        extraction = parse_memory_extract(str(raw))
        ledger_changed, detail = memory.apply_chapter_extraction(chapter_num, extraction)
        logger.info(f"✅ 第{chapter_num}章记忆回写完成：{detail}")
    except Exception as e:
        # 抽取失败：显式告警、记忆库停留上一致状态（apply_chapter_extraction单事务已保证），章节照常交付；
        # 不提前return——「每5章」的滚动摘要节奏独立于抽取成败，仍要维持
        logger.error(f"🚨 第{chapter_num}章记忆回写失败，记忆库停留上一致状态：{e}")
        send_mobile_alert(
            f"第{chapter_num}章记忆回写失败",
            f"错误摘要：{str(e)[:200]}（章节已交付，记忆未更新，可在记忆宫殿Tab手工补录）",
            is_error=True
        )
    # 滚动全局摘要：每5章或本章伏笔台账有变动时压缩一次；失败保留旧摘要并告警
    if ledger_changed or chapter_num % SUMMARY_EVERY_N_CHAPTERS == 0:
        def _summary_llm(user_prompt, system_prompt):
            return engine.call_llm_sync(user_prompt, system_prompt, step="global_summary",
                                        chapter=chapter_num, model=extract_model)
        ok, detail = memory.refresh_global_summary(chapter_num, llm_call=_summary_llm)
        if not ok:
            send_mobile_alert(
                f"第{chapter_num}章全局摘要压缩失败",
                f"错误摘要：{detail[:200]}（旧摘要已原样保留）",
                is_error=True
            )


# =============================================
# 每章运行报告（denova/无双「全程可见」卖点：三步输出摘要、门禁结果、重写次数、
# token/成本、告警记录逐项落盘 output/<书>/第N章_报告.json，过程可回放）
# =============================================
def write_run_report(novel_name, chapter_num, report):
    """运行报告落盘；写入失败只告警日志，绝不影响生成主流程。返回报告路径或 None"""
    try:
        out_dir = os.path.join("output", str(novel_name))
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"第{chapter_num}章_报告.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"📋 运行报告已写入：{path}")
        return path
    except Exception as e:
        logger.error(f"🚨 运行报告写入失败（不影响生成）：{e}")
        return None


def _summarize_run_tokens(chapter_num, before, config):
    """本次生成的 token/成本：生成前后各取一次 llm_calls 按章聚合，差集即真实消耗；
    配置了单价（元/百万Token）时附估算成本，未配置只记 token"""
    after = summarize_chapter(chapter_num)
    keys = ("calls", "success_calls", "prompt_tokens", "completion_tokens", "total_tokens")
    tokens = {k: max(0, int(after.get(k, 0)) - int(before.get(k, 0))) for k in keys}
    try:
        price_in = float(getattr(config, "llm_price_in_per_1m", 0) or 0)
        price_out = float(getattr(config, "llm_price_out_per_1m", 0) or 0)
    except (TypeError, ValueError):
        price_in, price_out = 0.0, 0.0
    if price_in > 0 and price_out > 0:
        cost = tokens["prompt_tokens"] / 1e6 * price_in + tokens["completion_tokens"] / 1e6 * price_out
        tokens["estimated_cost_yuan"] = round(cost, 6)
    else:
        tokens["estimated_cost_yuan"] = None
        tokens["cost_note"] = "未配置 LLM_PRICE_IN_PER_1M/LLM_PRICE_OUT_PER_1M，只记token不算钱"
    return tokens


def _run_gate_loop(engine, content, target_words, novel_name, chapter_num, gate_config, kill_event):
    """质量门禁闭环：validate_chapter→不达标定向重写（违规清单回喂prompt，最多 GATE_MAX_REWRITES 轮）。

    - 重写触发条件只含硬门禁违规；platform_hint 绝不触发（r2：passed 只由硬门禁决定）；
    - 定向重写用违规清单回喂 prompt（render_gate_repair_prompts），只修清单内问题；
    - 重写调用失败/返回空/kill_event 置位：保留当前稿提前收尾，绝不把空串当正文
      （million-word 静默降级教训：收尾状态由调用方显式标注+告警）。
    返回 (重写后的最终正文, 最终 GateReport, 实际重写轮数)。
    """
    gate = validate_chapter(content, target_words, novel_name=novel_name, config=gate_config)
    rewrite_rounds = 0
    while (gate_config.tomato_audit_compatible
           and not gate.passed
           and rewrite_rounds < GATE_MAX_REWRITES
           and not (kill_event is not None and kill_event.is_set())):
        violations_text = format_gate_violations(gate)
        logger.warning(f"⚠️  第{chapter_num}章质量门禁未通过，发起第{rewrite_rounds + 1}/{GATE_MAX_REWRITES}轮定向重写：\n{violations_text}")
        try:
            repair_system, repair_prompt = render_gate_repair_prompts(
                chapter_num=chapter_num, target_words=target_words, draft=content,
                violations=violations_text, words_floor=gate.words_floor, words_ceil=gate.words_ceil)
            revised = engine.call_llm_sync(repair_prompt, repair_system,
                                           step="gate_rewrite", chapter=chapter_num)
        except LLMCallError as e:
            logger.error(f"🚨  第{chapter_num}章门禁定向重写调用失败，保留当前稿收尾：{e}")
            break
        revised_text = str(revised).strip()
        if revised_text:
            content = revised_text
        else:
            logger.error(f"🚨  第{chapter_num}章门禁重写返回空内容，保留当前稿")
        rewrite_rounds += 1
        gate = validate_chapter(content, target_words, novel_name=novel_name, config=gate_config)
    return content, gate, rewrite_rounds


def _write_failure_report(novel_name, chapter_num, error, stage="unknown", alert_mark=None):
    """失败路径也落一份运行报告（「全程可见」：失败同样可回放，不留黑盒）"""
    write_run_report(novel_name, chapter_num, {
        "novel_name": novel_name,
        "chapter_num": chapter_num,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "success": False,
        "stage": stage,
        "error": str(error)[:300],
        "alerts": _alerts_since(alert_mark) if alert_mark is not None else [],
    })


# =============================================
# 批量连跑·断点续跑（github-3 状态外置 + github-19 记忆文件即断点）：
# chapter_memory 存在非空 full_content 即视为完成——启动时跳过已完成章，
# 中途取消后再次启动不重跑已完成章
# =============================================
def get_completed_chapters(novel_name, start_chapter, end_chapter):
    """读断点完成集：chapter_memory 中 [start, end] 区间内 full_content 非空的章号集合。

    只读查询；库/表缺失或查询失败一律返回空集合（视为全部未完成，从头连跑），
    绝不因断点读取失败中断批量任务。
    """
    db_path = os.path.join("novel_settings", str(novel_name), "memory.db")
    if not os.path.exists(db_path):
        return set()
    try:
        conn = sqlite3.connect(db_path, timeout=15.0)
        try:
            rows = conn.execute(
                "SELECT chapter_num FROM chapter_memory "
                "WHERE chapter_num BETWEEN ? AND ? "
                "AND full_content IS NOT NULL AND TRIM(full_content) != ''",
                (int(start_chapter), int(end_chapter))).fetchall()
            return {int(r[0]) for r in rows}
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(f"⚠️  读取断点完成集失败（按全部未完成处理）：{e}")
        return set()


def _save_batch_chapter(novel_name, chapter_num, content):
    """批量模式下每章成功后立即落盘 .md（与面板单章保存同一 output/<书>/ 目录约定）"""
    try:
        out_dir = os.path.join("output", str(novel_name))
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"第{chapter_num}章_{int(time.time())}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"📁 批量章节保存到：{path}")
    except Exception as e:
        logger.error(f"🚨 批量章节保存失败（不阻断连跑）：{e}")


def generate_chapter_batch(start_chapter, end_chapter, target_words, custom_prompt,
                           novel_name="默认小说", kill_event=None, progress_cb=None):
    """批量连跑：从第 start 章串行连跑到第 end 章（循环复用 generate_chapter_full，
    天然复用既有全局锁与免费模型串行保护）。

    断点续跑：启动时读 chapter_memory 完成集，已有非空 full_content 的章直接跳过；
    每章开始前检查 kill_event，取消后不再启动后续章（已完成的章保持完成）。
    返回逐章结果列表 [{"chapter_num", "status", "detail"}]，
    status ∈ ok / skipped_completed / failed / cancelled。
    """
    start_chapter = int(start_chapter)
    end_chapter = int(end_chapter)
    if end_chapter < start_chapter:
        return [{"chapter_num": start_chapter, "status": "failed",
                 "detail": "批量结束章需大于等于起始章"}]
    completed = get_completed_chapters(novel_name, start_chapter, end_chapter)
    if completed:
        logger.info(f"⏭️  断点续跑：第{start_chapter}-{end_chapter}章中已完成{len(completed)}章"
                    f"（{sorted(completed)}），将自动跳过")
    results = []
    for ch in range(start_chapter, end_chapter + 1):
        if kill_event is not None and kill_event.is_set():
            results.append({"chapter_num": ch, "status": "cancelled",
                            "detail": "批量任务已被取消，本章及后续章未启动"})
            break
        if ch in completed:
            logger.info(f"⏭️  第{ch}章已有完整正文（chapter_memory.full_content 非空），断点续跑跳过")
            results.append({"chapter_num": ch, "status": "skipped_completed",
                            "detail": "chapter_memory 已有完整正文，断点续跑跳过"})
            continue
        logger.info(f"🏃 批量连跑：开始第{ch}/{end_chapter}章")
        ok, detail = generate_chapter_full(ch, target_words, custom_prompt, novel_name=novel_name,
                                           kill_event=kill_event, progress_cb=progress_cb)
        if ok:
            # 保存的是交付内容（门禁未过时含显式标注块，保持「不达标绝不静默」）
            _save_batch_chapter(novel_name, ch, detail)
        results.append({"chapter_num": ch, "status": "ok" if ok else "failed",
                        "detail": str(detail)[:200]})
    return results


def generate_chapter_full(chapter_num, target_words, custom_prompt, novel_name="默认小说",
                          kill_event=None, progress_cb=None):
    # kill_event：协作式取消事件（面板「停止生成」/2小时超时置位），流水线在下一步边界安全停止；
    # progress_cb：阶段化进度回调 progress_cb(step, pct)（大纲10→30、排雷30→40、正文40→95），
    #              二者均可选，旧调用方（不传）行为完全不变
    # 免费模型风控保护：严格串行执行，防限流
    if is_free_model():
        logger.info("使用免费模型，启用严格串行执行保护")
        # 尝试获取锁，5秒内获取不到直接拒绝新任务
        if not generate_global_lock.acquire(blocking=True, timeout=5):
            logger.warning("检测到免费模型并发生成，已拒绝新任务")
            return False, "⚠️  免费模型限流保护：当前有任务正在执行，请稍后再试"
    else:
        # 付费模型：常规锁保护
        logger.info("使用付费模型，启用常规并发保护")
        if not generate_global_lock.acquire(blocking=False):
            logger.warning("检测到并发生成，已拒绝新任务")
            return False, "当前有任务正在执行，请稍后再试"

    # 无论哪种模型，统一在 finally 里释放锁
    # 告警游标与 token 快照：本章开始前取点，结束时差集即本章的告警/消耗（运行报告用）
    alert_mark = None
    ledger_before = summarize_chapter(chapter_num)
    try:
        alert_mark = _alert_log_mark()
        # 免费模型额外加 2 秒延迟，确保不触发 30 并发上限
        if is_free_model():
            time.sleep(2)

        # 你的生成逻辑，一行不改
        logger.info(f"🚀 开始生成 | 小说：{novel_name} | 章节：{chapter_num}")
        send_mobile_alert(f"开始生成第{chapter_num}章", f"目标字数：{target_words} | 小说：{novel_name}")
        
        memory = SimpleMemoryPalace(novel_name)
        fixed_mem = memory.get_fixed_prompt()
        # 统一注入口：build_generation_context 装配生成骨架（全局摘要+近3章摘要+上一章结尾+未兑现伏笔+活跃角色状态），
        # 替换旧 get_dynamic_prompt 的「只回看3章」；上一章结尾已并入骨架第三节，
        # 不再经 run_chapter_pipeline 的 ending_excerpt 参数重复注入（该参数保留给直连调用方使用）
        dynamic_mem = memory.build_generation_context(chapter_num)

        engine = get_engine()

        # 进度起点：任务已锁定进入流水线，先报start段位，大纲步细化为10→30
        if progress_cb is not None:
            try:
                progress_cb("start", 5)
            except Exception as e:
                logger.warning(f"⚠️  进度回调失败（不影响生成）: {e}")

        result = engine.run_chapter_pipeline(
            chapter_num, target_words, fixed_mem, dynamic_mem, custom_prompt,
            kill_event=kill_event, progress_cb=progress_cb)

        if result and result.get("content"):
            content = result["content"]
            gate_config = get_config()

            # —— 质量门禁闭环：不达标定向重写≤2轮（违规清单回喂prompt）；
            #    platform_hint 只是报告级提示，绝不触发重写（r2）；
            #    门禁总开关关闭（TOMATO_AUDIT_COMPATIBLE=false）时跳过重写、只出报告 ——
            content, gate, rewrite_rounds = _run_gate_loop(
                engine, content, target_words, novel_name, chapter_num, gate_config, kill_event)
            deliver_content = content
            gate_enforced = bool(gate_config.tomato_audit_compatible)
            if gate_enforced and not gate.passed:
                # 仍失败：显式标注+告警含违规清单，绝不静默降级（million-word 20章挂19章教训）
                annotation = format_gate_annotation(gate, rewrite_rounds)
                violations_brief = format_gate_violations(gate)
                logger.error(f"🚨 第{chapter_num}章质量门禁最终未通过（重写{rewrite_rounds}轮）：\n{violations_brief}")
                send_mobile_alert(
                    f"第{chapter_num}章质量门禁未通过",
                    f"违规清单：{violations_brief[:300]}（已保留当前稿并在正文尾部显式标注，绝不静默降级）",
                    is_error=True
                )
                deliver_content = content + "\n\n" + annotation

            # —— 可选一致性审校（独立步骤，不进生成prompt）：
            #    CONTENT_SAFETY_CHECK=false 时零额外LLM调用；必须在写后回写之前跑，
            #    对照的是本章写作前的台账/角色状态，才能抓出「死亡角色出场」类矛盾 ——
            consistency = run_consistency_review(engine, memory, novel_name, chapter_num, content,
                                                 model=_get_extract_model())
            if consistency.get("enabled") and consistency.get("error"):
                send_mobile_alert(
                    f"第{chapter_num}章一致性审校失败",
                    f"{str(consistency['error'])[:200]}（已留痕运行报告，不阻断交付）",
                    is_error=True
                )
            elif consistency.get("enabled") and consistency.get("suspects"):
                preview = "；".join(s.get("detail", "")[:40] for s in consistency["suspects"][:3])
                send_mobile_alert(
                    f"第{chapter_num}章一致性审校发现{len(consistency['suspects'])}处可疑点",
                    f"{preview}（详见运行报告，供人工复核）"
                )

            # 先落基础记忆行（摘要/大纲/排雷/结尾摘录列），确保后续抽取失败时本章记忆也已入库
            # 注意：记忆与运行报告用的是干净正文 content，显式标注块只进交付内容 deliver_content
            summary = content[:150] + "..." if len(content) > 150 else content
            memory.safe_update(chapter_num, summary, len(content), content,
                               outline=result.get("outline"), review=result.get("review"),
                               ending_excerpt=content[-ENDING_EXCERPT_CHARS:])
            # 写后回写闭环（NovelClaw/MuMuAINovel模式）：抽取→台账/角色/摘要回写，失败告警不阻断交付
            _writeback_memory(memory, engine, chapter_num, content)
            send_mobile_alert(
                f"第{chapter_num}章生成完毕",
                f"实际字数：{len(content)} | 目标：{target_words} | "
                f"门禁：{'通过' if gate.passed else '未通过'}"
                f"{' | 重写' + str(rewrite_rounds) + '轮' if rewrite_rounds else ''} | 小说：{novel_name}"
            )

            # —— 每章运行报告：三步输出摘要+门禁结果（含platform_hint）+重写次数+token/成本+告警记录 ——
            report = {
                "novel_name": novel_name,
                "chapter_num": chapter_num,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "success": True,
                "target_words": target_words,
                "steps": {
                    "outline": {"summary": str(result.get("outline", ""))[:300]},
                    "review": {"summary": str(result.get("review", ""))[:300]},
                    "content": {
                        "real_chars": len(content),
                        "summary": content[:300],
                        "truncated": bool(result.get("truncated")),
                        "body_calls": result.get("body_calls"),
                    },
                },
                "gate": {**gate.as_dict(),
                         "rewrite_rounds": rewrite_rounds,
                         "enforced": gate_enforced},
                "rewrite_rounds": rewrite_rounds,
                "consistency_review": consistency,
                "tokens": _summarize_run_tokens(chapter_num, ledger_before, gate_config),
                "alerts": _alerts_since(alert_mark),
                "delivery": {
                    "annotated": bool(gate_enforced and not gate.passed),
                    "content_chars": len(deliver_content),
                },
            }
            write_run_report(novel_name, chapter_num, report)
            return True, deliver_content

        send_mobile_alert(
            f"第{chapter_num}章生成异常",
            "大模型返回内容为空",
            is_error=True
        )
        _write_failure_report(novel_name, chapter_num, "大模型返回内容为空",
                              stage="pipeline", alert_mark=alert_mark)
        return False, "大模型返回内容为空"

    except PipelineCancelled:
        # 协作式取消（用户手动停止/2小时超时）：流水线已在步骤边界安全停止，不误报为步骤失败；
        # 按「已取消」告警分流（91Writing反面清单：停止必须接真实取消，并给用户明确反馈）
        logger.info(f"🛑 第{chapter_num}章生成已取消 | 小说：{novel_name}（流水线在步骤边界安全停止）")
        send_mobile_alert(
            f"第{chapter_num}章生成已取消",
            "用户手动停止或任务超时，流水线已在步骤边界安全停止，已完成的调用照常记账"
        )
        _write_failure_report(novel_name, chapter_num, "生成已被用户取消（协作式取消）",
                              stage="cancelled", alert_mark=alert_mark)
        return False, "生成已被用户取消"
    except StepError as e:
        # 按失败步骤分流告警文案，让手机上一眼看出坏在哪一步（大纲/排雷/正文）
        step_display = STEP_ALERT_TITLES.get(e.step_name, f"未知步骤({e.step_name})失败")
        error_summary = str(e)[:200]
        logger.error(f"🚨 {step_display} | 小说：{novel_name} | 章节：{chapter_num} | {error_summary}")
        send_mobile_alert(
            f"第{chapter_num}章{step_display}",
            f"错误摘要：{error_summary}",
            is_error=True
        )
        _write_failure_report(novel_name, chapter_num, f"{step_display}: {error_summary}",
                              stage=e.step_name, alert_mark=alert_mark)
        return False, f"{step_display}: {error_summary}"
    except Exception as e:
        logger.error(f"生成失败: {e}", exc_info=True)
        error_msg = str(e)[:200]
        send_mobile_alert(
            f"流水线严重崩溃", 
            f"第{chapter_num}章 | {error_msg}", 
            is_error=True
        )
        _write_failure_report(novel_name, chapter_num, f"流水线严重崩溃: {error_msg}",
                              stage="unknown", alert_mark=alert_mark)
        return False, str(e)
    finally:
        # 免费模型执行完成后再加 2 秒延迟
        if is_free_model():
            time.sleep(2)
        # 无论成功失败，都释放全局互斥锁
        generate_global_lock.release()
        logger.info(f"🔓 生成任务完成，释放全局互斥锁 | 小说：{novel_name} | 章节：{chapter_num}")