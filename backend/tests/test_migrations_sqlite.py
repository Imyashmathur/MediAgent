"""Exercise fresh SQLite migrations and note-source rollback without external services."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_sqlite_upgrade_and_note_source_round_trip(tmp_path):
    database = tmp_path / 'synthetic-migrations.db'
    environment = {**os.environ, 'DATABASE_URL': f'sqlite+aiosqlite:///{database}'}
    root = Path(__file__).resolve().parents[1]
    def migrate(*args):
        subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=root, env=environment, check=True, capture_output=True, timeout=30)
    migrate('upgrade', 'head')
    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(clinical_note_versions)')}
        assert {'source_processing_job_id', 'source_extraction_version', 'source_fields'} <= columns
        assert any(row[2] == 'extraction_jobs' and row[3] == 'source_processing_job_id' for row in db.execute('PRAGMA foreign_key_list(clinical_note_versions)'))
        indexes = {row[1] for row in db.execute('PRAGMA index_list(clinical_note_versions)')}
        assert {'uq_note_open_version', 'uq_note_approved_version'} <= indexes
    migrate('downgrade', '20261125_38')
    migrate('upgrade', 'head')
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '20261125_53'
        assert any(row[2] == 'object_document_submissions' and row[3] == 'intent_id' for row in db.execute('PRAGMA foreign_key_list(document_object_links)'))
        assert any(row[2] == 'object_document_submissions' and row[3] == 'intent_id' for row in db.execute('PRAGMA foreign_key_list(object_upload_discards)'))
        assert 'error_code' in {row[1] for row in db.execute('PRAGMA table_info(object_upload_discards)')}
        assert 'ix_object_upload_discard_error_list' in {row[1] for row in db.execute('PRAGMA index_list(object_upload_discards)')}
        assert any(row[2] == 'extraction_jobs' and row[3] == 'source_processing_job_id' for row in db.execute('PRAGMA foreign_key_list(clinical_note_versions)'))
