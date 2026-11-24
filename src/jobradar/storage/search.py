"""Search tasks and context-scoped read models, using the approved schema."""

import json

from jobradar.profiles.contracts import ProfileError
from .profiles import now
from . import vacancies
from jobradar.search.filters import preliminary_mismatch


def create_task(db, state, goal, target_count):
    return db.execute("INSERT INTO search_tasks(context_id,goal,state,target_count) VALUES (?,?,'running',?)",
                      (state["active_context_id"],goal,target_count)).lastrowid


def save_plan(db, task_id, plan):
    db.execute("UPDATE search_tasks SET search_queries=? WHERE id=?",(vacancies.encode(plan),task_id))


def attach(db, task_id, vacancy_id, origin):
    db.execute("INSERT OR IGNORE INTO task_vacancies(task_id,vacancy_id,origin) VALUES (?,?,?)",(task_id,vacancy_id,origin))


def counts(db, task_id, state):
    found = db.execute("SELECT count(*) FROM task_vacancies WHERE task_id=?",(task_id,)).fetchone()[0]
    evaluated = db.execute("SELECT count(*) FROM task_vacancies tv JOIN context_vacancies cv ON cv.vacancy_id=tv.vacancy_id "
                           "JOIN vacancy_cards c ON c.vacancy_id=tv.vacancy_id JOIN vacancies v ON v.id=tv.vacancy_id "
                           "WHERE tv.task_id=? AND cv.context_id=? AND cv.profile_revision=? AND cv.criteria_revision=? "
                           "AND c.source_hash=v.content_hash AND c.extraction_version=1",
                           (task_id,state["active_context_id"],state["profile"]["revision"],state["context"]["criteria_revision"])).fetchone()[0]
    return {"found_count":found,"evaluated_count":evaluated}


def finish(db, task_id, *, partial=False, reason=None, failed=False):
    db.execute("UPDATE search_tasks SET state=?,is_partial=?,stop_reason=?,completed_at=? WHERE id=?",
               ("failed" if failed else "completed",int(partial and not failed),reason,now(),task_id))


def task(db, context_id, task_id=None):
    if task_id is None:
        row = db.execute("SELECT * FROM search_tasks WHERE context_id=? ORDER BY id DESC LIMIT 1",(context_id,)).fetchone()
    else:
        row = db.execute("SELECT * FROM search_tasks WHERE id=?",(task_id,)).fetchone()
    if row is None:
        raise ProfileError("TASK_NOT_FOUND","Поисковая задача не найдена.")
    if row["context_id"] != context_id:
        raise ProfileError("ACCESS_DENIED","Задача не относится к активному контексту.")
    return dict(row)


def assessment_current(vacancy, card, assessment, state):
    return bool(vacancies.card_current(vacancy,card) and assessment and assessment["profile_revision"] == state["profile"]["revision"]
                and assessment["criteria_revision"] == state["context"]["criteria_revision"])


def vacancy_view(db, vacancy_id, state, *, details=False):
    vacancy = vacancies.get_raw(db,vacancy_id)
    if not vacancy:
        raise ProfileError("VACANCY_NOT_FOUND","Вакансия не найдена.")
    card = vacancies.get_card(db,vacancy_id)
    assessment = vacancies.get_assessment(db,state["active_context_id"],vacancy_id)
    view = {"vacancy_id":vacancy_id,"title":vacancy["title"],"employer":vacancy["employer"],"url":vacancy["url"],
            "status":vacancy["status"],
            "summary":card.get("summary") if card else None,"assessment":assessment,
            "assessment_is_current":assessment_current(vacancy,card,assessment,state),
            "source_checked_at":vacancy["fetched_at"]}
    if details:
        view.update(description=vacancy["description"],card=card,tags=vacancies.get_tags(db,vacancy_id),source_text_untrusted=True)
    return view


def results(db, state, task_id=None, limit=20):
    current = task(db,state["active_context_id"],task_id)
    ids = [row[0] for row in db.execute(
        "SELECT tv.vacancy_id FROM task_vacancies tv LEFT JOIN context_vacancies cv "
        "ON cv.vacancy_id=tv.vacancy_id AND cv.context_id=? WHERE tv.task_id=? "
        "ORDER BY (cv.profile_revision=? AND cv.criteria_revision=?) DESC,cv.relevance_score DESC,tv.vacancy_id LIMIT ?",
        (state["active_context_id"],current["id"],state["profile"]["revision"],state["context"]["criteria_revision"],1000))]
    eligible = []
    for vid in ids:
        raw, card = vacancies.get_raw(db,vid), vacancies.get_card(db,vid)
        if not preliminary_mismatch(raw,state["context"]["criteria"],card if vacancies.card_current(raw,card) else None):
            eligible.append(vid)
    views = [vacancy_view(db,vid,state) for vid in eligible]
    evaluated = [view for view in views if view["assessment_is_current"]]
    visible = evaluated
    unassessed = [{"vacancy_id":view["vacancy_id"],"title":view["title"],"url":view["url"],
                   "state":"stale" if view["assessment"] else "missing"} for view in views if not view["assessment_is_current"]]
    return {"task_id":current["id"],"status":current["state"],"target_count":current["target_count"],"is_partial":bool(current["is_partial"]),
            "filtered_count":len(ids)-len(eligible),"result_count":len(visible),"unassessed_count":len(unassessed),"unassessed":unassessed,
            "stop_reason":current["stop_reason"],"vacancies":visible[:limit],
            "counts":counts(db,current["id"],state)}


def details(db, state, vacancy_id):
    accessible = db.execute("SELECT 1 FROM task_vacancies tv JOIN search_tasks st ON st.id=tv.task_id "
                            "WHERE tv.vacancy_id=? AND st.context_id=? UNION SELECT 1 FROM context_vacancies "
                            "WHERE vacancy_id=? AND context_id=? LIMIT 1",
                            (vacancy_id,state["active_context_id"],vacancy_id,state["active_context_id"])).fetchone()
    if not accessible:
        raise ProfileError("ACCESS_DENIED","Вакансия не относится к активному контексту.")
    return vacancy_view(db,vacancy_id,state,details=True)


def recover_interrupted(database):
    with database.transaction() as db:
        for row in db.execute("SELECT id FROM search_tasks WHERE state IN ('pending','running')").fetchall():
            saved = db.execute("SELECT count(*) FROM task_vacancies tv JOIN context_vacancies cv "
                               "ON cv.vacancy_id=tv.vacancy_id JOIN search_tasks st ON st.id=tv.task_id AND st.context_id=cv.context_id "
                               "WHERE tv.task_id=?",(row["id"],)).fetchone()[0]
            finish(db,row["id"],partial=bool(saved),reason="INTERRUPTED: приложение было остановлено",failed=not saved)
