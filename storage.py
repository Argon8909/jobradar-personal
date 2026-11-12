"""SQLite source of truth: one connection per operation; safe for Flask threads."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'jobradar.db'


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=15)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('PRAGMA journal_mode=WAL')
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS vacancies (
            id TEXT PRIMARY KEY, data TEXT NOT NULL, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS analyses (
            vacancy_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK(id=1), notes TEXT NOT NULL DEFAULT '',
            skills TEXT NOT NULL DEFAULT '[]', preferences TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, state TEXT NOT NULL, goal TEXT NOT NULL,
            spec TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, checked INTEGER NOT NULL DEFAULT 0,
            analyzed INTEGER NOT NULL DEFAULT 0, queries_done INTEGER NOT NULL DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT NOT NULL, args TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vacancy_cards (
            vacancy_id TEXT PRIMARY KEY, data TEXT NOT NULL, search_text TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_vacancy_cards_updated ON vacancy_cards(updated_at);
        CREATE TABLE IF NOT EXISTS task_vacancies (
            task_id INTEGER NOT NULL, vacancy_id TEXT NOT NULL, origin TEXT NOT NULL,
            PRIMARY KEY(task_id,vacancy_id));
        CREATE INDEX IF NOT EXISTS idx_task_vacancies_task ON task_vacancies(task_id);
        CREATE TABLE IF NOT EXISTS resumes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL);
        ''')
        db.execute("INSERT OR IGNORE INTO profile(id,notes,skills,preferences,updated_at) VALUES (1,'','[]','{}',?)", (now(),))
        # A crashed worker must never appear active after restarting the server.
        db.execute("UPDATE tasks SET state='interrupted',updated_at=? WHERE state IN ('running','stopping')", (now(),))


def get_meta(key):
    with connect() as db:
        row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return row['value'] if row else None


def set_meta(key,value):
    with connect() as db:
        db.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))


def get_profile():
    with connect() as db:
        d = db.execute('SELECT * FROM profile WHERE id=1').fetchone()
    return dict(notes=d['notes'], skills=json.loads(d['skills']),
                preferences=json.loads(d['preferences']), updated_at=d['updated_at'])


def save_profile(notes=None, skills=None, preferences=None):
    with connect() as db:
        old=db.execute('SELECT * FROM profile WHERE id=1').fetchone()
        db.execute('UPDATE profile SET notes=?,skills=?,preferences=?,updated_at=? WHERE id=1',
                   (old['notes'] if notes is None else notes,
                    old['skills'] if skills is None else json.dumps(skills,ensure_ascii=False),
                    old['preferences'] if preferences is None else json.dumps(preferences,ensure_ascii=False),now()))
    return get_profile()


def bootstrap_profile():
    if get_meta('profile_imported'):return
    profile = get_profile()
    old = ROOT / 'profile.txt'
    if not profile['notes'] and old.exists():
        contents=old.read_text(encoding='utf-8-sig').strip()
        if contents:
            save_profile(notes=contents)
    set_meta('profile_imported','1')


def add_message(role,content):
    with connect() as db:
        db.execute('INSERT INTO messages(role,content,created_at) VALUES (?,?,?)',(role,content,now()))


def messages(limit=24):
    with connect() as db:
        rows=db.execute('SELECT id,role,content,created_at FROM messages ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(x) for x in reversed(rows)]


def log(kind,content):
    with connect() as db:
        db.execute('INSERT INTO events(kind,content,created_at) VALUES (?,?,?)',(kind,content,now()))


def events(after=0, limit=50):
    with connect() as db:
        rows=db.execute('SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?', (after,limit)).fetchall()
    return [dict(x) for x in reversed(rows)]


def upsert_vacancies(items):
    count=0
    with connect() as db:
        for item in items:
            if not isinstance(item,dict) or not str(item.get('id','')).strip():
                continue
            key=str(item['id'])
            cur=db.execute('INSERT OR IGNORE INTO vacancies(id,data,first_seen,updated_at) VALUES (?,?,?,?)',
                           (key,json.dumps(item,ensure_ascii=False),now(),now()))
            if cur.rowcount: count+=1
            else:
                db.execute('UPDATE vacancies SET data=?,updated_at=? WHERE id=?',
                           (json.dumps(item,ensure_ascii=False),now(),key))
    return count


def get_vacancies(limit=100, unanalyzed=False):
    with connect() as db:
        if unanalyzed:
            rows=db.execute('SELECT v.data FROM vacancies v LEFT JOIN analyses a ON v.id=a.vacancy_id WHERE a.vacancy_id IS NULL ORDER BY v.first_seen DESC LIMIT ?', (limit,)).fetchall()
        else:
            rows=db.execute('SELECT data FROM vacancies ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()
    return [json.loads(row['data']) for row in rows]


def save_analysis(vacancy_id, result):
    with connect() as db:
        db.execute('INSERT INTO analyses(vacancy_id,data,updated_at) VALUES (?,?,?) ON CONFLICT(vacancy_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                   (str(vacancy_id),json.dumps(result,ensure_ascii=False),now()))


def get_analyses(limit=100):
    with connect() as db:
        rows=db.execute('SELECT a.data AS analysis,v.data AS vacancy FROM analyses a JOIN vacancies v ON v.id=a.vacancy_id ORDER BY a.updated_at DESC LIMIT ?', (limit,)).fetchall()
    return [dict(vacancy=json.loads(r['vacancy']),analysis=json.loads(r['analysis'])) for r in rows]


def counts():
    with connect() as db:
        return {table:db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('vacancies','analyses')}


def create_task(goal,spec):
    with connect() as db:
        cur=db.execute('INSERT INTO tasks(state,goal,spec,updated_at) VALUES (?,?,?,?)',('running',goal,json.dumps(spec,ensure_ascii=False),now()))
        return cur.lastrowid


def task(task_id):
    with connect() as db:
        row=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not row: return None
    d=dict(row);d['spec']=json.loads(d['spec']);return d


def active_task():
    with connect() as db:
        row=db.execute("SELECT id FROM tasks WHERE state IN ('running','stopping') ORDER BY id DESC LIMIT 1").fetchone()
    return task(row['id']) if row else None


def last_task():
    with connect() as db:
        row=db.execute('SELECT id FROM tasks ORDER BY id DESC LIMIT 1').fetchone()
    return task(row['id']) if row else None


def update_task(task_id, **fields):
    allowed={'state','goal','spec','revision','checked','analyzed','queries_done','error'}
    if set(fields)-allowed: raise ValueError('Неизвестное поле задачи')
    if 'spec' in fields:fields['spec']=json.dumps(fields['spec'],ensure_ascii=False)
    fields['updated_at']=now()
    with connect() as db:
        db.execute('UPDATE tasks SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',(*fields.values(),task_id))


def create_pending(operation,args):
    with connect() as db:
        cur=db.execute('INSERT INTO pending(operation,args,created_at) VALUES (?,?,?)',
                       (operation,json.dumps(args,ensure_ascii=False),now()))
        return cur.lastrowid


def pending_latest():
    with connect() as db:
        row=db.execute("SELECT * FROM pending WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
    if not row:return None
    item=dict(row);item['args']=json.loads(item['args']);return item


def resolve_pending(pending_id,status):
    with connect() as db:
        db.execute("UPDATE pending SET status=? WHERE id=? AND status='pending'",(status,pending_id))


def clear_records():
    with connect() as db:
        db.execute('DELETE FROM analyses')
        db.execute('DELETE FROM task_vacancies')
        db.execute('DELETE FROM vacancy_cards')
        db.execute('DELETE FROM vacancies')
    # Keep the legacy collector JSON from resurrecting deleted data on next startup.
    for filename in ('vacancies.json','analyses.json'):
        (ROOT/filename).unlink(missing_ok=True)
    return True


def delete_vacancy(vacancy_id):
    with connect() as db:
        db.execute('DELETE FROM analyses WHERE vacancy_id=?',(vacancy_id,))
        db.execute('DELETE FROM vacancy_cards WHERE vacancy_id=?',(vacancy_id,))
        db.execute('DELETE FROM task_vacancies WHERE vacancy_id=?',(vacancy_id,))
        return db.execute('DELETE FROM vacancies WHERE id=?',(vacancy_id,)).rowcount


def store_resume(filename,content):
    with connect() as db:
        cur=db.execute('INSERT INTO resumes(filename,content,created_at) VALUES (?,?,?)',(filename,content,now()))
        return cur.lastrowid


def get_resume(resume_id):
    with connect() as db:
        row=db.execute('SELECT * FROM resumes WHERE id=?',(resume_id,)).fetchone()
    return dict(row) if row else None


def latest_resume_meta():
    """Metadata only: the model reads resume text via import_resume tool."""
    with connect() as db:
        row=db.execute('SELECT id,filename,created_at FROM resumes ORDER BY id DESC LIMIT 1').fetchone()
    return dict(row) if row else None


# Universal vacancy cards are distinct from user-specific assessments.
def save_card(vacancy_id, card):
    if not isinstance(card, dict) or not isinstance(card.get('profession'),str):
        raise ValueError('Некорректная структурированная карточка')
    words=[]
    for key in ('profession','specialization','skills','certificates','conditions','required','preferred','summary'):
        value=card.get(key,[])
        words.extend(str(x) for x in value) if isinstance(value,list) else words.append(str(value or ''))
    search_text=' '.join(words).casefold()[:12000]
    with connect() as db:
        db.execute('INSERT INTO vacancy_cards(vacancy_id,data,search_text,updated_at) VALUES(?,?,?,?) '
                   'ON CONFLICT(vacancy_id) DO UPDATE SET data=excluded.data,search_text=excluded.search_text,updated_at=excluded.updated_at',
                   (str(vacancy_id),json.dumps(card,ensure_ascii=False),search_text,now()))


def get_card(vacancy_id):
    with connect() as db:
        row=db.execute('SELECT data FROM vacancy_cards WHERE vacancy_id=?',(str(vacancy_id),)).fetchone()
    return json.loads(row['data']) if row else None


def existing_ids(ids):
    ids=list(dict.fromkeys(str(x) for x in ids if x is not None))
    if not ids:return set()
    with connect() as db:
        result=set()
        for i in range(0,len(ids),400):
            chunk=ids[i:i+400]
            rows=db.execute('SELECT id FROM vacancies WHERE id IN ('+','.join('?'*len(chunk))+')',chunk).fetchall()
            result.update(row['id'] for row in rows)
        return result


def find_vacancies(queries,limit=100):
    """Cheap local retrieval; score matches against cards/title, never send DB to AI."""
    import re
    queries=[str(q) for q in queries if isinstance(q,str)][:8]
    terms=[]
    stop={'вакансии','вакансия','работа','работу','найди','ищи','меня','для','или','на','по','из','и','с','со','в','от','до','у','где','хочу','анализа'}
    for q in queries:
        tokens=[x for x in re.findall(r'[\w+#.-]{2,}',q.casefold()) if x not in stop]
        terms.extend(tokens)
    terms=list(dict.fromkeys(terms))[:16]
    if not terms:return []
    # Use indexed candidate selection where available later; current limited local LIKE search is safe for MVP.
    predicates=[]; args=[]
    for term in terms:
        predicates.append('(lower(json_extract(v.data,"$.title")) LIKE ? OR c.search_text LIKE ? OR (c.vacancy_id IS NULL AND lower(json_extract(v.data,"$.description")) LIKE ?))')
        args.extend(('%'+term+'%','%'+term+'%','%'+term+'%'))
    with connect() as db:
        rows=db.execute('SELECT v.data,c.data AS card,a.data AS analysis FROM vacancies v '
                        'LEFT JOIN vacancy_cards c ON c.vacancy_id=v.id '
                        'LEFT JOIN analyses a ON a.vacancy_id=v.id '
                        'WHERE '+' OR '.join(predicates)+' ORDER BY v.updated_at DESC LIMIT ?',
                        (*args,max(1,min(limit*5,1000)))).fetchall()
    scored=[]
    for row in rows:
        vacancy=json.loads(row['data']);card=json.loads(row['card']) if row['card'] else None
        hay=(' '.join(str(vacancy.get(k,'')) for k in ('title','area','work_format'))+' '+
             (' '.join(str(x) for x in card.values()) if card else str(vacancy.get('description','')))).casefold()
        score=sum(2 if t in str(vacancy.get('title','')).casefold() else 1 for t in terms if t in hay)
        if score:scored.append((score,vacancy,card,json.loads(row['analysis']) if row['analysis'] else None))
    scored.sort(key=lambda x:x[0],reverse=True)
    return [dict(vacancy=v,card=c,analysis=a,score=score) for score,v,c,a in scored[:limit]]


def attach_to_task(task_id,vacancy_id,origin):
    with connect() as db:
        cur=db.execute('INSERT OR IGNORE INTO task_vacancies(task_id,vacancy_id,origin) VALUES (?,?,?)',
                       (task_id,str(vacancy_id),origin))
        return bool(cur.rowcount)


def task_results(task_id):
    with connect() as db:
        rows=db.execute('SELECT t.origin,v.data AS vacancy,a.data AS analysis,c.data AS card '
                        'FROM task_vacancies t JOIN vacancies v ON v.id=t.vacancy_id '
                        'LEFT JOIN analyses a ON a.vacancy_id=t.vacancy_id '
                        'LEFT JOIN vacancy_cards c ON c.vacancy_id=t.vacancy_id '
                        'WHERE t.task_id=? ORDER BY t.rowid',(task_id,)).fetchall()
    return [dict(origin=r['origin'],vacancy=json.loads(r['vacancy']),
                 analysis=json.loads(r['analysis']) if r['analysis'] else None,
                 card=json.loads(r['card']) if r['card'] else None) for r in rows]


def get_analysis(vacancy_id):
    with connect() as db:
        row=db.execute('SELECT data FROM analyses WHERE vacancy_id=?',(str(vacancy_id),)).fetchone()
    return json.loads(row['data']) if row else None
