import os
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("LABELS_DISABLE_FILE_LOGGING", "1")

try:
    import flask  # noqa: F401
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc


EXPECTED_GROUPS = {
    "Fungi": "fungi",
    "Plantae": "plantae",
    "Protozoa": "protozoa",
    "Chromista": "chromista",
    "Arachnida": "orange-animal",
    "Insecta": "orange-animal",
    "Mollusca": "orange-animal",
    "Amphibia": "blue-animal",
    "Reptilia": "blue-animal",
    "Aves": "blue-animal",
    "Mammalia": "blue-animal",
    "Actinopterygii": "blue-animal",
    "Animalia": "blue-animal",
}


class TestTaxonColorMapping(unittest.TestCase):
    def test_all_supported_iconic_taxa_map_to_expected_groups(self):
        for iconic_taxon_name, expected_group in EXPECTED_GROUPS.items():
            with self.subTest(iconic_taxon_name=iconic_taxon_name):
                self.assertEqual(
                    labels_app.taxon_color_group(iconic_taxon_name), expected_group
                )

    def test_missing_or_unknown_iconic_taxon_is_neutral(self):
        for iconic_taxon_name in (None, "", "Life", "UnknownTaxon", 123, [], {}):
            with self.subTest(iconic_taxon_name=iconic_taxon_name):
                self.assertEqual(
                    labels_app.taxon_color_group(iconic_taxon_name), "unknown"
                )

    @patch("app.inat_api_get")
    def test_lookup_response_exposes_semantic_taxon_fields(self, inat_api_get):
        response = MagicMock()
        response.json.return_value = {
            "results": [
                {
                    "id": 123,
                    "taxon": {
                        "name": "Araneus diadematus",
                        "iconic_taxon_name": "Arachnida",
                    },
                    "user": {"login": "observer"},
                    "ofvs": [],
                }
            ]
        }
        inat_api_get.return_value = response

        item = labels_app.lookup_batch_internal(["123"])["items"][0]

        self.assertEqual(item["iconic_taxon_name"], "Arachnida")
        self.assertEqual(item["taxon_color_group"], "orange-animal")
        self.assertEqual(item["color"], "red")

    def test_bugguide_remains_neutral_without_guessing(self):
        item = labels_app.lookup_batch_internal(["BG2520730"])["items"][0]

        self.assertEqual(item["iconic_taxon_name"], "")
        self.assertEqual(item["taxon_color_group"], "unknown")
        self.assertEqual(item["color"], "black")


class TestTaxonColorStyles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_path = Path(labels_app.app.root_path) / "templates" / "index.html"
        cls.template = template_path.read_text(encoding="utf-8")

    def test_every_group_colors_binomial_and_row_edge(self):
        for group in set(EXPECTED_GROUPS.values()) | {"unknown"}:
            with self.subTest(group=group, element="binomial"):
                self.assertRegex(
                    self.template,
                    re.compile(
                        rf'\.slip\[data-taxon="{re.escape(group)}"\] \.binomial\s*'
                        rf'\{{[^}}]*color:\s*var\(--t-{re.escape(group)}\)',
                        re.DOTALL,
                    ),
                )
            with self.subTest(group=group, element="slip-edge"):
                self.assertRegex(
                    self.template,
                    re.compile(
                        rf'\.slip\[data-taxon="{re.escape(group)}"\] \.slip-edge\s*'
                        rf'\{{[^}}]*background:\s*var\(--t-{re.escape(group)}\)',
                        re.DOTALL,
                    ),
                )

    def test_dark_and_light_themes_define_every_taxon_variable(self):
        root_vars = self.template.split("html[data-theme=\"light\"]", 1)[0]
        light_vars = self.template.split("html[data-theme=\"light\"]", 1)[1].split(
            "html[data-theme=\"dark\"]", 1
        )[0]

        for group in set(EXPECTED_GROUPS.values()) | {"unknown"}:
            variable = f"--t-{group}:"
            with self.subTest(group=group, theme="dark"):
                self.assertIn(variable, root_vars)
            with self.subTest(group=group, theme="light"):
                self.assertIn(variable, light_vars)

    def test_frontend_uses_semantic_group_not_legacy_color(self):
        self.assertIn("item.taxon_color_group", self.template)
        self.assertIn("item.iconic_taxon_name", self.template)
        self.assertNotIn("taxonSlugForColor", self.template)
        self.assertNotRegex(self.template, r"taxonColorGroup\([^)]*\.color")

    def test_each_grouped_animal_legend_label_has_a_color_chip(self):
        keyline = self.template.split('<div class="keyline">', 1)[1].split(
            "</div>", 1
        )[0]
        for label in ("Birds", "Reptiles", "Mammals"):
            with self.subTest(label=label):
                self.assertRegex(
                    keyline,
                    re.compile(
                        rf'<i\s+style="background: var\(--t-blue-animal\)"></i>\s*{label}'
                    ),
                )
        for label in ("Insects", "Arachnids", "Mollusks"):
            with self.subTest(label=label):
                self.assertRegex(
                    keyline,
                    re.compile(
                        rf'<i\s+style="background: var\(--t-orange-animal\)"></i>\s*{label}'
                    ),
                )


if __name__ == "__main__":
    unittest.main()
