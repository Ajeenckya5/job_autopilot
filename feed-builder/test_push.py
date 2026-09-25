import json
import tempfile
import unittest
from pathlib import Path

from push_to_api import changed_jobs, jobs_from_payload, load_dir


class PushTest(unittest.TestCase):
    def test_only_new_and_changed_jobs_are_sent(self):
        previous = {"a": {"id": "a", "title": "Nurse"}, "b": {"id": "b", "title": "Engineer"}}
        current = {
            "a": {"id": "a", "title": "Nurse"},
            "b": {"id": "b", "title": "Senior Engineer"},
            "c": {"id": "c", "title": "Analyst"},
        }
        self.assertEqual([job["id"] for job in changed_jobs(current, previous)], ["b", "c"])

    def test_reads_shards_and_skips_the_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "manifest.json").write_text(json.dumps({"shards": ["x.json"]}))
            (path / "x.json").write_text(json.dumps({"jobs": [{"id": "1"}, {"id": "2"}]}))
            (path / "y.json").write_text(json.dumps([{"id": "3"}, {"title": "no id"}]))
            self.assertEqual(sorted(load_dir(path)), ["1", "2", "3"])
        self.assertEqual(list(jobs_from_payload(b'{"jobs": [{"id": 7}]}')), ["7"])


if __name__ == "__main__":
    unittest.main()
