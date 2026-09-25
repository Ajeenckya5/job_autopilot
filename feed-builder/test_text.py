import unittest

from build_feeds import normalize_title, posting_key, strip_html


class TextTest(unittest.TestCase):
    def test_escaped_greenhouse_html_becomes_text(self):
        raw = "&lt;div class=&quot;intro&quot;&gt;&lt;p&gt;About &amp;amp; us&lt;/p&gt;&lt;/div&gt;"
        self.assertEqual(strip_html(raw), "About & us")

    def test_plain_html_still_works(self):
        self.assertEqual(strip_html("<p>Python</p><p>SQL</p>"), "Python SQL")

    def test_title_spacing_is_one_job(self):
        self.assertEqual(normalize_title("In Office RN- NY Licensed "), normalize_title("In Office RN-NY Licensed"))
        self.assertEqual(normalize_title("C++ Engineer (Senior)"), "c++ engineer senior")
        self.assertNotEqual(normalize_title("クラウドエンジニア"), normalize_title("車両テストエンジニア"))

    def test_posting_key_ignores_title_punctuation(self):
        base = {"company": "One Medical", "source": "greenhouse", "description_text": "Care for members. " * 8}
        a = posting_key({**base, "title": "In Office RN- NY Licensed "})
        b = posting_key({**base, "title": "In Office RN-NY Licensed"})
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
