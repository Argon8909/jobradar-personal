CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE user_accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL,
    external_user_id TEXT,
    status TEXT,
    linked_at TEXT
);

CREATE TABLE user_profiles (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    profile_data TEXT CHECK (profile_data IS NULL OR (json_valid(profile_data) AND json_type(profile_data) = 'object')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE search_contexts (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES user_profiles(id),
    title TEXT,
    criteria TEXT CHECK (criteria IS NULL OR (json_valid(criteria) AND json_type(criteria) = 'object')),
    criteria_revision INTEGER NOT NULL DEFAULT 1 CHECK (criteria_revision >= 1),
    status TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE user_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    active_context_id INTEGER REFERENCES search_contexts(id),
    channel TEXT,
    external_chat_id TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER NOT NULL REFERENCES user_sessions(id),
    context_id INTEGER REFERENCES search_contexts(id),
    role TEXT,
    content TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE search_tasks (
    id INTEGER PRIMARY KEY,
    context_id INTEGER NOT NULL REFERENCES search_contexts(id),
    goal TEXT,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    search_queries TEXT CHECK (search_queries IS NULL OR (json_valid(search_queries) AND json_type(search_queries) = 'object')),
    target_count INTEGER CHECK (target_count IS NULL OR target_count > 0),
    is_partial INTEGER NOT NULL DEFAULT 0 CHECK (is_partial IN (0, 1)),
    stop_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT,
    CHECK (is_partial = 0 OR (state = 'completed' AND length(trim(stop_reason)) > 0 AND stop_reason IS NOT NULL)),
    CHECK (state != 'completed' OR is_partial = 1 OR stop_reason IS NULL)
);

CREATE TABLE vacancies (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT,
    title TEXT,
    employer TEXT,
    description TEXT,
    content_hash TEXT,
    status TEXT,
    published_at TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    last_checked_at TEXT,
    fetched_at TEXT,
    UNIQUE (source, external_id)
);

CREATE TABLE task_vacancies (
    task_id INTEGER NOT NULL REFERENCES search_tasks(id),
    vacancy_id INTEGER NOT NULL REFERENCES vacancies(id),
    origin TEXT,
    PRIMARY KEY (task_id, vacancy_id)
);

CREATE TABLE vacancy_cards (
    vacancy_id INTEGER PRIMARY KEY REFERENCES vacancies(id),
    profession TEXT,
    specialization TEXT,
    area TEXT,
    work_format TEXT CHECK (work_format IS NULL OR work_format IN ('remote', 'hybrid', 'office', 'unknown')),
    salary_from NUMERIC,
    salary_to NUMERIC,
    salary_currency TEXT,
    experience_min NUMERIC,
    summary TEXT,
    structured_data TEXT CHECK (structured_data IS NULL OR (json_valid(structured_data) AND json_type(structured_data) = 'object')),
    source_hash TEXT,
    extraction_version INTEGER,
    extracted_at TEXT
);

CREATE TABLE tags (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (category, canonical_name)
);

CREATE TABLE tag_aliases (
    id INTEGER PRIMARY KEY,
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL CHECK (normalized_alias = normalize_tag(alias) AND length(normalized_alias) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (tag_id, normalized_alias)
);

CREATE TABLE vacancy_tags (
    vacancy_id INTEGER NOT NULL REFERENCES vacancy_cards(vacancy_id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    requirement_level TEXT NOT NULL CHECK (requirement_level IN ('required', 'optional', 'mentioned')),
    evidence TEXT,
    PRIMARY KEY (vacancy_id, tag_id)
);

CREATE TABLE context_vacancies (
    context_id INTEGER NOT NULL REFERENCES search_contexts(id),
    vacancy_id INTEGER NOT NULL REFERENCES vacancies(id),
    relevance_score NUMERIC,
    decision TEXT,
    reasoning TEXT,
    review_status TEXT,
    profile_revision INTEGER,
    criteria_revision INTEGER,
    assessed_at TEXT,
    PRIMARY KEY (context_id, vacancy_id)
);

CREATE INDEX idx_accounts_user ON user_accounts(user_id);
CREATE INDEX idx_profiles_user ON user_profiles(user_id);
CREATE INDEX idx_contexts_profile ON search_contexts(profile_id);
CREATE INDEX idx_sessions_user ON user_sessions(user_id);
CREATE INDEX idx_sessions_context ON user_sessions(active_context_id);
CREATE INDEX idx_messages_user_id ON messages(user_id, id);
CREATE INDEX idx_messages_session ON messages(session_id);
CREATE INDEX idx_messages_context ON messages(context_id);
CREATE INDEX idx_tasks_context ON search_tasks(context_id, id);
CREATE INDEX idx_task_vacancies_vacancy ON task_vacancies(vacancy_id);
CREATE INDEX idx_cards_profession_area_format ON vacancy_cards(profession, area, work_format);
CREATE INDEX idx_aliases_normalized ON tag_aliases(normalized_alias);
CREATE INDEX idx_vacancy_tags_tag ON vacancy_tags(tag_id, vacancy_id);
CREATE INDEX idx_context_vacancies_vacancy ON context_vacancies(vacancy_id);

-- Category belongs to tags in the approved model. Cross-table uniqueness therefore
-- uses transaction-safe triggers instead of adding category to tag_aliases.
CREATE TRIGGER tag_alias_insert_conflict BEFORE INSERT ON tag_aliases
WHEN EXISTS (
    SELECT 1 FROM tag_aliases a JOIN tags other ON other.id = a.tag_id
    JOIN tags own ON own.id = NEW.tag_id
    WHERE other.category = own.category AND a.normalized_alias = NEW.normalized_alias
) OR EXISTS (
    SELECT 1 FROM tags other JOIN tags own ON own.id = NEW.tag_id
    WHERE other.category = own.category AND other.id != NEW.tag_id
      AND normalize_tag(other.canonical_name) = NEW.normalized_alias
)
BEGIN
    SELECT RAISE(ABORT, 'Conflicting tag alias in category');
END;

CREATE TRIGGER tag_alias_update_conflict BEFORE UPDATE ON tag_aliases
WHEN EXISTS (
    SELECT 1 FROM tag_aliases a JOIN tags other ON other.id = a.tag_id
    JOIN tags own ON own.id = NEW.tag_id
    WHERE a.id != OLD.id AND other.category = own.category
      AND a.normalized_alias = NEW.normalized_alias
) OR EXISTS (
    SELECT 1 FROM tags other JOIN tags own ON own.id = NEW.tag_id
    WHERE other.category = own.category AND other.id != NEW.tag_id
      AND normalize_tag(other.canonical_name) = NEW.normalized_alias
)
BEGIN
    SELECT RAISE(ABORT, 'Conflicting tag alias in category');
END;

CREATE TRIGGER tag_insert_conflict BEFORE INSERT ON tags
WHEN EXISTS (
    SELECT 1 FROM tags WHERE category = NEW.category
      AND normalize_tag(canonical_name) = normalize_tag(NEW.canonical_name)
) OR EXISTS (
    SELECT 1 FROM tag_aliases a JOIN tags t ON t.id = a.tag_id
    WHERE t.category = NEW.category AND a.normalized_alias = normalize_tag(NEW.canonical_name)
)
BEGIN
    SELECT RAISE(ABORT, 'Conflicting canonical tag in category');
END;

CREATE TRIGGER tag_update_conflict BEFORE UPDATE OF category, canonical_name ON tags
WHEN EXISTS (
    SELECT 1 FROM tags WHERE id != OLD.id AND category = NEW.category
      AND normalize_tag(canonical_name) = normalize_tag(NEW.canonical_name)
) OR EXISTS (
    SELECT 1 FROM tag_aliases a JOIN tags t ON t.id = a.tag_id
    WHERE t.id != OLD.id AND t.category = NEW.category
      AND a.normalized_alias = normalize_tag(NEW.canonical_name)
) OR EXISTS (
    SELECT 1 FROM tag_aliases own JOIN tag_aliases other
      ON own.normalized_alias = other.normalized_alias
    JOIN tags t ON t.id = other.tag_id
    WHERE own.tag_id = OLD.id AND other.tag_id != OLD.id AND t.category = NEW.category
) OR EXISTS (
    SELECT 1 FROM tag_aliases own JOIN tags t
      ON own.normalized_alias = normalize_tag(t.canonical_name)
    WHERE own.tag_id = OLD.id AND t.id != OLD.id AND t.category = NEW.category
)
BEGIN
    SELECT RAISE(ABORT, 'Conflicting canonical tag or aliases in category');
END;
