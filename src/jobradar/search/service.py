"""Synchronous task pipeline; event yields do not require a worker or queue."""

import re
import json
import sqlite3

from jobradar.analysis.modules import SearchPlanner, StructuredLLM, TagResolver, VacancyAnalyzer
from jobradar.analysis.errors import AnalysisError
from jobradar.diagnostics import record
from jobradar.llm.client import LLMError
from jobradar.profiles import ProfileError
from jobradar.sources.base import SourceError, SourceEvent
from jobradar.storage import search as tasks, vacancies
from .budget import SearchStop, TaskBudget
from .filters import hard_mismatch, preliminary_mismatch, candidate_priority
from .trace import TaskTrace, attach_diagnostics

class SearchService:
    def __init__(self, database, settings, client, sources, *, budget_factory=TaskBudget):
        self.database, self.settings, self.client, self.sources = database, settings, client, sources
        self.budget_factory = budget_factory

    def register(self, registry):
        registry.register("search_vacancies",self.search)
        registry.register("get_search_results",self.get_results)
        registry.register("get_vacancy_details",self.get_details)
        registry.diagnostics_reader = self.diagnostics

    def _valid_state(self, manager, state):
        if manager.get_state() != state:
            raise SearchStop("CONTEXT_CHANGED","Профиль или критерии изменились во время поиска.")

    # Bounded in-memory portion, not a persistent queue or a new business limit.
    BATCH_SIZE = 10

    def search(self, manager, arguments, state):
        budget = self.budget_factory(self.settings)
        request = arguments.get("request")
        goal = request or "Поиск: " + state["profile"]["profile_data"]["profession"]
        limits = self.settings.values["search"]
        target = limits.get("target_count",20)
        with self.database.transaction() as db:
            manager._active(db)
            task_id = tasks.create_task(db,state,goal,target)
        trace = TaskTrace(self.database,task_id,self.settings)
        trace.prune()
        structured = StructuredLLM(self.client,self.settings,budget,observer=trace.issue,recorder=trace)
        planner, analyzer, resolver = SearchPlanner(structured), VacancyAnalyzer(structured), TagResolver(self.database,structured)
        processed, attempted, filtered, deferred = set(), set(), set(), set()
        discovered, adapter_seen, read_keys = set(), set(), set()
        coverage = {"planned_queries":0,"completed_queries":0,"source_complete":False,
                    "hh_limit_reached":False,"stages_completed":[]}
        trace.data["coverage"] = coverage
        reasons, iterator, clarification = [], None, None
        last_snapshot = None
        last_visible_ids = set()
        record("search","task_started",task_id=task_id,context_id=state["active_context_id"])

        def issue(code, message, stage, severity="info"):
            trace.issue({"code":code,"stage":stage,"message":message,"severity":severity})

        def defer(vacancy_id, code, message):
            deferred.add(vacancy_id)
            trace.data["metrics"]["deferred"] = len(deferred)
            reasons.append(code + ": " + message)
            issue(code,message,"deferred")

        def reject(vacancy_id, message):
            filtered.add(vacancy_id)
            trace.data["metrics"]["filtered"] = len(filtered)
            issue("PRE_FILTERED",message,"pre_filter")

        def process(vacancy_id):
            budget.check(); self._valid_state(manager,state)
            with self.database.connection() as db:
                raw = vacancies.get_raw(db,vacancy_id)
                card = vacancies.get_card(db,vacancy_id)
                assessment = vacancies.get_assessment(db,state["active_context_id"],vacancy_id)
                tags = vacancies.get_tags(db,vacancy_id)
            key = (vacancy_id,raw["content_hash"],analyzer.VERSION,raw["status"])
            if key in attempted:
                trace.count("duplicate_skipped")
                return
            attempted.add(key)
            processed.discard(vacancy_id); filtered.discard(vacancy_id); deferred.discard(vacancy_id)
            current = vacancies.card_current(raw,card,analyzer.VERSION)
            mismatch = preliminary_mismatch(raw,state["context"]["criteria"],card if current else None)
            if mismatch:
                reject(vacancy_id,mismatch)
                return
            reusable = tasks.assessment_current(raw,card,assessment,state)
            if current and reusable:
                trace.count("cache_hits")
            else:
                if not raw.get("description") and not current:
                    defer(vacancy_id,"SOURCE_INCOMPLETE","Для извлечения карточки нужен полный исходный текст.")
                    return
                if budget.calls >= budget.max_calls:
                    defer(vacancy_id,"LLM_LIMIT","Достигнут предел обращений к модели; сбор исходников продолжается.")
                    return
                if current:
                    trace.count("reassessments")
                    assessment = analyzer.assess(card,tags,state)
                    self._valid_state(manager,state)
                    with self.database.transaction() as db:
                        manager._active(db)
                        vacancies.save_assessment(db,vacancy_id,assessment,state)
                else:
                    if trace.data["metrics"]["analysis_attempts"] >= limits["max_new_analyses"]:
                        defer(vacancy_id,"ANALYSIS_LIMIT","Достигнут предел новых анализов; сбор исходников продолжается.")
                        return
                    trace.count("analysis_attempts")
                    result = analyzer.extract(raw,state)
                    tags, uncertain = resolver.resolve(result["tags"])
                    if uncertain:
                        result["card"]["structured_data"].setdefault("uncertainties",[]).extend(uncertain)
                    mismatch = hard_mismatch(result["card"],state["context"]["criteria"])
                    if mismatch:
                        result["assessment"] = {"decision":"Не соответствует обязательным условиям","reasoning":mismatch,"relevance_score":None}
                        reject(vacancy_id,mismatch)
                    self._valid_state(manager,state)
                    with self.database.transaction() as db:
                        manager._active(db)
                        vacancies.save_card_bundle(db,raw,result["card"],tags,result["assessment"],state,analyzer.VERSION)
                    trace.count("analysis_succeeded")
            processed.add(vacancy_id)

        def process_safely(vacancy_id):
            trace.context = {"vacancy_id":vacancy_id}
            try:
                process(vacancy_id)
            except LLMError as exc:
                trace.issue(exc.diagnostic() if isinstance(exc,AnalysisError) else
                            {"code":exc.code,"stage":"vacancy_analysis","message":str(exc),"severity":"error"})
                reasons.append(exc.code + ": " + str(exc))
            except SearchStop as exc:
                if exc.code != "LLM_LIMIT":
                    raise
                defer(vacancy_id,exc.code,str(exc))
            except (ProfileError,sqlite3.Error):
                issue("DATA_ERROR","Не удалось сохранить карточку или оценку.","persistence","error")
                raise
            finally:
                trace.context = {}
                metrics = trace.data["metrics"]
                metrics["analysis_failed"] = metrics["analysis_attempts"] - metrics["analysis_succeeded"]
                metrics["filtered"], metrics["deferred"] = len(filtered),len(deferred)
                trace.save()

        def progress():
            return {"event":"search_progress","data":{"task_id":task_id,"evaluated_count":len(processed),
                     "collected_count":len(discovered),"saved_count":trace.data["metrics"]["hh_saved"]}}

        def snapshot():
            nonlocal last_snapshot, last_visible_ids
            with self.database.connection() as db:
                result = tasks.results(db,state,task_id,self.settings.values["results"]["default_limit"])
            trace.data["metrics"]["visible_results"] = result["result_count"]
            signature = vacancies.encode(result["vacancies"])
            if signature != last_snapshot and (result["vacancies"] or last_snapshot is not None):
                last_snapshot = signature
                last_visible_ids = {item["vacancy_id"] for item in result["vacancies"]}
                yield {"event":"search_results","data":{"task_id":task_id,"vacancies":result["vacancies"],
                        "result_count":result["result_count"],"replace":True}}

        def process_batch(ids):
            # Save the whole available portion first; route existing cards before
            # raw candidates within a newly collected portion as well.
            with self.database.connection() as db:
                rows = [(vid,vacancies.get_raw(db,vid),vacancies.get_card(db,vid)) for vid in dict.fromkeys(ids)]
            rows.sort(key=lambda item:(not vacancies.card_current(item[1],item[2],analyzer.VERSION),
                                       -candidate_priority(item[1],terms)))
            for vid,raw,card in rows:
                process_safely(vid)
                yield progress()
                yield from snapshot()

        def discover(source, external_id):
            key = (source,str(external_id))
            if key in discovered:
                return True
            if len(discovered) >= limits["max_source_vacancies"]:
                return False
            discovered.add(key); adapter_seen.add(key)
            trace.data["metrics"]["hh_discovered"] = len(discovered)
            return True

        try:
            yield {"event":"search_started","data":{"task_id":task_id,"message":"Поиск запущен"}}
            output = planner.plan(state,request,self.sources)
            plan = output["search_queries"]
            trace.data["search_plan"] = plan
            coverage["planned_queries"] = len(plan["queries"])
            target = output["target_count"] or target
            with self.database.transaction() as db:
                tasks.save_plan(db,task_id,plan)
                db.execute("UPDATE search_tasks SET target_count=? WHERE id=?",(target,task_id))
            terms = list(dict.fromkeys(word for query in plan["queries"] for word in re.findall(r"[\w+#.-]{3,}",query["text"])))
            for current,stage in ((True,"cards"),(False,"raw")):
                budget.check(); self._valid_state(manager,state)
                with self.database.connection() as db:
                    ids = vacancies.local_candidate_ids(db,terms,current=current,version=analyzer.VERSION)
                trace.count("local_candidates",len(ids))
                for offset in range(0,len(ids),self.BATCH_SIZE):
                    batch = ids[offset:offset+self.BATCH_SIZE]
                    with self.database.transaction() as db:
                        manager._active(db)
                        for vid in batch:
                            tasks.attach(db,task_id,vid,"database")
                    yield from process_batch(batch)
                coverage["stages_completed"].append(stage)
            for query in plan["queries"]:
                budget.check(); self._valid_state(manager,state)
                remaining = limits["max_source_vacancies"] - len(discovered)
                if remaining <= 0:
                    break
                source, pending, source_error = self.sources[query["source"]], [], None
                iterator = iter(source.search(query,limit=remaining,deadline=budget.deadline,seen=adapter_seen))
                try:
                    for item in iterator:
                        self._valid_state(manager,state)
                        if isinstance(item,SourceEvent):
                            if item.kind == "source_discovered":
                                if not discover(query["source"],item.external_id):
                                    break
                                trace.save()
                                budget.check()
                                continue
                            budget.check()
                            if item.kind == "source_duplicate":
                                trace.count("hh_duplicates")
                                continue
                            if item.kind == "source_warning":
                                if item.external_id and not discover(query["source"],item.external_id):
                                    break
                                trace.count("source_skipped")
                                reasons.append(item.code + ": " + item.message)
                                if item.code == "VACANCY_CLOSED" and item.external_id:
                                    with self.database.transaction() as db:
                                        vacancies.mark_closed(db,query["source"],item.external_id)
                                    yield from snapshot()
                            trace.issue({"code":item.code,"stage":item.stage,"message":item.message,
                                         "external_id":item.external_id,"url":item.url,
                                         "severity":"error" if item.kind == "source_warning" else "info"})
                            yield {"event":item.kind,"data":{"task_id":task_id,"message":item.message}}
                            continue
                        if item.source != query["source"] or not item.external_id:
                            trace.count("source_skipped")
                            issue("SOURCE_DATA_ERROR","Источник вернул некорректный идентификатор.","source_extraction","error")
                            reasons.append("SOURCE_DATA_ERROR: некорректное объявление")
                            continue
                        key = (item.source,str(item.external_id))
                        if not discover(*key):
                            break
                        if key in read_keys:
                            trace.count("hh_duplicates")
                            continue
                        read_keys.add(key)
                        trace.count("hh_read" if item.description else "hh_incomplete")
                        with self.database.transaction() as db:
                            manager._active(db)
                            vid = vacancies.upsert_raw(db,item)
                            tasks.attach(db,task_id,vid,"source")
                            if not vacancies.card_current(vacancies.get_raw(db,vid),vacancies.get_card(db,vid),analyzer.VERSION):
                                processed.discard(vid)
                        trace.count("hh_saved")
                        pending.append(vid)
                        yield progress()
                        if vid in last_visible_ids:
                            yield from snapshot()
                        # Preserve an already received source even if its read
                        # consumed the last time slice. Do not start another read
                        # or paid analysis after the deadline.
                        budget.check()
                        if len(pending) >= self.BATCH_SIZE:
                            batch,pending = pending,[]
                            yield from process_batch(batch)
                except (SourceError,SearchStop) as exc:
                    source_error = exc
                finally:
                    close = getattr(iterator,"close",None)
                    if close:
                        close()
                    iterator = None
                yield from process_batch(pending)
                yield from snapshot()
                if source_error:
                    raise source_error
                budget.check()
                coverage["completed_queries"] += 1
            coverage["hh_limit_reached"] = len(discovered) >= limits["max_source_vacancies"]
            coverage["source_complete"] = coverage["completed_queries"] == coverage["planned_queries"] and not coverage["hh_limit_reached"]
            coverage["stages_completed"].append("source")
            if coverage["hh_limit_reached"]:
                reasons.append("SOURCE_LIMIT: достигнут предел уникальных обнаружений HH")
            with self.database.connection() as db:
                visible = tasks.results(db,state,task_id,self.settings.values["results"]["default_limit"])["result_count"]
            if visible < target:
                reasons.append("RESULTS_EXHAUSTED: цель по количеству не достигнута")
        except (SearchStop,SourceError) as exc:
            if exc.code == "LOCATION_CLARIFICATION":
                clarification = str(exc)
            elif exc.code == "AREA_UNSUPPORTED":
                clarification = "Уточните конкретные страны или города поиска: один из регионов не удалось выбрать на HH.ru."
            reasons.append(exc.code + ": " + str(exc))
            issue(exc.code,str(exc),"search","error")
        except LLMError as exc:
            reasons.append(exc.code + ": " + str(exc))
            trace.issue(exc.diagnostic() if isinstance(exc,AnalysisError) else
                        {"code":exc.code,"stage":"search","message":str(exc),"severity":"error"})
        except (ProfileError,sqlite3.Error):
            reasons.append("DATA_ERROR: данные изменились или не удалось их сохранить")
            issue("DATA_ERROR","Данные изменились или не удалось сохранить результат.","persistence","error")
        except GeneratorExit:
            with self.database.transaction() as db:
                tasks.finish(db,task_id,partial=bool(processed),failed=not processed,reason="INTERRUPTED: соединение закрыто")
            issue("INTERRUPTED","Соединение закрыто до завершения поиска.","search","error")
            raise
        finally:
            if iterator is not None and getattr(iterator,"close",None):
                iterator.close()
        reason = "; ".join(dict.fromkeys(reasons)) or None
        failed = bool(not processed and reason and not all(item.startswith("RESULTS_EXHAUSTED") for item in reasons))
        if not processed and not failed:
            reason = None
        with self.database.transaction() as db:
            tasks.finish(db,task_id,partial=bool(reason and processed),reason=reason,failed=failed)
            report = tasks.results(db,state,task_id,self.settings.values["results"]["default_limit"])
        trace.data["metrics"]["analysis_failed"] = trace.data["metrics"]["analysis_attempts"] - trace.data["metrics"]["analysis_succeeded"]
        trace.data["metrics"]["visible_results"] = report["result_count"]
        trace.save()
        attach_diagnostics(report,trace.data)
        report.update(clarification=clarification,coverage=coverage)
        report["hh_counts"] = {key:trace.data["metrics"][key] for key in ("hh_discovered","hh_duplicates","hh_read","hh_saved","hh_incomplete")}
        report["summary"] = self.render_report(report)
        record("search","task_finished",task_id=task_id,state=report["status"],partial=report["is_partial"],
               found=report["counts"]["found_count"],evaluated=report["counts"]["evaluated_count"],llm_calls=budget.calls,
               stop_codes=list(dict.fromkeys(item.split(":",1)[0] for item in reasons)))
        yield {"event":"search_finished","data":{"task_id":task_id,"status":report["status"],"is_partial":report["is_partial"],"message":report["summary"].split("\n")[0]}}
        return report

    @staticmethod
    def render_report(report):
        counts = report["counts"]
        header = f"Поиск #{report['task_id']}: найдено {counts['found_count']}, оценено {counts['evaluated_count']}."
        header += f" Цель: {report.get('target_count', 'не задана')}."
        if report["is_partial"]:
            header += " Выполнен частично."
        if report["status"] == "failed":
            header += " Поиск завершился с ошибкой."
        lines = [header]
        if "hh_counts" in report:
            lines.append(f"В этом поиске с HH.ru прочитано объявлений: {report['hh_counts']['hh_read']}.")
            if not report["coverage"]["source_complete"]:
                lines.append("Источник просмотрен не полностью.")
        if report.get("clarification"):
            lines.append(report["clarification"])
        if report.get("unassessed_count") or report["status"] == "failed":
            lines.append("Подробности доступны в отдельной панели диагностики.")
        for index,item in enumerate(report["vacancies"],1):
            assessment = item["assessment"] or {}
            decision = assessment.get('decision','Не оценена')
            if assessment and not item["assessment_is_current"]:
                decision = "Сохранённая оценка устарела"
            lines.append(f"{index}. {item['title']} — {decision}")
            if assessment.get("reasoning"):
                lines.append(assessment["reasoning"][:500])
            lines.append(item["url"] or "Ссылка не указана")
        if report["vacancies"]:
            lines.append("Актуальность объявлений на сайте повторно не проверялась.")
        return "\n".join(lines)

    def diagnostics(self, manager, state, task_id=None):
        with self.database.transaction() as db:
            manager._active(db)
            current = tasks.task(db,state["active_context_id"],task_id)
            result = tasks.results(db,state,current["id"],self.settings.values["results"]["max_limit"])
            result["search_plan"] = json.loads(current["search_queries"]) if current["search_queries"] else None
            result["created_at"] = current["created_at"]
            result["completed_at"] = current["completed_at"]
        return attach_diagnostics(result,TaskTrace.load(self.database,result["task_id"]))

    def _refresh_assessments(self, manager, state, ids, trace=None):
        budget = self.budget_factory(self.settings)
        analyzer = VacancyAnalyzer(StructuredLLM(self.client,self.settings,budget,observer=trace.issue if trace else None,recorder=trace))
        warnings = []
        for index,vacancy_id in enumerate(ids):
            if trace:
                trace.context = {"vacancy_id":vacancy_id}
            try:
                budget.check(); self._valid_state(manager,state)
                with self.database.connection() as db:
                    raw = vacancies.get_raw(db,vacancy_id)
                    card = vacancies.get_card(db,vacancy_id)
                    old = vacancies.get_assessment(db,state["active_context_id"],vacancy_id)
                    tags = vacancies.get_tags(db,vacancy_id)
                if not vacancies.card_current(raw,card,analyzer.VERSION) or preliminary_mismatch(raw,state["context"]["criteria"],card):
                    continue
                if not tasks.assessment_current(raw,card,old,state):
                    if trace:
                        trace.count("reassessments")
                    assessment = analyzer.assess(card,tags,state)
                    mismatch = hard_mismatch(card,state["context"]["criteria"])
                    if mismatch:
                        assessment = {"decision":"Не соответствует обязательным условиям","reasoning":mismatch,"relevance_score":None}
                    self._valid_state(manager,state)
                    with self.database.transaction() as db:
                        manager._active(db)
                        vacancies.save_assessment(db,vacancy_id,assessment,state)
                    yield {"event":"assessment_progress","data":{"message":"Обновляем оценки по текущим условиям","evaluated_count":index+1}}
            except (SearchStop,LLMError) as exc:
                warnings.append(f"Вакансия #{vacancy_id}: {exc.code}: {exc}")
                if trace:
                    trace.issue(exc.diagnostic() if isinstance(exc,AnalysisError) else {"code":exc.code,"stage":"assessment_only","message":str(exc),"severity":"error"})
                if isinstance(exc,SearchStop):
                    break
            finally:
                if trace:
                    trace.context = {}
                    trace.save()
        return warnings

    def get_results(self, manager, arguments, state):
        limit = min(arguments.get("limit",self.settings.values["results"]["default_limit"]),self.settings.values["results"]["max_limit"])
        with self.database.transaction() as db:
            manager._active(db)
            task = tasks.task(db,state["active_context_id"],arguments.get("task_id"))
            ids = [row[0] for row in db.execute("SELECT vacancy_id FROM task_vacancies WHERE task_id=? ORDER BY vacancy_id LIMIT ?",
                                               (task["id"],self.settings.values["search"]["max_source_vacancies"]))]
        trace = TaskTrace.existing(self.database,task["id"],self.settings)
        warnings = yield from self._refresh_assessments(manager,state,ids,trace)
        with self.database.transaction() as db:
            result = tasks.results(db,state,task["id"],limit)
        attach_diagnostics(result,TaskTrace.load(self.database,task["id"]))
        result["warnings"] = warnings
        return result

    def get_details(self, manager, arguments, state):
        with self.database.transaction() as db:
            manager._active(db)
            tasks.details(db,state,arguments["vacancy_id"])
            task = db.execute("SELECT st.id FROM search_tasks st JOIN task_vacancies tv ON tv.task_id=st.id WHERE st.context_id=? AND tv.vacancy_id=? ORDER BY st.id DESC LIMIT 1",
                              (state["active_context_id"],arguments["vacancy_id"])).fetchone()
        trace = TaskTrace.existing(self.database,task["id"],self.settings) if task else None
        warnings = yield from self._refresh_assessments(manager,state,[arguments["vacancy_id"]],trace)
        with self.database.transaction() as db:
            result = tasks.details(db,state,arguments["vacancy_id"])
        result["warnings"] = warnings
        return result
