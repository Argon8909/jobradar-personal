"""SQL operations for profiles/contexts, using the caller's transaction."""

import json
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def session(db, session_id):
    return db.execute("SELECT * FROM user_sessions WHERE id=?", (session_id,)).fetchone()


def context(db, context_id):
    return db.execute(
        "SELECT c.*,p.user_id FROM search_contexts c "
        "JOIN user_profiles p ON p.id=c.profile_id WHERE c.id=?", (context_id,)
    ).fetchone()


def profile(db, profile_id):
    return db.execute("SELECT * FROM user_profiles WHERE id=?", (profile_id,)).fetchone()


def profiles(db, user_id):
    return db.execute("SELECT * FROM user_profiles WHERE user_id=? ORDER BY id", (user_id,)).fetchall()


def contexts(db, profile_id):
    return db.execute("SELECT * FROM search_contexts WHERE profile_id=? ORDER BY id", (profile_id,)).fetchall()


def activate(db, session_id, context_id):
    db.execute("UPDATE user_sessions SET active_context_id=?,updated_at=? WHERE id=?",
               (context_id, now(), session_id))


def create_profile(db, user_id, data):
    return db.execute("INSERT INTO user_profiles(user_id,profile_data) VALUES (?,?)",
                      (user_id, json.dumps(data, ensure_ascii=False, allow_nan=False))).lastrowid


def create_context(db, profile_id, name, criteria):
    return db.execute("INSERT INTO search_contexts(profile_id,title,criteria) VALUES (?,?,?)",
                      (profile_id, name, json.dumps(criteria, ensure_ascii=False, allow_nan=False))).lastrowid


def update_profile(db, profile_id, data):
    db.execute("UPDATE user_profiles SET profile_data=?,revision=revision+1,updated_at=? WHERE id=?",
               (json.dumps(data, ensure_ascii=False, allow_nan=False), now(), profile_id))


def update_context(db, context_id, criteria):
    db.execute("UPDATE search_contexts SET criteria=?,criteria_revision=criteria_revision+1,updated_at=? WHERE id=?",
               (json.dumps(criteria, ensure_ascii=False, allow_nan=False), now(), context_id))
