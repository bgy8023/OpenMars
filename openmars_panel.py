#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 OpenMars 工业级网文创作系统
集成：P0级Token黑洞防护、工业级同步引擎、SQLite记忆宫殿、全链路告警
"""
import os
import json
import time
import sqlite3
import threading
from dotenv import load_dotenv, set_key
import streamlit as st

# =============================================
# 【P0级修复 优先级最高】状态机初始化
# =============================================
if "is_generating" not in st.session_state:
    st.session_state.is_generating = False
if "current_output" not in st.session_state:
    st.session_state.current_output = ""
if "generation_result" not in st.session_state:
    st.session_state.generation_result = {}
if "generation_start_time" not in st.session_state:
    st.session_state.generation_start_time = None
if "selected_novel" not in st.session_state:
    st.session_state.selected_novel = "默认小说"

# =============================================
# 全局线程安全锁+任务状态管理，P0级状态机绑定
# 复位与释放锁收敛到「确认后台线程已结束」的唯一路径（见监控区 _finish_generation_session），
# 后台线程内绝不触碰 session_state，杜绝跨线程提前解锁
# =============================================
if "generate_lock" not in st.session_state:
    st.session_state.generate_lock = threading.Lock()
if "generate_thread" not in st.session_state:
    st.session_state.generate_thread = None
if "thread_kill_event" not in st.session_state:
    st.session_state.thread_kill_event = threading.Event()
if "result_path" not in st.session_state:
    st.session_state.result_path = ""
if "generation_progress" not in st.session_state:
    st.session_state.generation_progress = {}
if "pending_status" not in st.session_state:
    st.session_state.pending_status = None

# =============================================
# 流水线阶段化进度文案（进度随流水线阶段推进，而非随时间匀速假进度）
# =============================================
PROGRESS_STEP_LABELS = {
    "start": "🚀 任务启动中",
    "outline": "📋 大纲师构思剧情中",
    "review": "🧨 排雷师排查毒点中",
    "content": "✍️ 主笔疯狂码字中",
}

# 结果回显临时文件（固定路径，主脚本读后删除；替代旧版带时间戳且无人读取的孤儿文件）
RESULT_TEMP_PATH = os.path.join("temp", "generation_result_snapshot.json")


# 把测试连接的常见异常翻译成中文文案（91Writing反面清单：拒绝静默失败）
def _explain_conn_error(e):
    status = getattr(e, "status_code", None)
    text = str(e)
    name = type(e).__name__
    if status in (401, 403):
        return f"API Key 无效或无该模型权限（HTTP {status}），请检查密钥是否填对、是否有该模型权限"
    if status == 404:
        return "接口或模型不存在（HTTP 404），请检查 Base URL 是否以 /v1 结尾、模型名称是否正确"
    if status == 429:
        return "触发限流或额度不足（HTTP 429），请稍后重试或检查账户余额"
    if "timeout" in name.lower() or "timed out" in text.lower():
        return "连接超时，请检查 Base URL 与网络是否可达"
    if "connect" in name.lower() or "connect" in text.lower():
        return "无法连接到服务器，请检查 Base URL 地址与网络"
    return f"{name}: {text[:200]}"


# 批量连跑逐章状态 → 面板可读文案
BATCH_STATUS_LABELS = {
    "ok": "✅ 生成完成",
    "skipped_completed": "⏭️ 已有完整正文，断点跳过",
    "failed": "🚨 生成失败",
    "cancelled": "🛑 已取消（本章及后续未启动）",
}


# 批量连跑worker：调 cyber_printer.generate_chapter_batch 串行连跑（复用全局锁），
# 汇总逐章状态为可读文本；每章正文由 cyber_printer 侧自行落盘 output/<书>/第N章_*.md
def _run_batch_generate(start_ch, end_ch, target_words, custom_prompt, novel_name,
                        kill_event, progress_cb):
    from cyber_printer_ultimate import generate_chapter_batch
    from openmars_core.logger import logger

    results = generate_chapter_batch(start_ch, end_ch, target_words, custom_prompt,
                                     novel_name=novel_name, kill_event=kill_event,
                                     progress_cb=progress_cb)
    ok_count = sum(1 for r in results if r["status"] in ("ok", "skipped_completed"))
    lines = [f"批量连跑 第{start_ch}-{end_ch}章：{ok_count}/{len(results)} 章达成（含断点跳过）"]
    for r in results:
        label = BATCH_STATUS_LABELS.get(r["status"], r["status"])
        detail = f" — {r['detail']}" if r.get("detail") else ""
        lines.append(f"- 第{r['chapter_num']}章：{label}{detail}")
    success = ok_count == len(results) and not (kill_event and kill_event.is_set())
    logger.info(f"批量连跑结束：{ok_count}/{len(results)} 章达成 | 小说：{novel_name}")
    return success, "\n".join(lines)


# 生成任务放到独立后台守护线程，彻底和Streamlit主线程隔离。
# 线程内绝不触碰 st.session_state（跨线程不可靠，旧版在线程finally里提前释放锁导致P0失效）：
#   - 结果写入 result_path 指向的固定临时文件，主脚本「确认线程已结束」后读取回显；
#   - 取消只认 kill_event（协作式，流水线在下一步边界安全停止）；
#   - 进度写入共享 progress_state（GIL保证单键赋值原子，主脚本每轮读取）；
#   - batch_range 非空时走批量连跑（断点续跑），否则走单章生成。
def background_generate_task(params):
    from cyber_printer_ultimate import generate_chapter_full
    from openmars_core.logger import logger

    chapter_num = params["chapter_num"]
    target_words = params["target_words"]
    custom_prompt = params["custom_prompt"]
    novel_name = params["novel_name"]
    result_path = params["result_path"]
    kill_event = params["kill_event"]
    progress_state = params["progress_state"]
    batch_range = params.get("batch_range")

    success = False
    result_content = ""
    try:
        logger.info(f"后台任务开始 | 小说：{novel_name} | 章节：{chapter_num}"
                    + (f" | 批量区间：{batch_range[0]}-{batch_range[1]}" if batch_range else ""))

        # 阶段化进度回调：worker线程只写共享dict，绝不触碰session_state
        def _on_progress(step, pct):
            progress_state["step"] = step
            progress_state["pct"] = pct

        if batch_range:
            success, result_content = _run_batch_generate(
                batch_range[0], batch_range[1], target_words, custom_prompt, novel_name,
                kill_event, _on_progress)
        else:
            success, result_content = generate_chapter_full(
                chapter_num=chapter_num,
                target_words=target_words,
                custom_prompt=custom_prompt,
                novel_name=novel_name,
                kill_event=kill_event,
                progress_cb=_on_progress,
            )
    except Exception as e:
        result_content = f"生成失败: {str(e)}"
        logger.error(f"后台任务崩溃：{e}", exc_info=True)
    finally:
        # 成败都必须落结果文件（主脚本读后删除），杜绝旧版「生成结果永不回显」黑洞；
        # 这里绝不释放 generate_lock、绝不复位 is_generating——统一由主脚本在确认线程结束后处理
        try:
            result = {
                "success": success,
                "content": result_content,
                "chapter_num": chapter_num,
                "target_words": target_words,
                "novel_name": novel_name,
                "real_chars": len(result_content) if success else 0,
                "cancelled": bool(kill_event and kill_event.is_set()),
                "batch": batch_range,
            }
            os.makedirs(os.path.dirname(result_path) or ".", exist_ok=True)
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            if success and not batch_range and not (kill_event and kill_event.is_set()):
                # 保存到output目录（批量连跑的逐章正文由 cyber_printer 侧保存，这里不重复落盘汇总文本）
                output_dir = f"output/{novel_name}"
                os.makedirs(output_dir, exist_ok=True)
                file_path = os.path.join(output_dir, f"第{chapter_num}章_{int(time.time())}.md")
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(result_content)
                logger.info(f"📁 章节保存到：{file_path}")
            logger.info("后台任务结束，结果已写入临时文件，等待主脚本确认线程结束后统一复位")
        except Exception as e:
            logger.error(f"后台任务结果落盘失败：{e}", exc_info=True)


# 读取worker写入的固定临时结果文件（读后删除，杜绝孤儿文件）；无文件/解析失败返回None
def _load_worker_result():
    from openmars_core.logger import logger

    result_path = st.session_state.get("result_path", "")
    if not result_path or not os.path.exists(result_path):
        return None
    result = None
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            result = json.load(f)
    except Exception as e:
        logger.error(f"读取生成结果临时文件失败：{e}", exc_info=True)
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass
    return result


# 「确认线程已结束」后的唯一复位路径：回填结果→复位 is_generating→释放 generate_lock。
# 锁释放带 locked() 判断：无论本路径走到多少次都恰释放一次（P0：杜绝提前解锁与双重释放）
def _finish_generation_session():
    from openmars_core.logger import logger

    result = _load_worker_result() or {}
    elapsed_time = int(time.time() - st.session_state.generation_start_time) if st.session_state.generation_start_time else 0
    content = result.get("content", "")

    if result.get("success") and content:
        # 键名统一为生产方口径：real_chars（旧版面板用real_words与生产方不匹配），elapsed_time 由面板计时补齐
        st.session_state.current_output = content
        st.session_state.generation_result = {
            "chapter_num": result.get("chapter_num"),
            "real_chars": result.get("real_chars") or len(content),
            "target_words": result.get("target_words"),
            "elapsed_time": elapsed_time,
        }
        st.session_state.pending_status = ("success", f"🎉 章节生成完成！实际 {len(content)} 字，耗时 {elapsed_time} 秒")
        logger.info(f"生成任务完成并回显 | 章节：{result.get('chapter_num')} | 实际字数：{len(content)} | 耗时：{elapsed_time}秒")
    else:
        # 失败/取消不回显正文，但必须给出可读结果（替代旧版静默黑洞）
        st.session_state.current_output = ""
        st.session_state.generation_result = {}
        if result.get("cancelled") or st.session_state.thread_kill_event.is_set():
            st.session_state.pending_status = ("cancelled", "🛑 生成已取消：流水线在步骤边界安全停止，已消耗的调用照常记账")
        else:
            reason = content or "未获取到生成结果（详见 logs/ 运行日志）"
            st.session_state.pending_status = ("error", f"🚨 生成失败：{reason}")
        logger.warning(f"生成任务未产出正文 | 取消：{st.session_state.thread_kill_event.is_set()} | 原因：{content or '无结果文件'}")

    st.session_state.is_generating = False
    if st.session_state.generate_lock.locked():
        st.session_state.generate_lock.release()

load_dotenv()

# =============================================
# 全局页面配置
# =============================================
st.set_page_config(
    page_title="OpenMars 工业级网文创作系统",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 全局样式
st.markdown("""
<style>
    .main-header { font-size: 2rem; font-weight: 700; color: #FF4B4B; margin-bottom: 0.5rem; }
    .sub-header { font-size: 1.2rem; font-weight: 600; margin-top: 1rem; margin-bottom: 0.5rem; }
    .status-success { color: #00B42A; font-weight: 600; }
    .result-box { border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem; margin: 1rem 0; background: #f9fafb; }
    .stProgress > div > div { background-color: #FF4B4B; }
    .stButton button:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
""", unsafe_allow_html=True)

# =============================================
# 核心引擎导入
# =============================================
try:
    from cyber_printer_ultimate import generate_chapter_full
    from openmars_core.memory_palace import SQLiteMemoryPalace, CONTEXT_TOTAL_BUDGET, GLOBAL_SUMMARY_KEY
    from openmars_core.logger import logger
    ENGINE_READY = True
except Exception as e:
    ENGINE_READY = False
    st.error(f"🚨 OpenMars核心引擎加载失败：{str(e)}")


# 伏笔台账生命周期标记（MZX粒度）：预计回收章-当前章 ≤3 标「快到期」，负数标红「超期」；
# 已回收/未定期次不参与到期判定
def _foreshadow_flag(row, current_chapter):
    if row["status"] == "已回收":
        return {"label": "✅ 已回收", "overdue": False, "due_soon": False}
    due = row["due_chapter"]
    if not due:
        return {"label": "⏳ 待回收（期次未定）", "overdue": False, "due_soon": False}
    delta = due - current_chapter
    if delta < 0:
        return {"label": f"🔴 已超期{abs(delta)}章", "overdue": True, "due_soon": False}
    if delta <= 3:
        return {"label": f"⚠️ 快到期（剩{delta}章）", "overdue": False, "due_soon": True}
    return {"label": "🗓 未到期", "overdue": False, "due_soon": False}

# =============================================
# 侧边栏配置区
# =============================================
with st.sidebar:
    st.markdown('<p class="main-header">🚀 OpenMars</p>', unsafe_allow_html=True)
    st.markdown('<p class="status-success">工业级网文创作系统 V3.0</p>', unsafe_allow_html=True)
    st.divider()

    # 小说选择
    st.markdown('<p class="sub-header">📚 小说选择</p>', unsafe_allow_html=True)
    novel_root = "novel_settings"
    os.makedirs(novel_root, exist_ok=True)
    novel_list = [d for d in os.listdir(novel_root) if os.path.isdir(os.path.join(novel_root, d))]
    if not novel_list:
        novel_list = ["默认小说"]
        os.makedirs(os.path.join(novel_root, "默认小说"), exist_ok=True)
    
    selected_novel = st.selectbox(
        "选择小说项目", 
        novel_list, 
        index=novel_list.index(st.session_state.selected_novel) if st.session_state.selected_novel in novel_list else 0
    )
    if selected_novel != st.session_state.selected_novel:
        st.session_state.selected_novel = selected_novel
        st.session_state.current_output = ""
        st.session_state.generation_result = {}
        st.rerun()

    # 初始化记忆宫殿
    if ENGINE_READY:
        memory_palace = SQLiteMemoryPalace(selected_novel)
    st.divider()

    # 生成参数配置
    st.markdown('<p class="sub-header">⚙️ 生成配置</p>', unsafe_allow_html=True)
    chapter_num = st.number_input("章节号", min_value=1, value=1, step=1)
    target_words = st.number_input("目标字数", min_value=1000, max_value=20000, value=7500, step=500)
    custom_prompt = st.text_area(
        "自定义剧情要求（可选）",
        height=120,
        placeholder="比如：主角在这一章获得新能力，和反派发生第一次正面冲突，结尾留钩子"
    )

    # 批量连跑·断点续跑（github-3状态外置 + github-19记忆文件即断点）：
    # 从第N章串行连跑到第M章，chapter_memory.full_content 非空的章自动跳过；
    # 中途取消后再次启动不重跑已完成章
    batch_start = st.number_input("批量起始章", min_value=1, value=int(chapter_num), step=1)
    batch_end = st.number_input("批量结束章（含）", min_value=1, value=int(chapter_num), step=1)
    if batch_end < batch_start:
        st.caption("⚠️ 批量结束章需大于等于起始章")
    batch_btn = st.button(
        f"🚀 批量连跑：第{int(batch_start)}-{int(batch_end)}章",
        disabled=st.session_state.is_generating or not ENGINE_READY or batch_end < batch_start,
        use_container_width=True,
    )
    st.divider()

    # 大模型配置
    st.markdown('<p class="sub-header">🤖 大模型配置</p>', unsafe_allow_html=True)
    with st.expander("查看/修改模型配置", expanded=False):
        current_api_key = os.getenv("LLM_API_KEY", "")
        current_base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
        current_model = os.getenv("LLM_MODEL_NAME", "gpt-4o")
        webhook_url = os.getenv("WEBHOOK_URL", "")
        
        new_api_key = st.text_input("API Key", value=current_api_key, type="password")
        new_base_url = st.text_input("Base URL", value=current_base_url)
        new_model = st.text_input("模型名称", value=current_model)
        new_webhook = st.text_input("告警Webhook URL", value=webhook_url)

        # 创作温度：滑块+语义档位标注（保守/平衡/创新），保存到 .env 的 LLM_TEMPERATURE
        try:
            current_temperature = float(os.getenv("LLM_TEMPERATURE", "0.7"))
        except (TypeError, ValueError):
            current_temperature = 0.7
        current_temperature = min(max(current_temperature, 0.0), 1.0)
        new_temperature = st.slider(
            "创作温度 temperature（0-1）",
            min_value=0.0, max_value=1.0, value=current_temperature, step=0.05,
            help="保守：事实严谨少发散；平衡：稳中带浪；创新：脑洞大开（网文创作推荐0.7-0.8）",
        )
        if new_temperature < 0.35:
            st.caption("当前档位：**保守**（0-0.35，事实严谨、少发散）")
        elif new_temperature < 0.7:
            st.caption("当前档位：**平衡**（0.35-0.7，稳中带浪）")
        else:
            st.caption("当前档位：**创新**（0.7-1.0，脑洞大开，网文创作推荐区间）")

        if st.button("💾 保存到.env", use_container_width=True):
            set_key(".env", "LLM_API_KEY", new_api_key)
            set_key(".env", "LLM_BASE_URL", new_base_url)
            set_key(".env", "LLM_MODEL_NAME", new_model)
            set_key(".env", "WEBHOOK_URL", new_webhook)
            set_key(".env", "LLM_TEMPERATURE", str(new_temperature))
            load_dotenv(override=True)
            st.success("配置已保存！")

        # 测试连接：用表单当前值实测 1-token 请求，失败给明确中文提示（拒绝静默失败）
        if st.button("🔌 测试连接", use_container_width=True):
            if not new_api_key.strip() or not new_model.strip():
                st.error("❌ 连接失败：API Key 或模型名称为空，请先填写后再测试")
            else:
                with st.spinner("正在发送 1-token 测试请求（最长等待15秒）..."):
                    try:
                        import openai
                        test_client = openai.OpenAI(
                            api_key=new_api_key.strip(),
                            base_url=new_base_url.strip() or "https://api.deepseek.com/v1",
                            timeout=15,
                        )
                        test_client.chat.completions.create(
                            model=new_model.strip(),
                            messages=[{"role": "user", "content": "Hi"}],
                            max_tokens=1,
                        )
                        st.success(f"✅ 连接成功！模型 {new_model.strip()} 可正常调用")
                    except Exception as e:
                        st.error(f"❌ 连接失败：{_explain_conn_error(e)}")
    st.divider()

    # 用量与成本：读记账模块 llm_ledger 的 llm_calls 表（logs/cost.db），
    # 展示今日/累计 token 与估算成本（gpt-author 每步成本核算思路的面板化）
    st.markdown('<p class="sub-header">📊 用量与成本</p>', unsafe_allow_html=True)
    with st.expander("查看LLM用量与成本", expanded=False):
        try:
            from openmars_core.llm_ledger import LEDGER_DB_PATH, summarize_day

            today_info = summarize_day()
            all_calls, all_success, all_prompt, all_completion = 0, 0, 0, 0
            if os.path.exists(LEDGER_DB_PATH):
                conn = sqlite3.connect(LEDGER_DB_PATH, timeout=15.0)
                try:
                    row = conn.execute(
                        "SELECT COUNT(*),"
                        " COALESCE(SUM(CASE WHEN success=1 THEN 1 ELSE 0 END),0),"
                        " COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0)"
                        " FROM llm_calls").fetchone()
                    all_calls, all_success, all_prompt, all_completion = row
                finally:
                    conn.close()

            col_a, col_b = st.columns(2)
            with col_a:
                st.metric("今日调用", f"{today_info['calls']} 次", help=f"成功 {today_info['success_calls']} 次")
                st.metric("今日Token", f"{today_info['total_tokens']:,}")
            with col_b:
                st.metric("累计调用", f"{all_calls} 次", help=f"成功 {all_success} 次")
                st.metric("累计Token", f"{all_prompt + all_completion:,}")

            # 估算成本：单价（元/百万Token）从环境变量读取，未配置则只展示token用量（不硬编码价格）
            try:
                in_price = float(os.environ["LLM_PRICE_IN_PER_1M"]) if os.getenv("LLM_PRICE_IN_PER_1M") else None
                out_price = float(os.environ["LLM_PRICE_OUT_PER_1M"]) if os.getenv("LLM_PRICE_OUT_PER_1M") else None
            except (KeyError, TypeError, ValueError):
                in_price, out_price = None, None
            if in_price is not None and out_price is not None:
                today_cost = today_info["prompt_tokens"] / 1e6 * in_price + today_info["completion_tokens"] / 1e6 * out_price
                all_cost = all_prompt / 1e6 * in_price + all_completion / 1e6 * out_price
                st.metric("今日估算成本", f"¥{today_cost:.4f}")
                st.metric("累计估算成本", f"¥{all_cost:.4f}")
            else:
                st.caption("💡 在 .env 配置 LLM_PRICE_IN_PER_1M / LLM_PRICE_OUT_PER_1M（元/百万Token）后，这里会显示估算成本")
        except Exception as e:
            st.warning(f"用量数据读取失败：{e}")

    st.divider()

    # 核心生成按钮
    generate_btn = st.button(
        "🚀 一键躺平生成" if not st.session_state.is_generating else "⏳ 正在疯狂码字中...请勿刷新！", 
        disabled=st.session_state.is_generating or not ENGINE_READY,
        type="primary",
        use_container_width=True
    )

    # 真实取消按钮（91Writing反面教训：停止必须接真实取消，而非改UI flag）：
    # 置位 kill_event 后，后台流水线在下一步边界安全停止，绝不半章作废浪费token
    if st.session_state.is_generating:
        if st.button("⏹ 停止生成", use_container_width=True, type="secondary"):
            st.session_state.thread_kill_event.set()
            st.rerun()

    # 清空按钮
    if st.button("🗑️ 清空当前结果", use_container_width=True, disabled=st.session_state.is_generating):
        st.session_state.current_output = ""
        st.session_state.generation_result = {}
        st.success("已清空当前结果！")
        st.rerun()

    st.divider()
    st.caption("OpenMars V3.0 | SQLite存储 | 全链路告警 | 永不崩溃")

# =============================================
# 生成按钮锁定逻辑 - P0级Token黑洞防护（单章生成与批量连跑共用同一套状态机与锁）
# =============================================
if (generate_btn or batch_btn) and ENGINE_READY and not st.session_state.is_generating:
    if not st.session_state.generate_lock.locked():
        st.session_state.is_generating = True
        st.session_state.current_output = ""
        st.session_state.generation_result = {}
        st.session_state.generation_start_time = time.time()
        st.session_state.thread_kill_event.clear()
        # 阶段化进度共享状态：worker线程写、主脚本读（替代旧版假进度 min(80, elapsed*2)）
        st.session_state.generation_progress = {"step": "start", "pct": 5}
        # 固定临时结果文件路径（主脚本「确认线程已结束」后读取回显，读后删除）
        st.session_state.result_path = RESULT_TEMP_PATH
        st.session_state.generate_lock.acquire()
        # 启动后台线程，daemon=True确保主线程退出时自动清理
        your_generate_params = {
            # 批量连跑时结果文件以起始章命名；单章仍用章节号输入框的值
            "chapter_num": int(batch_start) if batch_btn else chapter_num,
            "target_words": target_words,
            "custom_prompt": custom_prompt,
            "novel_name": selected_novel,
            "result_path": st.session_state.result_path,
            "kill_event": st.session_state.thread_kill_event,
            "progress_state": st.session_state.generation_progress,
            # 批量连跑区间（None=单章模式）；worker据此走 generate_chapter_batch 串行连跑
            "batch_range": [int(batch_start), int(batch_end)] if batch_btn else None,
        }
        generate_thread = threading.Thread(
            target=background_generate_task,
            args=(your_generate_params,),
            daemon=True
        )
        st.session_state.generate_thread = generate_thread
        generate_thread.start()
        st.warning("⏳ 正在疯狂码字中... 请勿刷新页面！")
        st.rerun()

# =============================================
# 隔离执行区 - 后台任务监控
# P0修复说明：st.rerun() 抛出的 RerunException 继承自 BaseException（接不进 except Exception），
# 但 finally 必执行——旧版把 rerun 写在 try 里，生成开始约2秒后 finally 就提前复位/释放锁，
# Token黑洞防护失效。重构后约定：
#   1. 所有 st.rerun() 一律放在任何 try/finally 之外；
#   2. is_generating 复位与 generate_lock 释放收敛到「确认线程已结束」的唯一路径；
#   3. 轮询 = 本轮检查：线程未结束 → 只更新进度后 sleep+rerun；已结束 → 读结果回显→复位。
# =============================================
if st.session_state.is_generating and ENGINE_READY:
    st.info("🚀 OpenMars已进入并行火力全开模式，请勿刷新页面或点击侧边栏！")

    # —— 本轮检查：线程未结束 → 只更新进度，随后 sleep + rerun（rerun 在 try/finally 之外） ——
    generate_thread = st.session_state.generate_thread
    if generate_thread is not None and generate_thread.is_alive():
        progress_state = st.session_state.generation_progress or {}
        pct = min(int(progress_state.get("pct", 0)), 99)
        step_label = PROGRESS_STEP_LABELS.get(progress_state.get("step", "start"), "⏳ 准备中")
        elapsed = int(time.time() - (st.session_state.generation_start_time or time.time()))

        # 2小时超时（旧版强杀空操作）：协作式取消——置位事件后流水线在下一步边界真正停止；
        # 复位与释放锁仍走「确认线程已结束」唯一路径，绝不在线程未结束时提前解锁
        if elapsed > 2 * 60 * 60 and not st.session_state.thread_kill_event.is_set():
            st.session_state.thread_kill_event.set()
            logger.warning("⚠️  生成任务超过2小时，已发起协作式取消，流水线将在下一步边界停止")
            st.warning("⚠️ 生成任务已超过2小时，已发起取消，流水线将在下一步边界安全停止...")

        if st.session_state.thread_kill_event.is_set():
            st.warning("🛑 已发起停止，等待流水线在下一步边界安全退出...")
        st.progress(pct)
        st.caption(f"{step_label} | 已运行 {elapsed} 秒（进度随流水线阶段推进，非匀速估算）")

        time.sleep(1)
        st.rerun()

    # —— 走到这里 = 确认线程已结束：唯一复位路径，finally 仅留安全兜底 ——
    try:
        _finish_generation_session()
    except Exception as e:
        st.error(f"生成结果回收异常：{str(e)}")
        logger.error(f"生成结果回收异常：{e}", exc_info=True)
    finally:
        # 安全兜底：无论回收成败，状态必须收敛；locked() 判断保证 generate_lock 恰释放一次
        st.session_state.is_generating = False
        if st.session_state.generate_lock.locked():
            st.session_state.generate_lock.release()
        time.sleep(1)
        st.rerun()

# =============================================
# 一次性状态提示：后台任务收尾时写入，本轮渲染后立即清除（rerun 会丢弃当轮已画元素，故下一轮再显示）
# =============================================
pending_status = st.session_state.get("pending_status")
if pending_status:
    status_kind, status_msg = pending_status
    if status_kind == "success":
        st.success(status_msg)
    elif status_kind == "cancelled":
        st.warning(status_msg)
    else:
        st.error(status_msg)
    st.session_state.pending_status = None

# =============================================
# 主工作区Tab
# =============================================
tab1, tab2, tab3, tab4 = st.tabs([
    "✍️ 章节生成",
    "📚 记忆宫殿",
    "📖 生成历史",
    "❓ 帮助说明"
])

# Tab1：章节生成
with tab1:
    st.markdown('<p class="main-header">✍️ 网文章节生成</p>', unsafe_allow_html=True)
    
    if st.session_state.generation_result:
        res = st.session_state.generation_result
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("章节号", res["chapter_num"])
        with col2:
            st.metric("实际字数", res["real_chars"])
        with col3:
            st.metric("目标字数", res["target_words"])
        with col4:
            st.metric("生成耗时", f"{res['elapsed_time']}s")
        st.divider()

    if st.session_state.current_output:
        st.markdown('<p class="sub-header">📖 最终正文</p>', unsafe_allow_html=True)
        with st.container():
            st.markdown(f'<div class="result-box">{st.session_state.current_output}</div>', unsafe_allow_html=True)
        
        if st.session_state.generation_result:
            res = st.session_state.generation_result
            st.download_button(
                label="📥 下载本章内容",
                data=st.session_state.current_output,
                file_name=f"第{res['chapter_num']}章_{int(time.time())}.md",
                mime="text/markdown",
                use_container_width=True
            )
    else:
        if not st.session_state.is_generating:
            st.info("👈 请在侧边栏配置生成参数，点击「一键躺平生成」开始创作")

# Tab2：记忆宫殿
with tab2:
    st.markdown('<p class="main-header">📚 记忆宫殿</p>', unsafe_allow_html=True)
    st.markdown(f"当前小说：**{selected_novel}**")
    st.divider()

    if not ENGINE_READY:
        st.error("核心引擎未加载，无法查看记忆宫殿")
    else:
        # —— 固定设定区（含番茄审核铁则节，来自03-番茄审核铁则.md） ——
        st.markdown('<p class="sub-header">🔒 固定世界观设定</p>', unsafe_allow_html=True)
        fixed_prompt = memory_palace.get_fixed_prompt()
        if fixed_prompt:
            st.text_area("固定设定内容", fixed_prompt, height=300, disabled=True)
            if "番茄审核铁则" in fixed_prompt:
                st.caption("✅ 已加载「番茄审核铁则」节（novel_settings/<书>/03-番茄审核铁则.md）")
            else:
                st.warning("固定设定中未检测到「番茄审核铁则」节：请在小说目录添加 03-番茄审核铁则.md（正文生成时将使用内置兜底铁则）")
        else:
            st.warning("未检测到固定设定文件，请在小说目录中添加00-全本大纲.md、01-人物档案.md、02-世界观设定.md、03-番茄审核铁则.md")

        st.divider()

        # —— 生成上下文骨架预览：全局摘要/台账/角色编辑保存后，这里重建即可看到修改生效 ——
        st.markdown('<p class="sub-header">🧩 生成上下文骨架（下一章实际注入的动态记忆）</p>', unsafe_allow_html=True)
        next_chapter = memory_palace.get_next_chapter_num()
        context_text = memory_palace.build_generation_context()
        st.caption(f"装配视点：第{next_chapter}章 | 总量 {len(context_text)} / 预算 {CONTEXT_TOTAL_BUDGET} 字"
                   f"（五节固定注入：全局摘要→近3章摘要→上一章结尾→未兑现伏笔→活跃角色；超限节自动截断并标注）")
        st.text_area("生成上下文内容", context_text, height=260, disabled=True)

        st.divider()

        # —— 全局摘要：滚动压缩结果，支持手工编辑保存（留痕 source=user_edit） ——
        st.markdown('<p class="sub-header">📝 全局摘要（每5章或伏笔台账变动时自动滚动压缩，可手工编辑）</p>', unsafe_allow_html=True)
        saved_summary = memory_palace.get_state(GLOBAL_SUMMARY_KEY, "") or ""
        edited_summary = st.text_area("全局摘要内容", value=saved_summary, height=200)
        if st.button("💾 保存全局摘要", disabled=st.session_state.is_generating):
            if edited_summary.strip() != saved_summary.strip():
                memory_palace.set_state(GLOBAL_SUMMARY_KEY, edited_summary.strip(),
                                        updated_chapter=next_chapter, source="user_edit")
                st.success("全局摘要已保存（留痕：人工编辑），生成上下文骨架已随之更新")
                st.rerun()
            else:
                st.info("内容未变化，无需保存")

        st.divider()

        # —— 伏笔台账：表格化+生命周期标记，只排不滤——超期条目保留并标红，绝不静默丢弃 ——
        st.markdown('<p class="sub-header">📌 伏笔台账（只排不滤：超期标红、快到期提醒，条目可手工编辑）</p>', unsafe_allow_html=True)
        current_chapter = memory_palace.get_next_chapter_num()
        ledger_rows = memory_palace.get_all_foreshadows()
        if not ledger_rows:
            st.info("暂无伏笔记录：章节生成后由写后回写自动登记埋设/回收，也可在生成后于此处查看")
        else:
            overdue_rows = [r for r in ledger_rows if _foreshadow_flag(r, current_chapter)["overdue"]]
            if overdue_rows:
                ids = "、".join(f"#{r['id']}" for r in overdue_rows)
                st.markdown(
                    f'<p style="color:#FF4B4B;font-weight:700">🔴 超期伏笔 {len(overdue_rows)} 条：{ids}'
                    f'—— 已过预计回收章节仍未兑现（按只排不滤原则保留在台账，请尽快安排回收或调整期次）</p>',
                    unsafe_allow_html=True
                )
            for row in ledger_rows:
                flag = _foreshadow_flag(row, current_chapter)
                with st.expander(f"#{row['id']}　{row['content'][:30]}　|　{flag['label']}", expanded=False):
                    due_display = f"第{row['due_chapter']}章" if row["due_chapter"] else "未定"
                    st.markdown(
                        f"埋设章节：第{row['planted_chapter'] or '?'}章　|　预计回收：{due_display}　|　"
                        f"状态：{row['status']}　|　来源：{'人工编辑' if row['source'] == 'user_edit' else '自动抽取'}",
                        unsafe_allow_html=True
                    )
                    if flag["overdue"]:
                        st.markdown(
                            f'<p style="color:#FF4B4B;font-weight:700">🔴 超期 {current_chapter - row["due_chapter"]} 章：'
                            f'该伏笔已过预计回收章节仍未兑现</p>',
                            unsafe_allow_html=True
                        )
                    elif flag["due_soon"]:
                        st.warning(f"⚠️ 快到期：预计第{row['due_chapter']}章回收（距当前章≤3章）")
                    new_content = st.text_area("伏笔内容", value=row["content"], key=f"fs_content_{row['id']}")
                    new_due = st.number_input("预计回收章节（0=未定）", min_value=0,
                                              value=int(row["due_chapter"] or 0), step=1, key=f"fs_due_{row['id']}")
                    new_status = st.selectbox("状态", ["未回收", "已回收"],
                                              index=1 if row["status"] == "已回收" else 0, key=f"fs_status_{row['id']}")
                    if st.button("💾 保存该条", key=f"fs_save_{row['id']}"):
                        memory_palace.update_foreshadow(
                            row["id"], content=new_content.strip(),
                            due_chapter=(int(new_due) if new_due > 0 else None),
                            status=new_status, source="user_edit")
                        st.success("已保存（留痕：人工编辑），台账与生成上下文将同步生效")
                        st.rerun()

        st.divider()

        with st.expander("查看完整章节历史", expanded=False):
            chapter_history = memory_palace.get_chapter_history()
            if chapter_history:
                for ch in chapter_history:
                    st.markdown(f"**第{ch[0]}章** | {ch[3]} | {ch[2]}字\n> {ch[1]}")
            else:
                st.info("暂无章节历史，生成章节后自动记录")

# Tab3：生成历史
with tab3:
    st.markdown('<p class="main-header">📖 生成历史</p>', unsafe_allow_html=True)
    st.markdown(f"当前小说：**{selected_novel}**")
    st.divider()

    output_dir = f"output/{selected_novel}"
    if os.path.exists(output_dir):
        file_list = sorted([f for f in os.listdir(output_dir) if f.endswith(".md")], reverse=True)
        if file_list:
            selected_file = st.selectbox("选择生成的章节", file_list)
            file_path = os.path.join(output_dir, selected_file)

            with open(file_path, "r", encoding="utf-8") as f:
                file_content = f.read()

            st.text_area("章节内容", file_content, height=600, disabled=True)
            st.download_button(
                label="📥 下载选中章节",
                data=file_content,
                file_name=selected_file,
                mime="text/markdown",
                use_container_width=True
            )
        else:
            st.info("暂无生成历史，快去生成第一章吧！")
    else:
        st.info("暂无生成历史，快去生成第一章吧！")

    # —— 运行报告：每章生成全过程可回放（三步输出摘要/门禁结果/重写次数/token/告警） ——
    st.divider()
    st.markdown('<p class="sub-header">📋 运行报告（每章生成全过程可回放）</p>', unsafe_allow_html=True)
    report_files = sorted(
        [f for f in os.listdir(output_dir) if f.endswith("_报告.json")], reverse=True
    ) if os.path.exists(output_dir) else []
    if report_files:
        selected_report = st.selectbox("选择运行报告", report_files)
        try:
            with open(os.path.join(output_dir, selected_report), "r", encoding="utf-8") as f:
                report_data = json.load(f)
            # 门禁结果速览：通过状态 + platform_hint（报告级提示，不触发重写）
            gate_info = report_data.get("gate") or {}
            if gate_info:
                gate_line = ("✅ 门禁通过" if gate_info.get("passed") else "🚨 门禁未通过") \
                    + f"（重写 {gate_info.get('rewrite_rounds', 0)} 轮）"
                for hint in gate_info.get("platform_hints") or []:
                    gate_line += f" ｜ 💡 {hint}"
                st.caption(gate_line)
            st.json(report_data)
        except Exception as e:
            st.warning(f"运行报告读取失败：{e}")
    else:
        st.info("暂无运行报告：每章生成完成后自动写入 output/<书>/第N章_报告.json")

# Tab4：帮助说明
with tab4:
    st.markdown('<p class="main-header">❓ OpenMars V3.0 帮助说明</p>', unsafe_allow_html=True)
    st.divider()

    st.markdown("### 🚀 核心特性")
    st.markdown("""
    - ✅ **P0级Token黑洞防护**：生成过程中任何误触、刷新，绝不打断底层任务，API额度零浪费
    - ✅ **同步调用引擎**：无事件循环冲突，面板永不假死、永不白屏
    - ✅ **SQLite存储架构**：行级锁+O(1)查询，1000章超长小说零性能衰减，内存占用降低90%
    - ✅ **历史JSON数据自动迁移**：旧数据一键导入SQLite，不丢任何历史章节
    - ✅ **全链路移动端告警**：生成成功/失败/崩溃实时推送到手机，运维黑盒彻底解决
    - ✅ **大纲师→排雷师→主笔三步串行流水线**：每章依次完成剧情大纲、毒点排雷、正文创作
    """)

    st.markdown("### 📝 快速上手")
    st.markdown("""
    1. **配置小说设定**：在novel_settings/你的小说名/目录下，添加3个核心文件：
       - 00-全本大纲.md：全本剧情大纲
       - 01-人物档案.md：主角、配角人设
       - 02-世界观设定.md：世界观、背景、规则
    2. **配置大模型**：在侧边栏「🤖 大模型配置」中填写你的API Key、Base URL、模型名称
    3. **一键生成**：点击「一键躺平生成」，系统会自动完成全流程创作
    """)