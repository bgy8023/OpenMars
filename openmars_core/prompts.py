# =============================================
# OpenMars | 三步流水线提示词模板（大纲 → 排雷 → 正文）
# 三步 prompt 全部具名收口在本文件，query_engine 只负责渲染与调用，不再内联拼 f-string
# 模板设计依据（重构调研结论）：
#   - LongWriter（github-8）：「计划即字数配额」——大纲步产出逐场景字数配额，长章按配额逐段续写，
#     中间场景禁止总结收尾；逐段携带已写全文尾部递增上下文，防止跨段断裂
#   - AI_NovelGenerator（github-1）：固定字段卡 + 容错解析器，对 GLM-4-Flash 等免费模型
#     远比 strict JSON 可靠（免费模型普遍吐不合法 JSON）
#   - 91Writing（github-9）：=== 分节模板 + user prompt 末尾统一【警告·绝对不能】禁止清单
#   - Long-Novel-GPT（github-11）：稳定大块前置的缓存友好布局——设定/铁则等不变内容靠前，
#     逐章变化内容居中，逐段变化内容靠后，命中服务商 prompt 缓存省钱提速
#   - writing-agent（github-16）：评审意见四要素（位置/原文引用/伤害说明/规避动作）+ 禁止凑问题
#   - 51mazi（github-15）/ chinese-novelist-skill（github-3）：上一章末尾只作文风衔接、
#     时间线冲突以章纲为准；正文语态文字放在 prompt 末尾（动笔前最后读到的应是正文语态）
#   - sepia（github-6）+ bili-4（466条评论AI味清单）：AI味规则只做频次治理不做词表替换
# =============================================
import json
import math
import os
import re

from .logger import logger

# ---- 字数配额常量（LongWriter：计划即字数配额） ----
SCENE_WORDS_MIN = 200        # 单场景配额下限（字）
SCENE_WORDS_MAX = 1000       # 单场景配额上限（字）：贴近免费模型单次稳定输出能力，防截断
SCENE_COUNT_MIN = 3          # 场景计划场景数下限，越界判坏计划
SCENE_COUNT_MAX = 15         # 场景计划场景数上限，越界判坏计划
LONG_CHAPTER_THRESHOLD = 5000    # 目标字数达到该值走逐段生成，否则整章单次调用
CONTINUATION_TAIL_CHARS = 2000   # 逐段续写时携带的「已写全文尾部」上限（字）
ENDING_EXCERPT_CHARS = 800       # 「上一章结尾」衔接块原文上限（字）

# 大纲步场景计划不合格时的重生成提示尾缀（只重生成一次，见 query_engine）
OUTLINE_RETRY_SUFFIX = (
    "\n\n（系统提示：上一次的场景计划不合格。请严格按「场景N-要点-字数」逐行重新输出，"
    f"场景总数{SCENE_COUNT_MIN}-{SCENE_COUNT_MAX}个，单场字数{SCENE_WORDS_MIN}-{SCENE_WORDS_MAX}，配额总和≈目标字数。）"
)

# 排雷步输出不合格时的重试提示尾缀（只重试一次，见 query_engine）
REVIEW_RETRY_SUFFIX = (
    "\n\n（系统提示：上一次输出不符合格式要求。请严格按「【毒点N】+五个字段」逐条重新输出；"
    "确无问题则只输出一行「未发现-排查理由」，禁止凑数。）"
)


# =============================================
# 大纲步（OUTLINE）：剧情大纲 + 场景计划字段卡
# =============================================
OUTLINE_SYSTEM = (
    "你是网文界白金大纲师，擅长设计节奏紧凑、爽点密集的章节结构。"
    "你的任务是把一章拆解成带字数配额的场景计划，供主笔逐场景写作。"
)

OUTLINE_USER = """<context>
<static_worldview>
{fixed_memory}
</static_worldview>
<recent_events>
{dynamic_memory}
</recent_events>
</context>
<user_request>
{custom_prompt}
</user_request>

=== 本章任务 ===
请基于<context>与<user_request>，为第{chapter_num}章制定剧情大纲，并把本章拆解为「场景计划字段卡」。本章目标全章约{target_words}字。

=== 场景计划字段卡格式（每行一张卡，严格按此格式） ===
场景1-主角在拍卖会上被当众羞辱，发现家传玉佩出现在仇人手中-800
场景2-主角暗中跟踪仇人，撞破其与长老的私会-750

=== 字段卡硬性要求 ===
- 每行以「场景N」开头（N为1开始的连续编号），用 - 或 ： 分隔三个字段；
- 第二列为该场一句话冲突/事件要点；
- 第三列为该场字数配额，取值必须在{scene_words_min}-{scene_words_max}字之间；
- 场景总数必须在{scene_count_min}-{scene_count_max}个之间；
- 所有场景配额加总必须≈{target_words}字（偏差±30%以内）；
- 字段卡之前可先用三五行概述本章简纲，但字段卡必须逐行排布、便于逐行解析。

【警告·绝对不能】
- 绝对不能输出{scene_count_min}-{scene_count_max}个场景数量范围之外的计划；
- 绝对不能让任何单场配额超出{scene_words_min}-{scene_words_max}字；
- 绝对不能让配额总和偏离{target_words}字±30%；
- 绝对不能在字段卡之外输出大段设定科普或剧情解释。
"""


def render_outline_prompts(fixed_memory, dynamic_memory, chapter_num, custom_prompt, target_words):
    """渲染大纲步 SYSTEM+USER；占位符全部由本函数填充，调用方拿去即用"""
    user = OUTLINE_USER.format(
        fixed_memory=fixed_memory or "（暂无固定设定）",
        dynamic_memory=dynamic_memory or "无前置剧情",
        chapter_num=chapter_num,
        custom_prompt=(custom_prompt or "").strip(),
        target_words=target_words,
        scene_words_min=SCENE_WORDS_MIN,
        scene_words_max=SCENE_WORDS_MAX,
        scene_count_min=SCENE_COUNT_MIN,
        scene_count_max=SCENE_COUNT_MAX,
    )
    return OUTLINE_SYSTEM, user


# =============================================
# 排雷步（REVIEW）：固定四类毒点 + 四要素字段卡 + 禁止凑数
# =============================================
REVIEW_SYSTEM = (
    "你是网文界金牌主编，专门排查人设崩塌、前后矛盾、逻辑漏洞的问题。"
    "你只报告真实存在、有依据的问题，绝不为了显得尽责而凑数。"
)

REVIEW_USER = """<context>
<static_worldview>
{fixed_memory}
</static_worldview>
<recent_events>
{dynamic_memory}
</recent_events>
</context>

=== 待审材料 ===
<chapter_outline>
{chapter_outline}
</chapter_outline>

=== 本章任务 ===
请对照<context>，逐类排查第{chapter_num}章<chapter_outline>中的毒点。固定排查以下四类：人设崩塌、时间线矛盾、设定冲突、伏笔问题。

=== 毒点字段卡格式（每条毒点一张卡） ===
【毒点1】
类别：人设崩塌/时间线矛盾/设定冲突/伏笔问题 四选一
位置：场景N
原文引用：「从<chapter_outline>中原样摘抄的短句」
伤害说明：如果照这样写，会怎样伤害正文与读者体验
规避动作：写正文时的具体规避做法

=== 输出硬性要求 ===
- 四类逐一排查，但某类确无问题就不要为它硬凑条目；
- 每条毒点必须能在<context>或<chapter_outline>中找到依据，并附原文引用；
- 确实没有问题：只输出一行「未发现-排查理由」，说明四类各自的排查依据。

【警告·绝对不能】
- 绝对不能编造<context>或<chapter_outline>里不存在的问题凑数；
- 绝对不能输出没有原文引用的空泛条目；
- 绝对不能输出「未发现」却不写排查理由；
- 绝对不能跳过四类中的任何一类不排查。
"""


def render_review_prompts(fixed_memory, dynamic_memory, chapter_num, chapter_outline):
    """渲染排雷步 SYSTEM+USER；占位符全部由本函数填充"""
    user = REVIEW_USER.format(
        fixed_memory=fixed_memory or "（暂无固定设定）",
        dynamic_memory=dynamic_memory or "无前置剧情",
        chapter_num=chapter_num,
        chapter_outline=chapter_outline,
    )
    return REVIEW_SYSTEM, user


# =============================================
# 排雷输出容错解析（writing-agent四要素：位置/原文引用/伤害说明/规避动作）
# =============================================
# 固定四类毒点
MINE_CATEGORIES = ("人设崩塌", "时间线矛盾", "设定冲突", "伏笔问题")

# 毒点字段别名表（canonical → 宽容别名），免费模型字段名五花八门，一律兜住
_MINE_FIELD_ALIASES = {
    "category": ("类别", "类型", "分类", "毒点类型", "问题类型", "所属类别"),
    "location": ("位置", "地点", "场景", "场景位置", "出现位置", "出处"),
    "quote": ("原文引用", "原文", "引用", "引用原文", "原文摘录"),
    "harm": ("伤害说明", "伤害", "危害", "危害说明", "危害分析", "影响", "风险"),
    "action": ("规避动作", "规避", "规避方案", "规避方式", "修改建议", "建议", "对策", "处理"),
}
_MINE_FIELD_LOOKUP = {alias: key for key, aliases in _MINE_FIELD_ALIASES.items() for alias in aliases}

# 毒点条目起始行（markdown前缀剥除后「【毒点1】」会变成「毒点1】」，两种形态都要兜住；
# 裸写「毒点N」必须独占一行或以冒号结尾，防止把「毒点类型：…」误判成新条目）
_MINE_HEAD_RE = re.compile(r"^(?:【\s*毒点\s*\d*\s*】.*|毒点\s*\d+\s*】.*|毒点\s*\d+\s*[:：]?)$")

# 「未发现」探测（空清单必须携带排查理由）
_NO_ISSUE_RE = re.compile(r"(未发现|无毒点|没有发现|无问题|未见毒点)")

# 类别简称 → 固定四类归一化
_MINE_CATEGORY_HINTS = (
    ("人设", "人设崩塌"), ("ooc", "人设崩塌"),
    ("时间线", "时间线矛盾"), ("剧情矛盾", "时间线矛盾"), ("前后矛盾", "时间线矛盾"), ("逻辑", "时间线矛盾"),
    ("设定", "设定冲突"), ("世界观", "设定冲突"),
    ("伏笔", "伏笔问题"), ("钩子", "伏笔问题"), ("铺垫", "伏笔问题"),
)


def _normalize_mine_category(raw):
    """把模型输出的类别名归一到固定四类；归一不上就保留原文"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    for fixed in MINE_CATEGORIES:
        if fixed in raw:
            return fixed
    for hint, fixed in _MINE_CATEGORY_HINTS:
        if hint in raw.lower():
            return fixed
    return raw


def _extract_no_issue_reason(lines):
    """提取「未发现-排查理由」：优先取同一行分隔符后的说明，缺了取下一行非字段文本；无理由返回空串，没提未发现返回None"""
    for i, raw in enumerate(lines):
        line = _MARKDOWN_PREFIX_RE.sub("", raw.strip()).strip()
        if not line or _MINE_HEAD_RE.match(line):
            continue
        if not _NO_ISSUE_RE.search(line):
            continue
        parts = re.split(r"[:：\-—－、,，。]", line, maxsplit=1)
        reason = parts[1].strip() if len(parts) > 1 else ""
        if not reason or _NO_ISSUE_RE.search(reason):
            # 同行没带实质理由（或只是复述「未发现…毒点」），向下找一行非字段文本当理由
            for nxt in lines[i + 1:]:
                nxt_clean = _MARKDOWN_PREFIX_RE.sub("", nxt.strip()).strip()
                if not nxt_clean:
                    continue
                head = re.split(r"[:：]", nxt_clean, 1)[0].strip()
                if _MINE_HEAD_RE.match(nxt_clean) or head in _MINE_FIELD_LOOKUP:
                    break
                reason = nxt_clean
                break
        return reason or ""
    return None


def parse_mine_list(review_text):
    """排雷输出容错解析（不用strict JSON，免费模型友好）。

    - 毒点条目：【毒点N】字段卡，字段=类别/位置/原文引用/伤害说明/规避动作（走别名表，缺字段补空串）
    - 空清单：必须出现「未发现」且携带排查理由，否则判坏输出（valid=False，由调用方重试）
    返回 {"items": [条目对象...], "no_issue_reason": str|None, "valid": bool}
    """
    items = []
    if review_text:
        current = None
        for raw in str(review_text).splitlines():
            line = _MARKDOWN_PREFIX_RE.sub("", raw.strip()).strip()
            if not line:
                continue
            if _MINE_HEAD_RE.match(line):
                current = {"category": "", "location": "", "quote": "", "harm": "", "action": ""}
                items.append(current)
                continue
            if current is not None:
                sep = "：" if "：" in line else ":"
                head, _, value = line.partition(sep)
                key = _MINE_FIELD_LOOKUP.get(head.strip())
                if key:
                    current[key] = value.strip()
    no_issue_reason = None if items else _extract_no_issue_reason(str(review_text or "").splitlines())
    for item in items:
        item["category"] = _normalize_mine_category(item["category"])
    valid = bool(items) or bool(no_issue_reason)
    return {"items": items, "no_issue_reason": no_issue_reason, "valid": valid}


def format_mine_list(parsed):
    """把解析后的排雷结果渲染回字段卡文本，供正文prompt的<strict_warnings>节注入"""
    items = (parsed or {}).get("items") or []
    if items:
        blocks = []
        for i, it in enumerate(items, 1):
            blocks.append(
                f"【毒点{i}】{it.get('category') or '未分类'}\n"
                f"位置：{it.get('location') or '未标注'}\n"
                f"原文引用：{it.get('quote') or '未摘抄'}\n"
                f"伤害说明：{it.get('harm') or '未说明'}\n"
                f"规避动作：{it.get('action') or '未给出'}"
            )
        return "\n".join(blocks)
    reason = (parsed or {}).get("no_issue_reason")
    if reason:
        return f"未发现毒点。排查理由：{reason}"
    return "（排雷输出未能解析为字段卡，请按原始排雷文本自行规避问题）"


# =============================================
# 正文步（BODY）：稳定块前置 + 逐场景配额 + 衔接块居尾
# =============================================
# 番茄铁则兜底文案：novel_settings/<书>/03-番茄审核铁则.md 缺失时启用，保证审核红线始终在prompt里
DEFAULT_TOMATO_RULES = (
    "1. 政治与价值观：不涉及敏感政治人物与事件，不渲染极端对立，主角行为符合公序良俗；\n"
    "2. 低俗与色情：不写露骨性描写，亲密关系点到为止，涉及未成年角色一律纯情向；\n"
    "3. 暴力与血腥：冲突可以激烈，但不细致描写肢解、虐待过程，不给可复现的犯罪教程式细节；\n"
    "4. 赌毒与违法：不美化赌博、毒品与自残行为，违法行为必须承受剧情代价；\n"
    "5. 真实人物与机构：不点名真实在世公众人物与真实企业，必要时使用化名与虚构机构；\n"
    "6. 宗教与民族：不调侃宗教信仰与民族习俗，避免刻板印象。"
)


def load_tomato_rules(novel_name=None):
    """读取 novel_settings/<书>/03-番茄审核铁则.md；文件缺失/读取失败时返回内置兜底文案，绝不报错"""
    if novel_name:
        path = os.path.join("novel_settings", str(novel_name), "03-番茄审核铁则.md")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    return content
        except Exception as e:
            # 铁则文件读不到就走兜底，绝不影响生成主流程
            logger.warning(f"⚠️  读取番茄审核铁则失败，使用内置兜底文案: {e}")
    return DEFAULT_TOMATO_RULES


def resolve_tomato_rules(tomato_rules="", fixed_memory="", novel_name=None):
    """番茄铁则三级来源：显式传入 > 固定设定已携带（去重，避免同一段红线注入两遍） > 内置兜底文案"""
    explicit = (tomato_rules or "").strip()
    if explicit:
        return explicit
    if "番茄审核铁则" in (fixed_memory or ""):
        # 记忆宫殿已把 03-番茄审核铁则.md 的内容拼进固定设定，这里去重
        return "（番茄审核铁则全文已包含在上方<world_rules>中，以其为准。）"
    return load_tomato_rules(novel_name)


# AI味规则设计原则（sepia频次治理 + bili-4「466条评论AI味清单」调研结论）：
#   1) 只做「频次治理」，不做词表替换——机械替换被调研证伪（替换词很快变成新一套AI味）；
#   2) 白名单防误伤：清单内表达「单次命中不算问题」，同一表达章内反复出现才提示收敛，
#      避免把正常文笔误判成AI味、逼着模型把句子改得生硬；正文步只收到规则提示，不做程序化改写。
AI_FLAVOR_RULES = (
    "1. 套话频次上限：诸如「不是……而是……」句式、「瞳孔骤缩」「瞳孔地震」「眼里亮起光芒」"
    "「眼中闪过一丝」「空气仿佛凝固」「嘴角勾起一抹弧度」这类高频套话，同一种全章至多出现1次；\n"
    "2. 白名单原则：以上表达单次自然使用完全没问题，不要为避词把句子改得生硬，只防同一种套路反复出现；\n"
    "3. 短段落：单段原则上不超过3行，关键情绪与反转独立成段；\n"
    "4. 张力波峰：每章至少安排2个张力波峰（冲突升级、反转或危机爆发），波峰之间留缓冲段落，"
    "禁止全程平铺或从头吵到尾；\n"
    "5. 对话比例：正文对话占比不低于30%，禁止大段独白式叙述推进。"
)

BODY_SYSTEM = (
    "你是网文白金主笔，擅长写节奏紧凑、爽点密集、人物立体的爆款网文，严格遵守XML边界创作。"
    "你严格按场景计划与字数配额写作，续写时绝不复述已有内容。"
)

BODY_USER = """=== 创作设定（全书稳定的世界观与设定，优先级最高） ===
<world_rules>
{fixed_memory}
</world_rules>

=== 番茄审核铁则（审核红线，逐条遵守） ===
{tomato_rules}

=== AI味自查规则（写作时对照自查） ===
{ai_flavor_rules}

=== 前情记忆（近几章摘要） ===
<previous_chapters>
{dynamic_memory}
</previous_chapters>

=== 本章大纲 ===
<chapter_outline>
{chapter_outline}
</chapter_outline>

=== 场景计划（逐场字数配额，严格按顺序推进） ===
{scene_plan}

=== 排雷清单（写作时逐条规避） ===
<strict_warnings>
{strict_warnings}
</strict_warnings>

{writing_task}
{ending_section}
{tail_section}
【警告·绝对不能】
- 绝对不能复述已写正文或「上一章结尾」中的任何句子；
- 绝对不能输出场景编号、要点标题、「以下是正文」「本章完」等场外内容；
- 绝对不能违背<world_rules>的既定设定与<strict_warnings>的规避动作；
- 绝对不能触碰番茄审核铁则的红线内容；
- 除最后一场外，绝对不能用总结性语句收尾。
"""


def render_chapter_task(chapter_num, target_words):
    """整章单次调用（短章）的写作任务指令"""
    return (
        "=== 写作任务 ===\n"
        f"请严格按<chapter_outline>与场景计划的顺序撰写第{chapter_num}章正文，绝对避开<strict_warnings>中的毒点：\n"
        "- 逐场景推进，每场用字贴近其配额，全章总字数逼近" + str(target_words) + "字；\n"
        "- 中间场景结尾停在动作、对话或悬念上，禁止总结收尾，只允许最后一场收尾并留钩子；\n"
        "- 只输出正文，不要标题、注释、场景编号等任何额外内容。"
    )


def render_segment_task(scene_index, gist, quota, is_last):
    """长章逐段续写（LongWriter思路）的单段写作任务指令：只输出新段、不复述已写文本、中间场景禁止总结收尾"""
    lines = [
        "=== 本段写作任务 ===",
        f"只写场景{scene_index}「{gist}」，本段配额{quota}字（浮动控制在10%以内）。",
    ]
    if is_last:
        lines.append("本段是最后一场：收尾并留下钩子，但禁止写「本章完」等场外话。")
    else:
        # LongWriter结论：中间段一旦总结收尾，后续段落就接不下去，只能越写越水
        lines.append("本段是中间场景：结尾必须停在动作、对话或悬念上，禁止总结收尾，禁止时间跳跃式收束。")
    lines.append("只输出本段新内容：绝不复述<written_so_far>里的已写正文，绝不复述<previous_ending>里的上一章结尾。")
    return "\n".join(lines)


def render_ending_section(ending_excerpt):
    """渲染「上一章结尾」衔接块（51mazi：只作文风衔接；chinese-novelist-skill：动笔前最后读正文语态）。
    第一章/无数据返回空串——正文prompt不含该节。原文超长只取结尾800字。"""
    text = (ending_excerpt or "").strip()
    if not text:
        return ""
    if len(text) > ENDING_EXCERPT_CHARS:
        text = text[-ENDING_EXCERPT_CHARS:]
    return (
        "=== 上一章结尾（原文，仅作文风与情节衔接） ===\n"
        "<previous_ending>\n"
        f"{text}\n"
        "</previous_ending>\n"
        "时间线如与本章大纲不符，以<chapter_outline>为准；上一章结尾只用于文风与情节衔接，禁止照搬原句。"
    )


def render_tail_section(written_text):
    """渲染「已写正文结尾」递增上下文块（逐段续写用），超长只携带尾部2000字；首段返回空串"""
    text = (written_text or "").strip()
    if not text:
        return ""
    if len(text) > CONTINUATION_TAIL_CHARS:
        text = text[-CONTINUATION_TAIL_CHARS:]
    return (
        "=== 已写正文结尾（从这里无缝续写，禁止复述） ===\n"
        "<written_so_far>\n"
        f"{text}\n"
        "</written_so_far>"
    )


def render_body_prompts(chapter_num, target_words, fixed_memory, dynamic_memory, chapter_outline,
                        scene_plan, strict_warnings, tomato_rules="", ending_excerpt="",
                        written_tail="", segment=None):
    """渲染正文步 SYSTEM+USER。

    - segment=None：整章单次调用（短章），writing_task为整章指令；
    - segment=(scene_index, gist, quota, is_last)：长章逐段续写，writing_task为单段指令；
    - ending_excerpt 非空才渲染「上一章结尾」节且位于prompt尾部（正文语态文字放末尾），第一章无此节。
    """
    tomato = (tomato_rules or "").strip() or DEFAULT_TOMATO_RULES
    if segment is None:
        writing_task = render_chapter_task(chapter_num, target_words)
    else:
        scene_index, gist, quota, is_last = segment
        writing_task = render_segment_task(scene_index, gist, quota, is_last)
    user = BODY_USER.format(
        fixed_memory=fixed_memory or "（暂无固定设定）",
        tomato_rules=tomato,
        ai_flavor_rules=AI_FLAVOR_RULES,
        dynamic_memory=dynamic_memory or "无前置剧情",
        chapter_num=chapter_num,
        chapter_outline=chapter_outline,
        scene_plan=scene_plan,
        strict_warnings=strict_warnings,
        writing_task=writing_task,
        ending_section=render_ending_section(ending_excerpt),
        tail_section=render_tail_section(written_tail),
    )
    return BODY_SYSTEM, user


# =============================================
# 场景计划字段卡容错解析（AI_NovelGenerator思路：字段卡+别名表，不用strict JSON）
# =============================================
# 场景行识别：场景/幕/场 + 编号（支持中文数字与全角数字）
_SCENE_HEAD_RE = re.compile(
    r"^(?:第?\s*(?:场景|場景|场次|节拍|幕|场)\s*([0-9０-９一二三四五六七八九十]{1,3})"
    r"|第\s*([0-9０-９一二三四五六七八九十]{1,3})\s*(?:场|幕|节))"
)
# markdown树形符号与列表序号前缀（剥除用：# > - * • 【 等装饰符号）
_MARKDOWN_PREFIX_RE = re.compile(r"^(?:[#>\-*•·・\s【\[]+|\d+\s*[.、)）]\s*)+")
# 字数提取：标签式（字数/配额/篇幅）→ 后缀式（800字）→ 带分隔符的行尾数字 → 裸行尾数字兜底
_WORDS_LABEL_RE = re.compile(r"(?:字数|配额|篇幅|words?|quota)\s*[:：=＝]?\s*(\d{2,5})", re.IGNORECASE)
_WORDS_SUFFIX_RE = re.compile(r"(\d{2,5})\s*字")
_WORDS_TAILSEP_RE = re.compile(r"[:：\-—－~～/]\s*(\d{2,5})\s*$")
# 裸数字兜底限定3-4位（对应200-1000配额区间），防止把要点里的楼层/编号误当配额
_WORDS_TAIL_RE = re.compile(r"(\d{3,4})\s*$")
# 要点字段标签别名（剥除用）
_GIST_LABEL_RE = re.compile(r"^(?:要点|概要|梗概|剧情|情节|内容|描述|summary|gist)\s*[:：\-—－]\s*", re.IGNORECASE)

# 字段卡各列两端需要剥掉的装饰字符（全角/半角标点、markdown残余、引号）
_REST_STRIP_CHARS = " \t：:－—－-–=＝，,、；;。】」"
_GIST_STRIP_CHARS = _REST_STRIP_CHARS + "「」『』\"'*#"

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 默认节拍池（兜底计划用）
_DEFAULT_BEATS = ("开场抛出冲突或悬念", "主角应对并推进铺垫", "矛盾升级爆发", "反转或高潮落地", "收尾并留下钩子")


def _cn_to_int(text):
    """中文/全角数字 → int（支持到九十九），解析失败返回None"""
    text = str(text).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    fullwidth = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if fullwidth.isdigit():
        return int(fullwidth)
    total, num = 0, 0
    for ch in text:
        if ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        else:
            return None
    return (total + num) or None


def _clamp_words(words):
    """把场景配额收敛进 SCENE_WORDS_MIN-MAX 区间"""
    return max(SCENE_WORDS_MIN, min(SCENE_WORDS_MAX, int(words)))


def parse_scene_plan(plan_text, target_words):
    """大纲输出的「场景N-要点-字数」字段卡容错解析。

    - 兼容 -/#/* 标题与引用等markdown树形符号前缀、全角冒号、中文数字场景号；
    - 字数取值依次尝试：标签式（字数/配额/篇幅）→ 后缀式（800字）→ 行尾带分隔符数字 → 行尾裸数字；
    - 要点字段走别名表（要点/概要/剧情/内容…）；
    - 缺字数的场景按 目标字数÷场景数 给默认配额；解析出的配额一律收敛进 200-1000；
    - 完全解析不出场景时返回 []，由调用方判坏计划并重生成一次。
    返回 [{"index": int, "gist": str, "words": int}, ...]（index按解析顺序重排为1..N）
    """
    if not plan_text:
        return []
    raw_scenes = []
    for raw_line in str(plan_text).splitlines():
        line = _MARKDOWN_PREFIX_RE.sub("", raw_line.strip()).strip()
        if not line:
            continue
        head = _SCENE_HEAD_RE.match(line)
        if not head:
            continue
        index = _cn_to_int(head.group(1) or head.group(2))
        if index is None:
            continue
        rest = line[head.end():].strip(_REST_STRIP_CHARS)
        words = None
        for pattern in (_WORDS_LABEL_RE, _WORDS_SUFFIX_RE, _WORDS_TAILSEP_RE, _WORDS_TAIL_RE):
            m = pattern.search(rest)
            if m:
                words = int(m.group(1))
                rest = rest[:m.start()] + rest[m.end():]
                break
        rest = _GIST_LABEL_RE.sub("", rest)
        gist = rest.strip(_GIST_STRIP_CHARS)
        raw_scenes.append({"index": index, "gist": gist, "words": words})
    if not raw_scenes:
        return []
    # 缺省配额兜底：目标字数÷场景数，收敛进200-1000
    fallback_per_scene = (_clamp_words(round(target_words / len(raw_scenes)))
                          if target_words and target_words > 0 else 500)
    scenes = []
    for i, s in enumerate(raw_scenes, 1):
        scenes.append({
            "index": i,
            "gist": s["gist"] or "按本章大纲推进主线剧情",
            "words": _clamp_words(s["words"]) if s["words"] else fallback_per_scene,
        })
    return scenes


def is_scene_plan_bad(scenes):
    """场景计划好坏判定：场景数不在3-15区间（含解析为空）即判坏计划，由调用方重生成一次"""
    return not (SCENE_COUNT_MIN <= len(scenes) <= SCENE_COUNT_MAX)


def build_default_scene_plan(target_words):
    """场景计划两次解析都失败时的兜底：按目标字数均分为符合「3-15场、单场200-1000字」约束的默认计划"""
    n = max(SCENE_COUNT_MIN, min(SCENE_COUNT_MAX, math.ceil(target_words / 900)))
    quota = _clamp_words(round(target_words / n))
    scenes = []
    for i in range(n):
        gist = _DEFAULT_BEATS[i] if i < len(_DEFAULT_BEATS) else "按本章大纲继续推进剧情"
        scenes.append({"index": i + 1, "gist": gist, "words": quota})
    return scenes


def format_scene_plan(scenes, target_words=None):
    """把解析后的场景计划渲染回逐行字段卡，供正文prompt注入（与大纲步字段卡格式同构）"""
    if not scenes:
        return "（场景计划缺失，请按<chapter_outline>自行分段推进）"
    total = sum(s["words"] for s in scenes)
    header = f"共{len(scenes)}个场景，配额合计约{total}字"
    if target_words:
        header += f"（本章目标{target_words}字）"
    lines = [header]
    for s in scenes:
        lines.append(f"场景{s['index']}-{s['gist']}-{s['words']}字")
    return "\n".join(lines)


# =============================================
# 记忆回写（MEMORY_WRITEBACK）：章节写后结构化抽取 + 滚动全局摘要压缩
# 设计依据（重构调研结论）：
#   - NovelClaw（github-18）/MuMuAINovel（github-4）：生成→结构化分析→状态回写→下一章自动带回闭环；
#   - goink（github-17）：台账只排不滤，防LLM估算丢数据；
#   - chinese-novelist-skill（github-3）：伏笔台账生命周期（埋设/快到期/超期/回收）；
#   - 沿用 AI_NovelGenerator（github-1）同款哲学：JSON+容错解析，解析失败显式告警并保留旧状态，
#     绝不让回写失败打断章节交付。
# =============================================
# 抽取结果的字段钳制（防超长记忆污染上下文预算）
EXTRACT_SUMMARY_MAX_CHARS = 200     # 章节摘要上限（与记忆宫殿单章摘要预算对齐）
EXTRACT_FORESHADOW_MAX = 8          # 单章最多登记新埋伏笔条数
EXTRACT_FORESHADOW_CHARS = 120      # 单条伏笔描述上限
EXTRACT_RESOLVED_MAX = 8            # 单章最多登记回收伏笔条数
EXTRACT_CHARACTER_MAX = 8           # 单章最多更新角色状态条数
EXTRACT_CHARACTER_CHARS = 200       # 单条角色状态上限

MEMORY_EXTRACT_SYSTEM = (
    "你是网文连载的专职场记，负责把刚写完的一章整理成结构化记忆卡，供后续章节自动回看。"
    "你只依据给定正文与既有记忆做增量抽取，绝不虚构正文里没有的信息。"
)

MEMORY_EXTRACT_USER = """=== 既有记忆（供对照增量，禁止原样照抄） ===
<existing_foreshadows>
{existing_foreshadows}
</existing_foreshadows>
<existing_characters>
{existing_characters}
</existing_characters>

=== 本章正文（第{chapter_num}章） ===
<chapter_content>
{chapter_content}
</chapter_content>

=== 抽取任务 ===
请通读本章正文，对照既有记忆做增量抽取，只输出一个JSON对象（不要任何解释文字），结构如下：
{{
  "chapter_summary": "本章剧情摘要（120字以内）：发生了什么、主线推进到哪、结尾钩子是什么",
  "foreshadows_planted": [{{"content": "本章新埋设伏笔的一句话描述", "due_chapter": 12}}],
  "foreshadows_resolved": ["本章兑现回收的伏笔描述，需能对上<existing_foreshadows>或正文原文"],
  "characters": [{{"name": "角色名", "state": "本章结束时该角色的关键状态：伤势/修为/位置/关系/手中关键物品"}}]
}}

=== 填写硬性要求 ===
- chapter_summary 必填；due_chapter 写预计回收章节号，写不出就填 null；
- foreshadows_planted 只收「本章新埋设」的伏笔，<existing_foreshadows>里已有的不要重复上报；
- 本章没有新埋伏笔/没有回收伏笔/没有值得记录的角色变化时，对应数组留空即可，禁止硬凑；
- characters 只收本章状态发生实质变化的角色，state 写成一句可直接回看的事实陈述。

【警告·绝对不能】
- 绝对不能输出JSON以外的任何文字（包括```围栏、解释、道歉）；
- 绝对不能虚构正文与既有记忆中不存在的伏笔或角色；
- 绝对不能把<existing_foreshadows>里的旧伏笔重复算作新埋设。
"""


def render_memory_extract_prompts(chapter_num, chapter_content, existing_foreshadows, existing_characters):
    """渲染写后抽取 SYSTEM+USER；既有记忆对照块由记忆宫殿提供，空则给占位说明"""
    if existing_foreshadows:
        foreshadow_lines = "\n".join(
            f"- {f['content']}（埋设第{f.get('planted_chapter') or '?'}章"
            + (f"，预计第{f['due_chapter']}章回收" if f.get("due_chapter") else "")
            + "）"
            for f in existing_foreshadows
        )
    else:
        foreshadow_lines = "（暂无未兑现伏笔记录）"
    if existing_characters:
        character_lines = "\n".join(
            f"- {c['name']}：{c.get('state') or ''}" for c in existing_characters
        )
    else:
        character_lines = "（暂无角色状态记录）"
    user = MEMORY_EXTRACT_USER.format(
        chapter_num=chapter_num,
        chapter_content=chapter_content or "（正文缺失）",
        existing_foreshadows=foreshadow_lines,
        existing_characters=character_lines,
    )
    return MEMORY_EXTRACT_SYSTEM, user


def _parse_due_chapter(raw):
    """due_chapter 容错归一：int/float/数字串 → int，其余（含null/空/布尔）→ None"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def parse_memory_extract(raw_text):
    """写后抽取结果容错解析：剥围栏与前后散文后取最外层花括号做JSON解析。

    字段缺省兜底为空、超长按上限钳制、条数按上限截取；
    完全解析不出抛ValueError，由调用方显式告警并保留旧状态（禁止静默丢记忆）。
    """
    text = str(raw_text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"抽取输出中未找到JSON对象：{text[:60]!r}")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"抽取输出不是合法JSON：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("抽取输出JSON顶层必须是对象")

    planted = []
    for item in (data.get("foreshadows_planted") or [])[:EXTRACT_FORESHADOW_MAX]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()[:EXTRACT_FORESHADOW_CHARS]
        if content:
            planted.append({"content": content, "due_chapter": _parse_due_chapter(item.get("due_chapter"))})

    resolved = []
    for item in (data.get("foreshadows_resolved") or [])[:EXTRACT_RESOLVED_MAX]:
        hint = str(item or "").strip()[:EXTRACT_FORESHADOW_CHARS]
        if hint:
            resolved.append(hint)

    characters = []
    for item in (data.get("characters") or [])[:EXTRACT_CHARACTER_MAX]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:40]
        state = str(item.get("state") or "").strip()[:EXTRACT_CHARACTER_CHARS]
        if name and state:
            characters.append({"name": name, "state": state})

    return {
        "chapter_summary": str(data.get("chapter_summary") or "").strip()[:EXTRACT_SUMMARY_MAX_CHARS],
        "foreshadows_planted": planted,
        "foreshadows_resolved": resolved,
        "characters": characters,
    }


# =============================================
# 滚动全局摘要压缩（每5章/伏笔台账变动时触发，见 memory_palace.refresh_global_summary）
# =============================================
GLOBAL_SUMMARY_SYSTEM = (
    "你是网文连载的剧情档案员，负责把多章剧情合并压缩成一份滚动全局摘要，供AI写下一章时回看全书脉络。"
)

GLOBAL_SUMMARY_USER = """=== 上一版全局摘要 ===
<old_summary>
{old_summary}
</old_summary>

=== 待并入的新章节摘要 ===
<new_chapters>
{new_chapters}
</new_chapters>

=== 当前未兑现伏笔（台账） ===
<open_foreshadows>
{open_foreshadows}
</open_foreshadows>

=== 当前主要角色状态 ===
<characters>
{characters}
</characters>

=== 压缩任务 ===
请把<old_summary>与<new_chapters>合并压缩成一份不超过2000字的全局摘要，硬性要求：
1. 必须保留三要素：「关键人物状态」（主要角色在哪、伤势/修为/关系变化）、「主线推进」（主线推进到哪一步、下一个目标）、「未兑现伏笔」（埋了还没收的钩子逐条列出）；
2. 删除过细情节：具体打斗招式、对话原句、场景描写细节一律舍弃，只留因果与结果；
3. 按时间顺序组织叙述，新章节信息并入对应脉络，禁止按章流水账罗列。

【警告·绝对不能】
- 绝对不能丢失<open_foreshadows>中任何一条未兑现伏笔；
- 绝对不能出现「第X章：」式的逐章罗列；
- 绝对不能超过2000字。
"""


def render_global_summary_prompts(old_summary, new_chapters, open_foreshadows, characters):
    """渲染滚动全局摘要压缩 SYSTEM+USER；四块素材文本由记忆宫殿装配，本函数只填充占位符"""
    user = GLOBAL_SUMMARY_USER.format(
        old_summary=old_summary or "（首次生成，暂无历史摘要）",
        new_chapters=new_chapters or "（暂无待并入章节）",
        open_foreshadows=open_foreshadows or "（暂无未兑现伏笔）",
        characters=characters or "（暂无角色状态记录）",
    )
    return GLOBAL_SUMMARY_SYSTEM, user


# =============================================
# 质量门禁定向重写（GATE_REPAIR）+ 一致性审校（CONSISTENCY_REVIEW）
# 提示词继续具名收口在本文件（与三步流水线同一套约定：调用方只渲染不拼模板）：
#   - GATE_REPAIR：门禁违规清单回喂prompt，定向修订而非整章重写
#     （chinese-novelist-skill：失败自动重写设上限；重写只修清单内问题，白名单防误伤）
#   - CONSISTENCY_REVIEW：独立审校步骤（不进生成prompt），对照人物档案+伏笔台账
#     输出连续性可疑点清单，写入运行报告供人工复核
# =============================================
GATE_REPAIR_SYSTEM = (
    "你是网文主笔的定向修订助手，负责按质量门禁违规清单修订正文。"
    "你只修复清单列出的问题，其余内容原样保留，只输出修订后的完整正文。"
)

GATE_REPAIR_USER = """=== 本章原稿（第{chapter_num}章） ===
<draft>
{draft}
</draft>

=== 质量门禁违规清单（逐条修订依据，全部为硬门禁） ===
<violations>
{violations}
</violations>

=== 修订任务 ===
请逐条对照<violations>定向修订<draft>，并把全章字数控制在{words_floor}-{words_ceil}字（目标约{target_words}字）：
- 套话类违规：同一种表达保留至多1次自然使用，其余改写为多样化的动作/细节/心理描写，禁止把句子改得生硬；
- 长段类违规：把超长段拆短，关键情绪与反转独立成段；
- 字数类违规：不足则充实场景细节与对话，超出则收紧冗余叙述，禁止整段删情节。

=== 修订硬性要求 ===
- 只修复违规清单列出的问题：情节走向、人设、伏笔与既有文风原样保留；
- 修订后必须仍是一篇完整连贯的正文，逐场景推进、结尾留钩子。

【警告·绝对不能】
- 绝对不能输出修订说明、修订对比或正文以外的任何内容；
- 绝对不能为规避违规删掉关键情节或烂尾收场；
- 绝对不能引入新的剧情矛盾或人设崩塌。
"""


def render_gate_repair_prompts(chapter_num, target_words, draft, violations, words_floor, words_ceil):
    """渲染门禁定向重写 SYSTEM+USER；违规清单文本由 validators.format_gate_violations 生成"""
    user = GATE_REPAIR_USER.format(
        chapter_num=chapter_num,
        draft=draft or "（原稿缺失）",
        violations=violations or "（无）",
        words_floor=words_floor,
        words_ceil=words_ceil,
        target_words=target_words,
    )
    return GATE_REPAIR_SYSTEM, user


CONSISTENCY_REVIEW_SYSTEM = (
    "你是网文连载的连续性审校员，负责对照人物档案与伏笔台账，找出本章正文与既有设定矛盾的可疑点。"
    "你只报告有依据的可疑点，绝不为了显得尽责而凑数。"
)

CONSISTENCY_REVIEW_USER = """=== 人物档案（固定设定） ===
<characters_profile>
{character_profile}
</characters_profile>

=== 伏笔台账 ===
<foreshadow_ledger>
{foreshadow_ledger}
</foreshadow_ledger>

=== 角色状态记忆 ===
<characters_state>
{characters_state}
</characters_state>

=== 本章正文（第{chapter_num}章） ===
<chapter_content>
{chapter_content}
</chapter_content>

=== 审校任务 ===
请对照上述档案、台账与角色状态，找出<chapter_content>中的连续性可疑点，只输出一个JSON对象（不要任何解释文字）：
{{"suspects": [{{"type": "角色状态矛盾/伏笔冲突/时间线矛盾 三选一", "detail": "可疑点描述，如：角色第N章已死亡但本章出场", "evidence": "正文原句或档案/台账依据"}}]}}

=== 硬性要求 ===
- 每条可疑点必须能在档案/台账/角色状态/正文中找到依据，并附 evidence；
- 本章确无可疑点时 suspects 输出空数组，禁止硬凑。

【警告·绝对不能】
- 绝对不能输出JSON以外的任何文字（包括```围栏、解释、道歉）；
- 绝对不能虚构档案与台账中不存在的矛盾；
- 绝对不能把档案里未提及的信息自行脑补成矛盾。
"""


def render_consistency_review_prompts(character_profile, foreshadow_ledger, characters_state,
                                      chapter_num, chapter_content):
    """渲染一致性审校 SYSTEM+USER；素材文本由 validators.run_consistency_review 装配"""
    user = CONSISTENCY_REVIEW_USER.format(
        character_profile=character_profile,
        foreshadow_ledger=foreshadow_ledger,
        characters_state=characters_state,
        chapter_num=chapter_num,
        chapter_content=chapter_content,
    )
    return CONSISTENCY_REVIEW_SYSTEM, user
