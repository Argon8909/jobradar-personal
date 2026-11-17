"""Persist protocol messages inside the approved messages.content TEXT field."""

import json

class ChatHistory:
    def __init__(self, manager):
        self.manager = manager

    def append(self, messages, context_id):
        with self.manager.database.transaction() as db:
            self.manager._session(db)
            if context_id is not None:
                self.manager._owned_context(db, context_id)
            for message in messages:
                db.execute("INSERT INTO messages(user_id,session_id,context_id,role,content) VALUES (?,?,?,?,?)",
                           (self.manager.user_id, self.manager.session_id, context_id, message["role"],
                            json.dumps({"format":"jobradar.chat.v1","message":message}, ensure_ascii=False)))

    def read(self, limit=60):
        with self.manager.database.transaction() as db:
            self.manager._session(db)
            rows = db.execute("SELECT role,content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
                              (self.manager.user_id, limit)).fetchall()
        result = []
        for row in reversed(rows):
            try:
                envelope = json.loads(row["content"])
            except (ValueError, TypeError):
                envelope = None
            message = (envelope["message"] if isinstance(envelope, dict)
                       and envelope.get("format") == "jobradar.chat.v1"
                       else {"role":row["role"], "content":row["content"]})
            result.append(message)
        return result

    def dialogue_messages(self, limit=60):
        # Past tool calls stay in storage for inspection. Only conversational text
        # is replayed, so a crash/truncated history cannot leave orphan tool IDs.
        return [{"role": m["role"], "content":m["content"]} for m in self.read(limit)
                if m["role"] in ("user", "assistant") and m.get("content")]

    def public_page(self, *, before_id=None, limit=50):
        """Return chronological text messages; never expose tool protocol to the UI."""
        with self.manager.database.transaction() as db:
            self.manager._session(db)
            rows = db.execute(
                "SELECT id,role,content,context_id,created_at FROM messages "
                "WHERE user_id=? AND role IN ('user','assistant') AND (? IS NULL OR id<?) "
                "ORDER BY id DESC LIMIT ?",
                (self.manager.user_id, before_id, before_id, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        messages = []
        for row in reversed(selected):
            try:
                envelope = json.loads(row["content"])
            except (ValueError, TypeError):
                envelope = None
            message = (envelope.get("message", {}) if isinstance(envelope, dict)
                       and envelope.get("format") == "jobradar.chat.v1"
                       else {"content":row["content"]})
            if isinstance(message.get("content"), str) and message["content"]:
                messages.append({"id":row["id"], "role":row["role"], "content":message["content"],
                                 "context_id":row["context_id"], "created_at":row["created_at"]})
        return {"messages":messages, "has_more":has_more,
                "next_before":selected[-1]["id"] if has_more else None}
