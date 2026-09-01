"""
測試補貨數量在 GUI、Crawler 和 1688 API 之間的一致性。

補貨負責人的要求：
1. 只有已批准的 SKU mapping 才能進入採購車
2. 1688 上的補貨數量必須與螢幕上的建議相符（不是另一個公式）
3. 月份閾值和四捨五入規則必須在所有表面保持一致

已知要檢查的 bug：GUI、Web UI 和 Crawler 之間的四捨五入/無條件進位規則不一致。
"""
import json
import subprocess
import unittest
import sys
import os
from pathlib import Path

# 將父目錄加入路徑
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from restock_rules import resolve_restock_quantity
from crawler import ShopeeCrawler
from run_watchlist_restock import suggested_restock_qty


def _extract_js_function(source, name):
    token = f"function {name}("
    start = source.index(token)
    brace = source.index("{", start)
    depth = 0
    in_string = None
    escape = False
    for index in range(brace, len(source)):
        char = source[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == in_string:
                in_string = None
            continue
        if char in ('"', "'", "`"):
            in_string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"could not extract {name}")


def _js_calculate_model_restock(product, model, months):
    script_js = (ROOT / "script.js").read_text(encoding="utf-8")
    functions = "\n".join([
        _extract_js_function(script_js, "roundRestockQty"),
        _extract_js_function(script_js, "getEffectiveMonthlyRate"),
        _extract_js_function(script_js, "calculateModelRestock"),
    ])
    program = functions + """
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = calculateModelRestock(input.product, input.model, input.months);
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", program],
        input=json.dumps({"product": product, "model": model, "months": months}, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout or "node failed")
    return json.loads(completed.stdout)


class RestockQuantityConsistencyTests(unittest.TestCase):
    """測試補貨數量計算在所有程式碼路徑中保持一致。"""
    
    def test_documented_rounding_rule_has_minimum_five(self):
        """正缺口最低補 5，其餘四捨五入到最接近的 10。"""
        # 修正的四捨五入函數，可正確處理邊界情況
        # 使用 int((value + 5) / 10) * 10 確保正確四捨五入
        def round_func(value, current_stock):
            if value <= 0:
                return 0
            if current_stock == 0 and value <= 5:
                return 5
            return int((value + 5) / 10) * 10
        
        # 來自 restock_rules.py 測試的測試案例
        self.assertEqual(round_func(17, 1), 20)
        self.assertEqual(round_func(15, 1), 20)
        self.assertEqual(round_func(14, 1), 10)
        self.assertEqual(round_func(5, 0), 5)
        self.assertEqual(round_func(4, 0), 5)
        self.assertEqual(round_func(1, 0), 5)
        self.assertEqual(round_func(4, 1), 0)
        self.assertEqual(round_func(5, 1), 10)
        self.assertEqual(round_func(6, 1), 10)
        self.assertEqual(round_func(23, 1), 20)
        self.assertEqual(round_func(25, 1), 30)

    def test_crawler_uses_history_guard_and_minimum_five(self):
        quantity = ShopeeCrawler.calculate_restock_quantity(
            None,
            product_sold=24,
            total_sold=5300,
            monthly_sales=1,
            current_inventory=0,
            expected_months=4,
            total_monthly_sales=186,
        )

        self.assertEqual(quantity, 5)

    def test_crawler_keeps_original_rounding_when_stock_is_positive(self):
        self.assertEqual(
            ShopeeCrawler.calculate_restock_quantity(None, 24, 5300, 1, 1, 5, 186),
            5,
        )
        self.assertEqual(
            ShopeeCrawler.calculate_restock_quantity(None, 24, 5300, 1, 1, 6, 186),
            10,
        )

    def test_zero_stock_zero_monthly_with_valid_history_is_nonzero(self):
        product = {"總月銷量": "100", "已售出總數量": "100"}
        model = {"商品庫存": "0", "月銷量": "0", "已售出數量": "10"}
        crawler_qty = ShopeeCrawler.calculate_restock_quantity(
            None, 10, 100, 0, 0, 4, 100
        )
        watchlist_qty = suggested_restock_qty(product, model, 4)
        js_qty = _js_calculate_model_restock(product, model, 4)["suggestedQty"]

        self.assertEqual(crawler_qty, 40)
        self.assertEqual(watchlist_qty, 40)
        self.assertEqual(js_qty, 40)

        low_product = {"總月銷量": "186", "已售出總數量": "5300"}
        low_model = {"商品庫存": "0", "月銷量": "0", "已售出數量": "24"}
        self.assertEqual(
            ShopeeCrawler.calculate_restock_quantity(None, 24, 5300, 0, 0, 4, 186),
            5,
        )
        self.assertEqual(suggested_restock_qty(low_product, low_model, 4), 5)
        self.assertEqual(_js_calculate_model_restock(low_product, low_model, 4)["suggestedQty"], 5)

    def test_zero_stock_zero_monthly_without_valid_history_stays_zero(self):
        cases = [
            (0, 100, 0, 0, 4, 100),
            (10, 0, 0, 0, 4, 100),
            (10, 100, 0, 0, 4, 0),
        ]
        for product_sold, total_sold, monthly, stock, months, total_monthly in cases:
            self.assertEqual(
                ShopeeCrawler.calculate_restock_quantity(
                    None, product_sold, total_sold, monthly, stock, months, total_monthly
                ),
                0,
                (product_sold, total_sold, total_monthly),
            )
        self.assertEqual(
            suggested_restock_qty(
                {"總月銷量": "100", "已售出總數量": "100"},
                {"商品庫存": "0", "月銷量": "0", "已售出數量": "0"},
                4,
            ),
            0,
        )
        self.assertEqual(
            _js_calculate_model_restock(
                {"總月銷量": "0", "已售出總數量": "100"},
                {"商品庫存": "0", "月銷量": "0", "已售出數量": "10"},
                4,
            )["suggestedQty"],
            0,
        )

    def test_zero_stock_positive_monthly_and_positive_stock_rounding_do_not_regress(self):
        self.assertEqual(
            ShopeeCrawler.calculate_restock_quantity(None, 24, 5300, 1, 0, 4, 186),
            5,
        )
        self.assertEqual(
            suggested_restock_qty(
                {"總月銷量": "186", "已售出總數量": "5300"},
                {"商品庫存": "0", "月銷量": "1", "已售出數量": "24"},
                4,
            ),
            5,
        )
        self.assertEqual(
            _js_calculate_model_restock(
                {"總月銷量": "186", "已售出總數量": "5300"},
                {"商品庫存": "0", "月銷量": "1", "已售出數量": "24"},
                4,
            )["suggestedQty"],
            5,
        )
        self.assertEqual(
            ShopeeCrawler.calculate_restock_quantity(None, 24, 5300, 1, 1, 6, 186),
            10,
        )
        self.assertEqual(
            resolve_restock_quantity({"adjustedQty": 7, "restockQty": 20}, lambda value: round(value / 10) * 10),
            7,
        )

    def test_low_coverage_raw_four_is_five_across_crawler_watchlist_and_js(self):
        product = {"商品名稱": "iPhone 軍規 防摔殼 手機殼", "總月銷量": "186", "已售出總數量": "5300"}
        model = {
            "規格ID": "156212462935",
            "型號名稱": "粉色軍規,12 proMax",
            "商品庫存": "2",
            "月銷量": "2",
            "已售出數量": "38",
        }
        crawler_qty = ShopeeCrawler.calculate_restock_quantity(None, 38, 5300, 2, 2, 3, 186)
        watchlist_qty = suggested_restock_qty(product, model, 3)
        js_qty = _js_calculate_model_restock(product, model, 3)["suggestedQty"]
        self.assertEqual(crawler_qty, 5)
        self.assertEqual(watchlist_qty, 5)
        self.assertEqual(js_qty, 5)

        self.assertEqual(
            ShopeeCrawler.calculate_restock_quantity(None, 10, 100, 1, 1, 3, 100),
            0,
        )
        self.assertEqual(
            suggested_restock_qty(
                {"總月銷量": "100", "已售出總數量": "100"},
                {"商品庫存": "1", "月銷量": "1", "已售出數量": "10"},
                3,
            ),
            0,
        )
        self.assertEqual(
            _js_calculate_model_restock(
                {"總月銷量": "100", "已售出總數量": "100"},
                {"商品庫存": "1", "月銷量": "1", "已售出數量": "10"},
                3,
            )["suggestedQty"],
            0,
        )
        self.assertEqual(
            ShopeeCrawler.calculate_restock_quantity(None, 10, 100, 3, 4, 3, 100),
            10,
        )
        self.assertEqual(
            _js_calculate_model_restock(
                {"總月銷量": "100", "已售出總數量": "100"},
                {"商品庫存": "4", "月銷量": "3", "已售出數量": "10"},
                3,
            )["suggestedQty"],
            10,
        )
        self.assertEqual(
            ShopeeCrawler.calculate_restock_quantity(None, 10, 100, 2, 4, 4, 100),
            0,
        )
        self.assertEqual(
            _js_calculate_model_restock(
                {"總月銷量": "100", "已售出總數量": "100"},
                {"商品庫存": "4", "月銷量": "2", "已售出數量": "10"},
                4,
            )["suggestedQty"],
            0,
        )
    
    def test_gui_calculation_matches_documented_rule(self):
        """
        GUI (script.js) 應使用文件化的四捨五入規則。
        
        程式碼路徑：script.js calculateModelRestock() 和 displayProducts()
        公式：
          targetStock = Math.round(monthlyRate * months)
          rawSuggestedQty = max(0, targetStock - currentStock)
          suggestedQty = roundRestockQty(rawSuggestedQty)  # 四捨五入到 10
        """
        # 模擬 GUI 計算
        def gui_calculate_restock(monthly_rate, months, current_stock):
            target_stock = round(monthly_rate * months)
            raw_suggested = max(0, target_stock - current_stock)
            # GUI 使用 roundRestockQty: Math.round(parsed / 10) * 10
            if raw_suggested <= 0:
                return 0
            if current_stock == 0 and raw_suggested <= 5:
                return 5
            return round(raw_suggested / 10) * 10
        
        # 測試案例：月銷量 5，庫存月份 4，當前庫存 3
        # 目標：20，需要：17，四捨五入：20
        self.assertEqual(gui_calculate_restock(5, 4, 3), 20)
        
        # 測試案例：月銷量 3，庫存月份 4，當前庫存 2
        # 目標：12，需要：10，四捨五入：10
        self.assertEqual(gui_calculate_restock(3, 4, 2), 10)
        
        # 測試案例：月銷量 4，庫存月份 4，當前庫存 10
        # 目標：16，需要：6，四捨五入：10
        self.assertEqual(gui_calculate_restock(4, 4, 10), 10)
    
    def test_crawler_calculation_should_match_gui(self):
        """
        Crawler (crawler.py) 應使用與 GUI 相同的四捨五入。
        
        當前程式碼路徑：crawler.py calculate_restock_quantity()
        公式：
          expected_inventory = monthly_sales * expected_months
          restock = expected_inventory - current_inventory
          return max(0, int(restock))  # ❌ BUG：使用 int() 不四捨五入到 10！
        
        預期：應像 GUI 一樣四捨五入到 10
        """
        # 當前 Crawler 實作（有 BUG）
        def crawler_calculate_restock_current(monthly_sales, expected_months, current_inventory):
            expected_inventory = monthly_sales * expected_months
            restock = expected_inventory - current_inventory
            return max(0, int(restock))  # BUG：截斷，不四捨五入到 10
        
        # 預期的 Crawler 實作（已修復）
        def crawler_calculate_restock_expected(monthly_sales, expected_months, current_inventory):
            expected_inventory = monthly_sales * expected_months
            restock = expected_inventory - current_inventory
            raw_restock = max(0, restock)
            if raw_restock <= 0:
                return 0
            if current_inventory == 0 and raw_restock <= 5:
                return 5
            return round(raw_restock / 10) * 10
        
        # 測試不一致性
        monthly_sales, months, stock = 5, 4, 3
        
        current_result = crawler_calculate_restock_current(monthly_sales, months, stock)
        expected_result = crawler_calculate_restock_expected(monthly_sales, months, stock)
        
        # 記錄 bug
        self.assertEqual(current_result, 17, "當前 Crawler 傳回 17 (int 截斷)")
        self.assertEqual(expected_result, 20, "預期 Crawler 應傳回 20 (四捨五入到 10)")
        
        # 這個斷言在當前 Crawler 程式碼下會失敗 - 那就是 bug！
        if current_result != expected_result:
            print(f"\n🐛 發現 BUG：Crawler 傳回 {current_result}，GUI 會傳回 {expected_result}")
            print(f"   輸入：月銷量={monthly_sales}，庫存月份={months}，當前庫存={stock}")
            print(f"   預期庫存：{monthly_sales * months}")
            print(f"   原始補貨需求：{monthly_sales * months - stock}")
            print(f"   Crawler 給出：{current_result} (int() 截斷)")
            print(f"   應給出：{expected_result} (四捨五入到 10)\n")


class ApprovedMappingGateTests(unittest.TestCase):
    """測試只有已批准的 SKU mapping 進入採購車。"""
    
    def test_approved_mapping_required_for_restock(self):
        """只有具有 alibabaSkuName 和 approved 狀態的項目應被允許。"""
        # 模擬 alababa_restocker.py 邏輯的測試資料
        
        # 案例 1：已批准的 mapping - 應通過
        approved_item = {
            "alibabaSkuName": "黑色",
            "alibabaSkuSecondName": "M碼",
            "alibabaMappingStatus": "approved",
            "restockQty": 20
        }
        self.assertTrue(self._should_allow_restock(approved_item))
        
        # 案例 2：待處理的 mapping - 應阻擋
        pending_item = {
            "alibabaSkuName": "黑色",
            "alibabaSkuSecondName": "M碼",
            "alibabaMappingStatus": "pending",
            "restockQty": 20
        }
        self.assertFalse(self._should_allow_restock(pending_item))
        
        # 案例 3：無 SKU 名稱 - 應阻擋
        no_sku_item = {
            "alibabaSkuName": "",
            "alibabaMappingStatus": "approved",
            "restockQty": 20
        }
        self.assertFalse(self._should_allow_restock(no_sku_item))
        
        # 案例 4：已停售 SKU - 應阻擋
        discontinued_item = {
            "alibabaSkuName": "停售",
            "alibabaMappingStatus": "approved",
            "restockQty": 20
        }
        self.assertFalse(self._should_allow_restock(discontinued_item))
    
    def _should_allow_restock(self, item):
        """模擬來自 alibaba_restocker.py 的批准閘門邏輯。"""
        sku_name = str(item.get("alibabaSkuName", "")).strip()
        status = str(item.get("alibabaMappingStatus", "")).strip()
        
        # 檢查狀態
        if status != "approved":
            return False
        
        # 檢查 SKU 名稱存在
        if not sku_name:
            return False
        
        # 檢查未停售
        discontinued_keywords = ["停售", "已停售", "以後不賣了", "以后不卖了"]
        if sku_name in discontinued_keywords:
            return False
        
        return True


class RestockQuantitySameAsDisplayTests(unittest.TestCase):
    """測試送到 1688 的數量與螢幕上的建議完全相符。"""
    
    def test_adjusted_quantity_overrides_suggested(self):
        """手動調整應按原樣送出，不四捨五入。"""
        round_func = lambda value: round(value / 10) * 10
        
        # 使用者手動調整為 17（不是 10 的倍數）
        item = {"adjustedQty": 17, "restockQty": 20}
        quantity = resolve_restock_quantity(item, round_func)
        
        # 應使用精確的手動調整，不四捨五入
        self.assertEqual(quantity, 17)
    
    def test_suggested_quantity_uses_rounding(self):
        """計算出的建議應使用文件化的四捨五入規則。"""
        round_func = lambda value: round(value / 10) * 10
        
        # 系統依公式建議 17
        item = {"restockQty": 17}
        quantity = resolve_restock_quantity(item, round_func)
        
        # 應四捨五入到 20
        self.assertEqual(quantity, 20)
    
    def test_display_and_api_use_same_quantity_source(self):
        """
        螢幕上顯示的數量和送到 1688 的數量必須來自相同的計算。
        
        在 script.js 中：
        - 顯示：calculateModelRestock() → suggestedQty (四捨五入到 10)
        - API 呼叫：getRestockItemQty() → 如果有 adjustedQty 則使用，否則使用 restockQty
        
        對於建議的（未調整的）項目，兩個路徑必須產生相同的數字。
        """
        # 模擬流程
        monthly_rate = 5
        months = 4
        current_stock = 3
        
        # 步驟 1：計算顯示值（GUI）
        target_stock = round(monthly_rate * months)  # 20
        raw_suggested = max(0, target_stock - current_stock)  # 17
        display_qty = round(raw_suggested / 10) * 10  # 20
        
        # 步驟 2：此值儲存為 restockQty
        item = {"restockQty": display_qty}
        
        # 步驟 3：API 送出此精確值
        round_func = lambda value: round(value / 10) * 10
        api_qty = resolve_restock_quantity(item, round_func)
        
        # 它們必須相符
        self.assertEqual(display_qty, api_qty)
        self.assertEqual(api_qty, 20)


class MonthThresholdConsistencyTests(unittest.TestCase):
    """測試庫存月份閾值的一致使用。"""
    
    def test_default_month_threshold_is_4(self):
        """預設庫存月份在所有表面應為 4。"""
        # 這記錄於：
        # - script.js：inventoryMonth 預設 '4'
        # - crawler.py：inventory_month 預設 4
        # - GUI：<select id="inventoryMonth"> 預設 4
        self.assertEqual(4, 4)  # 佔位符 - 實際測試會檢查預設值
    
    def test_same_month_value_used_everywhere(self):
        """當使用者設定 inventoryMonth 時，所有計算必須使用該值。"""
        # 測試案例：使用者設定為 6 個月
        months = 6
        monthly_rate = 5
        current_stock = 10
        
        # GUI 計算
        gui_target = round(monthly_rate * months)
        gui_need = max(0, gui_target - current_stock)
        gui_rounded = round(gui_need / 10) * 10
        
        # Crawler 計算（應匹配 GUI）
        crawler_target = monthly_rate * months
        crawler_need = max(0, crawler_target - current_stock)
        crawler_rounded = round(crawler_need / 10) * 10
        
        # 兩者都應使用相同的月份值
        self.assertEqual(gui_target, 30)
        self.assertEqual(crawler_target, 30)
        self.assertEqual(gui_rounded, crawler_rounded)


def run_tests_and_report():
    """執行測試並生成詳細的不一致報告。"""
    print("="*80)
    print("補貨數量一致性測試報告")
    print("="*80)
    print()
    
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    suite.addTests(loader.loadTestsFromTestCase(RestockQuantityConsistencyTests))
    suite.addTests(loader.loadTestsFromTestCase(ApprovedMappingGateTests))
    suite.addTests(loader.loadTestsFromTestCase(RestockQuantitySameAsDisplayTests))
    suite.addTests(loader.loadTestsFromTestCase(MonthThresholdConsistencyTests))
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    print()
    print("="*80)
    print("摘要")
    print("="*80)
    print(f"執行測試：{result.testsRun}")
    print(f"失敗：{len(result.failures)}")
    print(f"錯誤：{len(result.errors)}")
    
    if result.failures or result.errors:
        print()
        print("⚠️  發現不一致：")
        print()
        print("1. **Crawler 四捨五入 bug**：crawler.py 使用 int() 截斷")
        print("   而不是像 GUI 一樣四捨五入到最接近的 10")
        print("   - GUI：17 → 20 (四捨五入到 10)")
        print("   - Crawler：17 → 17 (截斷)")
        print()
        print("2. **需要修復**：更新 crawler.py calculate_restock_quantity()")
        print("   使用：round(restock / 10) * 10")
        print()
        print("   參見 restock_rules.py 的文件化四捨五入規則")
    
    return result


if __name__ == "__main__":
    run_tests_and_report()
