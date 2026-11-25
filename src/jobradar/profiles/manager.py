"""Session-scoped domain service, independent of Flask and of LLM interpretation."""

import json

from jobradar.storage import Database
from jobradar.storage import profiles as repository

from .contracts import ProfileError, validate, validate_profile_fields
from jobradar.search.period import ATTRIBUTE, stored_period

_UNSET = object()


class ProfileManager:
    """user_id/session_id and authorized originate in trusted application code.

    They are never arguments supplied by the LLM. This service verifies persisted
    ownership/mode for every call. The dialogue layer must establish explicit user
    intent or consent before passing authorized=True to a mutating operation.
    """

    def __init__(self, database: Database, user_id: int, session_id: int, *, expected_context_id=_UNSET):
        for value in (user_id, session_id):
            if type(value) is not int or value <= 0:
                raise ProfileError("VALIDATION_ERROR", "Invalid user or session identifier")
        self.database = database
        self.user_id = user_id
        self.session_id = session_id
        self.expected_context_id = expected_context_id

    @classmethod
    def start_session(cls, database: Database, user_id: int = 1):
        """Internal application operation: open a new web session for a known user."""
        if type(user_id) is not int or user_id <= 0:
            raise ProfileError("VALIDATION_ERROR", "Invalid user identifier")
        with database.transaction() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise ProfileError("ACCESS_DENIED", "User is unavailable")
            session_id = db.execute("INSERT INTO user_sessions(user_id,channel) VALUES (?,'web')",
                                    (user_id,)).lastrowid
        return cls(database, user_id, session_id)

    def _session(self, db):
        row = repository.session(db, self.session_id)
        if row is None or row["user_id"] != self.user_id:
            raise ProfileError("ACCESS_DENIED", "Session is unavailable")
        if self.expected_context_id is not _UNSET and row["active_context_id"] != self.expected_context_id:
            raise ProfileError("REVISION_CONFLICT", "Active context changed")
        if row["active_context_id"] is not None:
            self._owned_context(db, row["active_context_id"])
        return row

    def _owned_context(self, db, context_id):
        row = repository.context(db, context_id)
        if row is None:
            raise ProfileError("CONTEXT_NOT_FOUND", "Context was not found")
        if row["user_id"] != self.user_id:
            raise ProfileError("ACCESS_DENIED", "Context is unavailable")
        return row

    def _active(self, db):
        session = self._session(db)
        if session["active_context_id"] is None:
            raise ProfileError("ACTIVE_CONTEXT_REQUIRED", "Select a search context first")
        return self._owned_context(db, session["active_context_id"])

    @staticmethod
    def _authorize(authorized):
        if authorized is not True:
            raise ProfileError("CONFIRMATION_REQUIRED", "User intent or consent is required")

    @staticmethod
    def _revision(actual, expected):
        if expected is not None:
            if type(expected) is not int or expected < 1:
                raise ProfileError("VALIDATION_ERROR", "Invalid expected revision")
            if actual != expected:
                raise ProfileError("REVISION_CONFLICT", "Data changed; reload before updating")

    @staticmethod
    def _context_summary(row, active_id):
        return {"context_id": row["id"], "name": row["title"],
                "summary": json.loads(row["criteria"] or "{}").get("notes"),
                "is_active": row["id"] == active_id}

    def get_state(self):
        with self.database.transaction() as db:
            session = self._session(db)
            if session["active_context_id"] is None:
                return {"active_context_id": None, "profile": None, "context": None}
            context = self._active(db)
            profile = repository.profile(db, context["profile_id"])
            return {"active_context_id": context["id"],
                    "profile": {"profile_id": profile["id"], "revision": profile["revision"],
                                "profile_data": json.loads(profile["profile_data"])},
                    "context": {"context_id": context["id"], "name": context["title"],
                                "criteria_revision": context["criteria_revision"],
                                "criteria": json.loads(context["criteria"])}}

    def list_profiles(self):
        with self.database.transaction() as db:
            session = self._session(db)
            result = []
            for profile in repository.profiles(db, self.user_id):
                data = json.loads(profile["profile_data"] or "{}")
                result.append({"profile_id": profile["id"], "profession": data.get("profession"),
                               "summary": data.get("notes"),
                               "contexts": [self._context_summary(row, session["active_context_id"])
                                            for row in repository.contexts(db, profile["id"])]})
            return {"profiles": result}

    def create_profile(self, profession, profile_data=_UNSET, *, authorized=False):
        arguments = {"profession": profession}
        if profile_data is not _UNSET:
            arguments["profile_data"] = profile_data
        arguments = validate("create_profile", arguments)
        validate_profile_fields(arguments.get("profile_data",{}))
        self._authorize(authorized)
        data = {**arguments.get("profile_data", {}), "profession": profession, "schema_version": 1}
        with self.database.transaction() as db:
            if self._session(db)["active_context_id"] is not None:
                raise ProfileError("TOOL_NOT_ALLOWED", "Return to general dialogue before creating a profile")
            profile_id = repository.create_profile(db, self.user_id, data)
            context_id = repository.create_context(db, profile_id, "Основной поиск", {"schema_version": 1})
            repository.activate(db, self.session_id, context_id)
            return {"profile_id": profile_id, "initial_context_id": context_id,
                    "initial_context_name": "Основной поиск", "active_context_id": context_id}

    def switch_context(self, context_id, *, authorized=False):
        validate("switch_context", {"context_id": context_id})
        self._authorize(authorized)
        with self.database.transaction() as db:
            self._session(db)
            context = self._owned_context(db, context_id)
            repository.activate(db, self.session_id, context_id)
            return {"active_context_id": context_id, "profile_id": context["profile_id"],
                    "context_name": context["title"]}

    def return_to_general(self, *, authorized=False):
        self._authorize(authorized)
        with self.database.transaction() as db:
            self._active(db)
            repository.activate(db, self.session_id, None)
            return {"active_context_id": None}

    def list_contexts(self):
        with self.database.transaction() as db:
            active = self._active(db)
            return {"profile_id": active["profile_id"],
                    "contexts": [self._context_summary(row, active["id"])
                                 for row in repository.contexts(db, active["profile_id"])]}

    def create_context(self, name, criteria=_UNSET, *, authorized=False):
        arguments = {"name": name}
        if criteria is not _UNSET:
            arguments["criteria"] = criteria
        arguments = validate("create_context", arguments)
        self._authorize(authorized)
        data = {**arguments.get("criteria", {}), "schema_version": 1}
        try:
            stored_period(data)
        except ValueError as error:
            raise ProfileError("VALIDATION_ERROR",str(error)) from None
        with self.database.transaction() as db:
            active = self._active(db)
            context_id = repository.create_context(db, active["profile_id"], name, data)
            repository.activate(db, self.session_id, context_id)
            return {"context_id": context_id, "profile_id": active["profile_id"],
                    "name": name, "criteria_revision": 1, "active_context_id": context_id}

    def update_profile(self, changes, *, authorized=False, expected_revision=None):
        changes = validate("update_profile", {"changes": changes})["changes"]
        validate_profile_fields(changes)
        self._authorize(authorized)
        with self.database.transaction() as db:
            active = self._active(db)
            profile = repository.profile(db, active["profile_id"])
            self._revision(profile["revision"], expected_revision)
            old = json.loads(profile["profile_data"])
            updated = {**old, **changes}
            fields = {key: value for key, value in changes.items() if key not in old or old[key] != value}
            if fields:
                repository.update_profile(db, profile["id"], updated)
            return {"profile_id": profile["id"], "revision": profile["revision"] + bool(fields),
                    "updated_fields": fields}

    def update_context(self, changes, *, authorized=False, expected_revision=None):
        changes = validate("update_context", {"changes": changes})["changes"]
        self._authorize(authorized)
        with self.database.transaction() as db:
            active = self._active(db)
            self._revision(active["criteria_revision"], expected_revision)
            old = json.loads(active["criteria"])
            updated = {**old, **changes}
            # A period-only patch uses the existing extensible restriction list;
            # preserve unrelated hard constraints even if the model omitted them.
            items = changes.get("restrictions")
            if items and all(item["attribute"] == ATTRIBUTE for item in items):
                updated["restrictions"] = [item for item in old.get("restrictions",[]) if item["attribute"] != ATTRIBUTE] + items
            try:
                stored_period(updated)
            except ValueError as error:
                raise ProfileError("VALIDATION_ERROR",str(error)) from None
            if isinstance(changes.get("salary"), dict):
                updated["salary"] = {**(old.get("salary") or {}), **changes["salary"]}
            fields = {key: updated[key] for key in changes if key not in old or updated[key] != old[key]}
            if fields:
                repository.update_context(db, active["id"], updated)
            return {"context_id": active["id"],
                    "criteria_revision": active["criteria_revision"] + bool(fields),
                    "updated_fields": fields}
