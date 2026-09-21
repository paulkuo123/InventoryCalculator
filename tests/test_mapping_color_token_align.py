"""Layer-2 colour / sku_name token alignment.

Synthetic fixtures mirror the #117 near-miss and FN clusters (same device,
different colour or print). No live crawl and no golden_table writes.
"""

import json
import os
import tempfile
import unittest

from mapping_knowledge import reset_match_category, set_match_category
from sku_mapping_service import (
    SkuMappingService,
    normalize_text,
    _abbreviated_color_equal,
    _sku_is_combo,
    _synonym_equal,
    _text_asks_for_combo,
)


def _sku(sku_id, name, second):
    return {
        "sku_id": sku_id,
        "sku_name": name,
        "second_name": second,
        "spec_text": f"{name},{second}",
        "parts": [name, second],
    }


class ColorTokenAlignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        golden = {"p": {"商品名稱": "手機殼", "型號": []}}
        with open(os.path.join(self.tmp.name, "golden_table.json"), "w", encoding="utf-8") as handle:
            json.dump(golden, handle)
        with open(os.path.join(self.tmp.name, "shopee_products.json"), "w", encoding="utf-8") as handle:
            json.dump(golden, handle)
        self.service = SkuMappingService(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _top(self, model_name, skus, product_name="iPhone 手機殼"):
        model = {"product_name": product_name, "model_name": model_name, "offer_id": "offer"}
        ranked = self.service.generate_candidates(model, skus)
        return [row["sku_id"] for row in ranked], ranked

    def test_normalize_simplified_color_and_print_tokens(self):
        pairs = (
            ("蔷薇粉", "薔薇粉"),
            ("烟灰蓝", "煙灰藍"),
            ("粉边", "粉邊"),
            ("涂鸦爱心", "塗鴉愛心"),
            ("镜面 猫咪围剿", "鏡面貓咪圍剿"),
            ("奶油黄 铃铛狗刺绣贴", "奶油黃鈴鐺狗刺繡貼"),
            ("磁吸 白边", "磁吸白邊"),
        )
        for simplified, traditional in pairs:
            self.assertEqual(normalize_text(simplified), normalize_text(traditional), simplified)

    def test_remaining_script_and_decoration_pairs_unify(self):
        pairs = (
            ("贝壳纹", "貝殼紋"),
            ("蝴蝶结", "蝴蝶結"),
            ("电镀", "電鍍"),
            ("发带", "髮帶"),
            ("面包头", "麵包頭"),
            ("韩粉", "韓粉"),
            ("千鸟格", "千鳥格"),
            ("饼干", "餅乾"),
            ("手链", "手鍊"),
            ("椭圆", "橢圓"),
            ("雾蓝", "霧藍"),
            ("少女粉.", "少女粉"),
            ("珍珠白。", "珍珠白"),
            ("圓形【鏡子】", "圓形鏡子"),
            ("白色熊猫🐼", "白色熊貓"),
            ("黑色爪印🐾", "黑色爪印"),
            ("綠色㉿", "綠色"),
            ("蓝白小花【单壳】", "藍白小花(單殼)"),
        )
        for left, right in pairs:
            self.assertEqual(normalize_text(left), normalize_text(right), left)

    def test_distinct_hues_and_sizes_stay_distinct(self):
        distinct = (
            ("白邊", "黑邊"),
            ("白邊", "粉邊"),
            ("黑邊", "綠邊"),
            ("奶酪白", "豆粉色"),
            ("豆粉色", "豆紫色"),
            ("薔薇粉", "奶油黃"),
            ("煙灰藍", "奶酪白"),
            ("海棠粉", "古董白"),
            ("塗鴉愛心", "lucky彩色愛心"),
            ("托腮奶茶熊", "吐司熊"),
            ("藍色", "海藍色"),
            ("11.深棕小熊", "咖色"),
            ("3.淺灰小熊", "淺灰"),
            ("45mm裸殼", "45mm"),
            ("鏡面貓咪+掛繩", "鏡面貓咪"),
        )
        for left, right in distinct:
            self.assertNotEqual(normalize_text(left), normalize_text(right), left)
        self.assertEqual(normalize_text("鏡面"), normalize_text("鏡麵"))
        self.assertNotIn("麵", normalize_text("鏡面愛心"))
        self.assertIn(",", normalize_text("白色>L"))
        self.assertIn("+", normalize_text("殼+掛繩"))

    def test_alias_still_matches_graphite_but_not_a_different_hue(self):
        self.assertTrue(_synonym_equal("黑色", "石墨黑"))
        self.assertFalse(_synonym_equal("藍色", "海藍色"))
        self.assertFalse(_synonym_equal("奶茶", "奶茶色"))
        self.assertTrue(_synonym_equal("粉膚邊", "粉边"))
        self.assertFalse(_synonym_equal("粉膚邊", "白边"))
        self.assertTrue(_synonym_equal("淡紫邊", "紫边"))
        self.assertFalse(_synonym_equal("淡紫邊", "粉边"))

    def test_edge_color_beats_sibling_edges_on_same_device(self):
        # 738755883311 pattern: 粉膚邊 → 粉边, not 白边 / 黑边. Pro Max stays exact.
        skus = [
            _sku("white-pro", "白边", "14pro"),
            _sku("white", "白边", "14promax"),
            _sku("black", "黑边", "14promax"),
            _sku("pink", "粉边", "14promax"),
            _sku("green", "绿边", "14promax"),
        ]
        ids, ranked = self._top("粉膚邊,14 Pro Max", skus)
        self.assertEqual(ids[0], "pink")
        self.assertNotIn("white-pro", ids)
        self.assertTrue(ranked[0]["evidence"]["complete"])

    def test_magsafe_acrylic_edge_ignores_filler_words(self):
        # 751755610831: 磁吸壓克力 白邊 → 磁吸 白边, not 磁吸 粉边.
        skus = [
            _sku("pink", "磁吸 粉边", "14plus"),
            _sku("black", "磁吸 黑边", "14plus"),
            _sku("white", "磁吸 白边", "14plus"),
            _sku("purple", "磁吸 紫边", "14plus"),
        ]
        ids, ranked = self._top("磁吸壓克力 白邊,14Plus/15Plus", skus)
        self.assertEqual(ids[0], "white")
        self.assertNotIn("pink", ids)
        self.assertTrue(ranked[0]["evidence"]["complete"])

    def test_embroidery_color_beats_sibling_motif(self):
        # 830801697147: colour head before 刺绣贴, simplified/traditional and spaces.
        skus = [
            _sku("rose", "蔷薇粉 哈气狗刺绣贴", "16plus"),
            _sku("bean", "豆粉色 泰迪狗刺绣贴", "16plus"),
            _sku("smoke", "烟灰蓝 凌乱狗刺绣贴", "16plus"),
            _sku("bean-pro", "豆粉色 泰迪狗刺绣贴", "16pro"),
        ]
        ids, ranked = self._top("豆粉色,16 Plus", skus)
        self.assertEqual(ids[0], "bean")
        self.assertNotIn("bean-pro", ids)
        self.assertNotIn("rose", ids)
        self.assertTrue(ranked[0]["evidence"]["complete"])

    def test_short_color_beats_unrelated_sibling(self):
        # 624135914858: 海棠粉 → 直边 棠粉, not 直边 古董白.
        skus = [
            _sku("white", "直边 古董白", "15pro"),
            _sku("slate", "直边 板岩蓝", "15pro"),
            _sku("pink", "直边 棠粉", "15pro"),
        ]
        ids, _ranked = self._top("海棠粉,15 Pro", skus)
        self.assertEqual(ids[0], "pink")
        self.assertNotIn("white", ids)

        note_skus = [
            _sku("pink", "直边 棠粉", "13"),
            _sku("white", "直边 古董白", "13"),
        ]
        ids, ranked = self._top("古董白(實品偏皮膚色),13", note_skus)
        self.assertEqual(ids[0], "white")
        self.assertTrue(ranked[0]["evidence"]["complete"])
        self.assertFalse(_synonym_equal("海棠粉", "古董白"))

    def test_hyphenated_print_prefers_tighter_tail(self):
        # 838528968671: 鏡面愛心 covers 涂鸦爱心 more tightly than lucky彩色爱心.
        skus = [
            _sku("lucky", "镜面奶油壳-透明-lucky彩色爱心", "苹果13"),
            _sku("graffiti", "镜面奶油壳-透明-涂鸦爱心", "苹果13"),
            _sku("cat", "镜面奶油壳-透明-猫咪", "苹果13"),
        ]
        ids, ranked = self._top("鏡面愛心,13", skus)
        self.assertEqual(ids[0], "graffiti")
        self.assertGreater(ranked[0]["evidence"]["style_bonus"], ranked[1]["evidence"].get("style_bonus", 0))
        self.assertNotEqual(ids[0], "cat")

    def test_combo_suffix_loses_to_bare_shell(self):
        # 745561940382: 鏡面貓咪(單殼) beats 围剿+掛繩.
        skus = [
            _sku("combo", "镜面 猫咪围剿+挂绳", "13pro"),
            _sku("bare", "镜面 猫咪围剿", "13pro"),
            _sku("other", "镜面 小狗", "13pro"),
            _sku("bare-max", "镜面 猫咪围剿", "13promax"),
        ]
        ids, ranked = self._top("鏡面貓咪(單殼),13 pro", skus)
        self.assertEqual(ids[0], "bare")
        self.assertNotIn("bare-max", ids)
        combo = next(row for row in ranked if row["sku_id"] == "combo")
        bare = ranked[0]
        self.assertTrue(combo["evidence"]["combo_demoted"])
        self.assertFalse(bare["evidence"]["combo_demoted"])
        self.assertGreater(bare["deterministic_score"], combo["deterministic_score"])

    def test_explicit_strap_request_keeps_combo_above_bare_shell(self):
        skus = [
            _sku("bare", "镜面 猫咪围剿", "13pro"),
            _sku("combo", "镜面 猫咪围剿+挂绳", "13pro"),
        ]
        ids, _ranked = self._top("鏡面貓咪掛繩,13 pro", skus)
        self.assertEqual(ids[0], "combo")

    def test_bear_style_beats_sibling_bear_when_name_is_present(self):
        # 626970345636 pattern when the style token is on the model.
        # A device-only Shopee model has no colour token to align.
        skus = [
            _sku("toast", "古董白吐司熊", "14pro"),
            _sku("tea", "古董白托腮奶茶熊", "14pro"),
            _sku("tea-max", "古董白托腮奶茶熊", "14promax"),
        ]
        ids, _ranked = self._top("托腮奶茶熊,14 pro", skus)
        self.assertEqual(ids[0], "tea")
        self.assertNotIn("tea-max", ids)
        self.assertNotIn("toast", ids)

    def test_phone_tier_boundary_is_unchanged(self):
        skus = [
            _sku("base", "黑边", "14"),
            _sku("pro", "黑边", "14pro"),
            _sku("max", "黑边", "14promax"),
        ]
        ids, _ranked = self._top("黑邊,14 Pro", skus)
        self.assertEqual(ids, ["pro"])

    def test_strap_markers_ignore_negative_and_hole_only_labels(self):
        self.assertTrue(_sku_is_combo("苹果16+挂绳"))
        self.assertTrue(_sku_is_combo("殼+掛繩"))
        self.assertTrue(_sku_is_combo("透明+吊繩"))
        self.assertTrue(_sku_is_combo("黑色+手機繩"))
        self.assertFalse(_sku_is_combo("不含掛件"))
        self.assertFalse(_sku_is_combo("透明可掛繩"))
        self.assertFalse(_sku_is_combo("單殼"))
        self.assertTrue(_text_asks_for_combo("奶油白掛繩"))
        self.assertFalse(_text_asks_for_combo("奶油白"))
        self.assertFalse(_text_asks_for_combo("不含掛繩,16"))
        self.assertFalse(_text_asks_for_combo("無掛繩"))
        self.assertFalse(_text_asks_for_combo("可掛繩"))
        self.assertFalse(_sku_is_combo("斜背包"))

    def test_second_name_strap_loses_to_same_device_bare_shell(self):
        # Strap lives on the device axis, so sku_name style alignment cannot see it.
        # Ids sort the strap row first when scores tie.
        skus = [
            _sku("a-strap", "黑色", "苹果16+挂绳"),
            _sku("b-bare", "黑色", "苹果16"),
            _sku("c-pro", "黑色", "苹果16pro"),
        ]
        ids, ranked = self._top("黑色,16", skus)
        self.assertEqual(ids[0], "b-bare")
        self.assertNotIn("c-pro", ids)
        strap = next(row for row in ranked if row["sku_id"] == "a-strap")
        self.assertTrue(strap["evidence"]["combo_demoted"])
        self.assertFalse(ranked[0]["evidence"]["combo_demoted"])
        self.assertGreater(ranked[0]["deterministic_score"], strap["deterministic_score"])

    def test_shell_axis_combo_loses_when_model_does_not_ask_for_strap(self):
        # 單殼 vs 殼+掛繩; colour sits on the other axis and does not align to the shell label.
        skus = [
            _sku("a-combo", "殼+掛繩", "奶油白苹果16"),
            _sku("b-bare", "單殼", "奶油白苹果16"),
            _sku("c-charm", "掛繩", "奶油白苹果16"),
        ]
        ids, ranked = self._top("奶油白,16", skus, product_name="iPhone 手機殼 附掛繩")
        self.assertEqual(ids[0], "b-bare")
        self.assertTrue(all(row["evidence"]["combo_demoted"] for row in ranked if row["sku_id"] != "b-bare"))

    def test_model_without_strap_bare_shell_wins_and_stays_in_review_window(self):
        # Same score, combo sku ids sort first. The review window is 4, so an
        # undemoted tie eliminates the bare shell (FN) instead of ranking it
        # second (near). Product-title 掛繩 must not count as a request.
        skus = [
            _sku("a-combo", "黑色+掛繩", "苹果16"),
            _sku("b-combo", "黑色+掛鏈", "苹果16"),
            _sku("c-combo", "黑色+手機繩", "苹果16"),
            _sku("d-combo", "黑色掛繩", "苹果16"),
            _sku("z-bare", "黑色", "苹果16"),
        ]
        ids, ranked = self._top("黑色,16", skus, product_name="iPhone 手機殼 附掛繩")
        self.assertEqual(ids[0], "z-bare")
        self.assertIn("z-bare", ids)
        self.assertLessEqual(len(ids), 4)
        bare = ranked[0]
        self.assertFalse(bare["evidence"]["combo_demoted"])
        self.assertTrue(all(row["evidence"]["combo_demoted"] for row in ranked if row["sku_id"] != "z-bare"))
        self.assertGreater(bare["deterministic_score"], ranked[1]["deterministic_score"])

    def test_explicit_strap_request_keeps_shell_axis_combo(self):
        skus = [
            _sku("a-bare", "單殼", "奶油白苹果16"),
            _sku("b-combo", "殼+掛繩", "奶油白苹果16"),
        ]
        ids, ranked = self._top("奶油白掛繩,16", skus)
        self.assertEqual(ids[0], "b-combo")
        bare = next(row for row in ranked if row["sku_id"] == "a-bare")
        self.assertGreater(ranked[0]["deterministic_score"], bare["deterministic_score"])
        self.assertFalse(ranked[0]["evidence"]["combo_demoted"])

    def test_unique_combo_is_not_demoted_under_green(self):
        skus = [_sku("only-combo", "奶油白+掛繩", "苹果16")]
        ids, ranked = self._top("奶油白,16", skus)
        self.assertEqual(ids, ["only-combo"])
        self.assertFalse(ranked[0]["evidence"]["combo_demoted"])
        self.assertTrue(ranked[0]["evidence"]["is_combo"])
        self.assertGreaterEqual(ranked[0]["deterministic_score"], 110)
        tier, reason = SkuMappingService.classify_review_tier("pending", ranked, "ok")
        self.assertEqual(tier, "green")
        self.assertIn("唯一候選", reason)

    def test_exact_strapped_tier_stays_above_slash_bare(self):
        # #117: exact 16 beats slash 16/16plus. A strap on the exact row must
        # not be demoted by a different device-token bare sibling.
        skus = [
            _sku("a-slash", "黑色", "iphone16/16plus"),
            _sku("b-strap", "黑色", "苹果16+挂绳"),
        ]
        ids, ranked = self._top("黑色,16", skus)
        self.assertEqual(ids[0], "b-strap")
        self.assertFalse(ranked[0]["evidence"]["combo_demoted"])
        self.assertGreater(ranked[0]["evidence"]["phone_tier_bonus"], ranked[1]["evidence"]["phone_tier_bonus"])

    def test_glitter_color_ignores_glued_pack_count(self):
        # 鏡頭貼：遠峰藍閃粉(一顆) must not tie 黑色闪粉单个 on the phone token.
        skus = [
            _sku("black", "黑色闪粉单个", "14pro/14promax/16pro/16promax"),
            _sku("silver", "银色闪粉单个", "14pro/14promax/16pro/16promax"),
            _sku("alpine", "远峰蓝闪粉单个", "14pro/14promax/16pro/16promax"),
            _sku("rainbow", "炫彩闪粉单个", "14pro/14promax/16pro/16promax"),
            _sku("alpine-11", "远峰蓝闪粉单个", "11系列/12mini/12/12pro"),
        ]
        ids, ranked = self._top(
            "遠峰藍閃粉(一顆),16pro/16proMax",
            skus,
            product_name="閃粉鏡頭貼 iPhone 16 15 14",
        )
        self.assertEqual(ids, ["alpine"])
        self.assertTrue(ranked[0]["evidence"]["complete"])
        self.assertFalse(_synonym_equal("遠峰藍閃粉(一顆)", "黑色闪粉单个"))
        self.assertTrue(_synonym_equal("遠峰藍閃粉(一顆)", "远峰蓝闪粉单个"))
        self.assertFalse(_abbreviated_color_equal("混彩色", "彩色"))

    def test_sock_denier_line_beats_same_denier_siblings(self):
        skus = [
            _sku("classic-15", "经典性感黑/15d", "均码"),
            _sku("upgrade-15", "升级款性感黑/15d", "均码"),
            _sku("dot-15", "波點黑丝/15d", "均码"),
            _sku("heart-15", "愛心黑丝/15d", "均码"),
            _sku("skin-15", "经典自然膚/15d", "均码"),
            _sku("classic-200", "经典性感黑/200d", "均码"),
            _sku("bikini-15", "比基尼性感黑/15d", "均码"),
        ]
        ids, ranked = self._top(
            "DS1112-黑絲襪經典款 15D",
            skus,
            product_name="黑絲襪 連褲襪 15D",
        )
        self.assertEqual(ids, ["classic-15"])
        self.assertTrue(ranked[0]["evidence"]["complete"])
        ids, _ranked = self._top(
            "DS2104-黑絲襪經典款 200D",
            skus,
            product_name="黑絲襪 連褲襪 200D",
        )
        self.assertEqual(ids, ["classic-200"])
        ids, _ranked = self._top(
            "DS1046-黑絲襪升級款 15D",
            skus,
            product_name="黑絲襪 連褲襪 15D",
        )
        self.assertEqual(ids, ["upgrade-15"])
        ids, _ranked = self._top(
            "DS1151-波點黑絲 15D",
            skus,
            product_name="黑絲襪 連褲襪 15D",
        )
        self.assertEqual(ids, ["dot-15"])
        ids, ranked = self._top(
            "DS1112-經典自然膚 15D",
            skus,
            product_name="黑絲襪 連褲襪 15D",
        )
        self.assertEqual(ids, ["skin-15"])
        self.assertNotIn("classic-15", ids)

    def test_charm_plush_filler_keeps_motif_and_color(self):
        skus = [
            _sku("khaki", "毛绒掛件-仿毛煤球【卡其色】", "吊飾"),
            _sku("black", "26#毛绒掛件-仿毛煤球黑色", "吊飾"),
            _sku("duck", "7#毛绒加油鸭-藍白", "吊飾"),
            _sku("duck-grey", "9#毛绒加油鸭-藍灰", "吊飾"),
            _sku("bunny", "毛绒煤球大白眼-焦糖咖", "吊飾"),
        ]
        ids, ranked = self._top(
            "毛絨煤球 - 卡其色",
            skus,
            product_name="ins韓風 吊飾 掛飾 手機掛件",
        )
        self.assertEqual(ids, ["khaki"])
        self.assertTrue(ranked[0]["evidence"]["complete"])
        ids, _ranked = self._top(
            "毛絨加油鴨 - 藍白",
            skus,
            product_name="ins韓風 吊飾 掛飾",
        )
        self.assertEqual(ids, ["duck"])
        ids, ranked = self._top(
            "可愛獺兔毛球 - 焦糖咖",
            skus,
            product_name="ins韓風 吊飾 掛飾",
        )
        self.assertNotIn("bunny", ids)
        self.assertNotIn("khaki", ids)

    def test_charm_clip_fillers_align_remaining_color(self):
        # Strap index ``#10深灰`` and clip catalogue copy must not hide the hue.
        # 丁香紫 / 象牙白 / 寶藍 / 暖陽黃 stay different colours.
        product = "手機掛繩 手機吊繩 夾片掛片 掛飾"
        skus = [
            _sku("grey", "【#10深灰】可调掛繩+一体注塑夹片", "吊飾"),
            _sku("wine-strap", "【#11酒紅】可调掛繩+一体注塑夹片", "吊飾"),
            _sku("wine-clip", "薄至0.6mm【d扣·酒紅色】注塑一体成型", "吊飾"),
            _sku("lilac", "薄至0.6mm【d扣·丁香紫】注塑一体成型", "吊飾"),
            _sku("ivory", "薄至0.6mm【d扣·象牙白】注塑一体成型", "吊飾"),
            _sku("gem", "薄至0.6mm【d扣·宝石蓝】注塑一体成型", "吊飾"),
            _sku("warm", "薄至0.6mm【d扣·暖阳黃】注塑一体成型", "吊飾"),
        ]
        ids, ranked = self._top("深灰色掛繩+透明夾片", skus, product_name=product)
        self.assertEqual(ids, ["grey"])
        self.assertTrue(ranked[0]["evidence"]["complete"])
        ids, ranked = self._top("酒紅夾片", skus, product_name=product)
        self.assertEqual(ids[0], "wine-clip")
        self.assertIn("wine-strap", ids)
        self.assertLess(ids.index("wine-clip"), ids.index("wine-strap"))
        self.assertTrue(ranked[0]["evidence"]["complete"])
        ids, _ranked = self._top("紫色夾片", skus, product_name=product)
        self.assertNotIn("lilac", ids)
        ids, _ranked = self._top("白色夾片", skus, product_name=product)
        self.assertNotIn("ivory", ids)
        ids, _ranked = self._top("藍色夾片", skus, product_name=product)
        self.assertNotIn("gem", ids)
        ids, _ranked = self._top("暖黃夾片", skus, product_name=product)
        self.assertNotIn("warm", ids)
        token = set_match_category("charm")
        try:
            self.assertTrue(_synonym_equal(
                "深灰色掛繩+透明夾片", "【#10深灰】可调掛繩+一体注塑夹片",
            ))
            self.assertTrue(_synonym_equal(
                "酒紅夾片", "薄至0.6mm【d扣·酒紅色】注塑一体成型",
            ))
            self.assertFalse(_synonym_equal("紫色", "丁香紫"))
            self.assertFalse(_synonym_equal(
                "紫色夾片", "薄至0.6mm【d扣·丁香紫】注塑一体成型",
            ))
            self.assertFalse(_synonym_equal("白色", "象牙白"))
            self.assertFalse(_synonym_equal(
                "白色夾片", "薄至0.6mm【d扣·象牙白】注塑一体成型",
            ))
            self.assertFalse(_synonym_equal("藍色", "海藍色"))
            self.assertFalse(_synonym_equal("暖黃", "暖陽黃"))
        finally:
            reset_match_category(token)

    def test_standalone_size_letters_alias_chinese_sizes(self):
        skus = [
            _sku("s", "小號", "均碼"),
            _sku("m", "中號", "均碼"),
            _sku("l", "大號", "均碼"),
            _sku("code", "S29", "均碼"),
        ]
        product = "旅行收納包"
        ids, ranked = self._top("S", skus, product_name=product)
        self.assertEqual(ids[0], "s")
        self.assertTrue(ranked[0]["evidence"]["complete"])
        self.assertNotIn("m", ids)
        self.assertNotIn("l", ids)
        ids, _ranked = self._top("M", skus, product_name=product)
        self.assertEqual(ids[0], "m")
        ids, _ranked = self._top("L", skus, product_name=product)
        self.assertEqual(ids[0], "l")
        ids, _ranked = self._top("小號", [
            _sku("s", "S", "均碼"),
            _sku("m", "M", "均碼"),
            _sku("l", "L", "均碼"),
        ], product_name=product)
        self.assertEqual(ids, ["s"])
        ids, _ranked = self._top("S29", skus, product_name=product)
        self.assertEqual(ids, ["code"])
        self.assertTrue(_synonym_equal("S", "小號"))
        self.assertTrue(_synonym_equal("M", "中號"))
        self.assertTrue(_synonym_equal("L", "大號"))
        self.assertFalse(_synonym_equal("S", "中號"))
        self.assertFalse(_synonym_equal("S", "大號"))
        self.assertFalse(_synonym_equal("S29", "小號"))
        self.assertFalse(_synonym_equal("sl061", "小號"))
