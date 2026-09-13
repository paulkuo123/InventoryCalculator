import json
import subprocess
import unittest
from pathlib import Path

from product_catalog import apply_offer_to_models, find_model_by_identity


ROOT = Path(__file__).resolve().parents[1]


def _extract_js_function(source, name):
    start = source.index(f"function {name}(")
    if source[start - 6:start] == "async ":
        start -= 6
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
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"could not extract {name}")


def _run_products_js(function_names, program):
    source = (ROOT / "products.js").read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in function_names)
    completed = subprocess.run(
        ["node", "-e", f"{functions}\n{program}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout or "node failed")
    return json.loads(completed.stdout)


class ProductAlibabaEditTests(unittest.TestCase):
    def test_url_offer_all_updates_every_model_without_overwriting_sku_fields(self):
        models = [
            {
                "型號名稱": "黑色", "1688_sku_name": "黑色",
                "1688_sku_second_name": "均码", "1688_last_price_cny": 4.5,
            },
            {
                "型號名稱": "白色", "1688_sku_name": "白色",
                "1688_sku_second_name": "大码", "1688_last_price_cny": 7.5,
            },
        ]

        updated = apply_offer_to_models(
            models, "https://detail.1688.com/offer/999.html", "999"
        )

        self.assertEqual(len(updated), 2)
        self.assertTrue(all(model["1688_offer_id"] == "999" for model in models))
        self.assertTrue(all(model["阿里巴巴商品URL"].endswith("/999.html") for model in models))
        self.assertEqual(models[0]["1688_sku_name"], "黑色")
        self.assertEqual(models[1]["1688_sku_name"], "白色")
        self.assertEqual(models[0]["1688_sku_second_name"], "均码")
        self.assertEqual(models[1]["1688_sku_second_name"], "大码")
        self.assertEqual(models[0]["1688_last_price_cny"], 4.5)
        self.assertEqual(models[1]["1688_last_price_cny"], 7.5)

    def test_product_editor_has_no_sku_mapping_redirect(self):
        script = (ROOT / "products.js").read_text(encoding="utf-8")
        html = (ROOT / "products.html").read_text(encoding="utf-8")

        self.assertNotIn("/sku-mapping.html", script)
        self.assertIn("套用 URL／Offer ID 到所有規格", html)
        self.assertIn("不會覆蓋各規格的 SKU、價格、MOQ 或包裝倍數", html)

    def test_fetch_full_product_rejects_non_exact_catalog_result(self):
        result = _run_products_js(
            ["fetchFullProduct"],
            """
global.fetch = async () => ({
    ok: true,
    json: async () => ({status: 'success', products: [{productId: '1000'}]}),
});
fetchFullProduct('100')
    .then(() => process.stdout.write(JSON.stringify({resolved: true})))
    .catch(error => process.stdout.write(JSON.stringify({error: error.message})));
""",
        )

        self.assertEqual(result, {"error": "找不到商品 ID 100"})

    def test_empty_spec_id_uses_unique_model_name_without_updating_sibling(self):
        result = _run_products_js(
            ["findModelByIdentity"],
            """
const models = [
    {specId: '', modelName: '黑色'},
    {specId: '', modelName: '白色'},
];
const target = findModelByIdentity(models, {'規格ID': '', '型號名稱': '白色'});
target.updated = true;
process.stdout.write(JSON.stringify(models));
""",
        )

        self.assertEqual(result, [
            {"specId": "", "modelName": "黑色"},
            {"specId": "", "modelName": "白色", "updated": True},
        ])

    def test_duplicate_empty_spec_model_name_is_ambiguous(self):
        result = _run_products_js(
            ["findModelByIdentity"],
            """
const models = [
    {specId: '', modelName: '同名'},
    {specId: '', modelName: '同名'},
];
process.stdout.write(JSON.stringify({
    found: Boolean(findModelByIdentity(models, {specId: '', modelName: '同名'})),
}));
""",
        )

        self.assertEqual(result, {"found": False})

    def test_backend_model_identity_never_falls_back_from_spec_id(self):
        models = [
            {"規格ID": "spec-1", "型號名稱": "同名"},
            {"規格ID": "spec-2", "型號名稱": "同名"},
        ]

        result = find_model_by_identity(models, "missing-spec", "同名")

        self.assertIsNone(result)

    def test_backend_model_name_without_spec_must_be_unique(self):
        models = [
            {"規格ID": "", "型號名稱": "同名"},
            {"規格ID": "", "型號名稱": "同名"},
        ]

        result = find_model_by_identity(models, "", "同名")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
