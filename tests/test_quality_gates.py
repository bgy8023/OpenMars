#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""质量门禁与过审能力维度单测（python -m unittest tests.test_quality_gates -v）：

1. 零 token 正则校验器 validate_chapter：
   ① 正常 3000 字文（target=3000）→ 全部通过；
   ② 注入 10 处「不是…而是」+3 条连续超长段 → fail 且报告逐条给计数与位置；
   ③ r2：target=7500 首稿 7200 字 → 硬门禁通过、报告仅含「超出番茄建议区间 2500-4500」
      的 platform_hint 且零重写（violations 为空 ⇒ 重写条件为假，自洽性验证）；
   ④ target 未提供时兜底读 MAX_CHAPTER_WORDS（默认4500）；白名单单次命中不算；
      对话比例可选开关；05-质量规则.md 覆盖词表与阈值。
2. 门禁接线（cyber_printer_ultimate.generate_chapter_full，stub 引擎）：
   首稿不过二稿过 → 成功且报告记录 1 次重写；stub 持续不过 → 显式标注+告警含违规清单，
   无静默降级；门禁开关关闭 → 零重写零标注；platform_hint 不触发重写。
3. 可选一致性审校：种子矛盾（死亡角色出场）→ 审校报告出现该可疑点；
   关闭开关时零额外 LLM 调用；解析失败留痕不抛出。
4. 批量断点续跑：第 2 章已完成 → 批量 1-3 仅对 1、3 发起调用；中途取消再启动不重跑已完成章。
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cyber_printer_ultimate
from openmars_core.config import AppConfig
from openmars_core.memory_palace import SQLiteMemoryPalace
from openmars_core.query_engine import SyncQueryEngine
from openmars_core.validators import (
    GATE_ANNOTATION_MARK,
    QualityRules,
    count_words,
    load_quality_rules,
    run_consistency_review,
    validate_chapter,
)

# =============================================
# 测试素材：安全句池（不含任何默认套话词/「不是…而是」句式，防止填充文本误触发门禁）
# =============================================
SAFE_SENTENCES = [
    "巷口的灯忽明忽暗，雨水顺着屋檐连成一条线。",
    "他把刀插回鞘中，转身走向巷子深处。",
    "柜台后的老人抬起头，目光在他脸上停了片刻。",
    "远处传来几声犬吠，很快又归于沉寂。",
    "桌上的茶早就凉透，杯壁凝着一圈水痕。",
    "风从破窗灌进来，吹得油灯火苗歪向一边。",
    "她低头缝补衣角，针脚细密得看不出裂口。",
    "马蹄声由远及近，在门前勒住。",
    "他把地图铺开，用炭笔在山河之间画了一道弧。",
    "檐下铁马轻响，像是谁在暗中数着更次。",
]

DIALOGUE_PARAS = [
    "「这么晚了，谁还在外面？」她隔着门问了一句。",
    "「是我。」门外的声音低哑，像是裹着一层风霜。",
]


def _filler_paragraph(desired_chars=100):
    """用安全句池拼一段约 desired_chars 字的普通段落（远低于300字长段阈值）"""
    parts, total, i = [], 0, 0
    while total < desired_chars:
        s = SAFE_SENTENCES[i % len(SAFE_SENTENCES)]
        parts.append(s)
        total += len(s)
        i += 1
    return "".join(parts)


def _long_paragraph(desired_chars=320):
    """拼一段超过300字的超长段"""
    s = "他在雨里走了很久，鞋底磨穿了也没有停下。"
    parts, total = [], 0
    while total < desired_chars:
        parts.append(s)
        total += len(s)
    return "".join(parts)


def _flavor_paragraphs(n=10):
    """n 个含「不是…而是」句式的段落（每段恰命中一次）"""
    subjects = ["他", "她", "老者", "少年", "掌柜", "捕头", "学徒", "车夫", "医师", "琴师"]
    return [f"{subjects[i % len(subjects)]}盯着眼前这盘残局，这一手不是失着，而是伏笔。"
            for i in range(n)]


def _build_chapter(paragraphs, desired_words):
    """以给定段落为基础，用普通填充段落补到约 desired_words 字（过冲<120字）"""
    paragraphs = list(paragraphs)
    text = "\n\n".join(paragraphs)
    while count_words(text) < desired_words:
        paragraphs.append(_filler_paragraph(100))
        text = "\n\n".join(paragraphs)
    return text


class ValidateChapterTests(unittest.TestCase):
    """验收1：零 token 正则校验器三组验收 + 兜底/白名单/可选开关"""

    def setUp(self):
        self.config = AppConfig()  # 确定性配置：max_chapter_words=4500 等默认值

    def test_normal_3000_words_passes_all_gates(self):
        # ① 正常 3000 字文（target=3000）→ 全部通过，零违规零提示
        text = _build_chapter(list(DIALOGUE_PARAS), 2800)
        self.assertGreaterEqual(count_words(text), 2500)   # 番茄建议区间内，应无 platform_hint
        report = validate_chapter(text, 3000, config=self.config)
        self.assertTrue(report.passed)
        self.assertEqual(report.violations, [])
        self.assertEqual(report.platform_hints, [])
        self.assertEqual(report.actual_words, count_words(text))

    def test_ai_flavor_and_long_paragraphs_fail_with_count_and_location(self):
        # ② 注入 10 处「不是…而是」+3 条连续超长段 → fail 且逐条给计数与位置
        paragraphs = _flavor_paragraphs(10) + [_long_paragraph() for _ in range(3)]
        text = _build_chapter(paragraphs, 2200)  # 补进硬门禁区间[2100,3300]，只让两类规则失败
        report = validate_chapter(text, 3000, config=self.config)
        self.assertFalse(report.passed)
        self.assertEqual(len(report.violations), 2)
        flavor_v = next(v for v in report.violations if v.rule == "ai_flavor:不是…而是句式")
        self.assertEqual(flavor_v.count, 10)
        self.assertIn("10次", flavor_v.message)
        self.assertIn("第", flavor_v.location)
        self.assertIn("段", flavor_v.location)
        long_v = next(v for v in report.violations if v.rule == "long_paragraph")
        self.assertEqual(long_v.count, 3)
        self.assertIn("第", long_v.location)
        self.assertIn("-", long_v.location)  # 「第a-b段」区间写法
        # 检查明细全程可见：每条规则无论过没过都有记录
        self.assertTrue(any(c["rule"] == "word_count" and c["passed"] for c in report.checks))

    def test_r2_target_7500_draft_7200_only_platform_hint(self):
        # ③ r2：target=7500 首稿 7200 字 → 硬门禁通过、仅 platform_hint、violations 为空 ⇒ 零重写
        text = _build_chapter([], 7150)
        self.assertGreater(count_words(text), 7000)
        report = validate_chapter(text, 7500, config=self.config)
        self.assertTrue(report.passed, "7200字应落在硬门禁[5250,8250]内")
        self.assertEqual(report.words_floor, 5250)
        self.assertEqual(report.words_ceil, 8250)
        self.assertEqual(report.violations, [])
        self.assertEqual(len(report.platform_hints), 1)
        self.assertIn("超出番茄建议区间 2500-4500", report.platform_hints[0])
        # 自洽性：重写触发条件只看硬门禁违规，violations 为空 ⇒ 重写循环条件为假（零重写）
        self.assertFalse(report.violations)

    def test_target_fallback_to_max_chapter_words_when_absent(self):
        # target 未提供 → 兜底以 MAX_CHAPTER_WORDS 默认 4500 为字数门禁基准
        text = _build_chapter([], 3300)
        report = validate_chapter(text, None, config=self.config)
        self.assertEqual(report.target_words, 4500)
        self.assertEqual(report.words_floor, 3150)
        self.assertEqual(report.words_ceil, 4950)
        self.assertTrue(report.passed)
        self.assertEqual(report.platform_hints, [])  # 3300 ∈ [2500,4500]

    def test_below_floor_word_violation_with_platform_hint(self):
        text = _build_chapter([], 1500)
        report = validate_chapter(text, 3000, config=self.config)
        self.assertFalse(report.passed)
        word_vs = [v for v in report.violations if v.rule == "word_count"]
        self.assertEqual(len(word_vs), 1)
        self.assertIn("[2100-3300]", word_vs[0].message)
        self.assertEqual(len(report.platform_hints), 1)  # 1500 < 2500 也给报告级提示
        self.assertIn("2500-4500", report.platform_hints[0])

    def test_single_flavor_hit_whitelisted(self):
        # 白名单原则：套话单次命中不算、累计频次才判
        paragraphs = ["他盯着黑影，瞳孔骤缩，握紧了刀。",
                      "这不是退缩，而是另寻出路。",
                      "空气仿佛凝固了一瞬。"]
        text = _build_chapter(paragraphs, 2800)
        report = validate_chapter(text, 3000, config=self.config)
        self.assertTrue(report.passed)
        self.assertFalse(any(v.rule.startswith("ai_flavor") for v in report.violations))

    def test_dialogue_ratio_gate_is_optional_switch(self):
        text = _build_chapter([], 2800)  # 全叙述无引号对话
        # 默认关：不判违规
        report = validate_chapter(text, 3000, config=self.config)
        self.assertTrue(report.passed)
        self.assertFalse(any(c["rule"] == "dialogue_ratio" for c in report.checks))
        # 显式开启：同文本判违规
        rules = QualityRules(dialogue_ratio_enabled=True, dialogue_ratio_min=0.3)
        report2 = validate_chapter(text, 3000, rules=rules, config=self.config)
        self.assertFalse(report2.passed)
        self.assertTrue(any(v.rule == "dialogue_ratio" for v in report2.violations))

    def test_gate_internal_error_fails_loud(self):
        # 门禁器自身异常：显式判不通过并留违规项，绝不静默放行
        report = validate_chapter("正文", 3000, rules=object(), config=self.config)
        self.assertFalse(report.passed)
        self.assertTrue(any(v.rule == "gate_internal_error" for v in report.violations))


class QualityRulesOverrideTests(unittest.TestCase):
    """验收1补充：novel_settings/<书>/05-质量规则.md 覆盖词表与阈值"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_rules_file_overrides_patterns_and_thresholds(self):
        book_dir = os.path.join(self._tmpdir, "novel_settings", "规则书")
        os.makedirs(book_dir)
        with open(os.path.join(book_dir, "05-质量规则.md"), "w", encoding="utf-8") as f:
            f.write("\n".join([
                "# 质量规则覆盖",
                "",
                "## 套话词表",
                "- 自创套话A",
                "- 自创句式B：这不是终点[^。]{0,8}而是起点",
                "",
                "## 阈值覆盖",
                "- 长段阈值：350",
                "- 连续长段判定：2",
                "- 对话比例门禁：开",
                "- 对话比例下限：20%",
            ]))
        os.chdir(self._tmpdir)
        rules = load_quality_rules("规则书")
        self.assertEqual([name for name, _ in rules.flavor_patterns], ["自创套话A", "自创句式B"])
        self.assertEqual(rules.long_para_chars, 350)
        self.assertEqual(rules.long_para_run, 2)
        self.assertTrue(rules.dialogue_ratio_enabled)
        self.assertAlmostEqual(rules.dialogue_ratio_min, 0.2)
        # 覆盖生效：连续 2 段 >350 字判违规；默认套话（瞳孔骤缩等）不再检查
        long_para = _long_paragraph(360)
        report = validate_chapter(long_para + "\n\n" + long_para, 600, novel_name="规则书",
                                  config=AppConfig())
        self.assertTrue(any(v.rule == "long_paragraph" for v in report.violations))
        self.assertFalse(any("瞳孔" in v.rule for v in report.violations))

    def test_missing_rules_file_falls_back_to_defaults(self):
        os.chdir(self._tmpdir)  # 干净目录
        rules = load_quality_rules("不存在的书")
        self.assertEqual(rules.long_para_chars, 300)
        self.assertEqual(rules.long_para_run, 3)
        self.assertFalse(rules.dialogue_ratio_enabled)
        self.assertTrue(len(rules.flavor_patterns) > 0)


# =============================================
# 门禁接线测试：stub 引擎（mock SyncQueryEngine.call_llm_sync 按步骤路由）
# =============================================
OUTLINE_TEXT_3SCENES = (
    "本章简纲：主角夜探矿洞，遭遇塌方，携手脱困。\n"
    "场景1-夜探矿洞-667\n"
    "场景2-遭遇塌方-667\n"
    "场景3-携手脱困-666"
)
REVIEW_OK_TEXT = "未发现-已对照人物档案、时间线、设定与伏笔四类排查，均无冲突"


def _failing_draft():
    """首稿：3 处「瞳孔骤缩」（>上限1次判违规），字数落在硬门禁区间内"""
    paragraphs = ["他盯着黑影，瞳孔骤缩，握紧了刀。",
                  "她听到脚步声，瞳孔骤缩，屏住呼吸。",
                  "老者抬眼望去，瞳孔骤缩，缓缓起身。"]
    return _build_chapter(paragraphs, 1500)


def _passing_draft():
    """二稿：纯安全填充 1900 字 → 硬门禁[1400,2200]通过，但触发报告级 platform_hint（1900<2500）"""
    return _build_chapter([], 1900)


def _make_router(content_outputs, rewrite_outputs=None):
    """按 step 路由的 call_llm_sync 替身，并记录各步调用次数。

    真实 call_llm_sync 每次调用都写一行 llm_calls 记账；替身保持同一契约，
    使运行报告的 token 差集可验证。
    """
    from openmars_core.llm_ledger import record_llm_call

    calls = {"outline": 0, "review": 0, "content": 0, "gate_rewrite": 0,
             "memory_extract": 0, "consistency_review": 0}
    content_outputs = list(content_outputs)
    rewrite_outputs = list(rewrite_outputs or [])

    def fake_call(user_prompt, system_prompt="你是专业助手", step="unknown",
                  chapter=None, model=None):
        calls[step] = calls.get(step, 0) + 1
        record_llm_call(step=step, model=model or "test-model",
                        prompt_tokens=100, completion_tokens=200, chapter=chapter)
        if step == "outline":
            return OUTLINE_TEXT_3SCENES
        if step == "review":
            return REVIEW_OK_TEXT
        if step == "content":
            return content_outputs.pop(0) if len(content_outputs) > 1 else content_outputs[0]
        if step == "gate_rewrite":
            return rewrite_outputs.pop(0) if len(rewrite_outputs) > 1 else rewrite_outputs[0]
        if step == "memory_extract":
            return json.dumps({"chapter_summary": "主角夜探矿洞脱困并留下线索",
                               "foreshadows_planted": [], "foreshadows_resolved": [],
                               "characters": []}, ensure_ascii=False)
        if step == "consistency_review":
            return '{"suspects": []}'
        return "占位输出"

    return fake_call, calls


class _WiringTestBase(unittest.TestCase):
    """接线测试公共基类：tmp目录隔离 + 环境变量固定（非免费模型/无webhook）"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def run_generate(self, novel_name, content_outputs, rewrite_outputs=None, config=None):
        """跑一次 generate_chapter_full，返回 (ok, 交付内容, 调用计数, 运行报告dict|None)"""
        fake_call, calls = _make_router(content_outputs, rewrite_outputs)
        env = {"LLM_MODEL_NAME": "test-model", "LLM_BASE_URL": "http://localhost:9/v1",
               "WEBHOOK_URL": ""}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env))
            stack.enter_context(mock.patch.object(SyncQueryEngine, "call_llm_sync",
                                                  side_effect=fake_call))
            stack.enter_context(mock.patch("time.sleep"))
            if config is not None:
                stack.enter_context(mock.patch("cyber_printer_ultimate.get_config",
                                               return_value=config))
                stack.enter_context(mock.patch("openmars_core.validators.get_config",
                                               return_value=config))
            ok, deliver = cyber_printer_ultimate.generate_chapter_full(
                1, 2000, "", novel_name=novel_name)
        report = None
        report_path = os.path.join("output", novel_name, "第1章_报告.json")
        if os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
        return ok, deliver, calls, report


class GateWiringTests(_WiringTestBase):
    """验收2：门禁接线——重写闭环/持续失败标注告警/开关关闭/platform_hint 不触发"""

    def test_first_draft_fails_second_passes_records_one_rewrite(self):
        # stub 首稿不过、二稿过 → 成功且报告记录 1 次重写
        ok, deliver, calls, report = self.run_generate(
            "门禁测试书", content_outputs=[_failing_draft()],
            rewrite_outputs=[_passing_draft()])
        self.assertTrue(ok)
        self.assertEqual(calls["content"], 1, "首稿只应生成一次")
        self.assertEqual(calls["gate_rewrite"], 1, "二稿过线后不应再重写")
        self.assertNotIn(GATE_ANNOTATION_MARK, deliver)
        self.assertEqual(deliver, _passing_draft())
        self.assertIsNotNone(report, "运行报告JSON应已落盘")
        self.assertTrue(report["success"])
        self.assertEqual(report["rewrite_rounds"], 1)
        self.assertTrue(report["gate"]["passed"])
        self.assertEqual(report["gate"]["enforced"], True)
        # 二稿 1900 字 < 2500：报告级 platform_hint 在场，但未触发重写（r2）
        self.assertTrue(report["gate"]["platform_hints"])
        self.assertEqual(report["steps"]["content"]["real_chars"], len(_passing_draft()))

    def test_persistent_failure_annotates_and_alerts_without_silent_degrade(self):
        # stub 持续不过 → 显式标注+告警含违规清单，无静默降级
        ok, deliver, calls, report = self.run_generate(
            "门禁测试书", content_outputs=[_failing_draft()],
            rewrite_outputs=[_failing_draft(), _failing_draft()])
        self.assertTrue(ok, "章节照常交付，但必须显式标注")
        self.assertEqual(calls["gate_rewrite"], cyber_printer_ultimate.GATE_MAX_REWRITES,
                         "持续不过应打满2轮定向重写上限")
        self.assertIn(GATE_ANNOTATION_MARK, deliver)
        self.assertIn("瞳孔骤缩", deliver, "标注块应含违规清单")
        self.assertIn("出现3次", deliver)
        self.assertIsNotNone(report)
        self.assertFalse(report["gate"]["passed"])
        self.assertEqual(report["rewrite_rounds"], cyber_printer_ultimate.GATE_MAX_REWRITES)
        self.assertTrue(report["delivery"]["annotated"])
        gate_alerts = [a for a in report["alerts"] if "质量门禁未通过" in a["title"]]
        self.assertTrue(gate_alerts, "告警记录应进运行报告")
        self.assertTrue(all(a["is_error"] for a in gate_alerts))
        self.assertIn("瞳孔骤缩", gate_alerts[0]["content"], "告警内容应含违规清单")

    def test_gate_switch_off_disables_rewrite_and_annotation(self):
        # TOMATO_AUDIT_COMPATIBLE=false：门禁只出报告，零重写零标注
        config = AppConfig()
        config.tomato_audit_compatible = False
        ok, deliver, calls, report = self.run_generate(
            "门禁测试书", content_outputs=[_failing_draft()], config=config)
        self.assertTrue(ok)
        self.assertEqual(calls["gate_rewrite"], 0)
        self.assertNotIn(GATE_ANNOTATION_MARK, deliver)
        self.assertIsNotNone(report)
        self.assertFalse(report["gate"]["passed"], "门禁结果仍如实记录")
        self.assertEqual(report["gate"]["enforced"], False)
        self.assertEqual(report["rewrite_rounds"], 0)

    def test_platform_hint_never_triggers_rewrite(self):
        # r2 验收③接线半边：首稿硬门禁通过但带 platform_hint → 零重写
        ok, deliver, calls, report = self.run_generate(
            "门禁测试书", content_outputs=[_passing_draft()])
        self.assertTrue(ok)
        self.assertEqual(calls["gate_rewrite"], 0, "platform_hint 绝不触发重写")
        self.assertNotIn(GATE_ANNOTATION_MARK, deliver)
        self.assertIsNotNone(report)
        self.assertTrue(report["gate"]["passed"])
        self.assertEqual(report["rewrite_rounds"], 0)
        self.assertTrue(report["gate"]["platform_hints"])


class RunReportTests(_WiringTestBase):
    """验收6：每章运行报告字段完整（三步输出摘要/门禁/重写次数/token/告警/审校）"""

    def test_report_fields_complete_after_generation(self):
        ok, _, _, report = self.run_generate(
            "报告测试书", content_outputs=[_failing_draft()],
            rewrite_outputs=[_passing_draft()])
        self.assertTrue(ok)
        self.assertIsNotNone(report)
        for key in ("novel_name", "chapter_num", "generated_at", "success", "target_words",
                    "steps", "gate", "rewrite_rounds", "consistency_review", "tokens",
                    "alerts", "delivery"):
            self.assertIn(key, report)
        self.assertEqual(report["novel_name"], "报告测试书")
        self.assertEqual(report["chapter_num"], 1)
        for step in ("outline", "review", "content"):
            self.assertIn("summary", report["steps"][step])
        self.assertTrue(report["steps"]["outline"]["summary"])
        self.assertIn("platform_hints", report["gate"])
        self.assertIn("violations", report["gate"])
        self.assertGreaterEqual(report["tokens"]["calls"], 4,
                                "大纲+排雷+正文+重写等本次运行的调用都应记账")
        self.assertGreater(report["tokens"]["total_tokens"], 0)
        self.assertTrue(report["alerts"], "开始/完毕等告警记录应进报告")
        # 一致性审校默认开启且已执行（stub 返回空可疑点清单）
        self.assertTrue(report["consistency_review"]["enabled"])
        self.assertEqual(report["consistency_review"]["suspects"], [])


class ConsistencyReviewTests(unittest.TestCase):
    """验收3：可选一致性审校——种子矛盾出现可疑点；关闭开关零额外LLM调用"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)
        book_dir = os.path.join(self._tmpdir, "novel_settings", "审校测试书")
        os.makedirs(book_dir, exist_ok=True)
        with open(os.path.join(book_dir, "01-人物档案.md"), "w", encoding="utf-8") as f:
            f.write("张三：第三章末已坠崖身亡。\n李四：主角挚友，现居北城。")
        self.palace = SQLiteMemoryPalace("审校测试书")
        self.palace.add_foreshadow("神秘玉佩的来历", 1, 5)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_seed_contradiction_surfaces_in_review(self):
        engine = mock.Mock()
        engine.call_llm_sync.return_value = json.dumps(
            {"suspects": [{"type": "角色状态矛盾",
                           "detail": "张三第3章已死亡但本章出场",
                           "evidence": "张三推门而入，笑着说：好久不见"}]},
            ensure_ascii=False)
        result = run_consistency_review(engine, self.palace, "审校测试书", 4,
                                        "张三推门而入，笑着打招呼。")
        engine.call_llm_sync.assert_called_once()  # 恰一次独立审校调用
        args, kwargs = engine.call_llm_sync.call_args
        self.assertEqual(kwargs.get("step"), "consistency_review")
        user_prompt = args[0]
        self.assertIn("已死亡", user_prompt, "人物档案应进审校素材")
        self.assertIn("神秘玉佩", user_prompt, "伏笔台账应进审校素材")
        self.assertEqual(len(result["suspects"]), 1)
        self.assertEqual(result["suspects"][0]["type"], "角色状态矛盾")
        self.assertIn("死亡", result["suspects"][0]["detail"])

    def test_disabled_switch_zero_extra_llm_calls(self):
        config = AppConfig()
        config.content_safety_check = False
        with mock.patch("openmars_core.validators.get_config", return_value=config):
            engine = mock.Mock()
            result = run_consistency_review(engine, self.palace, "审校测试书", 4, "正文")
        engine.call_llm_sync.assert_not_called()  # 零额外 LLM 调用
        self.assertFalse(result["enabled"])
        self.assertEqual(result["suspects"], [])

    def test_parse_failure_recorded_not_raised(self):
        engine = mock.Mock()
        engine.call_llm_sync.return_value = "这是散文，不是JSON"
        result = run_consistency_review(engine, self.palace, "审校测试书", 4, "正文")
        self.assertEqual(result["suspects"], [])
        self.assertTrue(result["error"], "审校失败应显式留痕")


class BatchResumeTests(unittest.TestCase):
    """验收5：批量断点续跑——已完成章跳过；中途取消再启动不重跑已完成章"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_get_completed_chapters_semantics(self):
        palace = SQLiteMemoryPalace("断点书")
        palace.safe_update(1, "第一章摘要", 10, "第一章完整正文")
        palace.safe_update(2, "第二章摘要", 10, None)      # 无正文 → 未完成
        palace.safe_update(3, "第三章摘要", 10, "   ")     # 纯空白 → 未完成
        palace.safe_update(4, "第四章摘要", 10, "第四章完整正文")
        self.assertEqual(cyber_printer_ultimate.get_completed_chapters("断点书", 1, 5), {1, 4})
        self.assertEqual(cyber_printer_ultimate.get_completed_chapters("不存在的书", 1, 5), set())

    def test_batch_skips_completed_chapters(self):
        # 批量 1-3 且第 2 章已完成 → 仅对 1、3 发起调用
        novel = "批量测试书"
        SQLiteMemoryPalace(novel).safe_update(2, "第二章摘要", 100, "第二章完整正文")
        called = []

        def fake_full(ch, target_words, custom_prompt, novel_name="默认小说",
                      kill_event=None, progress_cb=None):
            called.append(ch)
            return True, f"第{ch}章正文"

        with mock.patch("cyber_printer_ultimate.generate_chapter_full", side_effect=fake_full):
            results = cyber_printer_ultimate.generate_chapter_batch(
                1, 3, 2000, "", novel_name=novel)
        self.assertEqual(called, [1, 3], "已完成章不应发起生成调用")
        self.assertEqual([r["status"] for r in results],
                         ["ok", "skipped_completed", "ok"])
        # 每个成功章都有 .md 落盘（批量侧保存约定）
        md_files = [f for f in os.listdir(os.path.join("output", novel)) if f.endswith(".md")]
        self.assertEqual(len(md_files), 2)

    def test_cancel_then_resume_does_not_rerun_completed(self):
        novel = "批量测试书"
        kill_event = threading.Event()
        called = []

        def fake_full_v1(ch, target_words, custom_prompt, novel_name="默认小说",
                         kill_event=None, progress_cb=None):
            called.append(ch)
            if ch == 1:
                SQLiteMemoryPalace(novel_name).safe_update(ch, "摘要", 100, f"第{ch}章完整正文")
                return True, f"第{ch}章正文"
            if ch == 2:
                kill_event.set()  # 模拟第2章生成过程中用户取消
                return False, "生成已被用户取消"
            return True, f"第{ch}章正文"

        with mock.patch("cyber_printer_ultimate.generate_chapter_full",
                        side_effect=fake_full_v1):
            first = cyber_printer_ultimate.generate_chapter_batch(
                1, 3, 2000, "", novel_name=novel, kill_event=kill_event)
        self.assertEqual(called, [1, 2], "取消后不应继续启动第3章")
        self.assertEqual([r["status"] for r in first], ["ok", "failed", "cancelled"])

        # 再次启动：第1章已完成被跳过，仅续跑 2、3 章
        called.clear()
        kill_event2 = threading.Event()

        def fake_full_v2(ch, target_words, custom_prompt, novel_name="默认小说",
                         kill_event=None, progress_cb=None):
            called.append(ch)
            SQLiteMemoryPalace(novel_name).safe_update(ch, "摘要", 100, f"第{ch}章完整正文")
            return True, f"第{ch}章正文"

        with mock.patch("cyber_printer_ultimate.generate_chapter_full",
                        side_effect=fake_full_v2):
            second = cyber_printer_ultimate.generate_chapter_batch(
                1, 3, 2000, "", novel_name=novel, kill_event=kill_event2)
        self.assertEqual(called, [2, 3], "再启动不得重跑已完成章")
        self.assertEqual([r["status"] for r in second],
                         ["skipped_completed", "ok", "ok"])


if __name__ == "__main__":
    unittest.main()
