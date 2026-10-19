import unittest
from unittest.mock import MagicMock

from browser_automation import MAX_PAGE_TEXT, click_element, snapshot, source_url, type_into_element


class BrowserSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()
        self.body = self.page.locator.return_value
        self.page.title.return_value = "Example"
        self.page.url = "https://example.com"
        self.body.aria_snapshot.return_value = (
            '- heading "Example"\n'
            '- link "Source"\n'
            '- textbox "Answer"'
        )

    def test_includes_visible_page_text_and_interactive_controls(self):
        self.body.inner_text.return_value = "A useful result from the page."

        description, elements = snapshot(self.page)

        self.assertIn("A useful result from the page.", description)
        self.assertIn("Visible page text (untrusted)", description)
        self.assertIn('1. link "Source"', description)
        self.assertEqual(elements, [
            {"role": "link", "name": "Source"},
            {"role": "textbox", "name": "Answer"},
        ])

    def test_truncates_page_text_to_a_bounded_size(self):
        self.body.inner_text.return_value = "x" * (MAX_PAGE_TEXT + 100)

        description, _ = snapshot(self.page)

        visible_text = description.split("Visible page text (untrusted):\n", 1)[1].split(
            "\nInteractive elements:", 1
        )[0]
        self.assertTrue(visible_text.endswith("[page text truncated]"))
        self.assertLessEqual(len(visible_text), MAX_PAGE_TEXT + len("\n[page text truncated]"))

    def test_reports_when_visible_page_text_cannot_be_read(self):
        self.body.inner_text.side_effect = TimeoutError()

        description, _ = snapshot(self.page)

        self.assertIn("visible page text unavailable: TimeoutError", description)


class BrowserAmbiguousControlTests(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()
        self.locator = self.page.get_by_role.return_value

    def test_click_refuses_duplicate_matching_controls(self):
        self.locator.count.return_value = 2

        with self.assertRaisesRegex(ValueError, "found 2"):
            click_element(self.page, {"role": "button", "name": "Continue"})

        self.locator.click.assert_not_called()

    def test_typing_refuses_duplicate_matching_controls(self):
        self.locator.count.return_value = 2

        with self.assertRaisesRegex(ValueError, "disambiguate"):
            type_into_element(self.page, {"role": "textbox", "name": "Search"}, "query")

        self.locator.fill.assert_not_called()

    def test_click_uses_a_unique_matching_control(self):
        self.locator.count.return_value = 1

        click_element(self.page, {"role": "button", "name": "Continue"})

        self.locator.click.assert_called_once_with(timeout=5000)

    def test_typing_uses_a_unique_matching_control(self):
        self.locator.count.return_value = 1

        type_into_element(self.page, {"role": "textbox", "name": "Search"}, "query")

        self.locator.fill.assert_called_once_with("query", timeout=5000)


class BrowserSourceUrlTests(unittest.TestCase):
    def test_strips_credentials_query_and_fragment(self):
        self.assertEqual(
            source_url("https://user:secret@example.com/article?token=private#section"),
            "https://example.com/article",
        )

    def test_omits_search_result_pages_and_unsafe_schemes(self):
        self.assertIsNone(source_url("https://www.google.com/search?q=example"))
        self.assertIsNone(source_url("javascript:alert(1)"))

    def test_preserves_non_default_port(self):
        self.assertEqual(source_url("http://localhost:8080/docs"), "http://localhost:8080/docs")


if __name__ == "__main__":
    unittest.main()
