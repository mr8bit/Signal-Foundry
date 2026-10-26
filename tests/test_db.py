from pathlib import Path
import sqlite3
import tempfile
import unittest

import db
import ops
from db_test import use_schema


class MigrationTests(unittest.TestCase):
    def test_news_identity_continues_after_imported_positions(self):
        use_schema(self)
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)
            with sqlite3.connect(source/'news.sqlite3') as conn:
                conn.execute('CREATE TABLE news (key TEXT PRIMARY KEY,payload TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL)')
                conn.execute("INSERT INTO news VALUES ('old','{}',1,2)")
            ops.migrate_sqlite(source)
        with db.connect() as conn:
            row=conn.execute("INSERT INTO news (key,payload,first_seen,last_seen) VALUES ('new','{}',3,4) RETURNING position").fetchone()
        self.assertEqual(row['position'],2)
