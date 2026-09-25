import random
import unittest

import lexicon
from build_feeds import shard_name, trim_boilerplate

FIELDS = {
    "legal": (["Paralegal", "Legal Assistant"], ["pleadings", "Westlaw", "e-discovery", "court filings", "deposition prep"]),
    "nursing": (["Registered Nurse", "Charge Nurse"], ["BLS", "ACLS", "med-surg", "IV therapy", "Epic charting"]),
    "driving": (["Truck Driver", "Delivery Driver"], ["CDL", "DOT", "pre-trip inspection", "ELD", "hazmat"]),
    "accounting": (["Staff Accountant", "Senior Accountant"], ["GAAP", "reconciliations", "NetSuite", "month-end close", "accruals"]),
    "teaching": (["Teacher", "Science Teacher"], ["lesson plans", "IEP", "classroom management", "Google Classroom", "grading"]),
    "software": (["Software Engineer", "Backend Engineer"], ["Python", "Kubernetes", "PostgreSQL", "REST APIs", "CI/CD"]),
}
CITIES = ["Chicago, IL", "Dallas, TX", "Denver, CO", "Austin, TX", "Boston, MA", "Seattle, WA"]


def corpus():
    rng = random.Random(7)
    jobs = []
    for field, (titles, skills) in FIELDS.items():
        for employer in range(12):
            company = f"{field.title()} Co {employer}"
            for n in range(3):
                picked = rng.sample(skills, 3)
                city = rng.choice(CITIES)
                jobs.append({
                    "id": f"{field}-{employer}-{n}",
                    "company": company,
                    "title": titles[n % 2],
                    "location_raw": city,
                    "locations": [{"city": city.split(",")[0], "region": city.split(", ")[1], "country": "United States"}],
                    "description_text": (
                        f"About us: we are a mission driven company in {city.split(',')[0]}. You will work with the team. "
                        f"Requirements: experience with {picked[0]}, {picked[1]} and {picked[2]}. Strong communication skills."
                    ),
                })
    return jobs


class LexiconTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = lexicon.build(corpus())

    def test_skills_come_from_postings_not_a_list(self):
        phrases = self.lex["phrases"]
        for skill in ("pleadings", "westlaw", "ediscovery", "cdl", "gaap", "month end close", "lesson plans", "kubernetes", "iv therapy"):
            self.assertIn(skill, phrases)
        for filler in ("mission driven", "about us", "communication skills", "team", "chicago", "dallas"):
            self.assertNotIn(filler, phrases)

    def test_filler_is_what_every_kind_of_job_says(self):
        for word in ("the", "with", "team", "experience"):
            self.assertIn(word, self.lex["filler"])
        self.assertNotIn("pleadings", self.lex["filler"])

    def test_titles_that_read_alike_are_related(self):
        related = dict(self.lex["titles"].get("paralegal", []))
        self.assertIn("legal assistant", related)
        self.assertNotIn("truck driver", related)
        self.assertGreater(self.lex["heads"]["paralegal"], 0.9)

    def test_small_feeds_learn_nothing(self):
        self.assertEqual(lexicon.build(corpus()[:50])["phrases"], {})

    def test_rare_events_are_not_skills(self):
        self.assertLess(lexicon.binomial_tail(4, 8, 0.002), 1e-6)
        self.assertGreater(lexicon.binomial_tail(3, 6, 0.05), 1e-4)


class BoilerplateTest(unittest.TestCase):
    def test_repeated_employer_text_is_cut_and_duties_stay(self):
        about = "Acme builds rockets for the moon and beyond. We are proud to be backed by great investors."
        jobs = [
            {"company": "Acme", "title": "Paralegal", "_full": f"{about} Draft pleadings for trial teams. " + "Manage court calendars and filings. " * 12},
            {"company": "Acme", "title": "Paralegal - Denver", "_full": f"{about} Draft pleadings for trial teams. " + "Manage court calendars and filings. " * 12},
            {"company": "Acme", "title": "Staff Accountant", "_full": f"{about} Close the books every month. " + "Reconcile accounts and accruals. " * 14},
        ]
        texts = trim_boilerplate(jobs)
        self.assertNotIn("rockets", jobs[0]["description_text"])
        self.assertIn("Draft pleadings", jobs[0]["description_text"])
        self.assertIn("Reconcile accounts", texts[2])
        self.assertTrue(all("_full" not in job for job in jobs))
        self.assertLessEqual(max(len(job["description_text"]) for job in jobs), 1500)

    def test_shards_split_by_place_only(self):
        self.assertEqual(shard_name({"title": "Registered Nurse", "locations": [{"country": "United States"}]}), "united-states.json")
        self.assertEqual(shard_name({"title": "Software Engineer", "locations": [{"country": "Germany"}]}), "other.json")


if __name__ == "__main__":
    unittest.main()
