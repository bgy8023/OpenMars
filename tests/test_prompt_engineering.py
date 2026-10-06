#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词工程维度单测（python -m unittest tests.test_prompt_engineering -v）：

1. OUTLINE/REVIEW/BODY 三套模板：占位符全部填充、渲染无残留{、稳定节位于可变节之前、末尾【警告·绝对不能】
2. 场景计划字段卡容错解析：规范卡解析且配额总和在目标±30%；-/#/全角冒号等花式格式仍可解析
3. 长章(target_words>=5000)逐段生成：>=2次段调用且拼接无跨段重复句；短章仍单次调用
4. 排雷四字段条目解析；空清单必须携带理由字段否则判坏输出重试
5. 「上一章结尾」衔接块：有记忆时位于prompt尾部，第一章无此节
6. 番茄铁则：文件存在用文件、缺失兜底不报错；BODY渲染含AI味规则节关键词
"""
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openmars_core import prompts
from openmars_core.query_engine import (
    SyncQueryEngine,
    _OVERLAP_PUNCT_RE,
    _trim_overlap,
)

# 归一化骨架：剔标点与空白，只比句子骨架
def _skeleton(text):
    return _OVERLAP_PUNCT_RE.sub("", text or "")


def make_outline_text(target_words=7500, n=10):
    """规范的场景计划字段卡（模拟大纲步模型输出）"""
    quota = round(target_words / n)
    lines = ["本章简纲：主角深入矿坑，与反派正面冲突，结尾发现身世线索。"]
    lines += [f"场景{i}-事件{i}：冲突升级，主线推进-{quota}" for i in range(1, n + 1)]
    return "\n".join(lines)


REVIEW_OK = """【毒点1】
类别：人设崩塌
位置：场景3
原文引用：「主角当众跪地求饶」
伤害说明：与第一章立下的狠辣人设冲突，读者会弃书
规避动作：改为表面周旋、暗中记恨"""


class TemplateRenderTests(unittest.TestCase):
    """验收1：三套模板占位符全填充、无残留{、稳定节在可变节之前"""

    def render_all(self):
        outline_sys, outline_user = prompts.render_outline_prompts(
            fixed_memory="固定设定SENTINEL", dynamic_memory="前情SENTINEL",
            chapter_num=7, custom_prompt="剧情要求SENTINEL", target_words=7500)
        review_sys, review_user = prompts.render_review_prompts(
            fixed_memory="固定设定SENTINEL", dynamic_memory="前情SENTINEL",
            chapter_num=7, chapter_outline="大纲SENTINEL")
        body_sys, body_user = prompts.render_body_prompts(
            chapter_num=7, target_words=7500, fixed_memory="固定设定SENTINEL",
            dynamic_memory="前情SENTINEL", chapter_outline="大纲SENTINEL",
            scene_plan="场景计划SENTINEL", strict_warnings="毒点SENTINEL",
            tomato_rules="铁则SENTINEL")
        return (outline_sys, outline_user, review_sys, review_user,
                body_sys, body_user)

    def test_named_template_pairs_exist(self):
        # 三套模板以 SYSTEM+USER 对的具名常量存在
        for name in ("OUTLINE_SYSTEM", "OUTLINE_USER", "REVIEW_SYSTEM",
                     "REVIEW_USER", "BODY_SYSTEM", "BODY_USER"):
            self.assertTrue(hasattr(prompts, name), f"缺少模板常量 {name}")
            self.assertIsInstance(getattr(prompts, name), str)

    def test_all_placeholders_filled_and_no_leftover_brace(self):
        (outline_sys, outline_user, review_sys, review_user,
         body_sys, body_user) = self.render_all()
        for name, rendered in [("outline_user", outline_user), ("review_user", review_user),
                               ("body_user", body_user), ("outline_sys", outline_sys),
                               ("review_sys", review_sys), ("body_sys", body_sys)]:
            self.assertNotIn("{", rendered, f"{name} 渲染后残留未填充占位符")
        # 哨兵值逐一出现，证明占位符确实被填充而非留空
        self.assertIn("固定设定SENTINEL", outline_user)
        self.assertIn("前情SENTINEL", outline_user)
        self.assertIn("剧情要求SENTINEL", outline_user)
        self.assertIn("第7章", outline_user)
        self.assertIn("7500", outline_user)
        self.assertIn("大纲SENTINEL", review_user)
        self.assertIn("大纲SENTINEL", body_user)
        self.assertIn("场景计划SENTINEL", body_user)
        self.assertIn("毒点SENTINEL", body_user)

    def test_stable_sections_before_variable_sections(self):
        # 缓存友好布局：稳定大块（设定/铁则）在前、任务指令在后
        (_, outline_user, _, review_user, _, body_user) = self.render_all()
        self.assertLess(outline_user.index("<static_worldview>"), outline_user.index("本章任务"))
        self.assertLess(review_user.index("<static_worldview>"), review_user.index("<chapter_outline>"))
        self.assertLess(review_user.index("<chapter_outline>"), review_user.index("本章任务"))
        self.assertLess(body_user.index("<world_rules>"), body_user.index("<previous_chapters>"))
        self.assertLess(body_user.index("<previous_chapters>"), body_user.index("<chapter_outline>"))
        self.assertLess(body_user.index("<chapter_outline>"), body_user.index("=== 写作任务 ==="))

    def test_warning_section_at_end_of_every_user_prompt(self):
        # user prompt 末尾统一【警告·绝对不能】禁止清单节（91Writing模板）
        (_, outline_user, _, review_user, _, body_user) = self.render_all()
        for name, user in [("outline", outline_user), ("review", review_user), ("body", body_user)]:
            self.assertIn("【警告·绝对不能】", user, f"{name} 缺少警告节")
            tail = user[user.index("【警告·绝对不能】"):]
            self.assertIn("绝对不能", tail)
            # 警告节之后不允许再出现其它 === 节
            self.assertNotIn("=== ", tail, f"{name} 警告节不是最后一节")


class ScenePlanParseTests(unittest.TestCase):
    """验收2：场景计划字段卡容错解析"""

    def test_canonical_cards_parse_and_quota_sum_within_30pct(self):
        text = make_outline_text(target_words=7500, n=10)
        scenes = prompts.parse_scene_plan(text, 7500)
        self.assertEqual(len(scenes), 10)
        total = sum(s["words"] for s in scenes)
        self.assertGreaterEqual(total, 7500 * 0.7, "配额总和低于目标-30%")
        self.assertLessEqual(total, 7500 * 1.3, "配额总和高于目标+30%")
        for s in scenes:
            self.assertTrue(200 <= s["words"] <= 1000)
            self.assertTrue(s["gist"])

    def test_fancy_formats_with_markdown_and_fullwidth_colon(self):
        text = "\n".join([
            "- 场景1：主角夜探矿洞 - 800字",
            "### 场景2、遭遇塌方：字数750",
            "* 场景三 - 与反派正面对峙 - 700",
            "1. 场景4：携手脱困，配额：900",
            "【场景5】后备方案启动～850",
        ])
        scenes = prompts.parse_scene_plan(text, 4000)
        self.assertEqual([s["words"] for s in scenes], [800, 750, 700, 900, 850])
        self.assertEqual([s["index"] for s in scenes], [1, 2, 3, 4, 5])
        self.assertIn("夜探矿洞", scenes[0]["gist"])
        self.assertIn("遭遇塌方", scenes[1]["gist"])
        self.assertIn("正面对峙", scenes[2]["gist"])
        self.assertIn("携手脱困", scenes[3]["gist"])
        self.assertIn("后备方案", scenes[4]["gist"])

    def test_missing_words_field_gets_default_quota(self):
        text = "场景1-只有要点没写配额\n场景2-也没写配额"
        scenes = prompts.parse_scene_plan(text, 2000)
        self.assertEqual(len(scenes), 2)
        for s in scenes:
            self.assertEqual(s["words"], 1000)  # 2000/2=1000，收敛进200-1000

    def test_total_parse_failure_returns_empty(self):
        self.assertEqual(prompts.parse_scene_plan("与场景计划无关的散文段落。", 7500), [])
        self.assertEqual(prompts.parse_scene_plan("", 7500), [])

    def test_bad_plan_judgement_and_default_plan(self):
        self.assertTrue(prompts.is_scene_plan_bad([]))
        self.assertTrue(prompts.is_scene_plan_bad(prompts.parse_scene_plan("场景1-只有一场-500", 7500)))
        self.assertFalse(prompts.is_scene_plan_bad(prompts.parse_scene_plan(make_outline_text(), 7500)))
        # 兜底计划：3-15场、单场200-1000、配额合计贴近目标
        plan = prompts.build_default_scene_plan(7500)
        self.assertTrue(3 <= len(plan) <= 15)
        total = sum(s["words"] for s in plan)
        self.assertLessEqual(abs(total - 7500), 0.3 * 7500)


class MineListParseTests(unittest.TestCase):
    """验收4：排雷四字段条目解析；空清单必须携带理由否则判坏"""

    def test_card_entries_parsed_as_four_field_objects(self):
        text = """【毒点1】
类别：人设崩塌
位置：场景3
原文引用：「主角当众跪地求饶」
伤害说明：与狠辣人设冲突
规避动作：改为表面周旋

【毒点2】
类型：时间线矛盾
出处：场景5
原文：「三天前他还在京城」
危害：与上一章赶路时间对不上
建议：改为半月前出发"""
        parsed = prompts.parse_mine_list(text)
        self.assertTrue(parsed["valid"])
        self.assertEqual(len(parsed["items"]), 2)
        first, second = parsed["items"]
        # 四要素（位置/原文引用/伤害说明/规避动作）+类别，别名表字段名也能归位
        self.assertEqual(first["category"], "人设崩塌")
        self.assertEqual(first["location"], "场景3")
        self.assertIn("跪地求饶", first["quote"])
        self.assertIn("人设冲突", first["harm"])
        self.assertIn("表面周旋", first["action"])
        self.assertEqual(second["category"], "时间线矛盾")
        self.assertIn("京城", second["quote"])
        self.assertIn("对不上", second["harm"])
        self.assertIn("半月前", second["action"])

    def test_no_issue_without_reason_is_invalid(self):
        self.assertFalse(prompts.parse_mine_list("未发现")["valid"])
        self.assertFalse(prompts.parse_mine_list("我觉得写得还行")["valid"])
        self.assertFalse(prompts.parse_mine_list("")["valid"])

    def test_no_issue_with_reason_is_valid(self):
        parsed = prompts.parse_mine_list("未发现-已对照人物档案与世界观，四类毒点均无冲突")
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["items"], [])
        self.assertIn("四类毒点均无冲突", parsed["no_issue_reason"])
        # 「未发现毒点」独占一行、理由在下一行的写法也要兜住
        parsed2 = prompts.parse_mine_list("未发现毒点\n理由：四类逐一排查，设定自洽")
        self.assertTrue(parsed2["valid"])
        self.assertIn("设定自洽", parsed2["no_issue_reason"])

    def test_format_mine_list_roundtrip(self):
        parsed = prompts.parse_mine_list(REVIEW_OK)
        rendered = prompts.format_mine_list(parsed)
        self.assertIn("【毒点1】人设崩塌", rendered)
        self.assertIn("伤害说明", rendered)
        self.assertIn("规避动作", rendered)


class EndingBlockTests(unittest.TestCase):
    """验收5：「上一章结尾」衔接块位于尾部；第一章无此节"""

    def render(self, ending_excerpt=""):
        _, user = prompts.render_body_prompts(
            chapter_num=2, target_words=2000, fixed_memory="设定",
            dynamic_memory="前情", chapter_outline="大纲",
            scene_plan="场景1-要点-600", strict_warnings="无",
            ending_excerpt=ending_excerpt)
        return user

    def test_ending_block_present_and_located_at_tail(self):
        user = self.render("上一章结尾原文：夜色中他合上了房门。")
        self.assertIn("<previous_ending>", user)
        self.assertIn("=== 上一章结尾", user)
        # 位于尾部：在大纲、场景计划与写作任务之后
        self.assertGreater(user.index("<previous_ending>"), user.index("<chapter_outline>"))
        self.assertGreater(user.index("<previous_ending>"), user.index("场景1-要点-600"))
        self.assertGreater(user.index("<previous_ending>"), user.index("=== 写作任务 ==="))
        # 声明：时间线不符以本章大纲为准，只作文风与情节衔接
        self.assertIn("以<chapter_outline>为准", user)
        self.assertIn("文风与情节衔接", user)

    def test_first_chapter_has_no_ending_block(self):
        user = self.render("")
        self.assertNotIn("<previous_ending>", user)
        self.assertNotIn("=== 上一章结尾", user)

    def test_ending_excerpt_clamped_to_800_chars(self):
        long_text = "字" * 100 + "尾" * 800
        user = self.render(long_text)
        self.assertNotIn("字" * 100, user)          # 超长时开头被裁掉
        self.assertIn("尾" * 800, user)             # 只保留结尾800字


class TomatoRulesAndAIFlavorTests(unittest.TestCase):
    """验收6：番茄铁则文件/兜底；BODY渲染含AI味规则节关键词"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmpdir = None

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_missing_rules_file_falls_back_without_error(self):
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)  # 干净目录：novel_settings下无任何铁则文件
        rules = prompts.load_tomato_rules("不存在的书")
        self.assertEqual(rules, prompts.DEFAULT_TOMATO_RULES)
        self.assertEqual(prompts.load_tomato_rules(None), prompts.DEFAULT_TOMATO_RULES)

    def test_rules_file_used_when_present(self):
        self._tmpdir = tempfile.mkdtemp()
        book_dir = os.path.join(self._tmpdir, "novel_settings", "铁则测试书")
        os.makedirs(book_dir)
        with open(os.path.join(book_dir, "03-番茄审核铁则.md"), "w", encoding="utf-8") as f:
            f.write("自定义铁则MARK：主角不得滥杀无辜")
        os.chdir(self._tmpdir)
        rules = prompts.load_tomato_rules("铁则测试书")
        self.assertIn("自定义铁则MARK", rules)

    def test_resolve_dedups_when_fixed_memory_carries_rules(self):
        resolved = prompts.resolve_tomato_rules("", "## 番茄审核铁则\n已随固定设定注入")
        self.assertIn("已包含在上方<world_rules>", resolved)
        self.assertEqual(prompts.resolve_tomato_rules("显式铁则", ""), "显式铁则")
        self.assertEqual(prompts.resolve_tomato_rules("", ""), prompts.DEFAULT_TOMATO_RULES)

    def test_body_render_contains_ai_flavor_keywords(self):
        _, user = prompts.render_body_prompts(
            chapter_num=1, target_words=2000, fixed_memory="设定",
            dynamic_memory="前情", chapter_outline="大纲",
            scene_plan="场景1-要点-600", strict_warnings="无")
        # AI味规则节关键词：套话频次上限、张力波峰、对话比例
        self.assertIn("AI味自查规则", user)
        self.assertIn("瞳孔骤缩", user)
        self.assertIn("眼里亮起光芒", user)
        self.assertIn("不是……而是", user)
        self.assertIn("张力波峰", user)
        self.assertIn("30%", user)
        self.assertIn("频次", user)
        # 番茄铁则节始终存在（缺文件时用内置兜底）
        self.assertIn("番茄审核铁则", user)


class _FakeEngineTestBase(unittest.TestCase):
    """引擎级测试公共基类：mock掉call_llm_sync与sleep"""

    def run_pipeline(self, target_words, outline_outputs, review_outputs, ending_excerpt=""):
        calls = {"outline": 0, "review": 0, "content": 0}
        echoes = []           # 逐段续写时各段复述的已写结尾
        content_prompts = []  # 正文步收到的user prompt
        seg_counter = {"n": 0}
        outline_outputs = list(outline_outputs)
        review_outputs = list(review_outputs)

        def fake_call(user_prompt, system_prompt="你是专业助手", step="unknown", chapter=None):
            calls[step] += 1
            if step == "outline":
                return outline_outputs.pop(0) if len(outline_outputs) > 1 else outline_outputs[0]
            if step == "review":
                return review_outputs.pop(0) if len(review_outputs) > 1 else review_outputs[0]
            content_prompts.append(user_prompt)
            seg_counter["n"] += 1
            n = seg_counter["n"]
            m = re.search(r"<written_so_far>\n(.*?)\n</written_so_far>", user_prompt, re.S)
            if m:
                tail = m.group(1).strip()
                echoes.append(tail[-14:])  # 模拟免费模型复述已写结尾的毛病
                # 每段以唯一编号收尾，保证「重复句」只可能来自复述而非撞词
                return tail[-14:] + f"矿坑深处钟声骤响，双方同时出手，火花散尽，第{n}场落幕。"
            return f"主角踏进矿坑第{n}段：头顶钟乳石滴水，火把在风里摇晃。反派从阴影里走出，两人刀锋相向，第{n}场开幕。"

        with mock.patch.object(SyncQueryEngine, "call_llm_sync", side_effect=fake_call), \
                mock.patch("time.sleep"):
            engine = SyncQueryEngine()
            result = engine.run_chapter_pipeline(
                1, target_words, "固定设定", "前情记忆", "主角爆发",
                ending_excerpt=ending_excerpt)
        return result, calls, echoes, content_prompts


class EngineSegmentLoopTests(_FakeEngineTestBase):
    """验收3：长章逐段生成（>=2次段调用且无跨段重复句），短章单次调用"""

    def test_long_chapter_7500_generates_per_segment_without_duplicate_sentences(self):
        result, calls, echoes, content_prompts = self.run_pipeline(
            7500, [make_outline_text(7500, 10)], [REVIEW_OK],
            ending_excerpt="上一章结尾：他合上了房门，熄了灯。")
        self.assertGreaterEqual(calls["content"], 2, "长章应产生>=2次段调用")
        self.assertEqual(calls["content"], 10)  # 10个场景逐段生成
        self.assertEqual(result["body_calls"], 10)
        self.assertGreater(len(result["content"]), 0)
        # 拼接无跨段重复句：每个被复述的结尾骨架在全章正文中只出现1次
        full_skeleton = _skeleton(result["content"])
        for echo in echoes:
            self.assertEqual(full_skeleton.count(_skeleton(echo)), 1,
                             f"跨段重复句未被修剪: {echo}")
        # 衔接块注入：每个段prompt都带上一章结尾
        self.assertTrue(all("<previous_ending>" in p for p in content_prompts))

    def test_short_chapter_2000_stays_single_call(self):
        result, calls, _, content_prompts = self.run_pipeline(
            2000, [make_outline_text(2000, 3)], [REVIEW_OK])
        self.assertEqual(calls["content"], 1, "短章应维持整章单次调用")
        self.assertEqual(result["body_calls"], 1)
        self.assertNotIn("<written_so_far>", content_prompts[0])

    def test_bad_outline_plan_regenerates_once(self):
        # 第一次大纲只有1个场景（3-15越界判坏计划）→ 重生成一次 → 第二次合格
        result, calls, _, _ = self.run_pipeline(
            7500, ["场景1-只有一个场景-500", make_outline_text(7500, 10)], [REVIEW_OK])
        self.assertEqual(calls["outline"], 2, "坏计划应重生成一次")
        self.assertEqual(calls["content"], 10)

    def test_invalid_review_retries_once(self):
        # 第一次排雷输出既无毒点卡也无「未发现+理由」→ 判坏重试一次
        result, calls, _, content_prompts = self.run_pipeline(
            2000, [make_outline_text(2000, 3)], ["我觉得写得还行", REVIEW_OK])
        self.assertEqual(calls["review"], 2, "坏排雷输出应重试一次")
        self.assertIn("人设崩塌", content_prompts[0])  # 重试后的结构化毒点卡进入正文prompt


class TrimOverlapTests(unittest.TestCase):
    """防复读修剪单元行为"""

    def test_echo_sentence_trimmed(self):
        prev = "火光照亮矿壁。他握紧了手中的剑。"
        new = "他握紧了手中的剑。然后转身离开。"
        self.assertEqual(_trim_overlap(prev, new), "然后转身离开。")

    def test_unrelated_text_untouched(self):
        prev = "火光照亮矿壁。他握紧了手中的剑。"
        new = "远处传来脚步声，火把一支支熄灭。"
        self.assertEqual(_trim_overlap(prev, new), new)

    def test_short_overlap_not_trimmed_to_avoid_false_positive(self):
        # 重叠骨架不足6字不剪（白名单防误伤原则）
        prev = "……他说来就来。"
        new = "他说来就来去自如，全然不惧。"
        self.assertEqual(_trim_overlap(prev, new), new)

    def test_full_echo_returns_empty(self):
        prev = "他握紧了手中的剑，转身离开。"
        self.assertEqual(_trim_overlap(prev, "他握紧了手中的剑，转身离开。"), "")


if __name__ == "__main__":
    unittest.main()
