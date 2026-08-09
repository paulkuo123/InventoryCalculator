import os
import tempfile
import unittest
from pathlib import Path

from housekeeping import prune_generated_files, remove_files, remove_stale_matching_files


class HousekeepingTest(unittest.TestCase):
    def test_remove_files_ignores_missing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "temporary.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(remove_files((path, Path(directory) / "missing.json", None)), [])
            self.assertFalse(path.exists())

    def test_prune_generated_files_keeps_newest_matches_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(4):
                path = root / f"snapshot_{index}.html"
                path.write_text(str(index), encoding="utf-8")
                os.utime(path, (index + 1, index + 1))
            unrelated = root / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")

            removed = prune_generated_files(root, ("snapshot_*.html",), keep=2)

            self.assertEqual({path.name for path in removed}, {"snapshot_0.html", "snapshot_1.html"})
            self.assertEqual(
                {path.name for path in root.glob("snapshot_*.html")},
                {"snapshot_2.html", "snapshot_3.html"},
            )
            self.assertTrue(unrelated.exists())

    def test_stale_cleanup_requires_matching_name_and_age(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_match = root / "inventory_inbound_old_input.json"
            new_match = root / "inventory_inbound_new_input.json"
            unrelated = root / "other_old.json"
            for path in (old_match, new_match, unrelated):
                path.write_text("{}", encoding="utf-8")
            os.utime(old_match, (10, 10))
            os.utime(unrelated, (10, 10))
            os.utime(new_match, (190, 190))

            removed = remove_stale_matching_files(
                root,
                ("inventory_inbound_*_input.json",),
                older_than_seconds=50,
                now=200,
            )

            self.assertEqual(removed, [old_match])
            self.assertFalse(old_match.exists())
            self.assertTrue(new_match.exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
