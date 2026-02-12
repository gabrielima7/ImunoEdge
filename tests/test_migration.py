import os
import sys
import json
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Adjust path to import src and scripts
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# Import the migration module
import migrate_v1_to_v2

class TestMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.legacy_dir = Path(self.temp_dir) / "legacy_buffer"
        self.legacy_dir.mkdir()
        self.db_path = Path(self.temp_dir) / "buffer.db"

        # Create some legacy JSON files
        self.payload1 = {"id": 1, "data": "test1"}
        with open(self.legacy_dir / "1.json", "w") as f:
            json.dump(self.payload1, f)

        self.payload2 = {"id": 2, "data": "test2"}
        with open(self.legacy_dir / "2.json", "w") as f:
            json.dump(self.payload2, f)

        # Corrupt file
        with open(self.legacy_dir / "bad.json", "w") as f:
            f.write("invalid json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_migration(self):
        # We patch DEFAULT_DB_PATH in the module with our temp path
        # And we verify get_legacy_dirs is mocked

        with patch("migrate_v1_to_v2.DEFAULT_DB_PATH", self.db_path), \
             patch("migrate_v1_to_v2.get_legacy_dirs", return_value=[self.legacy_dir]):

            # Run migration
            migrate_v1_to_v2.migrate()

            # Verify DB content
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("SELECT payload_json FROM telemetry_queue")
            rows = cursor.fetchall()
            conn.close()

            self.assertEqual(len(rows), 2)
            payloads = [json.loads(r[0]) for r in rows]
            # Sort by id to verify content
            payloads.sort(key=lambda x: x["id"])
            self.assertEqual(payloads[0], self.payload1)
            self.assertEqual(payloads[1], self.payload2)

            # Verify files are gone
            self.assertFalse((self.legacy_dir / "1.json").exists())
            self.assertFalse((self.legacy_dir / "2.json").exists())

            # Verify corrupt file is in quarantine
            # Quarantine is sibling of db_path
            quarantine = self.db_path.parent / ".quarantine"
            self.assertTrue((quarantine / "bad.json").exists())

if __name__ == "__main__":
    unittest.main()
