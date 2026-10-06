# =============================================
# OpenMars | 零 token 正则质量门禁 + 可选一致性审校
# 设计依据（重构调研结论）：
#   - chinese-novelist-skill（github-3）：规则校验落成脚本而非模型自述——字数 gate 程序判定、
#     失败自动重写设上限、超限标注不阻塞；
#   - ExplosiveCoderflome（github-5）：0 token 正则文笔检测层，全程不花一次 LLM 调用；
#   - sepia（github-6）+ bili-4：机械替换产出病句被证伪——只做频次治理+白名单防误伤
#     （单次命中不算、同一种累计出现才判），绝不程序化改写正文；
#   - million-word（github-19）：静默降级事故要求「不达标绝不静默」；
#   - show-me-the-story（github-14）：核查失败三层降级（硬门禁失败→定向重写≤2轮→标注+告警）；
#   - denova（github-13）：全程可见——门禁结果逐条进运行报告供回放。
# r2 修订：字数门禁从 target_words 推导（硬门禁 = 交付字数 ∈ [0.7×target, target+10%]），
#   番茄建议区间 2500-4500 降级为报告级 platform_hint，绝不触发重写——否则与面板默认
#   target_words=7500（openmars_panel.py）及长章（≥5000）逐段路径构造性冲突，
#   每次默认生成都会空转两轮重写。
# =============================================
import bisect
import json
import os
import re
from dataclasses import dataclass, field

from .config import get_config
from .logger import logger

# =============================================
# 字数硬门禁（r2 修订：从 target_words 推导，而非按番茄固定区间卡死）
#   硬门禁 = 交付字数 ∈ [0.7×target, target+10%]；
#   target 未提供/非法时兜底读配置 max_chapter_words（MAX_CHAPTER_WORDS）
# =============================================
WORDS_FLOOR_RATIO = 0.7   # 硬门禁下限系数：交付字数 ≥ 0.7×target
WORDS_CEIL_RATIO = 1.1    # 硬门禁上限系数：交付字数 ≤ target+10%

# 番茄平台建议区间：只产出报告级 platform_hint（进运行报告与告警文案），绝不影响 passed、绝不触发重写
TOMATO_SUGGESTED_MIN = 2500        # 建议区间下限
DEFAULT_PLATFORM_MAX_WORDS = 4500  # 建议区间上限兜底（正常应读配置 MAX_CHAPTER_WORDS）

# =============================================
# AI 味套话频次治理（白名单原则：单次命中不算、同一种累计出现才判）
# =============================================
DEFAULT_FLAVOR_MAX_HITS = 1  # 同一种套话全章允许出现次数上限，超过判违规

# 默认套话词表 (名称, 正则)：纯词组直接匹配，句式用正则表达
DEFAULT_FLAVOR_PATTERNS = [
    ("不是…而是句式", r"不是[^。！？!?\n]{1,24}[，,][^。！？!?\n]{0,6}而是"),
    ("瞳孔骤缩", r"瞳孔骤缩"),
    ("瞳孔地震", r"瞳孔地震"),
    ("指节泛白", r"指节泛白"),
    ("眼里亮起光芒", r"眼里亮起光芒"),
    ("眼中闪过一丝", r"眼中闪过一丝"),
    ("空气仿佛凝固", r"空气仿佛凝固"),
    ("嘴角勾起一抹弧度", r"嘴角勾起一抹弧度"),
]

# 长段判定：连续 N 段超过 C 字判「长段堆叠」（手机端阅读体验差）
LONG_PARA_CHARS = 300
LONG_PARA_RUN = 3

# 对话比例门禁（可选开关，默认关；可在 05-质量规则.md 开启）
DIALOGUE_RATIO_MIN = 0.30

# 一致性审校可疑点条数上限
CONSISTENCY_SUSPECT_MAX = 10

_WHITESPACE_RE = re.compile(r"\s+")
_PARA_LINE_RE = re.compile(r"[^\n]+")
# 对话引文（中文引号族，含套书名号）
_DIALOGUE_RE = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|‘[^’]*’")


def count_words(text) -> int:
    """字数口径：剔除全部空白后的字符数（与中文网文平台字数统计习惯一致）"""
    return len(_WHITESPACE_RE.sub("", text or ""))


# =============================================
# 质量规则集：默认值来自内置词表与调研结论，
# 可被 novel_settings/<书>/05-质量规则.md 覆盖（规则+词表两级）
# =============================================
@dataclass
class QualityRules:
    flavor_patterns: list = field(default_factory=lambda: list(DEFAULT_FLAVOR_PATTERNS))
    flavor_max_hits: int = DEFAULT_FLAVOR_MAX_HITS   # 同一种套话允许出现次数上限（≥2判违规）
    long_para_chars: int = LONG_PARA_CHARS           # 单段超长阈值（字）
    long_para_run: int = LONG_PARA_RUN               # 连续多少段超长判堆叠
    dialogue_ratio_enabled: bool = False             # 对话比例门禁开关（可选）
    dialogue_ratio_min: float = DIALOGUE_RATIO_MIN   # 对话占比下限


# 05-质量规则.md 阈值键名 → QualityRules 字段（宽容别名表，与毒点字段卡同一套哲学）
_THRESHOLD_INT_KEYS = {
    "套话频次上限": "flavor_max_hits", "频次上限": "flavor_max_hits",
    "长段阈值": "long_para_chars", "长段字数": "long_para_chars",
    "连续长段": "long_para_run", "连续长段判定": "long_para_run",
}
_THRESHOLD_FLOAT_KEYS = {
    "对话比例下限": "dialogue_ratio_min",
}
_THRESHOLD_BOOL_KEYS = {
    "对话比例门禁": "dialogue_ratio_enabled", "对话比例检查": "dialogue_ratio_enabled",
}
# 列表/序号装饰前缀（剥除用，与场景计划字段卡同一套）
_BULLET_PREFIX_RE = re.compile(r"^(?:[#>\-*•·・\s【\[]+|\d+\s*[.、)）]\s*)+")
_TRUE_WORDS = ("开", "true", "yes", "on", "1")
_FALSE_WORDS = ("关", "false", "no", "off", "0")


def _parse_ratio(raw):
    """比例值解析：'30%' / '0.3' / 0.3 → float；非法返回 None"""
    raw = str(raw).strip().rstrip("%。")
    if raw.endswith("%"):
        try:
            return max(0.0, min(1.0, float(raw[:-1]) / 100))
        except ValueError:
            return None
    try:
        v = float(raw)
        return v if 0 < v <= 1 else (v / 100 if 1 < v <= 100 else None)
    except ValueError:
        return None


def _parse_bool(raw):
    """开关值解析：开/关/true/false；不认识返回 None"""
    normalized = str(raw).strip().lower()
    if normalized in _TRUE_WORDS:
        return True
    if normalized in _FALSE_WORDS:
        return False
    return None


def _apply_threshold(rules, key, value):
    """把「键：值」阈值行应用到规则集；未知键忽略（宽容解析，绝不因规则文件写错而报错）"""
    key = key.strip()
    value = value.strip()
    try:
        if key in _THRESHOLD_INT_KEYS:
            setattr(rules, _THRESHOLD_INT_KEYS[key], int(float(value)))
        elif key in _THRESHOLD_FLOAT_KEYS:
            ratio = _parse_ratio(value)
            if ratio is not None:
                setattr(rules, _THRESHOLD_FLOAT_KEYS[key], ratio)
        elif key in _THRESHOLD_BOOL_KEYS:
            flag = _parse_bool(value)
            if flag is not None:
                setattr(rules, _THRESHOLD_BOOL_KEYS[key], flag)
    except (TypeError, ValueError):
        logger.warning(f"⚠️  05-质量规则.md 阈值行无法解析，已忽略：{key}：{value}")


def load_quality_rules(novel_name=None):
    """读取 novel_settings/<书>/05-质量规则.md 覆盖默认规则与词表。

    支持的写法（宽容解析，缺文件/写错一律回落默认，绝不报错）：
        ## 套话词表            ← 覆盖默认套话词表，每行一条：「名称：正则」或裸词
        - 自创套话A
        - 不是……就是句式：不是[^。]{1,10}就是
        ## 追加套话词表        ← 在默认词表基础上追加
        ## 阈值覆盖
        - 套话频次上限：1
        - 长段阈值：300
        - 连续长段判定：3
        - 对话比例门禁：开
        - 对话比例下限：30%
    """
    rules = QualityRules()
    if not novel_name:
        return rules
    path = os.path.join("novel_settings", str(novel_name), "05-质量规则.md")
    if not os.path.exists(path):
        return rules
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception as e:
        logger.warning(f"⚠️  读取质量规则覆盖文件失败，使用默认规则：{e}")
        return rules

    mode = None  # None / "word_override" / "word_append" / "thresholds"
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            header = line.lstrip("#").strip()
            if "套话词表" in header:
                mode = "word_append" if "追加" in header else "word_override"
                if mode == "word_override":
                    rules.flavor_patterns = []
            elif "阈值" in header:
                mode = "thresholds"
            else:
                mode = None
            continue
        entry = _BULLET_PREFIX_RE.sub("", line).strip()
        if not entry or entry.startswith("```"):
            continue
        if mode in ("word_override", "word_append"):
            # 「名称：正则」或裸词（名称即词）
            if "：" in entry:
                name, _, pattern = entry.partition("：")
            elif ":" in entry:
                name, _, pattern = entry.partition(":")
            else:
                name, pattern = entry, entry
            name, pattern = name.strip(), pattern.strip()
            if name:
                rules.flavor_patterns.append((name, pattern))
        elif mode == "thresholds":
            sep = "：" if "：" in entry else ":"
            key, _, value = entry.partition(sep)
            _apply_threshold(rules, key, value)
    if not rules.flavor_patterns:
        # 覆盖写成了空词表：回落默认，绝不把词表清空成「永不判套话」
        logger.warning("⚠️  05-质量规则.md 套话词表为空，回落默认词表")
        rules.flavor_patterns = list(DEFAULT_FLAVOR_PATTERNS)
    logger.info(f"✅ 已加载质量规则覆盖：{path}（套话{len(rules.flavor_patterns)}条）")
    return rules


# =============================================
# 门禁结果对象
# =============================================
@dataclass
class GateViolation:
    """一条硬门禁违规：计数+位置+定向重写建议（回喂重写prompt）"""
    rule: str          # 规则标识：word_count / ai_flavor:<套话名> / long_paragraph / dialogue_ratio
    message: str       # 人读违规描述（含计数与位置）
    count: int = 1     # 命中计数
    location: str = "" # 位置描述（第N段等）
    suggestion: str = ""  # 定向重写建议

    def as_dict(self):
        return {"rule": self.rule, "message": self.message, "count": self.count,
                "location": self.location, "suggestion": self.suggestion}


@dataclass
class GateReport:
    """validate_chapter 输出：passed 只由硬门禁决定，platform_hint 永不影响（r2）"""
    passed: bool
    target_words: int
    actual_words: int
    words_floor: int
    words_ceil: int
    violations: list = field(default_factory=list)   # 硬门禁违规（GateViolation，触发重写）
    platform_hints: list = field(default_factory=list)  # 平台建议提示（str，绝不触发重写）
    checks: list = field(default_factory=list)       # 全部检查项逐条结果（全程可见）

    def as_dict(self):
        return {
            "passed": self.passed,
            "target_words": self.target_words,
            "actual_words": self.actual_words,
            "words_floor": self.words_floor,
            "words_ceil": self.words_ceil,
            "violations": [v.as_dict() for v in self.violations],
            "platform_hints": list(self.platform_hints),
            "checks": list(self.checks),
        }


# =============================================
# 正则检查工具（全部零 token：不花一次 LLM 调用）
# =============================================
def _split_paragraphs(text):
    """切分段落：返回 [(段内起始偏移, 段文本stripped), ...]，空行跳过"""
    paras = []
    for m in _PARA_LINE_RE.finditer(text or ""):
        stripped = m.group().strip()
        if stripped:
            paras.append((m.start(), stripped))
    return paras


def _locate_offsets(paras, offsets, max_show=6):
    """把命中偏移映射到段落号：'第2、5、9段'；超过 max_show 处截断加「等N处」"""
    starts = [s for s, _ in paras]
    indexes = []
    for offset in offsets:
        idx = bisect.bisect_right(starts, offset) - 1
        if idx >= 0 and idx not in indexes:
            indexes.append(idx)
    if not indexes:
        return ""
    shown = indexes[:max_show]
    text = "、".join(f"第{i + 1}段" for i in shown)
    if len(indexes) > max_show:
        text += f"等{len(indexes)}处"
    return text


def _find_pattern_hits(text, pattern):
    """按正则找全部命中：返回 [(偏移, 命中文本), ...]"""
    compiled = re.compile(pattern)
    return [(m.start(), m.group()) for m in compiled.finditer(text or "")]


def _find_long_para_runs(paras, chars_limit, run_min):
    """找「连续 run_min 段以上超长」的段落区段：返回 [(起始段下标, 结束段下标), ...]"""
    runs = []
    run_start = None
    for i, (_, p) in enumerate(paras):
        is_long = count_words(p) > chars_limit
        if is_long and run_start is None:
            run_start = i
        elif not is_long and run_start is not None:
            if i - run_start >= run_min:
                runs.append((run_start, i - 1))
            run_start = None
    if run_start is not None and len(paras) - run_start >= run_min:
        runs.append((run_start, len(paras) - 1))
    return runs


def _dialogue_ratio(text, actual_words):
    """对话占比：引号内非空白字符数 / 全章非空白字符数"""
    if not actual_words:
        return 0.0
    quoted = "".join(m.group() for m in _DIALOGUE_RE.finditer(text or ""))
    return count_words(quoted) / actual_words


def _normalize_target(target_words, cfg):
    """r2：字数门禁基准 = 显式 target；未提供/非法时兜底读配置 max_chapter_words"""
    try:
        t = int(target_words)
        if t > 0:
            return t
    except (TypeError, ValueError):
        pass
    try:
        fallback = int(cfg.max_chapter_words)
        if fallback > 0:
            return fallback
    except (TypeError, ValueError, AttributeError):
        pass
    return DEFAULT_PLATFORM_MAX_WORDS


def validate_chapter(text, target_words=None, rules=None, novel_name=None, config=None):
    """零 token 正则质量门禁：对一章正文跑四类检查，返回 GateReport。

    ① 字数硬门禁（r2 从 target_words 推导）：交付字数 ∉ [0.7×target, target+10%] 判违规；
       target 未提供/非法时以配置 max_chapter_words 为兜底基准；
    ② AI 味套话频次：白名单原则——单次命中不算，同一种累计超过 flavor_max_hits 才判，
       报告逐条给计数与位置（第N段）；绝不程序化替换（sepia/bili-4教训）；
    ③ 长段堆叠：连续 long_para_run 段超过 long_para_chars 字判违规；
    ④ 对话比例：可选开关（默认关，可在 05-质量规则.md 开启）；
    platform_hint：交付字数超出番茄建议区间时仅产出报告级提示，绝不影响 passed、绝不触发重写。
    规则与词表可被 novel_settings/<书>/05-质量规则.md 覆盖（传 novel_name 自动加载）。
    门禁器自身异常时显式返回「未通过」并留违规项——不达标绝不静默放行。
    """
    try:
        cfg = config or get_config()
        if rules is None:
            rules = load_quality_rules(novel_name) if novel_name else QualityRules()
        actual = count_words(text)
        target = _normalize_target(target_words, cfg)
        floor = int(target * WORDS_FLOOR_RATIO)
        ceil = int(target * WORDS_CEIL_RATIO)
        violations = []
        checks = []
        paras = _split_paragraphs(text)

        # —— ① 字数硬门禁 ——
        word_ok = floor <= actual <= ceil
        checks.append({"rule": "word_count", "passed": word_ok, "count": actual})
        if not word_ok:
            violations.append(GateViolation(
                rule="word_count",
                message=f"交付{actual}字，超出硬门禁区间[{floor}-{ceil}]字（target={target}）",
                count=1, location="",
                suggestion=(f"充实场景细节与对话，把全章补到{floor}字以上" if actual < floor
                            else f"收紧冗余叙述，把全章压到{ceil}字以内")))

        # —— ② AI 味套话频次（白名单：单次命中不算，累计频次才判） ——
        for name, pattern in rules.flavor_patterns:
            try:
                hits = _find_pattern_hits(text, pattern)
            except re.error as e:
                logger.warning(f"⚠️  套话正则「{name}」非法，已跳过该词：{e}")
                continue
            ok = len(hits) <= rules.flavor_max_hits
            checks.append({"rule": f"ai_flavor:{name}", "passed": ok, "count": len(hits)})
            if not ok:
                loc = _locate_offsets(paras, [h[0] for h in hits])
                violations.append(GateViolation(
                    rule=f"ai_flavor:{name}",
                    message=f"AI味套话「{name}」出现{len(hits)}次（上限{rules.flavor_max_hits}次），位置：{loc}",
                    count=len(hits), location=loc,
                    suggestion=(f"保留至多{rules.flavor_max_hits}次自然使用，其余改写为多样化的"
                                "动作/细节/心理描写（不要生硬避词）")))

        # —— ③ 长段堆叠（连续 N 段超过 C 字） ——
        runs = _find_long_para_runs(paras, rules.long_para_chars, rules.long_para_run)
        checks.append({"rule": "long_paragraph", "passed": not runs, "count": len(runs)})
        for run_start, run_end in runs:
            violations.append(GateViolation(
                rule="long_paragraph",
                message=(f"连续{run_end - run_start + 1}段超过{rules.long_para_chars}字"
                         f"（第{run_start + 1}-{run_end + 1}段），手机端阅读体验差"),
                count=run_end - run_start + 1, location=f"第{run_start + 1}-{run_end + 1}段",
                suggestion=f"把超长段拆短到{rules.long_para_chars}字以内，关键情绪与反转独立成段"))

        # —— ④ 对话比例（可选开关） ——
        if rules.dialogue_ratio_enabled:
            ratio = _dialogue_ratio(text, actual)
            ok = ratio >= rules.dialogue_ratio_min
            checks.append({"rule": "dialogue_ratio", "passed": ok, "count": round(ratio, 3)})
            if not ok:
                violations.append(GateViolation(
                    rule="dialogue_ratio",
                    message=f"对话占比{ratio:.0%}低于下限{rules.dialogue_ratio_min:.0%}",
                    count=1, location="",
                    suggestion="增加人物对话推进剧情，减少大段独白式叙述"))

        # —— platform_hint：番茄建议区间（仅报告级提示，绝不触发重写） ——
        hints = []
        try:
            platform_max = int(cfg.max_chapter_words)
        except (TypeError, ValueError, AttributeError):
            platform_max = DEFAULT_PLATFORM_MAX_WORDS
        if platform_max <= 0:
            platform_max = DEFAULT_PLATFORM_MAX_WORDS
        if platform_max < TOMATO_SUGGESTED_MIN:
            platform_max = TOMATO_SUGGESTED_MIN
        if actual < TOMATO_SUGGESTED_MIN or actual > platform_max:
            hints.append(f"交付{actual}字，超出番茄建议区间 {TOMATO_SUGGESTED_MIN}-{platform_max}"
                         "（仅报告级提示，不触发重写）")

        return GateReport(passed=not violations, target_words=target, actual_words=actual,
                          words_floor=floor, words_ceil=ceil, violations=violations,
                          platform_hints=hints, checks=checks)
    except Exception as e:
        # 门禁器自身异常：显式判不通过并留违规项，绝不静默放行（million-word静默降级教训）
        logger.error(f"🚨 质量门禁器内部异常，按未通过处理：{e}")
        fallback = GateViolation(rule="gate_internal_error",
                                 message=f"质量门禁器内部异常：{e}", count=1, location="",
                                 suggestion="请检查05-质量规则.md与配置，或人工复核本章")
        return GateReport(passed=False, target_words=int(target_words or 0), actual_words=0,
                          words_floor=0, words_ceil=0, violations=[fallback],
                          platform_hints=[], checks=[{"rule": "gate_internal_error", "passed": False, "count": 1}])


def format_gate_violations(gate):
    """把硬门禁违规清单渲染成编号文本（回喂重写prompt与告警内容共用）。

    message 自身已含计数与位置描述，这里不再重复拼接 location。
    """
    return "\n".join(f"{i}. [{v.rule}] {v.message}" for i, v in enumerate(gate.violations, 1))


# 运行报告/正文尾部标注的统一标记（测试与人工检索锚点）
GATE_ANNOTATION_MARK = "【质量门禁告警·非正文】"


def format_gate_annotation(gate, rewrite_rounds):
    """门禁最终未通过时的显式标注块（追加在交付正文尾部，绝不静默降级）"""
    lines = [
        "---",
        f"⚠️{GATE_ANNOTATION_MARK}本章经{rewrite_rounds}轮定向重写后仍有{len(gate.violations)}项"
        "硬门禁未通过，按「不达标绝不静默」原则显式标注，请人工复核后删除本告警块：",
    ]
    lines += [f"{i}. {v.message}" for i, v in enumerate(gate.violations, 1)]
    if gate.platform_hints:
        lines.append(f"平台提示：{'；'.join(gate.platform_hints)}")
    return "\n".join(lines)


# =============================================
# 可选一致性审校（独立步骤，不进生成 prompt）
#   - 对照人物档案（01-人物档案.md）+ 伏笔台账 + 角色状态，输出连续性可疑点清单
#     （如「角色第N章已死亡但本章出场」），写入运行报告供人工复核；
#   - CONTENT_SAFETY_CHECK=false 时零额外 LLM 调用，直接返回 disabled；
#   - 审校失败显式留痕（返回 error），绝不阻断章节交付（show-me-the-story三层降级）。
# =============================================
def parse_consistency_review(raw_text):
    """一致性审校输出容错解析：剥围栏后取最外层JSON的 suspects 数组。

    字段缺省兜底为空、超长按上限钳制；解析不出抛 ValueError，由调用方显式留痕告警
    （禁止静默吞掉审校失败——million-word教训）。
    """
    text = str(raw_text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"审校输出中未找到JSON对象：{text[:60]!r}")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"审校输出不是合法JSON：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("审校输出JSON顶层必须是对象")
    suspects = []
    for item in (data.get("suspects") or [])[:CONSISTENCY_SUSPECT_MAX]:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail") or item.get("描述") or "").strip()[:200]
        if not detail:
            continue
        suspects.append({
            "type": (str(item.get("type") or "未分类").strip() or "未分类")[:20],
            "detail": detail,
            "evidence": str(item.get("evidence") or "").strip()[:200],
        })
    return suspects


def run_consistency_review(engine, memory, novel_name, chapter_num, content, model=None):
    """执行一次一致性审校（独立LLM调用），返回结果字典供运行报告落盘。

    - engine：具备 call_llm_sync(user, system, step=, chapter=, model=) 的调用方（SyncQueryEngine）；
    - memory：SQLiteMemoryPalace（提供人物档案固定设定 / 伏笔台账 / 角色状态）；
    - 开关关闭：零额外 LLM 调用，返回 {"enabled": False, ...}；
    - 审校失败：捕获并返回 {"enabled": True, "error": ...}，绝不抛出、绝不阻断交付。
    """
    cfg = get_config()
    if not cfg.content_safety_check:
        return {"enabled": False, "suspects": [], "error": "",
                "note": "CONTENT_SAFETY_CHECK=false，一致性审校未执行（零额外LLM调用）"}
    try:
        character_block = getattr(memory, "fixed_memory", {}).get("character") or {}
        character_profile = (character_block.get("content") or "").strip()
        ledger = memory.get_all_foreshadows()
        ledger_text = "\n".join(
            f"- #{f['id']} {f['content']}（埋设第{f['planted_chapter'] or '?'}章，状态：{f['status']}）"
            for f in ledger) or "（暂无伏笔台账记录）"
        characters = memory.get_characters(limit=20)
        state_text = "\n".join(
            f"- {c['name']}：{c['state']}（更新至第{c['updated_chapter']}章）"
            for c in characters) or "（暂无角色状态记录）"
        # 延迟导入：prompts 具名模板统一收口，避免模块环
        from .prompts import render_consistency_review_prompts
        system_prompt, user_prompt = render_consistency_review_prompts(
            character_profile=character_profile or "（未提供人物档案）",
            foreshadow_ledger=ledger_text,
            characters_state=state_text,
            chapter_num=chapter_num,
            chapter_content=content or "（正文缺失）",
        )
        raw = engine.call_llm_sync(user_prompt, system_prompt,
                                   step="consistency_review", chapter=chapter_num, model=model)
        suspects = parse_consistency_review(str(raw))
        logger.info(f"🔍 第{chapter_num}章一致性审校完成：{len(suspects)}处可疑点")
        return {"enabled": True, "suspects": suspects, "error": "",
                "note": "可疑点仅供人工复核，未进入生成prompt"}
    except Exception as e:
        # 审校失败：显式留痕（由调用方告警），绝不阻断章节交付
        logger.error(f"🚨 第{chapter_num}章一致性审校失败（已留痕，不阻断交付）：{e}")
        return {"enabled": True, "suspects": [], "error": str(e)[:300],
                "note": "审校失败已留痕，请人工复核或重试"}
