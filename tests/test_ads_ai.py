import json
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from ads_analysis import (
    AdsAnalyzer,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_REASONING_EFFORT,
    validate_openai_model,
    validate_reasoning_effort,
)


def valid_analysis(reviewed_product_count=1):
    decision = {
        "product_id": "p1",
        "primary_issue": "需繼續觀察",
        "confidence": "medium",
        "reason": "昨天與近一週尚未形成一致趨勢。",
        "why_not_other_issue": "CTR 與 CVR 樣本不足，不能單獨歸因素材或商品頁。",
        "evidence": [
            "昨天花費 120、點擊 24、直接 ROAS 2.8。",
            "week_01 點擊 80、直接 ROAS 3.1，與昨天方向不同。",
        ],
        "direct_actions": ["維持預算 3 天。", "若直接 ROAS 連續 2 天低於 3，再降 10%。"],
        "budget_change_pct": 0,
        "observation_days": 3,
        "risk_if_wrong": "過早降預算可能錯失回升流量。",
        "rule_disagreement": False,
        "rule_disagreement_reason": "",
    }
    return {
        "overall_health": "穩健",
        "executive_summary": "帳戶整體穩健，但 p1 需要累積更多樣本。",
        "account_diagnosis": {
            "decision": "先守住直接回收。",
            "confidence": "medium",
            "primary_risk": "單日雜訊。",
            "primary_opportunity": "累積足夠樣本後再擴量。",
            "evidence": ["昨天總 ROAS 3.2。", "近一週直接 ROAS 3.0。"],
            "data_limitations": ["缺少商品毛利。"],
        },
        "scale_up": [],
        "reduce_or_fix": [],
        "indirect_dependency": [],
        "watchlist": [decision],
        "next_actions": ["觀察 p1。", "確認毛利。", "檢查直接成交占比。"],
        "excluded_but_reviewed_products": [],
        "reviewed_product_count": reviewed_product_count,
        "analysis_limitations": ["缺少毛利與庫存。"],
    }


class FakeResponse:
    def __init__(self, payload, request_id="req_test"):
        self._payload = payload
        self.headers = {"x-request-id": request_id}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class AdsOpenAITests(unittest.TestCase):
    def build_analyzer(self, temp_dir, include_ai=True):
        return AdsAnalyzer(
            ads_export_dir=temp_dir,
            golden_table_path=f"{temp_dir}/missing.json",
            output_path=f"{temp_dir}/analysis.json",
            history_output_path=f"{temp_dir}/history.json",
            markdown_output_path=f"{temp_dir}/report.md",
            html_output_path=f"{temp_dir}/report.html",
            include_ai=include_ai,
            refresh_source=False,
            openai_model="gpt-5.6-sol",
            reasoning_effort="xhigh",
        )

    def test_model_and_reasoning_defaults_and_validation(self):
        self.assertEqual(validate_openai_model(""), DEFAULT_OPENAI_MODEL)
        self.assertEqual(validate_reasoning_effort(""), DEFAULT_OPENAI_REASONING_EFFORT)
        self.assertEqual(validate_openai_model("gpt-5.6-terra"), "gpt-5.6-terra")
        with self.assertRaisesRegex(ValueError, "不支援的 OpenAI 模型"):
            validate_openai_model("gpt-4.1-mini")
        with self.assertRaisesRegex(ValueError, "不支援的推理強度"):
            validate_reasoning_effort("extreme")

    def test_responses_request_uses_reasoning_and_strict_schema(self):
        with TemporaryDirectory() as temp_dir:
            analyzer = self.build_analyzer(temp_dir)
            request = analyzer._build_openai_request({
                "store": {"store_id": "shop-123"},
                "candidate_products": [],
            })

        self.assertEqual(request["model"], "gpt-5.6-sol")
        self.assertEqual(request["reasoning"], {"effort": "xhigh"})
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertTrue(request["background"])
        self.assertTrue(request["store"])
        self.assertNotEqual(request["safety_identifier"], "shop-123")

    def test_missing_api_key_does_not_silently_fall_back(self):
        with TemporaryDirectory() as temp_dir:
            analyzer = self.build_analyzer(temp_dir)
            with patch("ads_analysis.load_openai_api_key", return_value=("", "")):
                with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY_MISSING"):
                    analyzer._call_openai({"candidate_products": []})

        self.assertEqual(analyzer.openai_runtime["source"], "error")

    def test_ai_disabled_uses_rules_without_api_key(self):
        with TemporaryDirectory() as temp_dir:
            analyzer = self.build_analyzer(temp_dir, include_ai=False)
            with patch("ads_analysis.load_openai_api_key") as load_key:
                result = analyzer._call_openai({"candidate_products": []})

        self.assertIsNone(result)
        self.assertEqual(analyzer.openai_runtime["source"], "rules")
        load_key.assert_not_called()

    def test_valid_api_response_records_actual_model_and_request(self):
        payload = {
            "store": {"store_id": "shop-123"},
            "candidate_products": [{"product_id": "p1"}],
            "must_review_products": [{"product_id": "p1"}],
        }
        response_body = {
            "id": "resp_completed_1",
            "status": "completed",
            "model": "gpt-5.6-sol-2026-07-01",
            "output_text": json.dumps(valid_analysis(), ensure_ascii=False),
            "usage": {"input_tokens": 1200, "output_tokens": 800},
        }
        with TemporaryDirectory() as temp_dir:
            analyzer = self.build_analyzer(temp_dir)
            with patch("ads_analysis.load_openai_api_key", return_value=("sk-test", "env")), patch(
                "ads_analysis.requests.post", return_value=FakeResponse(response_body)
            ) as post:
                result = analyzer._call_openai(payload)

        self.assertEqual(result["reviewed_product_count"], 1)
        self.assertEqual(analyzer.openai_runtime["source"], "openai")
        self.assertEqual(analyzer.openai_runtime["response_model"], "gpt-5.6-sol-2026-07-01")
        self.assertEqual(analyzer.openai_runtime["request_id"], "req_test")
        self.assertEqual(analyzer.openai_runtime["response_id"], "resp_completed_1")
        self.assertEqual(analyzer.openai_runtime["attempts"], 1)
        sent_request = post.call_args.kwargs["json"]
        self.assertEqual(sent_request["model"], "gpt-5.6-sol")
        self.assertTrue(sent_request["background"])
        self.assertIn("X-Client-Request-Id", post.call_args.kwargs["headers"])

    def test_logical_validation_retries_once(self):
        payload = {
            "store": {},
            "candidate_products": [{"product_id": "p1"}],
            "must_review_products": [{"product_id": "p1"}],
        }
        invalid = {
            "id": "resp_invalid_1",
            "status": "completed",
            "model": "gpt-5.6-sol",
            "output_text": json.dumps(valid_analysis(reviewed_product_count=0), ensure_ascii=False),
        }
        corrected = {
            "id": "resp_corrected_1",
            "status": "completed",
            "model": "gpt-5.6-sol",
            "output_text": json.dumps(valid_analysis(), ensure_ascii=False),
        }
        with TemporaryDirectory() as temp_dir:
            analyzer = self.build_analyzer(temp_dir)
            with patch("ads_analysis.load_openai_api_key", return_value=("sk-test", "env")), patch(
                "ads_analysis.requests.post",
                side_effect=[FakeResponse(invalid, "req_1"), FakeResponse(corrected, "req_2")],
            ) as post:
                result = analyzer._call_openai(payload)

        self.assertEqual(result["reviewed_product_count"], 1)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(analyzer.openai_runtime["attempts"], 2)
        second_request = post.call_args_list[1].kwargs["json"]
        self.assertIn("validation_errors", second_request["input"][-1]["content"])

    def test_background_response_polls_same_response_until_completed(self):
        payload = {
            "store": {},
            "candidate_products": [{"product_id": "p1"}],
            "must_review_products": [{"product_id": "p1"}],
        }
        queued = {
            "id": "resp_background_1",
            "status": "queued",
            "model": "gpt-5.6-sol",
        }
        in_progress = {
            "id": "resp_background_1",
            "status": "in_progress",
            "model": "gpt-5.6-sol",
        }
        completed = {
            "id": "resp_background_1",
            "status": "completed",
            "model": "gpt-5.6-sol",
            "output_text": json.dumps(valid_analysis(), ensure_ascii=False),
            "usage": {"input_tokens": 1200, "output_tokens": 800},
        }
        with TemporaryDirectory() as temp_dir:
            analyzer = self.build_analyzer(temp_dir)
            with patch("ads_analysis.load_openai_api_key", return_value=("sk-test", "env")), patch(
                "ads_analysis.requests.post", return_value=FakeResponse(queued)
            ) as post, patch(
                "ads_analysis.requests.get",
                side_effect=[
                    requests.ReadTimeout("temporary poll timeout"),
                    FakeResponse(in_progress),
                    FakeResponse(completed),
                ],
            ) as get, patch("ads_analysis.time.sleep", return_value=None):
                result = analyzer._call_openai(payload)

        self.assertEqual(result["reviewed_product_count"], 1)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 3)
        for call in get.call_args_list:
            self.assertTrue(call.args[0].endswith("/responses/resp_background_1"))

    def test_completed_response_cache_prevents_second_paid_request(self):
        payload = {
            "store": {},
            "candidate_products": [{"product_id": "p1"}],
            "must_review_products": [{"product_id": "p1"}],
        }
        completed = {
            "id": "resp_cached_1",
            "status": "completed",
            "model": "gpt-5.6-sol",
            "output_text": json.dumps(valid_analysis(), ensure_ascii=False),
            "usage": {"input_tokens": 1200, "output_tokens": 800},
        }
        with TemporaryDirectory() as temp_dir:
            first = self.build_analyzer(temp_dir)
            with patch("ads_analysis.load_openai_api_key", return_value=("sk-test", "env")), patch(
                "ads_analysis.requests.post", return_value=FakeResponse(completed)
            ) as first_post:
                first_result = first._call_openai(payload)

            second = self.build_analyzer(temp_dir)
            with patch("ads_analysis.load_openai_api_key", return_value=("sk-test", "env")), patch(
                "ads_analysis.requests.post"
            ) as second_post, patch("ads_analysis.requests.get") as second_get:
                second_result = second._call_openai(payload)

        self.assertEqual(first_result, second_result)
        self.assertEqual(first_post.call_count, 1)
        second_post.assert_not_called()
        second_get.assert_not_called()

    def test_uncertain_submission_is_not_automatically_resent(self):
        payload = {"store": {}, "candidate_products": [], "must_review_products": []}
        with TemporaryDirectory() as temp_dir:
            first = self.build_analyzer(temp_dir)
            with patch("ads_analysis.load_openai_api_key", return_value=("sk-test", "env")), patch(
                "ads_analysis.requests.post",
                side_effect=requests.ReadTimeout("create timed out"),
            ) as first_post:
                with self.assertRaisesRegex(RuntimeError, "OPENAI_SUBMISSION_UNCERTAIN"):
                    first._call_openai(payload)

            second = self.build_analyzer(temp_dir)
            with patch("ads_analysis.load_openai_api_key", return_value=("sk-test", "env")), patch(
                "ads_analysis.requests.post"
            ) as second_post:
                with self.assertRaisesRegex(RuntimeError, "OPENAI_SUBMISSION_UNCERTAIN"):
                    second._call_openai(payload)

        self.assertEqual(first_post.call_count, 1)
        second_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
