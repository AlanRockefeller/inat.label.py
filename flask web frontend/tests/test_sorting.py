import importlib.util
import unittest
import sys
import os
from datetime import date

# Add parent directory to path to find inat.label.py
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import MagicMock

# Mock dependencies to avoid install requirements for unit testing sorting
sys.modules["requests"] = MagicMock()
sys.modules["requests.adapters"] = MagicMock()
sys.modules["urllib3"] = MagicMock()
sys.modules["urllib3.util"] = MagicMock()
sys.modules["urllib3.util.retry"] = MagicMock()
sys.modules["bs4"] = MagicMock()
sys.modules["qrcode"] = MagicMock()
sys.modules["reportlab"] = MagicMock()
sys.modules["reportlab.lib"] = MagicMock()
sys.modules["reportlab.lib.pagesizes"] = MagicMock()
sys.modules["reportlab.platypus"] = MagicMock()
sys.modules["reportlab.lib.styles"] = MagicMock()
sys.modules["reportlab.lib.units"] = MagicMock()
sys.modules["reportlab.pdfbase"] = MagicMock()
sys.modules["reportlab.pdfbase.ttfonts"] = MagicMock()
sys.modules["colorama"] = MagicMock()
sys.modules["replace_accents"] = MagicMock()

# Need dateutil for the date sorting test to works?
# If dateutil is missing, we might need to mock it too but the sorting logic uses it.
# Check if dateutil is installed. If not, we might fail unless we mock it to return a dummy date.
try:
    import dateutil
except ImportError:
    # If dateutil is missing, mock it but we need it to work for tests
    m = MagicMock()
    # Mock return value must have .date() method
    mock_dt = MagicMock()
    mock_dt.date.return_value = date(2023, 1, 1)
    m.parser.parse.return_value = mock_dt
    sys.modules["dateutil"] = m
    sys.modules["dateutil.parser"] = m.parser

module_path = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "inat.label.py")
)
spec = importlib.util.spec_from_file_location("inat_label", module_path)
if spec is None or spec.loader is None:
    raise ImportError(f"Could not load module specification from {module_path}")
inat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inat)


class TestSorting(unittest.TestCase):
    def setUp(self):
        # Helper to create current sortable items:
        # (index, (label, taxon), observation_datetime)
        self.make_item = lambda idx, label_dict: (
            idx,
            ([(k, v) for k, v in label_dict.items()], "Fungi"),
            None,
        )

    def test_cmp_alpha_then_trailing_num(self):
        # Test the comparator logic: cmp_alpha_then_trailing_num(a, b)
        # Returns < 0 if a < b, > 0 if a > b, 0 if a == b

        # 1. Plain alpha primary
        # "Plot 11" vs "Plot 2"
        # Prefix match ("plot"). 2 < 11. So "Plot 2" < "Plot 11".
        # Expect cmp("Plot 2", "Plot 11") < 0
        self.assertLess(inat.cmp_alpha_then_trailing_num("Plot 2", "Plot 11"), 0)
        self.assertGreater(inat.cmp_alpha_then_trailing_num("Plot 11", "Plot 2"), 0)

        # 2. "Voucher 2" vs "Voucher 10"
        # Prefix match. 2 < 10.
        self.assertLess(inat.cmp_alpha_then_trailing_num("Voucher 2", "Voucher 10"), 0)

        # 3. "District" vs "District 2"
        # "District" -> prefix None/different?
        # "District" -> split: (None, None)
        # "District 2" -> split: ("District", 2)
        # "district" vs "district 2". "district" < "district 2" via alpha.
        # Comparator falls back to alpha if prefixes don't match (or one is None).
        # "District" < "District 2" is True.
        self.assertLess(inat.cmp_alpha_then_trailing_num("District", "District 2"), 0)

        # 4. "A-2" vs "A-11"
        # Match prefix "a-". 2 < 11.
        self.assertLess(inat.cmp_alpha_then_trailing_num("A-2", "A-11"), 0)

        # 5. "A 2B" vs "A 11B"
        # No trailing number. Pure alpha.
        # "A 11B" < "A 2B"
        self.assertLess(inat.cmp_alpha_then_trailing_num("A 11B", "A 2B"), 0)

        # 6. Missing values
        # Missing sorts last. "A" vs None. "A" < None.
        self.assertLess(inat.cmp_alpha_then_trailing_num("A", None), 0)
        self.assertGreater(inat.cmp_alpha_then_trailing_num(None, "A"), 0)

    def test_default_sort_legacy(self):
        # Verify strict numeric sort for default
        # "Plot 2" -> 2
        # "Area 100" -> 100
        # "Pure 1" -> 1
        # "Plot 12a" -> 0 (strict trailing ignored if non-digits follow)
        items = [
            self.make_item(0, {"iNaturalist Observation Number": "Plot 2"}),
            self.make_item(1, {"iNaturalist Observation Number": "Area 100"}),
            self.make_item(2, {"iNaturalist Observation Number": "Pure 1"}),
            self.make_item(3, {"iNaturalist Observation Number": "Plot 12a"}),
        ]
        results = inat.sort_labels(items, None)  # Default
        self.assertEqual(results[0][0][0][1], "Plot 12a")  # 0
        self.assertEqual(results[1][0][0][1], "Pure 1")  # 1
        self.assertEqual(results[2][0][0][1], "Plot 2")  # 2
        self.assertEqual(results[3][0][0][1], "Area 100")  # 100

    def test_whitespace_normalization(self):
        # "Plot 2" vs "Plot  2" vs "Plot   2"
        # All normalize to "plot 2".
        # Prefixes match "plot". Numbers match 2.
        # Fallback to index or alpha?
        # If prefixes match (after norm) and numbers match, comparator returns 0.
        # Then stability (index) applies.

        # "Plot 2" (idx 0) vs "Plot  2" (idx 1)
        # normalize("Plot 2") -> "plot 2". Split -> ("plot", 2)
        # normalize("Plot  2") -> "plot 2". Split -> ("plot", 2)
        # Compare -> 0.
        # Result order: idx 0, idx 1.

        items = [
            self.make_item(0, {"Col": "Plot  2"}),
            self.make_item(1, {"Col": "Plot 2"}),
        ]
        results = inat.sort_labels(items, "custom", sort_field_name="Col")
        self.assertEqual(results[0][0][0][1], "Plot  2")
        self.assertEqual(results[1][0][0][1], "Plot 2")

        # Verify normalization helps prefix matching for "Plot 2" vs "Plot  10"
        # "Plot 2" -> "plot 2"
        # "Plot  10" -> "plot 10"
        # Prefixes "plot" == "plot".
        # 2 < 10. "Plot 2" first.

        items2 = [
            self.make_item(0, {"Col": "Plot  10"}),
            self.make_item(1, {"Col": "Plot 2"}),
        ]
        results2 = inat.sort_labels(items2, "custom", sort_field_name="Col")
        self.assertEqual(results2[0][0][0][1], "Plot 2")
        self.assertEqual(results2[1][0][0][1], "Plot  10")

    def test_sort_none(self):
        # Input order preservation
        items = [
            self.make_item(2, {"A": "1"}),
            self.make_item(0, {"A": "2"}),
            self.make_item(1, {"A": "3"}),
        ]
        sorted_labels = inat.sort_labels(items, "none")
        # Should be index 0, then 1, then 2
        self.assertEqual(sorted_labels[0][0][0][1], "2")  # Index 0 value
        self.assertEqual(sorted_labels[1][0][0][1], "3")  # Index 1 value
        self.assertEqual(sorted_labels[2][0][0][1], "1")  # Index 2 value

    def test_sort_voucher(self):
        items = [
            self.make_item(0, {"Voucher Number": "100"}),
            self.make_item(1, {"Voucher Number": "2"}),
            self.make_item(
                2, {"Voucher Number": "A"}
            ),  # Non-numeric, should be after numeric ones?
            self.make_item(3, {}),  # Missing
        ]
        # "2" < "100" < "A" < Missing
        # Wait, "A" has no number, so has_num=1. "100" and "2" have has_num=0.
        # "2" parses as 2. "100" parses as 100.

        results = inat.sort_labels(items, "voucher")

        # Expected order:
        # Index 1 ("2")
        # Index 0 ("100")
        # Index 2 ("A")
        # Index 3 (Missing)

        self.assertEqual(results[0][0][0][1], "2")
        self.assertEqual(results[1][0][0][1], "100")
        self.assertEqual(results[2][0][0][1], "A")
        self.assertEqual(len(results[3][0]), 0)  # Empty label matching Index 3

    def test_sort_custom(self):
        items = [
            self.make_item(0, {"Col": "B"}),
            self.make_item(1, {"Col": "A"}),
        ]
        results = inat.sort_labels(items, "custom", sort_field_name="Col")
        self.assertEqual(results[0][0][0][1], "A")
        self.assertEqual(results[1][0][0][1], "B")

    def test_label_get_robustness(self):
        # Test label with non-string keys and malformed elements
        label = [
            ("Good", "Value"),
            (123, "NumberKey"),  # Non-string key
            None,  # Non-tuple
            ("Too", "Many", "Items"),  # Wrong length
        ]
        # Should not raise exception
        self.assertEqual(inat.label_get(label, "Good"), "Value")
        self.assertIsNone(inat.label_get(label, "Missing"))

    def test_default_sort_stability(self):
        # Same key, different index
        items = [
            self.make_item(5, {"iNaturalist Observation Number": "123"}),
            self.make_item(2, {"iNaturalist Observation Number": "123"}),
        ]
        # Should sort by key "123" (equal), then index (2 < 5)
        results = inat.sort_labels(items, None)
        self.assertEqual(results[0][0][0][1], "123")
        # We can't easily check the original index from the result unless we inspect the object identity or something unique
        # But we know implicit stability relies on stable sort or index if appended.
        # My implementation appends index.
        # So it SHOULD be Index 2 then Index 5.

        # Let's verify by checking which one is first if they are truly identical in content.
        # But here content is identical.
        # Wait, sort_labels returns [(label, taxon), ...] stripping index.
        # So I can't verify index from result unless I tag the label content.

        # Let's re-make items with unique content but same sort key
        items = [
            self.make_item(
                5, {"iNaturalist Observation Number": "123", "Unique": "Second"}
            ),
            self.make_item(
                2, {"iNaturalist Observation Number": "123", "Unique": "First"}
            ),
        ]
        results = inat.sort_labels(items, None)
        self.assertEqual(inat.label_get(results[0][0], "Unique"), "First")
        self.assertEqual(inat.label_get(results[1][0], "Unique"), "Second")


if __name__ == "__main__":
    unittest.main()
