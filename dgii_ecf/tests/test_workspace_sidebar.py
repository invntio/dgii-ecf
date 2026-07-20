from __future__ import annotations

import json
from pathlib import Path

from frappe.tests import IntegrationTestCase


APP_ROOT = Path(__file__).resolve().parents[1]


class TestDGIIWorkspaceSidebar(IntegrationTestCase):
    def test_explicit_sidebar_exposes_all_operator_entries_with_icons(self):
        sidebar = json.loads(
            (APP_ROOT / "workspace_sidebar" / "dgii_ecf.json").read_text()
        )
        self.assertEqual(sidebar["name"], "DGII ECF")
        self.assertEqual(sidebar["app"], "dgii_ecf")

        links = {item["link_to"]: item for item in sidebar["items"]}
        self.assertEqual(
            set(links),
            {
                "ECF Document Log",
                "ECF Sequence Range",
                "ECF Provider Settings",
                "ECF Gateway Account",
            },
        )
        self.assertTrue(all(item.get("icon") for item in links.values()))
        self.assertEqual(links["ECF Provider Settings"]["link_type"], "DocType")
