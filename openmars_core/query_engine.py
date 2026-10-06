import random
import re
import time

from .config import get_config
from .llm_ledger import estimate_tokens, record_llm_call
from .logger import logger
from .prompts import (
    CONTINUATION_TAIL_CHARS,
    LONG_CHAPTER_THRESHOLD,
    OUTLINE_RETRY_SUFFIX,
    REVIEW_RETRY_SUFFIX,
    build_default_scene_plan,
    format_mine_list,
    format_scene_plan,
    is_scene_plan_bad,
    parse_mine_list,
    parse_scene_plan,
    render_body_prompts,
    render_outline_prompts,
    render_review_prompts,
    resolve_tomato_rules,
)
# 三套具名模板（OUTLINE/REVIEW/BODY 的 SYSTEM+USER 对）：渲染函数包裹它们，
# 单独一条 import 显式固化 query_engine → prompts.py 的模板依赖，禁止再内联拼 prompt；
# 本文件不直接使用这五个常量，noqa: F401 为有意保留（常量被删除时此处导入即报错，依赖固化不失效）。
# noqa 置于语句首行：flake8 对多行 import 的 F401 报在起始行，名称行尾标注不生效
from .prompts import (  # noqa: F401
    BODY_SYSTEM,
    OUTLINE_SYSTEM,
    OUTLINE_USER,
    REVIEW_SYSTEM,
    REVIEW_USER,
)


class LLMCallError(RuntimeError):
    """LLM调用最终失败异常：调用层统一失败出口——成功返回str、失败抛本异常，绝不隐式返回None"""

    def __init__(self, message: str, model: str = "", status_code=None, attempts: int = 0):
        super().__init__(message)
        self.model = model
        self.status_code = status_code
        self.attempts = attempts


class StepError(LLMCallError):
    """流水线步骤失败异常：step_name 标识失败步骤（outline=大纲 / review=排雷 / content=正文）"""

    STEP_DISPLAY = {"outline": "大纲", "review": "排雷", "content": "正文"}

    def __init__(self, step_name: str, message: str = ""):
        self.step_name = step_name
        display = self.STEP_DISPLAY.get(step_name, step_name)
        super().__init__(message or f"{display}步骤失败")


class PipelineCancelled(RuntimeError):
    """流水线被取消异常：kill_event 置位后在步骤边界抛出（协作式取消，
    绝不打断单步LLM调用中途，避免半章作废浪费token）"""


class CleanedResponse(str):
    """str子类：调用方按普通字符串直接使用，同时携带truncated标记（finish_reason=length 时为True）"""

    truncated = False

    def __new__(cls, value: str, truncated: bool = False):
        obj = super().__new__(cls, value)
        obj.truncated = truncated
        return obj


# 推理模型（DeepSeek-R1等）会把思考过程包在 <think>…</think> 里混进正文
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# 整段被```围栏包裹的响应（部分模型无视"只输出正文"的指令）
_CODE_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL)

# 滑窗续写防复读：归一化时剔除的空白与标点（只比对句子骨架，避免标点差异漏判）
_OVERLAP_PUNCT_RE = re.compile(r"[\s，。！？!?…、；;：:「」『』“”‘’\"'（）()【】\[\]—\-·]+")
# 防复读最小重叠骨架字数：低于该值不剪，防止把正常接续误伤（bili-4教训：机械误伤被证伪，修剪必须保守）
_MIN_OVERLAP_CHARS = 6


def _trim_overlap(prev_text: str, new_text: str) -> str:
    """逐段拼接防复读：新段开头若复述了已写文本结尾（整句或半句），剪掉重叠骨架再拼接。

    做法：取已写文本结尾的归一化骨架，找它与新段开头骨架的最长后缀-前缀重叠；
    只剪「连续命中已写结尾」的前缀，绝不改动新段其余内容。整段都在复述时返回空串，由调用方重试。
    """
    if not prev_text or not new_text:
        return new_text
    ref_tail = _OVERLAP_PUNCT_RE.sub("", prev_text)[-500:]
    new_norm = _OVERLAP_PUNCT_RE.sub("", new_text)
    max_check = min(len(ref_tail), len(new_norm), 300)
    overlap = 0
    for length in range(max_check, _MIN_OVERLAP_CHARS - 1, -1):
        if ref_tail.endswith(new_norm[:length]):
            overlap = length
            break
    if not overlap:
        return new_text
    # 把归一化骨架的重叠字数映射回未归一化原文：数到第overlap个骨架字处截断
    kept = 0
    for pos, ch in enumerate(new_text):
        if not _OVERLAP_PUNCT_RE.fullmatch(ch):
            kept += 1
            if kept == overlap:
                return new_text[pos + 1:].lstrip("，。！？…；、：: \n\t") or ""
    return ""  # 整段都是复述


class SyncQueryEngine:
    def __init__(self):
        config = get_config()
        self.api_key = config.llm_api_key
        self.base_url = config.llm_base_url
        self.model_name = config.llm_model_name
        self.max_retries = config.max_retry
        self.temperature = config.llm_temperature
        self.max_tokens = config.llm_max_tokens
        self.timeout = config.llm_timeout

    @staticmethod
    def _clean_response(raw: str) -> str:
        """响应清洗层：剥 <think>…</think> 思考标签与整段代码围栏（借鉴 AI_NovelGenerator invoke_with_cleaning）"""
        if not raw:
            return ""
        text = _THINK_RE.sub("", raw)
        text = text.strip()
        fence = _CODE_FENCE_RE.match(text)
        if fence:
            text = fence.group(1).strip()
        return text

    @staticmethod
    def _parse_retry_after(exc):
        """解析限流响应的 Retry-After 头（秒），解析失败返回None回退指数退避"""
        try:
            raw = exc.response.headers.get("retry-after")
            if raw is None:
                return None
            return min(float(raw), 60.0)  # 兜底上限60秒，防止异常大值卡死流水线
        except Exception:
            return None

    def _rate_limit_wait(self, attempt: int, exc) -> float:
        """限流退避：优先尊重 Retry-After 头，否则指数退避+随机抖动"""
        retry_after = self._parse_retry_after(exc)
        if retry_after is not None and retry_after > 0:
            return retry_after + random.uniform(0, 1)
        return min(2 ** attempt, 30) + random.uniform(0, 1)

    def _sleep_before_retry(self, wait_time: float, attempt: int):
        # 最后一次尝试失败后无需再等，直接进入抛错收尾
        if attempt >= self.max_retries - 1:
            return
        time.sleep(wait_time)

    def _record(self, step, chapter, latency_ms, success, prompt_tokens, completion_tokens, model=None):
        """调用记账：每次LLM调用写一行进logs/cost.db；记账失败只告警，绝不影响生成主流程"""
        try:
            record_llm_call(
                step=step, model=model or self.model_name,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                latency_ms=latency_ms, success=success, chapter=chapter
            )
        except Exception as e:
            logger.warning(f"⚠️  LLM调用记账失败（不影响生成）: {e}")

    def _record_failure(self, step, chapter, started, est_prompt_tokens, model=None):
        """失败尝试也记账：耗时照记，completion按0计"""
        latency_ms = int((time.monotonic() - started) * 1000)
        self._record(step, chapter, latency_ms, False, est_prompt_tokens, 0, model=model)

    @staticmethod
    def _resolve_tokens(response, est_prompt_tokens: int, raw_content: str):
        """优先取接口返回的usage，缺失时按启发式估算（中文≈1.5 token/字）"""
        usage = getattr(response, "usage", None)
        prompt_usage = getattr(usage, "prompt_tokens", None) if usage else None
        completion_usage = getattr(usage, "completion_tokens", None) if usage else None
        est_completion_tokens = estimate_tokens(raw_content)
        prompt_tokens = prompt_usage if isinstance(prompt_usage, int) and prompt_usage > 0 else est_prompt_tokens
        completion_tokens = completion_usage if isinstance(completion_usage, int) and completion_usage > 0 else est_completion_tokens
        return prompt_tokens, completion_tokens

    def call_llm_sync(self, user_prompt: str, system_prompt: str = "你是专业助手",
                      step: str = "unknown", chapter=None, model: str = None) -> str:
        # 契约：成功返回清洗后的str（CleanedResponse，带truncated标记），失败一律抛LLMCallError
        # model：可选模型覆盖（写后记忆抽取等辅助调用走便宜模型），缺省用引擎主模型
        # 使用同步客户端，彻底避免异步上下文问题
        import openai
        use_model = model or self.model_name
        # 超时时间大于0才传给客户端，0表示使用OpenAI SDK默认超时
        client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            **({"timeout": self.timeout} if self.timeout > 0 else {})
        )

        # 记账用：usage缺失时按启发式估算prompt token
        est_prompt_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)
        last_error = None
        empty_retried = False
        attempt = 0

        while attempt < self.max_retries:
            started = time.monotonic()
            try:
                response = client.chat.completions.create(
                    model=use_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=self.temperature,
                    top_p=0.9,
                    # 单轮最大输出Token数大于0才传给接口，0表示交由服务商决定
                    **({"max_tokens": self.max_tokens} if self.max_tokens > 0 else {})
                )
            except openai.RateLimitError as e:
                # 限流：指数退避+jitter，优先尊重服务端Retry-After头
                wait_time = self._rate_limit_wait(attempt, e)
                status_code = getattr(e, "status_code", None)
                logger.warning(f"⚠️  触发限流(HTTP {status_code}) | 模型: {use_model} | "
                               f"第{attempt + 1}/{self.max_retries}次尝试，等待{wait_time:.1f}秒后重试")
                last_error = e
                self._record_failure(step, chapter, started, est_prompt_tokens, model=use_model)
                self._sleep_before_retry(wait_time, attempt)
                attempt += 1
                continue
            except openai.APIStatusError as e:
                status_code = getattr(e, "status_code", None)
                if status_code is not None and 400 <= status_code < 500:
                    # 4xx客户端错误（401认证失败/400参数错误等）：重试注定失败，零退避直接抛
                    self._record_failure(step, chapter, started, est_prompt_tokens, model=use_model)
                    logger.error(f"❌ 客户端错误(HTTP {status_code})，不再重试 | 模型: {use_model} | 错误: {e}")
                    raise LLMCallError(
                        f"LLM调用失败(HTTP {status_code}): {e}",
                        model=use_model, status_code=status_code, attempts=attempt + 1
                    ) from e
                # 5xx服务端错误：退避后重试
                wait_time = 1 * (attempt + 1)
                logger.warning(f"⚠️  服务端错误(HTTP {status_code}) | 模型: {use_model} | "
                               f"第{attempt + 1}/{self.max_retries}次尝试，等待{wait_time}秒后重试")
                last_error = e
                self._record_failure(step, chapter, started, est_prompt_tokens, model=use_model)
                self._sleep_before_retry(wait_time, attempt)
                attempt += 1
                continue
            except (openai.APITimeoutError, openai.APIConnectionError) as e:
                # 超时/连接错误：按MAX_RETRY重试
                wait_time = 1 * (attempt + 1)
                logger.warning(f"⚠️  连接异常 | 模型: {use_model} | "
                               f"第{attempt + 1}/{self.max_retries}次尝试，等待{wait_time}秒后重试，错误: {e}")
                last_error = e
                self._record_failure(step, chapter, started, est_prompt_tokens, model=use_model)
                self._sleep_before_retry(wait_time, attempt)
                attempt += 1
                continue
            except LLMCallError:
                raise  # 主动抛出的调用失败（如空内容）不再重试
            except Exception as e:
                # 未知异常：保守起见仍重试，耗尽后抛LLMCallError
                wait_time = 1 * (attempt + 1)
                logger.warning(f"⚠️  调用异常({type(e).__name__}) | 模型: {use_model} | "
                               f"第{attempt + 1}/{self.max_retries}次尝试，等待{wait_time}秒后重试，错误: {e}")
                last_error = e
                self._record_failure(step, chapter, started, est_prompt_tokens, model=use_model)
                self._sleep_before_retry(wait_time, attempt)
                attempt += 1
                continue

            # —— 成功拿到响应：清洗 + finish_reason检查 + 记账 ——
            latency_ms = int((time.monotonic() - started) * 1000)
            choice = response.choices[0] if getattr(response, "choices", None) else None
            raw_content = ""
            if choice is not None and getattr(choice, "message", None) is not None:
                raw_content = choice.message.content or ""
            cleaned = self._clean_response(raw_content)
            finish_reason = (getattr(choice, "finish_reason", "") or "") if choice is not None else ""

            prompt_tokens, completion_tokens = self._resolve_tokens(response, est_prompt_tokens, raw_content)
            self._record(step, chapter, latency_ms, bool(cleaned), prompt_tokens, completion_tokens, model=use_model)

            if not cleaned:
                # 空响应：记警告并额外重试一次（不消耗重试预算），绝不把空串当正文往下传
                logger.warning(f"⚠️  模型返回内容为空 | 模型: {use_model} | "
                               f"finish_reason: {finish_reason or 'unknown'}")
                if not empty_retried:
                    empty_retried = True
                    time.sleep(1)
                    continue
                raise LLMCallError("模型连续2次返回空内容", model=self.model_name, attempts=attempt + 1)

            truncated = finish_reason == "length"
            if truncated:
                # 不静默接受被max_tokens截断的半章（反面参照million-word截断降级事故）
                logger.warning(f"⚠️  响应被max_tokens截断(finish_reason=length) | 模型: {use_model} | "
                               f"已返回{len(cleaned)}字，结果带truncated标记")
            return CleanedResponse(cleaned, truncated)

        # 重试耗尽：显式抛出，不再隐式返回None
        logger.error(f"❌ LLM调用重试{self.max_retries}次后仍失败 | 模型: {use_model} | 最后错误: {last_error}")
        raise LLMCallError(
            f"LLM调用重试{self.max_retries}次后仍失败: {last_error}",
            model=use_model,
            attempts=self.max_retries
        ) from last_error

    def _generate_segment(self, user_prompt, system_prompt, chapter_num, overlap_reference):
        """生成单个场景段：拼接前做防复读修剪；整段都在复述已写文本时重试一次。返回(文本, 是否截断)"""
        try:
            seg = self.call_llm_sync(user_prompt, system_prompt, step="content", chapter=chapter_num)
        except LLMCallError as e:
            raise StepError("content", f"正文步LLM调用失败: {e}") from e
        assert seg is not None and seg.strip(), "正文步返回空内容"
        truncated = bool(getattr(seg, "truncated", False))
        seg_text = _trim_overlap(overlap_reference, str(seg))
        if not seg_text.strip():
            # 模型整段在复述已写文本：重试一次，仍复述则原文收下（宁多勿缺，绝不空段拼接）
            logger.warning(f"⚠️  第{chapter_num}章某个场景段整体复述已写文本，重试一次")
            try:
                seg = self.call_llm_sync(user_prompt, system_prompt, step="content", chapter=chapter_num)
            except LLMCallError as e:
                raise StepError("content", f"正文步重试LLM调用失败: {e}") from e
            assert seg is not None and seg.strip(), "正文步重试返回空内容"
            truncated = truncated or bool(getattr(seg, "truncated", False))
            seg_text = _trim_overlap(overlap_reference, str(seg)) or str(seg)
        return seg_text, truncated

    def run_chapter_pipeline(self, chapter_num: int, target_words: int, fixed_memory: str,
                             dynamic_memory: str, custom_prompt: str,
                             ending_excerpt: str = "", tomato_rules: str = "",
                             kill_event=None, progress_cb=None):
        # 大纲师→排雷师→主笔三步串行流水线，返回字典键保持 outline/review/content/real_chars
        # 提示词全部来自 prompts.py 具名模板；长章（≥LONG_CHAPTER_THRESHOLD）按场景配额逐段生成（LongWriter思路）
        # ending_excerpt/tomato_rules 为可选注入：上一章结尾衔接块（维度4记忆库提供）与番茄审核铁则
        # kill_event/progress_cb 为可选注入（面板协作式取消与阶段化进度，旧调用方完全不传则行为不变）
        logger.info(f"⚡ 启动单章生成模式 | XML边界+字数配额 | 章节: {chapter_num} | 目标: {target_words}字")

        # —— 协作式取消与阶段化进度（MuMuAINovel阶段化进度协议） ——
        # 取消只在三步骤边界检查生效：绝不打断单步LLM调用中途，已产出的半步不至于白花token；
        # 进度段位：大纲 10→30、排雷 30→40、正文 40→95（长章逐段续写时在正文段内线性细化）
        def _check_cancelled():
            if kill_event is not None and kill_event.is_set():
                raise PipelineCancelled(f"第{chapter_num}章生成已被用户取消")

        def _report(step, pct):
            if progress_cb is None:
                return
            try:
                progress_cb(step, pct)
            except Exception as e:
                logger.warning(f"⚠️  进度回调失败（不影响生成）: {e}")

        # —— 大纲步：剧情大纲 + 场景计划字段卡 ——
        _check_cancelled()
        _report("outline", 10)
        outline_system, outline_prompt = render_outline_prompts(
            fixed_memory=fixed_memory, dynamic_memory=dynamic_memory,
            chapter_num=chapter_num, custom_prompt=custom_prompt, target_words=target_words)

        # 同步执行，稳扎稳打跑单章生成，避免并发
        try:
            outline = self.call_llm_sync(outline_prompt, outline_system, step="outline", chapter=chapter_num)
        except LLMCallError as e:
            raise StepError("outline", f"大纲步LLM调用失败: {e}") from e
        # None断言：从类型上杜绝None被字面拼进正文prompt（历史事故：<chapter_outline>\nNone\n）
        assert outline is not None and outline.strip(), "大纲步返回空内容"

        # 场景计划容错解析；场景数3-15越界判坏计划并重生成一次，仍失败用默认配额计划兜底
        scenes = parse_scene_plan(str(outline), target_words)
        if is_scene_plan_bad(scenes):
            logger.warning(f"⚠️  第{chapter_num}章场景计划不合格（解析到{len(scenes)}个场景），重新生成一次")
            try:
                outline = self.call_llm_sync(outline_prompt + OUTLINE_RETRY_SUFFIX, outline_system,
                                             step="outline", chapter=chapter_num)
            except LLMCallError as e:
                raise StepError("outline", f"大纲步重生成LLM调用失败: {e}") from e
            assert outline is not None and outline.strip(), "大纲步重生成返回空内容"
            scenes = parse_scene_plan(str(outline), target_words)
            if is_scene_plan_bad(scenes):
                logger.warning(f"⚠️  第{chapter_num}章场景计划重生成仍不合格，启用默认字数配额计划兜底")
                scenes = build_default_scene_plan(target_words)
        scene_plan_text = format_scene_plan(scenes, target_words)
        _report("outline", 30)
        total_quota = sum(s["words"] for s in scenes)
        logger.info(f"📋 第{chapter_num}章场景计划：{len(scenes)}场，配额合计{total_quota}字")
        if target_words > 0 and abs(total_quota - target_words) > 0.3 * target_words:
            logger.warning(f"⚠️  场景配额合计{total_quota}字偏离目标{target_words}字超过±30%，实际字数可能不达标")

        # —— 排雷步：固定四类毒点字段卡 ——
        # 添加短暂延迟，避免触发模型并发限制
        _check_cancelled()
        time.sleep(1)
        _report("review", 30)

        review_system, review_prompt = render_review_prompts(
            fixed_memory=fixed_memory, dynamic_memory=dynamic_memory,
            chapter_num=chapter_num, chapter_outline=str(outline))
        try:
            review = self.call_llm_sync(review_prompt, review_system, step="review", chapter=chapter_num)
        except LLMCallError as e:
            raise StepError("review", f"排雷步LLM调用失败: {e}") from e
        assert review is not None and review.strip(), "排雷步返回空内容"

        # 排雷输出容错解析：既无毒点条目也无「未发现+排查理由」判坏输出，重试一次
        mines = parse_mine_list(str(review))
        if not mines["valid"]:
            logger.warning(f"⚠️  第{chapter_num}章排雷输出不符合字段卡格式，重试一次")
            try:
                review = self.call_llm_sync(review_prompt + REVIEW_RETRY_SUFFIX, review_system,
                                            step="review", chapter=chapter_num)
            except LLMCallError as e:
                raise StepError("review", f"排雷步重试LLM调用失败: {e}") from e
            assert review is not None and review.strip(), "排雷步重试返回空内容"
            retried_mines = parse_mine_list(str(review))
            if retried_mines["valid"]:
                mines = retried_mines
            else:
                # 两次都不合格：退回把原始排雷文本整段交给正文步规避，绝不中断流水线
                logger.warning(f"⚠️  第{chapter_num}章排雷重试仍不合格，退回原始文本兜底")
        strict_warnings = format_mine_list(mines) if mines["valid"] else str(review)
        _report("review", 40)

        # —— 正文步：短章整章单次调用，长章按场景配额逐段续写 ——
        # 添加短暂延迟，避免触发模型并发限制
        _check_cancelled()
        time.sleep(1)
        _report("content", 40)

        body_kwargs = dict(
            chapter_num=chapter_num, target_words=target_words,
            fixed_memory=fixed_memory, dynamic_memory=dynamic_memory,
            chapter_outline=str(outline), scene_plan=scene_plan_text,
            strict_warnings=strict_warnings,
            tomato_rules=resolve_tomato_rules(tomato_rules, fixed_memory),
            ending_excerpt=ending_excerpt or "",
        )

        truncated_any = False
        body_calls = 1
        if target_words >= LONG_CHAPTER_THRESHOLD and len(scenes) >= 2:
            # 长章逐段续写（LongWriter：单次怼7500字已被调研证伪，后半截必崩）：
            # 每段prompt=原指令+完整场景计划+已写全文尾部(≤2000字)+本段配额
            segments = []
            prev_text = ""
            for i, scene in enumerate(scenes):
                is_last = i == len(scenes) - 1
                # 已写全文尾部只携带最近2000字（≤CONTINUATION_TAIL_CHARS），render层还有一次兜底截断
                written_tail = prev_text[-CONTINUATION_TAIL_CHARS:] if prev_text else ""
                system_prompt, user_prompt = render_body_prompts(
                    segment=(i + 1, scene["gist"], scene["words"], is_last),
                    written_tail=written_tail, **body_kwargs)
                # 防复读参照系：首段对照上一章结尾，后续段对照已写全文
                seg_text, seg_truncated = self._generate_segment(
                    user_prompt, system_prompt, chapter_num, prev_text or (ending_excerpt or ""))
                truncated_any = truncated_any or seg_truncated
                segments.append(seg_text)
                prev_text = "\n\n".join(segments)
                # 正文段内线性细化进度：40→95，随场景段推进（非随时间匀速）
                _report("content", min(95, 40 + int(55 * (i + 1) / len(scenes))))
                logger.info(f"✍️  第{chapter_num}章场景{i + 1}/{len(scenes)}完成，本段配额{scene['words']}字")
            final_content = "\n\n".join(segments)
            body_calls = len(scenes)
        else:
            # 短章：维持整章单次调用
            system_prompt, user_prompt = render_body_prompts(**body_kwargs)
            try:
                final_content = self.call_llm_sync(user_prompt, system_prompt, step="content", chapter=chapter_num)
            except LLMCallError as e:
                raise StepError("content", f"正文步LLM调用失败: {e}") from e
            assert final_content is not None and final_content.strip(), "正文步返回空内容"
            truncated_any = bool(getattr(final_content, "truncated", False))
            final_content = str(final_content)
            _report("content", 95)

        if truncated_any:
            logger.warning(f"⚠️  第{chapter_num}章正文疑似被max_tokens截断，结尾可能不完整，建议人工检查")

        return {"outline": str(outline), "review": str(review), "content": final_content,
                "real_chars": len(final_content), "truncated": truncated_any,
                "body_calls": body_calls}

def get_engine():
    return SyncQueryEngine()
