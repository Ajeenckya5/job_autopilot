import unittest

from build_feeds import coverage_problems, dedupe


def job(source, company):
    return {"source": source, "company": company, "title": "Engineer"}


class CoverageGateTest(unittest.TestCase):
    def test_rejects_a_thin_feed(self):
        problems = coverage_problems([job("ashby", "1X")])
        self.assertTrue(any("companies" in item for item in problems))
        self.assertTrue(any("jobs" in item for item in problems))
        self.assertTrue(any("greenhouse returned 0" in item for item in problems))

    def test_accepts_a_full_feed(self):
        jobs = []
        for source in ("greenhouse", "lever", "ashby", "remotive", "arbeitnow"):
            for index in range(400):
                jobs.append(job(source, f"{source}-{index}"))
        self.assertEqual(coverage_problems(jobs), [])

    def test_merges_one_posting_listed_in_several_cities(self):
        desc = "Care for patients in clinic. " * 8
        rows = dedupe([
            {"id": "a", "source": "greenhouse", "company": "One Medical ", "title": "Family Nurse Practitioner", "location_raw": "Chicago, IL", "description_text": desc, "updated_at": "2026-09-01T00:00:00+00:00", "url": "https://example.com/a"},
            {"id": "b", "source": "greenhouse", "company": "One Medical", "title": "Family Nurse Practitioner", "location_raw": "Austin, TX", "description_text": desc, "updated_at": "2026-09-02T00:00:00+00:00", "url": "https://example.com/b"},
            {"id": "ashby-anthropic-1", "source": "ashby", "company": "Anthropic", "title": "Engineer", "location_raw": "Remote", "description_text": desc, "url": "https://example.com/c"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "One Medical")
        self.assertIn("Chicago, IL", rows[0]["location_raw"])
        self.assertIn("Austin, TX", rows[0]["location_raw"])


if __name__ == "__main__":
    unittest.main()
