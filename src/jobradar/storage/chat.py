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
