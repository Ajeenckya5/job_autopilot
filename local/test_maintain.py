"""Mac ledger prune, weekly VACUUM, and 7-day log rotation."""

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import core


class MaintainTest(unittest.TestCase):
    def test_prune_vacuum_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = core.DB(root / "autopilot.sqlite")
            old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
            recent = datetime.now(timezone.utc).isoformat()
            db.insert_job({
                "id": "old",
                "source": "greenhouse",
                "url": "https://example.com/old",
                "title": "Old role",
                "posted_at": old,
            })
            db.insert_job({
                "id": "new",
                "source": "greenhouse",
                "url": "https://example.com/new",
                "title": "New role",
                "posted_at": recent,
            })
            stamp = db.path.with_name(db.path.name + ".vacuum")
            first = stamp.stat().st_mtime
            db.maintain()
            self.assertFalse(db.seen("old"))
            self.assertTrue(db.seen("new"))
            self.assertEqual(stamp.stat().st_mtime, first)
            db.conn.close()

            log_dir = root / "logs"
            log_dir.mkdir()
            stale = log_dir / "autopilot.log.2020-01-01"
            fresh = log_dir / "autopilot.log"
            stale.write_text("old\n", encoding="utf-8")
            fresh.write_text("fresh\n", encoding="utf-8")
            os.utime(stale, (time.time() - 10 * 86400, time.time() - 10 * 86400))
            core.rotate_logs(log_dir, days=7)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
