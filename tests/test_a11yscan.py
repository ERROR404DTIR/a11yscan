import unittest
import a11yscan

class A11yScanTests(unittest.TestCase):
    def rules(self, html):
        return {finding.rule for finding in a11yscan.scan(html, "sample.html")}

    def test_missing_image_alt(self):
        self.assertIn("A11Y001", self.rules('<html lang="en"><title>Page</title><img src="x.png"></html>'))

    def test_empty_alt_is_valid_for_decorative_image(self):
        self.assertNotIn("A11Y001", self.rules('<html lang="en"><title>Page</title><img src="x.png" alt=""></html>'))

    def test_missing_document_language_and_title(self):
        rules = self.rules("<html><body>Hello</body></html>")
        self.assertIn("A11Y003", rules)
        self.assertIn("A11Y004", rules)

    def test_duplicate_ids(self):
        self.assertIn("A11Y013", self.rules('<html lang="en"><title>x</title><div id="x"></div><p id="x"></p></html>'))

    def test_missing_aria_reference(self):
        self.assertIn("A11Y021", self.rules('<html lang="en"><title>x</title><button aria-labelledby="absent"></button></html>'))

    def test_positive_tabindex(self):
        self.assertIn("A11Y014", self.rules('<html lang="en"><title>x</title><button tabindex="3">Go</button></html>'))

if __name__ == "__main__":
    unittest.main()
