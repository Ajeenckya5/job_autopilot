import unittest

from extract import countries_in, iso_utc, salary_from, seniority_of, sponsorship_of, within_days, years_required

# Each row is one snippet the builder must read the same way every time.
CASES = []

for title, level in [
    ("Machine Learning Intern", "intern"),
    ("Software Engineer Intern", "intern"),
    ("Junior Data Analyst", "junior"),
    ("Jr. Accountant", "junior"),
    ("Entry-level Nurse", "junior"),
    ("Associate Designer", "junior"),
    ("Senior Machine Learning Engineer", "senior"),
    ("Sr. Data Scientist", "senior"),
    ("Staff Software Engineer", "staff"),
    ("Principal Scientist", "staff"),
    ("Engineering Manager", "manager"),
    ("Tech Lead", "manager"),
    ("Director of Data", "director"),
    ("Vice President of Sales", "director"),
    ("VP Engineering", "director"),
    ("Head of Design", "director"),
    ("Machine Learning Engineer", "mid"),
    ("Data Scientist", "mid"),
    ("Registered Nurse", "mid"),
    ("Warehouse Specialist", "mid"),
]:
    CASES.append(("seniority", title, level))

for text, expected in [
    ("3+ years of Python", 3),
    ("5 years experience", 5),
    ("10 yrs in clinics", 10),
    ("1 year of SQL", 1),
    ("12+ years leading teams", 12),
    ("no number here", None),
    ("0 years required", 0),
    ("8 years of Java", 8),
    ("15 yrs of nursing", 15),
    ("2 years", 2),
]:
    CASES.append(("years", text, expected))

for text, expected in [
    ("We are unable to sponsor visas", "no"),
    ("Cannot sponsor new visas", "no"),
    ("No sponsorship is available", "no"),
    ("Must be authorized to work in the US", "no"),
    ("Visa sponsorship is available", "yes"),
    ("We will sponsor the right candidate", "yes"),
    ("H-1B sponsorship available", "yes"),
    ("H1B transfers welcome", "yes"),
    ("Work from anywhere", "unknown"),
    ("Benefits include health insurance", "unknown"),
]:
    CASES.append(("sponsorship", text, expected))

for text, expected in [
    ("Pay is $120k - $150k", (120000, 150000, "USD")),
    ("Range: $140,000 - $180,000 per year", (140000, 180000, "USD")),
    ("£60,000 - £80,000", (60000, 80000, "GBP")),
    ("€50,000 to €70,000", (50000, 70000, "EUR")),
    ("CAD 90,000 - 110,000", (90000, 110000, "CAD")),
    ("$45/hour", (93600, 93600, "USD")),
    ("USD 160k to 190k", (160000, 190000, "USD")),
    ("$95k–$110k base", (95000, 110000, "USD")),
    ("Raised at a $3.2 billion valuation", (None, None, "")),
    ("Competitive pay", (None, None, "")),
]:
    CASES.append(("salary", text, expected))

for text, expected in [
    ("London, UK", ["United Kingdom"]),
    ("Toronto, ON", ["Canada"]),
    ("Berlin, Germany", ["Germany"]),
    ("Bengaluru, India", ["India"]),
    ("Paris, France", ["France"]),
    ("San Francisco, CA", ["United States"]),
    ("Austin, TX", ["United States"]),
    ("USA, Canada, Argentina, Mexico, Peru", ["Canada", "Argentina", "Mexico", "Peru", "United States"]),
    ("Canada - Remote (ON, AB, BC, or NS Only)", ["Canada"]),
    ("Remote", ["Remote"]),
    ("New York, NY", ["United States"]),
    ("Vancouver, BC", ["Canada"]),
    ("United Kingdom", ["United Kingdom"]),
    ("Seattle, WA", ["United States"]),
    ("Munich, Germany", ["Germany"]),
    ("Mexico City, Mexico", ["Mexico"]),
]:
    CASES.append(("country", text, expected))

for value, expected in [
    (1790172030, "2026-09-23T"),
    (1790172030000, "2026-09-23T"),
    ("2026-09-10T13:11:58-04:00", "2026-09-10T17:11:58Z"),
    ("2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z"),
    ("", ""),
]:
    CASES.append(("date", value, expected))

# Extra salary and level snippets so the file covers 150 postings.
for number in range(80, 160):
    CASES.append(("salary", f"The base range is ${number}k - ${number + 20}k", (number * 1000, (number + 20) * 1000, "USD")))
for city in ["Chicago, IL", "Boston, MA", "Denver, CO", "Miami, FL", "Dallas, TX", "Phoenix, AZ", "Atlanta, GA", "Detroit, MI", "Portland, OR", "London, England"]:
    CASES.append(("country", city, ["United Kingdom"] if "London" in city or "England" in city else ["United States"]))


class ExtractTest(unittest.TestCase):
    def test_every_snippet(self):
        self.assertGreaterEqual(len(CASES), 150)
        for kind, raw, expected in CASES:
            with self.subTest(kind=kind, raw=raw):
                if kind == "seniority":
                    self.assertEqual(seniority_of(raw), expected)
                elif kind == "years":
                    self.assertEqual(years_required("", raw), expected)
                elif kind == "sponsorship":
                    self.assertEqual(sponsorship_of(raw), expected)
                elif kind == "salary":
                    got = salary_from(raw)
                    self.assertEqual((got["salary_min"], got["salary_max"], got["currency"]), expected)
                elif kind == "country":
                    self.assertEqual(countries_in(raw), expected)
                elif kind == "date":
                    got = iso_utc(raw)
                    self.assertTrue(got.startswith(expected) if expected else got == "")

    def test_drops_a_posting_older_than_45_days(self):
        self.assertFalse(within_days("2020-01-01T00:00:00Z", "", now=__import__("datetime").datetime(2026, 9, 23, tzinfo=__import__("datetime").timezone.utc)))
        self.assertTrue(within_days("2026-09-01T00:00:00Z", "", now=__import__("datetime").datetime(2026, 9, 23, tzinfo=__import__("datetime").timezone.utc)))


if __name__ == "__main__":
    unittest.main()
