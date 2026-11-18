"""Synchronous task pipeline; event yields do not require a worker or queue."""

import re
import sqlite3

from jobradar.analysis.modules import SearchPlanner, StructuredLLM, TagResolver, VacancyAnalyzer
from jobradar.analysis.errors import AnalysisError
from jobradar.diagnostics import record
from jobradar.llm.client import LLMError
from jobradar.profiles import ProfileError
from jobradar.sources.base import SourceError, SourceEvent
from jobradar.storage import search as tasks, vacancies
from .budget import SearchStop, TaskBudget
from .filters import hard_mismatch
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

    def search(self, manager, arguments, state):
        budget = self.budget_factory(self.settings)
        request = arguments.get("request")
        goal = request or "Поиск: " + state["profile"]["profile_data"]["profession"]
        target = self.settings.values["search"].get("target_count",20)
        with self.database.transaction() as db:
            manager._active(db)
            task_id = tasks.create_task(db,state,goal,target)
        trace = TaskTrace(self.database,task_id)
        structured = StructuredLLM(self.client,self.settings,budget,observer=trace.issue)
        planner, analyzer, resolver = SearchPlanner(structured), VacancyAnalyzer(structured), TagResolver(self.database,structured)
        processed, new_analyses, collected = set(), 0, 0
        attempted = set()
        record("search","task_started",task_id=task_id,context_id=state["active_context_id"])
        reasons = []
        iterator = None
        try:
            yield {"event":"search_started","data":{"task_id":task_id,"message":"Поиск запущен"}}
            output = planner.plan(state,request,self.sources)
            plan = output["search_queries"]
            target = output["target_count"] or target
            with self.database.transaction() as db:
                tasks.save_plan(db,task_id,plan)
                db.execute("UPDATE search_tasks SET target_count=? WHERE id=?",(target,task_id))
            terms = list(dict.fromkeys(word for query in plan["queries"] for word in re.findall(r"[\w+#.-]{3,}",query["text"])))
            with self.database.connection() as db:
                # Reserve one slot for the mandatory source check; the shared cap
                # includes cached candidates and repeated source discoveries.
                local = vacancies.local_candidates(db,terms,max(0,self.settings.values["search"]["max_source_vacancies"]-1))

            def process(vacancy_id):
                nonlocal new_analyses
                budget.check(); self._valid_state(manager,state)
                with self.database.connection() as db:
                    raw = vacancies.get_raw(db,vacancy_id)
                    card = vacancies.get_card(db,vacancy_id)
                    assessment = vacancies.get_assessment(db,state["active_context_id"],vacancy_id)
                    tags = vacancies.get_tags(db,vacancy_id)
                key = (vacancy_id,raw["content_hash"],analyzer.VERSION)
                if key in attempted:
                    trace.count("duplicate_skipped")
                    return
                current = bool(card and card["source_hash"] == raw["content_hash"] and card["extraction_version"] == analyzer.VERSION)
                # A one-off request narrows retrieval, not the saved criteria or
                # the meaning of the context's persistent personal assessment.
                reusable = tasks.assessment_current(raw,card,assessment,state)
                if current:
                    attempted.add(key)
                    if not reusable:
                        trace.count("reassessments")
                        assessment = analyzer.assess(card,tags,state)
                    else:
                        trace.count("cache_hits")
                    mismatch = hard_mismatch(card,state["context"]["criteria"])
                    if mismatch:
                        assessment = {"decision":"Не соответствует обязательным условиям","reasoning":mismatch,"relevance_score":None}
                    if not reusable or mismatch:
                        with self.database.transaction() as db:
                            manager._active(db)
                            vacancies.save_assessment(db,vacancy_id,assessment,state)
                else:
                    if new_analyses >= self.settings.values["search"]["max_new_analyses"]:
                        raise SearchStop("ANALYSIS_LIMIT","Достигнут предел новых анализов вакансий.")
                    new_analyses += 1
                    attempted.add(key)
                    trace.count("analysis_attempts")
                    try:
                        result = analyzer.extract(raw,state)
                    except (LLMError,SearchStop):
                        trace.count("analysis_failed")
                        raise
                    tags, uncertain = resolver.resolve(result["tags"])
                    if uncertain:
                        result["card"]["structured_data"].setdefault("uncertainties",[]).extend(uncertain)
                    mismatch = hard_mismatch(result["card"],state["context"]["criteria"])
                    if mismatch:
                        result["assessment"] = {"decision":"Не соответствует обязательным условиям","reasoning":mismatch,"relevance_score":None}
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
                    issue = exc.diagnostic() if isinstance(exc,AnalysisError) else {"code":exc.code,"stage":"vacancy_analysis","message":str(exc),"severity":"error"}
                    trace.issue(issue)
                    reasons.append(exc.code + ": " + str(exc))
                except (ProfileError,sqlite3.Error):
                    trace.issue({"code":"DATA_ERROR","stage":"persistence","message":"Не удалось сохранить карточку или оценку вакансии.","severity":"error"})
                    raise
                except SearchStop as exc:
                    trace.issue({"code":exc.code,"stage":"vacancy_analysis","message":str(exc),"severity":"error"})
                    raise
                finally:
                    trace.context = {}
                    trace.save()

            def progress():
                return {"event":"search_progress","data":{"task_id":task_id,"evaluated_count":len(processed),"collected_count":collected}}

            for raw in local:
                budget.check()
                collected += 1
                with self.database.transaction() as db:
                    tasks.attach(db,task_id,raw["id"],"database")
                process_safely(raw["id"])
                yield progress()
                if len(processed) >= target:
                    break
            # Always try the source at least once, even with a sufficient local cache.
            for query in plan["queries"]:
                budget.check(); self._valid_state(manager,state)
                remaining = self.settings.values["search"]["max_source_vacancies"] - collected
                if remaining <= 0:
                    reasons.append("SOURCE_LIMIT: достигнут предел сбора")
                    break
                source = self.sources[query["source"]]
                iterator = iter(source.search(query,limit=remaining,deadline=budget.deadline))
                try:
                    for raw in iterator:
                        budget.check(); self._valid_state(manager,state)
                        if isinstance(raw,SourceEvent):
                            issue = {"code":raw.code,"stage":raw.stage,"message":raw.message,
                                     "external_id":raw.external_id,"url":raw.url,
                                     "severity":"error" if raw.kind == "source_warning" else "info"}
                            trace.issue(issue)
                            if raw.kind == "source_warning":
                                trace.count("source_skipped")
                                collected += 1
                                reasons.append(raw.code + ": " + raw.message)
                            yield {"event":raw.kind,"data":{"task_id":task_id,**issue}}
                            if collected >= self.settings.values["search"]["max_source_vacancies"]:
                                reasons.append("SOURCE_LIMIT: достигнут предел сбора")
                                break
                            continue
                        if collected >= self.settings.values["search"]["max_source_vacancies"]:
                            reasons.append("SOURCE_LIMIT: достигнут предел сбора")
                            break
                        collected += 1
                        if raw.source != query["source"] or not raw.external_id or not raw.description:
                            reasons.append("SOURCE_DATA_ERROR: неполное объявление")
                            trace.count("source_skipped")
                            trace.issue({"code":"SOURCE_DATA_ERROR","stage":"source_extraction","message":"Источник вернул неполное объявление.","severity":"error"})
                            continue
                        with self.database.transaction() as db:
                            vacancy_id = vacancies.upsert_raw(db,raw)
                            tasks.attach(db,task_id,vacancy_id,"source")
                        # The same vacancy may have changed since a local-cache pass.
                        with self.database.connection() as db:
                            stored_raw = vacancies.get_raw(db,vacancy_id)
                            card = vacancies.get_card(db,vacancy_id)
                        changed = not card or card["source_hash"] != stored_raw["content_hash"]
                        if (vacancy_id not in processed and len(processed) < target) or (vacancy_id in processed and changed):
                            processed.discard(vacancy_id)
                            process_safely(vacancy_id)
                        yield progress()
                        if len(processed) >= target:
                            break
                finally:
                    close = getattr(iterator,"close",None)
                    if close:
                        close()
                    iterator = None
                if len(processed) >= target:
                    break
                if collected >= self.settings.values["search"]["max_source_vacancies"]:
                    reasons.append("SOURCE_LIMIT: достигнут предел сбора")
                    break
            if len(processed) < target:
                budget.check()
                reasons.append("RESULTS_EXHAUSTED: цель по количеству не достигнута")
        except (SearchStop,SourceError) as exc:
            reasons.append(exc.code + ": " + str(exc))
            trace.issue({"code":exc.code,"stage":"search","message":str(exc),"severity":"error"})
        except LLMError as exc:
            reasons.append(exc.code + ": " + str(exc))
            trace.issue(exc.diagnostic() if isinstance(exc,AnalysisError) else {"code":exc.code,"stage":"search","message":str(exc),"severity":"error"})
        except (ProfileError,sqlite3.Error):
            reasons.append("DATA_ERROR: данные изменились или не удалось их сохранить")
            trace.issue({"code":"DATA_ERROR","stage":"persistence","message":"Данные изменились или не удалось сохранить результат.","severity":"error"})
        except GeneratorExit:
            with self.database.transaction() as db:
                tasks.finish(db,task_id,partial=bool(processed),failed=not processed,reason="INTERRUPTED: соединение закрыто")
            trace.issue({"code":"INTERRUPTED","stage":"search","message":"Соединение закрыто до завершения поиска.","severity":"error"})
            raise
        finally:
            if iterator is not None and getattr(iterator,"close",None):
                iterator.close()
        reason = "; ".join(dict.fromkeys(reasons)) or None
        failed = bool(not processed and reason and not all(item.startswith("RESULTS_EXHAUSTED") for item in reasons))
        if not processed and not failed:
            reason = None  # A successful empty source response is not a service failure.
        with self.database.transaction() as db:
            tasks.finish(db,task_id,partial=bool(reason and processed),reason=reason,failed=failed)
            report = tasks.results(db,state,task_id,self.settings.values["results"]["default_limit"])
        trace.data["metrics"]["analysis_failed"] = trace.data["metrics"]["analysis_attempts"] - trace.data["metrics"]["analysis_succeeded"]
        trace.save()
        attach_diagnostics(report,trace.data)
        report["summary"] = self.render_report(report)
        record("search","task_finished",task_id=task_id,state=report["status"],partial=report["is_partial"],
               found=report["counts"]["found_count"],evaluated=len(processed),llm_calls=budget.calls,
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
        if report["stop_reason"]:
            lines.append("Причина: " + "; ".join(part.partition(": ")[2] or part for part in report["stop_reason"].split("; ")))
        if report.get("filtered_count"):
            lines.append(f"Не прошли обязательные условия: {report['filtered_count']}.")
        if report.get("unassessed_count"):
            lines.append(f"Без актуальной оценки: {report['unassessed_count']}. Причины и список доступны в разделе «Диагностика последнего поиска».")
        trace = report.get("diagnostics")
        if trace and trace["metrics"]["source_skipped"]:
            lines.append(f"Не удалось прочитать на источнике: {trace['metrics']['source_skipped']}.")
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
            result = tasks.results(db,state,task_id,self.settings.values["results"]["max_limit"])
        return attach_diagnostics(result,TaskTrace.load(self.database,result["task_id"]))

    def _refresh_assessments(self, manager, state, ids, trace=None):
        budget = self.budget_factory(self.settings)
        analyzer = VacancyAnalyzer(StructuredLLM(self.client,self.settings,budget,observer=trace.issue if trace else None))
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
                if not card or card["source_hash"] != raw["content_hash"] or card["extraction_version"] != analyzer.VERSION:
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
        trace = TaskTrace.existing(self.database,task["id"])
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
        trace = TaskTrace.existing(self.database,task["id"]) if task else None
        warnings = yield from self._refresh_assessments(manager,state,[arguments["vacancy_id"]],trace)
        with self.database.transaction() as db:
            result = tasks.details(db,state,arguments["vacancy_id"])
        result["warnings"] = warnings
        return result
