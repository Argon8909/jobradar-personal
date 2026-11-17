"""Offline integration tests against real SQLite files, not database mocks."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobradar.storage import Database, SchemaError

TABLES = {
    "users", "user_accounts", "user_profiles", "search_contexts", "user_sessions",
    "messages", "search_tasks", "task_vacancies", "vacancies", "vacancy_cards",
    "tags", "tag_aliases", "vacancy_tags", "context_vacancies",
}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data" / "v01.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def graph(db):
        db.execute("INSERT INTO user_profiles(id,user_id,profile_data) VALUES (1,1,?)",
                   ('{"schema_version":1,"profession":"Аналитик"}',))
        db.execute("INSERT INTO search_contexts(id,profile_id,criteria) VALUES (1,1,?)",
                   ('{"schema_version":1}',))
        db.execute("INSERT INTO user_sessions(id,user_id,active_context_id) VALUES (1,1,1)")
        db.execute("INSERT INTO search_tasks(id,context_id) VALUES (1,1)")
        db.execute("INSERT INTO vacancies(id,source,external_id,description) VALUES (1,'hh','123','Исходный текст')")

    def test_schema_and_foreign_keys(self):
        with self.database.connection() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, TABLES)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], 1)
            # Card/tag association must point to the extracted card, not just the raw vacancy.
            targets = {row[2] for row in db.execute("PRAGMA foreign_key_list(vacancy_tags)")}
            self.assertEqual(targets, {"tags", "vacancy_cards"})

    def test_reinitialize_preserves_existing_data(self):
        with self.database.transaction() as db:
            self.graph(db)
        self.database.initialize()
        with self.database.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT description FROM vacancies").fetchone()[0], "Исходный текст")

    def test_refuses_legacy_database_without_changes(self):
        path = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE profile(id INTEGER PRIMARY KEY, notes TEXT)")
            db.execute("INSERT INTO profile VALUES (1,'старые данные')")
        before = path.read_bytes()
        with self.assertRaises(SchemaError):
            Database(path).initialize()
        self.assertEqual(path.read_bytes(), before)

    def test_refuses_newer_schema(self):
        with self.database.connection() as db:
            db.execute("PRAGMA user_version = 2")
        with self.assertRaises(SchemaError):
            self.database.initialize()

    def test_initialization_rolls_back_all_ddl_on_error(self):
        path = Path(self.temp.name) / "broken.sqlite3"
        with patch("jobradar.storage.database.files") as resource:
            resource.return_value.joinpath.return_value.read_text.return_value = (
                "CREATE TABLE users(id INTEGER PRIMARY KEY);\nINVALID SQL;\n"
            )
            with self.assertRaises(sqlite3.Error):
                Database(path).initialize()
        with sqlite3.connect(path) as db:
            self.assertEqual(list(db.execute("SELECT name FROM sqlite_master WHERE type='table'")), [])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_failed_transaction_rolls_back_profile_and_context(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as db:
                db.execute("INSERT INTO user_profiles(id,user_id) VALUES (1,1)")
                db.execute("INSERT INTO search_contexts(id,profile_id) VALUES (1,1)")
                db.execute("INSERT INTO user_sessions(user_id,active_context_id) VALUES (1,999)")
        with self.database.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM user_profiles").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM search_contexts").fetchone()[0], 0)

    def test_multiple_profiles_and_contexts_and_general_chat(self):
        with self.database.transaction() as db:
            self.graph(db)
            db.execute("INSERT INTO user_profiles(id,user_id) VALUES (2,1)")
            db.executemany("INSERT INTO search_contexts(id,profile_id) VALUES (?,?)", [(2,1),(3,2)])
            db.execute("UPDATE user_sessions SET active_context_id=NULL WHERE id=1")
            db.execute("INSERT INTO messages(user_id,session_id,context_id,role,content) VALUES (1,1,NULL,'user','Общий чат')")
        # A new connection represents reopening the application.
        with Database(self.path).connection() as db:
            self.assertIsNone(db.execute("SELECT active_context_id FROM user_sessions").fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM search_contexts WHERE profile_id=1").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT content FROM messages").fetchone()[0], "Общий чат")

    def test_every_foreign_key_rejects_missing_parent(self):
        for table in sorted(TABLES):
            with self.database.connection() as db:
                keys = list(db.execute(f"PRAGMA foreign_key_list({table})"))
            for key in keys:
                column = key[3]
                with self.subTest(table=table, column=column):
                    with self.assertRaises(sqlite3.IntegrityError):
                        with self.database.transaction() as db:
                            self.graph(db)
                            # Every relation can be tested by updating a valid child row.
                            db.execute("INSERT INTO user_accounts(id,user_id,provider) VALUES (1,1,'web')")
                            db.execute("INSERT INTO messages(id,user_id,session_id,context_id) VALUES (1,1,1,1)")
                            db.execute("INSERT INTO vacancy_cards(vacancy_id) VALUES (1)")
                            db.execute("INSERT INTO task_vacancies VALUES (1,1,'database')")
                            db.execute("INSERT INTO tags(id,category,canonical_name) VALUES (1,'skill','SQL')")
                            db.execute("INSERT INTO tag_aliases(id,tag_id,alias,normalized_alias) VALUES (1,1,'SQL','sql')")
                            db.execute("INSERT INTO vacancy_tags VALUES (1,1,'required','SQL')")
                            db.execute("INSERT INTO context_vacancies(context_id,vacancy_id) VALUES (1,1)")
                            db.execute(f"UPDATE {table} SET {column}=999999")

    def test_vacancy_deduplication_is_scoped_to_source(self):
        with self.database.transaction() as db:
            self.graph(db)
            db.execute("INSERT INTO vacancies(source,external_id) VALUES ('future','123')")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as db:
                db.execute("INSERT INTO vacancies(source,external_id) VALUES ('hh','123')")

    def test_task_membership_is_independent_and_unique(self):
        with self.database.transaction() as db:
            self.graph(db)
            db.execute("INSERT INTO search_tasks(id,context_id) VALUES (2,1)")
            db.executemany("INSERT INTO task_vacancies VALUES (?,1,'database')", [(1,),(2,)])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as db:
                db.execute("INSERT INTO task_vacancies VALUES (1,1,'database')")

    def test_raw_vacancy_cannot_have_tags_without_card(self):
        with self.database.transaction() as db:
            self.graph(db)
            db.execute("INSERT INTO tags(id,category,canonical_name) VALUES (1,'skill','SQL')")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as db:
                db.execute("INSERT INTO vacancy_tags VALUES (1,1,'required','SQL')")

    def test_reasoning_and_partial_result_survive_reopen(self):
        with self.database.transaction() as db:
            self.graph(db)
            db.execute("INSERT INTO context_vacancies(context_id,vacancy_id,reasoning,profile_revision,criteria_revision) VALUES (1,1,'Есть опыт SQL',1,1)")
            db.execute("UPDATE search_tasks SET state='completed',is_partial=1,stop_reason='deadline' WHERE id=1")
        with Database(self.path).connection() as db:
            self.assertEqual(db.execute("SELECT reasoning FROM context_vacancies").fetchone()[0], "Есть опыт SQL")
            self.assertEqual(tuple(db.execute("SELECT state,is_partial,stop_reason FROM search_tasks").fetchone()), ("completed",1,"deadline"))

    def test_partial_state_requires_completion_and_reason(self):
        with self.database.transaction() as db:
            self.graph(db)
        for state, partial, reason in [("running",1,"limit"),("completed",1,None),("completed",1," "),("completed",0,"limit")]:
            with self.subTest(state=state, partial=partial, reason=reason):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.database.transaction() as db:
                        db.execute("UPDATE search_tasks SET state=?,is_partial=?,stop_reason=?", (state,partial,reason))

    def test_invalid_json_rejected_in_all_json_columns(self):
        with self.database.transaction() as db:
            self.graph(db)
            db.execute("INSERT INTO vacancy_cards(vacancy_id) VALUES (1)")
        for table, column in [("user_profiles","profile_data"),("search_contexts","criteria"),("search_tasks","search_queries"),("vacancy_cards","structured_data")]:
            for value in ("{invalid", "[]", '"text"'):
                with self.subTest(table=table, value=value):
                    with self.assertRaises(sqlite3.IntegrityError):
                        with self.database.transaction() as db:
                            db.execute(f"UPDATE {table} SET {column}=?", (value,))

    def test_alias_uniqueness_is_per_category_and_normalized(self):
        with self.database.transaction() as db:
            db.executemany("INSERT INTO tags(id,category,canonical_name) VALUES (?,?,?)", [(1,"skill","PostgreSQL"),(2,"skill","Other"),(3,"certification","Other")])
            db.execute("INSERT INTO tag_aliases(tag_id,alias,normalized_alias) VALUES (1,' Postgres ','postgres')")
            db.execute("INSERT INTO tag_aliases(tag_id,alias,normalized_alias) VALUES (3,'Postgres','postgres')")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as db:
                db.execute("INSERT INTO tag_aliases(tag_id,alias,normalized_alias) VALUES (2,'POSTGRES','postgres')")

    def test_alias_canonical_conflicts_in_both_directions(self):
        with self.database.transaction() as db:
            db.executemany("INSERT INTO tags(id,category,canonical_name) VALUES (?,?,?)", [(1,"skill","SQL"),(2,"skill","PostgreSQL")])
            db.execute("INSERT INTO tag_aliases(tag_id,alias,normalized_alias) VALUES (2,'Postgres','postgres')")
        statements = [
            ("INSERT INTO tag_aliases(tag_id,alias,normalized_alias) VALUES (2,'sql','sql')", ()),
            ("INSERT INTO tags(category,canonical_name) VALUES ('skill',' POSTGRES ')", ()),
            ("UPDATE tags SET canonical_name='postgres' WHERE id=1", ()),
            ("UPDATE tag_aliases SET alias='SQL',normalized_alias='sql' WHERE tag_id=2", ()),
        ]
        for sql, parameters in statements:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.database.transaction() as db:
                        db.execute(sql, parameters)

    def test_tag_category_change_cannot_create_alias_conflict(self):
        with self.database.transaction() as db:
            db.executemany("INSERT INTO tags(id,category,canonical_name) VALUES (?,?,?)", [(1,"skill","A"),(2,"license","B")])
            db.executemany("INSERT INTO tag_aliases(tag_id,alias,normalized_alias) VALUES (?,'Shared','shared')", [(1,),(2,)])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as db:
                db.execute("UPDATE tags SET category='skill' WHERE id=2")

    def test_connection_does_not_create_missing_database(self):
        missing = Path(self.temp.name) / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            with Database(missing).connection():
                pass
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
