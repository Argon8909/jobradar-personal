"""Raw vacancy, card, tag and assessment SQL. Caller owns transactions."""

import hashlib
import json
from difflib import SequenceMatcher

from jobradar.profiles.contracts import ProfileError
from .database import normalize_tag
from .profiles import now


def encode(data):
    return json.dumps(data,ensure_ascii=False,allow_nan=False,sort_keys=True)


def upsert_raw(db, raw):
    values = {key:getattr(raw,key) for key in ("source","external_id","url","title","employer","description","published_at")}
    digest = hashlib.sha256(encode(values).encode()).hexdigest()
    previous = db.execute("SELECT id,content_hash FROM vacancies WHERE source=? AND external_id=?",
                          (raw.source,raw.external_id)).fetchone()
    stamp = now()
    if previous:
        vacancy_id = previous["id"]
        db.execute("UPDATE vacancies SET url=?,title=?,employer=?,description=?,content_hash=?,published_at=?,"
                   "status='available',last_seen_at=?,fetched_at=? WHERE id=?",
                   (raw.url,raw.title,raw.employer,raw.description,digest,raw.published_at,stamp,stamp,vacancy_id))
        if previous["content_hash"] != digest:
            db.execute("UPDATE context_vacancies SET profile_revision=NULL,criteria_revision=NULL WHERE vacancy_id=?",(vacancy_id,))
    else:
        vacancy_id = db.execute("INSERT INTO vacancies(source,external_id,url,title,employer,description,content_hash,"
                                "status,published_at,first_seen_at,last_seen_at,fetched_at) VALUES (?,?,?,?,?,?,?,'available',?,?,?,?)",
                                (raw.source,raw.external_id,raw.url,raw.title,raw.employer,raw.description,digest,
                                 raw.published_at,stamp,stamp,stamp)).lastrowid
    return vacancy_id


def get_raw(db, vacancy_id):
    row = db.execute("SELECT * FROM vacancies WHERE id=?",(vacancy_id,)).fetchone()
    return dict(row) if row else None


def get_card(db, vacancy_id):
    row = db.execute("SELECT * FROM vacancy_cards WHERE vacancy_id=?",(vacancy_id,)).fetchone()
    if row is None:
        return None
    card = dict(row)
    card["structured_data"] = json.loads(card["structured_data"] or '{"version":1}')
    return card


def get_tags(db, vacancy_id):
    return [dict(row) for row in db.execute(
        "SELECT t.id,t.category,t.canonical_name AS name,v.requirement_level,v.evidence FROM vacancy_tags v "
        "JOIN tags t ON t.id=v.tag_id WHERE v.vacancy_id=? ORDER BY t.id",(vacancy_id,))]


def get_assessment(db, context_id, vacancy_id):
    row = db.execute("SELECT * FROM context_vacancies WHERE context_id=? AND vacancy_id=?",(context_id,vacancy_id)).fetchone()
    return dict(row) if row else None


def find_tag(db, category, name):
    normalized = normalize_tag(name)
    return db.execute("SELECT t.* FROM tags t WHERE t.category=? AND (normalize_tag(t.canonical_name)=? OR "
                      "EXISTS (SELECT 1 FROM tag_aliases a WHERE a.tag_id=t.id AND a.normalized_alias=?))",
                      (category,normalized,normalized)).fetchone()


def tag_candidates(db, category, name, limit=12):
    rows = [dict(row) for row in db.execute("SELECT id,canonical_name FROM tags WHERE category=? ORDER BY id DESC LIMIT 500",(category,))]
    rows.sort(key=lambda row:SequenceMatcher(None,normalize_tag(name),normalize_tag(row["canonical_name"])).ratio(),reverse=True)
    return rows[:limit]


def _persist_tag(db, tag):
    existing = find_tag(db,tag["category"],tag["name"])
    if existing:
        if tag["tag_id"] is not None and tag["tag_id"] != existing["id"]:
            raise ProfileError("TAG_CONFLICT","Изменилось соответствие синонима тега.")
        return existing["id"]
    tag_id = tag["tag_id"]
    if tag_id is None:
        return db.execute("INSERT INTO tags(category,canonical_name) VALUES (?,?)",
                          (tag["category"]," ".join(tag["name"].split()))).lastrowid
    target = db.execute("SELECT * FROM tags WHERE id=? AND category=?",(tag_id,tag["category"])).fetchone()
    if target is None:
        raise ProfileError("TAG_CONFLICT","Канонический тег недоступен.")
    db.execute("INSERT INTO tag_aliases(tag_id,alias,normalized_alias) VALUES (?,?,?)",
               (tag_id,tag["name"],normalize_tag(tag["name"])))
    return tag_id


def save_card_bundle(db, vacancy, card, tags, assessment, state, version):
    previous = get_card(db,vacancy["id"])
    if previous and (previous["source_hash"] != vacancy["content_hash"] or previous["extraction_version"] != version):
        db.execute("UPDATE context_vacancies SET profile_revision=NULL,criteria_revision=NULL WHERE vacancy_id=?",(vacancy["id"],))
    columns = ("profession","specialization","area","work_format","salary_from","salary_to","salary_currency",
               "experience_min","summary","structured_data")
    data = [encode(card.get(key,{"version":1})) if key == "structured_data" else card.get(key) for key in columns]
    db.execute("INSERT INTO vacancy_cards(vacancy_id," + ",".join(columns) + ",source_hash,extraction_version,extracted_at) "
               "VALUES (" + ",".join("?" for _ in range(14)) + ") ON CONFLICT(vacancy_id) DO UPDATE SET " +
               ",".join(key+"=excluded."+key for key in (*columns,"source_hash","extraction_version","extracted_at")),
               [vacancy["id"],*data,vacancy["content_hash"],version,now()])
    db.execute("DELETE FROM vacancy_tags WHERE vacancy_id=?",(vacancy["id"],))
    merged = {}
    levels = {"required":3,"optional":2,"mentioned":1}
    for tag in tags:
        tag_id = _persist_tag(db,tag)
        if tag_id not in merged:
            merged[tag_id] = {"level":tag["requirement_level"],"evidence":[tag["evidence"]]}
        else:
            item = merged[tag_id]
            if levels[tag["requirement_level"]] > levels[item["level"]]:
                item["level"] = tag["requirement_level"]
            if tag["evidence"] not in item["evidence"]:
                item["evidence"].append(tag["evidence"])
    for tag_id, item in merged.items():
        db.execute("INSERT INTO vacancy_tags VALUES (?,?,?,?)",(vacancy["id"],tag_id,item["level"],"\n".join(item["evidence"])))
    save_assessment(db,vacancy["id"],assessment,state)


def save_assessment(db, vacancy_id, assessment, state):
    db.execute("INSERT INTO context_vacancies(context_id,vacancy_id,relevance_score,decision,reasoning,profile_revision,criteria_revision,assessed_at) "
               "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(context_id,vacancy_id) DO UPDATE SET "
               "relevance_score=excluded.relevance_score,decision=excluded.decision,reasoning=excluded.reasoning,"
               "profile_revision=excluded.profile_revision,criteria_revision=excluded.criteria_revision,assessed_at=excluded.assessed_at",
               (state["active_context_id"],vacancy_id,assessment.get("relevance_score"),assessment["decision"],assessment["reasoning"],
                state["profile"]["revision"],state["context"]["criteria_revision"],now()))


def local_candidates(db, terms, limit):
    if not terms:
        return []
    predicates, args = [], []
    for term in terms[:16]:
        value = "%" + normalize_tag(term).replace("\\","\\\\").replace("%","\\%").replace("_","\\_") + "%"
        predicates.append("(normalize_tag(coalesce(v.title,'')) LIKE ? ESCAPE '\\' OR "
                          "normalize_tag(coalesce(c.profession,'') || ' ' || coalesce(c.specialization,'') || ' ' || coalesce(c.summary,'')) LIKE ? ESCAPE '\\' "
                          "OR EXISTS(SELECT 1 FROM vacancy_tags vt JOIN tags t ON t.id=vt.tag_id WHERE vt.vacancy_id=v.id AND "
                          "(normalize_tag(t.canonical_name) LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM tag_aliases a WHERE a.tag_id=t.id AND a.normalized_alias LIKE ? ESCAPE '\\'))) "
                          "OR (c.vacancy_id IS NULL AND normalize_tag(coalesce(v.description,'')) LIKE ? ESCAPE '\\'))")
        args.extend([value]*5)
    return [dict(row) for row in db.execute("SELECT v.* FROM vacancies v LEFT JOIN vacancy_cards c ON c.vacancy_id=v.id WHERE " +
                                          " OR ".join(predicates) + " ORDER BY v.last_seen_at DESC,v.id DESC LIMIT ?",[*args,limit])]
