import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
GATEWAY = ROOT / "gateway.py"


class DashboardStaticTests(unittest.TestCase):
    def test_primary_ui_contains_no_cjk_characters(self):
        text = INDEX.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))

    def test_usage_dashboard_contract_is_present(self):
        text = INDEX.read_text(encoding="utf-8")
        for marker in (
            "/api/usage",
            "Текущий лимит",
            "Недельный лимит",
            "Активные сессии",
            "data-theme=",
        ):
            self.assertIn(marker, text)

    def test_ui_has_no_external_font_dependency(self):
        text = INDEX.read_text(encoding="utf-8").lower()
        self.assertNotIn("fonts.googleapis.com", text)
        self.assertNotIn("fonts.gstatic.com", text)

    def test_legacy_view_has_final_cjk_rendering_guard(self):
        text = GATEWAY.read_text(encoding="utf-8")
        self.assertIn("_LEGACY_CJK_GUARD", text)
        self.assertIn("new MutationObserver", text)
        self.assertIn("По умолчанию", text)
        self.assertIn("return _HTMLResponse(html)", text)


if __name__ == "__main__":
    unittest.main()
