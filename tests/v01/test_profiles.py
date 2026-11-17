"""Domain integration tests: persisted data, rollback, permissions and revisions."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobradar.profiles import ProfileError, ProfileManager
from jobradar.storage import Database
from jobradar.storage import profiles as repository


class ProfileManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "new.db")
        self.database.initialize()
        self.manager = ProfileManager.start_session(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        return self.manager.create_profile("Аналитик", {"competencies": ["SQL"], "notes": "Опыт"}, authorized=True)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(ProfileError) as result:
            function(*args, **kwargs)
        self.assertEqual(result.exception.code, code)

    def test_create_initial_context_and_restore_session(self):
        result = self.create()
        reopened = ProfileManager(Database(self.database.path), 1, self.manager.session_id)
        state = reopened.get_state()
        self.assertEqual(state["active_context_id"], result["initial_context_id"])
        self.assertEqual(state["context"]["name"], "Основной поиск")
        self.assertEqual(state["profile"]["profile_data"], {"schema_version":1,"profession":"Аналитик","competencies":["SQL"],"notes":"Опыт"})
        self.assertEqual(state["context"]["criteria"], {"schema_version":1})

    def test_creation_rolls_back_profile_context_and_session_on_activation_failure(self):
        with patch.object(repository, "activate", side_effect=sqlite3.OperationalError("failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.create()
        self.assertEqual(self.manager.list_profiles(), {"profiles":[]})
        self.assertIsNone(self.manager.get_state()["active_context_id"])
        with self.database.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM search_contexts").fetchone()[0], 0)

    def test_create_context_activates_it_and_keeps_first(self):
        first = self.create()
        created = self.manager.create_context("Удалённо", {"work_formats":["remote"]}, authorized=True)
        self.assertEqual(created["profile_id"], first["profile_id"])
        self.assertEqual(self.manager.get_state()["active_context_id"], created["context_id"])
        contexts = self.manager.list_contexts()["contexts"]
        self.assertEqual([c["is_active"] for c in contexts], [False,True])

    def test_additional_context_rolls_back_on_activation_failure(self):
        self.create()
        before = self.manager.get_state()
        with patch.object(repository, "activate", side_effect=sqlite3.OperationalError("failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.manager.create_context("Новый", authorized=True)
        self.assertEqual(self.manager.get_state(), before)
        self.assertEqual(len(self.manager.list_contexts()["contexts"]), 1)

    def test_general_mode_keeps_messages_and_allows_second_profile(self):
        first = self.create()
        with self.database.transaction() as db:
            db.execute("INSERT INTO messages(user_id,session_id,context_id,content) VALUES (1,?,?,?)",
                       (self.manager.session_id, first["initial_context_id"], "История"))
        self.manager.return_to_general(authorized=True)
        self.assertIsNone(self.manager.get_state()["active_context_id"])
        self.manager.create_profile("Водитель", authorized=True)
        profiles = self.manager.list_profiles()["profiles"]
        self.assertEqual([p["profession"] for p in profiles], ["Аналитик","Водитель"])
        self.manager.switch_context(first["initial_context_id"], authorized=True)
        self.assertEqual(self.manager.get_state()["profile"]["profile_id"], first["profile_id"])
        with self.database.connection() as db:
            self.assertEqual(db.execute("SELECT content FROM messages").fetchone()[0], "История")

    def test_general_mode_rejects_active_operations(self):
        for method, args in [(self.manager.list_contexts,()), (self.manager.create_context,("Новый",)),
                             (self.manager.update_profile,({"notes":"x"},)),
                             (self.manager.update_context,({"notes":"x"},)),
                             (self.manager.return_to_general,())]:
            with self.subTest(method=method.__name__):
                kwargs = {} if method.__name__ == "list_contexts" else {"authorized":True}
                self.assert_code("ACTIVE_CONTEXT_REQUIRED", method, *args, **kwargs)

    def test_profile_creation_is_unavailable_in_active_mode(self):
        self.create()
        self.assert_code("TOOL_NOT_ALLOWED", self.manager.create_profile, "Водитель", authorized=True)
        self.assertEqual(len(self.manager.list_profiles()["profiles"]), 1)

    def test_all_mutations_require_trusted_authorization(self):
        self.assert_code("CONFIRMATION_REQUIRED", self.manager.create_profile, "Аналитик")
        self.create()
        for method, args in [(self.manager.switch_context,(1,)),(self.manager.create_context,("Новый",)),
                             (self.manager.update_profile,({"notes":"x"},)),
                             (self.manager.update_context,({"notes":"x"},)),(self.manager.return_to_general,())]:
            with self.subTest(method=method.__name__):
                self.assert_code("CONFIRMATION_REQUIRED", method, *args)

    def test_partial_profile_update_replaces_array_and_preserves_other_fields(self):
        self.create()
        result = self.manager.update_profile({"competencies":["Python"]}, authorized=True, expected_revision=1)
        data = self.manager.get_state()["profile"]["profile_data"]
        self.assertEqual(data["competencies"], ["Python"])
        self.assertEqual(data["notes"], "Опыт")
        self.assertEqual(data["profession"], "Аналитик")
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["updated_fields"], {"competencies":["Python"]})

    def test_salary_merge_array_replace_and_null_clear(self):
        self.create()
        self.manager.update_context({"salary":{"min":100000,"currency":"RUB","strict":True},"locations":["Москва"]}, authorized=True)
        result = self.manager.update_context({"salary":{"min":200000},"work_formats":["remote"]}, authorized=True)
        data = self.manager.get_state()["context"]["criteria"]
        self.assertEqual(data["salary"], {"min":200000,"currency":"RUB","strict":True})
        self.assertEqual(data["locations"], ["Москва"])
        self.assertEqual(result["criteria_revision"], 3)
        self.manager.update_context({"salary":None,"locations":[]}, authorized=True)
        data = self.manager.get_state()["context"]["criteria"]
        self.assertIsNone(data["salary"])
        self.assertEqual(data["locations"], [])

    def test_noop_does_not_invalidate_revisions(self):
        self.create()
        profile = self.manager.update_profile({"notes":"Опыт"}, authorized=True)
        context = self.manager.update_context({}, authorized=True)
        self.assertEqual(profile["revision"], 1)
        self.assertEqual(profile["updated_fields"], {})
        self.assertEqual(context["criteria_revision"], 1)

    def test_stale_revision_cannot_overwrite_changes(self):
        self.create()
        second = ProfileManager(self.database, 1, self.manager.session_id)
        self.manager.update_profile({"notes":"Новые данные"}, authorized=True, expected_revision=1)
        self.assert_code("REVISION_CONFLICT", second.update_profile, {"notes":"Старые данные"}, authorized=True, expected_revision=1)
        self.manager.update_context({"notes":"Новый поиск"}, authorized=True, expected_revision=1)
        self.assert_code("REVISION_CONFLICT", second.update_context, {"notes":"Старый поиск"}, authorized=True, expected_revision=1)
        self.assertEqual(self.manager.get_state()["profile"]["profile_data"]["notes"], "Новые данные")

    def test_invalid_inputs_leave_database_unchanged(self):
        self.assert_code("VALIDATION_ERROR", self.manager.create_profile, "Аналитик", None, authorized=True)
        self.create()
        before = self.manager.get_state()
        profile_changes = [{"schema_version":2},{"user_id":2},{"experience":[{"domain":"IT","years":-1}]},
                           {"competencies":None},{"notes":{"x":1}},{"notes":float("nan")},
                           {"education":[{"surprise":True}]}]
        context_changes = [{"salary":{"currency":"руб"}},{"work_formats":["moon"]},
                           {"salary":{"min":-1}},{"criteria_revision":100},
                           {"restrictions":[{"attribute":"x","value":1}]}]
        for changes in profile_changes:
            self.assert_code("VALIDATION_ERROR", self.manager.update_profile, changes, authorized=True)
        for changes in context_changes:
            self.assert_code("VALIDATION_ERROR", self.manager.update_context, changes, authorized=True)
        self.assert_code("VALIDATION_ERROR", self.manager.switch_context, True, authorized=True)
        self.assert_code("VALIDATION_ERROR", self.manager.create_context, "Новый", None, authorized=True)
        self.assertEqual(self.manager.get_state(), before)

    def test_user_isolation_and_session_forgery(self):
        first = self.create()
        with self.database.transaction() as db:
            db.execute("INSERT INTO users(id,name) VALUES (2,'Другой пользователь')")
        other = ProfileManager.start_session(self.database, 2)
        other_profile = other.create_profile("Водитель", authorized=True)
        self.assertEqual(len(other.list_profiles()["profiles"]), 1)
        self.assert_code("ACCESS_DENIED", self.manager.switch_context, other_profile["initial_context_id"], authorized=True)
        forged = ProfileManager(self.database, 2, self.manager.session_id)
        self.assert_code("ACCESS_DENIED", forged.list_profiles)
        self.assert_code("ACCESS_DENIED", forged.update_profile, {"notes":"чужое"}, authorized=True)
        self.assertEqual(self.manager.get_state()["active_context_id"], first["initial_context_id"])

    def test_corrupt_active_context_fails_closed(self):
        self.create()
        with self.database.transaction() as db:
            db.execute("INSERT INTO users(id) VALUES (2)")
        other = ProfileManager.start_session(self.database, 2)
        result = other.create_profile("Другой", authorized=True)
        with self.database.transaction() as db:
            db.execute("UPDATE user_sessions SET active_context_id=? WHERE id=?", (result["initial_context_id"], self.manager.session_id))
        self.assert_code("ACCESS_DENIED", self.manager.get_state)
        self.assert_code("ACCESS_DENIED", self.manager.update_context, {"notes":"чужое"}, authorized=True)

    def test_updates_affect_only_active_profile_and_context(self):
        first = self.create()
        self.manager.return_to_general(authorized=True)
        self.manager.create_profile("Водитель", authorized=True)
        self.manager.update_profile({"notes":"Только водитель"}, authorized=True)
        self.manager.update_context({"notes":"Только новый контекст"}, authorized=True)
        self.manager.switch_context(first["initial_context_id"], authorized=True)
        state = self.manager.get_state()
        self.assertEqual(state["profile"]["revision"], 1)
        self.assertEqual(state["profile"]["profile_data"]["notes"], "Опыт")
        self.assertNotIn("notes", state["context"]["criteria"])

    def test_assessment_preserved_but_revision_mismatch_marks_it_stale(self):
        first = self.create()
        with self.database.transaction() as db:
            db.execute("INSERT INTO vacancies(id,source,external_id) VALUES (1,'hh','123')")
            db.execute("INSERT INTO context_vacancies(context_id,vacancy_id,profile_revision,criteria_revision,reasoning) VALUES (?,1,1,1,'Исходная оценка')", (first["initial_context_id"],))
        self.manager.update_profile({"notes":"Новый опыт"}, authorized=True)
        self.manager.update_context({"notes":"Новые условия"}, authorized=True)
        with self.database.connection() as db:
            row = db.execute("SELECT * FROM context_vacancies").fetchone()
            self.assertEqual(row["reasoning"], "Исходная оценка")
            self.assertEqual((row["profile_revision"],row["criteria_revision"]), (1,1))
        state = self.manager.get_state()
        self.assertEqual(state["profile"]["revision"], 2)
        self.assertEqual(state["context"]["criteria_revision"], 2)

    def test_independent_sessions_keep_independent_active_context(self):
        first = self.create()
        other_session = ProfileManager.start_session(self.database)
        self.assertIsNone(other_session.get_state()["active_context_id"])
        other_session.switch_context(first["initial_context_id"], authorized=True)
        self.manager.create_context("Второй", authorized=True)
        self.assertEqual(other_session.get_state()["active_context_id"], first["initial_context_id"])

    def test_concurrent_updates_merge_against_latest_revision(self):
        from concurrent.futures import ThreadPoolExecutor

        self.create()
        second = ProfileManager(self.database, 1, self.manager.session_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.manager.update_profile, {"notes":"Новый опыт"}, authorized=True),
                       pool.submit(second.update_profile, {"competencies":["Python"]}, authorized=True)]
            for future in futures:
                future.result(timeout=5)
        state = self.manager.get_state()["profile"]
        self.assertEqual(state["revision"], 3)
        self.assertEqual(state["profile_data"]["notes"], "Новый опыт")
        self.assertEqual(state["profile_data"]["competencies"], ["Python"])

    def test_invalid_session_missing_context_and_revision_are_rejected(self):
        self.assert_code("ACCESS_DENIED", ProfileManager(self.database, 1, 9999).list_profiles)
        self.assert_code("CONTEXT_NOT_FOUND", self.manager.switch_context, 9999, authorized=True)
        self.create()
        self.assert_code("VALIDATION_ERROR", self.manager.update_profile, {}, authorized=True, expected_revision=True)


if __name__ == "__main__":
    unittest.main()
