"""Small local per-task diagnostic files, separate from the logical domain schema."""

import json
import hashlib
import re
import time
from datetime import datetime, timezone

from jobradar.diagnostics import record


class TaskTrace:
    def __init__(self, database, task_id, settings=None):
        self.path = database.path.parent / "search-diagnostics" / f"task-{task_id}.json"
        self.data = {"task_id":task_id,"metrics":{"analysis_attempts":0,"analysis_succeeded":0,
            "analysis_failed":0,"cache_hits":0,"reassessments":0,"repair_requests":0,
            "tag_warnings":0,"source_skipped":0,"duplicate_skipped":0,
            "hh_discovered":0,"hh_duplicates":0,"hh_read":0,"hh_saved":0,"hh_incomplete":0,
            "local_candidates":0,"filtered":0,"deferred":0,"visible_results":0,
            "age_excluded":0,"unknown_publication_date":0,"availability_unconfirmed":0},"issues":[]}
        self.context = {}
        self.options = settings.values.get("diagnostics",{}) if settings else {}
        self.llm_options = {key:value for key,value in settings.values["llm"].items() if key in ("provider","model","temperature","max_tokens","timeout_seconds")} if settings else {}
        self.data["llm_calls"] = []
        self.data["started_at"] = datetime.now(timezone.utc).isoformat()

    def prune(self):
        cutoff = time.time() - self.options.get("retention_days",7)*86400
        try:
            for path in self.path.parent.glob("task-*.json"):
                if path != self.path and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            record("search","diagnostics_cleanup_failed")

    @staticmethod
    def redact(value):
        if isinstance(value,dict):
            return {key:("[REDACTED]" if re.search(r"api[_-]?key|authorization|cookie|password|secret|token",key,re.I) else TaskTrace.redact(item)) for key,item in value.items()}
        if isinstance(value,list):
            return [TaskTrace.redact(item) for item in value]
        if isinstance(value,str):
            value = re.sub(r"(?i)Bearer\s+[^\s\"']+","Bearer [REDACTED]",value)
            return re.sub(r"(?i)((?:api[_-]?key|password|secret|token|cookie|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+",r"\1[REDACTED]",value)
        return value

    def llm_call(self, *, prompt_name, messages, schema, model, response, attempt, elapsed_ms, error=None):
        call = {**self.context,"id":len(self.data.setdefault("llm_calls",[]))+1,
                "time":datetime.now(timezone.utc).isoformat(),"prompt":prompt_name,"model":model,
                "prompt_sha256":hashlib.sha256(messages[0]["content"].encode()).hexdigest(),
                "attempt":attempt+1,"is_repair":attempt>0,"elapsed_ms":elapsed_ms,
                "status":"error" if error else "success","error":self.redact(error),
                "finish_reason":response.get("finish_reason"),"usage":response.get("usage")}
        call["capture_mode"] = self.options.get("capture_llm","errors")
        call["parameters"] = self.llm_options
        capture = self.options.get("capture_llm","errors")
        if capture == "all" or capture == "errors" and (error or attempt):
            limit = self.options.get("max_response_chars",65536)
            raw = response.get("content")
            call.update(response=self.redact(raw[:limit]) if isinstance(raw,str) else raw,
                        response_truncated=isinstance(raw,str) and len(raw)>limit,
                        schema=schema,system_prompt=self.redact(messages[0]["content"]),
                        request=self.redact(messages[1]["content"][:limit]))
        self.data["llm_calls"].append(call)
        self.save()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            encoded = json.dumps(self.data,ensure_ascii=False,indent=2)
            limit = self.options.get("max_file_bytes",5242880)
            if len(encoded.encode()) > limit:
                for call in self.data.get("llm_calls",[]):
                    for key in ("response","request","system_prompt","schema"):
                        call.pop(key,None)
                    call["payload_evicted"] = True
                    encoded = json.dumps(self.data,ensure_ascii=False,indent=2)
                    if len(encoded.encode()) <= limit:
                        break
                self.data["payload_limit_reached"] = True
                encoded = json.dumps(self.data,ensure_ascii=False,indent=2)
            temporary.write_text(encoded,encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            record("search","diagnostics_write_failed",task_id=self.data["task_id"])

    def count(self, name, amount=1):
        self.data["metrics"][name] += amount

    def issue(self, item):
        item = self.redact({"time":datetime.now(timezone.utc).isoformat(),**self.context,**item})
        if item["code"] == "REPAIR_REQUESTED":
            self.count("repair_requests")
        if item.get("severity") == "warning" and str(item.get("stage","")).startswith("tag"):
            self.count("tag_warnings")
        self.data["issues"].append(item)
        record("search","processing_issue",task_id=self.data["task_id"],**{key:value for key,value in item.items() if key in ("code","stage","path","rule","severity","vacancy_id")})
        self.save()

    @staticmethod
    def load(database, task_id):
        path = database.path.parent / "search-diagnostics" / f"task-{task_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(data,dict) and data.get("task_id") == task_id and isinstance(data.get("issues"),list)
                    and isinstance(data.get("metrics"),dict)
                    and all(type(data["metrics"].get(key)) is int and data["metrics"][key] >= 0 for key in
                            ("analysis_attempts","analysis_succeeded","analysis_failed","cache_hits","reassessments",
                             "repair_requests","tag_warnings","source_skipped","duplicate_skipped"))
                    and all(isinstance(item,dict) and isinstance(item.get("message"),str) and isinstance(item.get("code"),str) for item in data["issues"])):
                return data
        except (OSError,ValueError):
            pass
        return None

    @classmethod
    def existing(cls, database, task_id, settings=None):
        data = cls.load(database,task_id)
        if data is None:
            return None
        trace = cls(database,task_id,settings)
        trace.data = data
        return trace


def attach_diagnostics(report, trace):
    report["diagnostics"] = trace
    issues = trace.get("issues",[]) if trace else []
    for item in report.get("unassessed",[]):
        matching = [issue for issue in issues if issue.get("vacancy_id") == item["vacancy_id"] and
                    (issue.get("severity") == "error" or issue.get("stage") == "deferred")]
        if matching:
            error = matching[-1]
            item.update(reason=error["message"],error_code=error["code"],stage=error.get("stage"),path=error.get("path"))
        else:
            item["reason"] = ("Сохранённая оценка устарела." if item["state"] == "stale" else
                              "Анализ не завершён. " + ("Задача остановлена: " + report["stop_reason"] if trace and report.get("stop_reason") else
                              "Подробная причина для этой записи не сохранена."))
    return report


def explanation(report):
    counts = report["counts"]
    lines = [f"В поиске #{report['task_id']} найдено {counts['found_count']} вакансий, актуальных оценок — {counts['evaluated_count']}.",
             f"Без актуальной оценки: {report['unassessed_count']}."]
    trace = report.get("diagnostics")
    if trace:
        metrics = trace["metrics"]
        lines.append(f"Попыток нового анализа: {metrics['analysis_attempts']}; успешно: {metrics['analysis_succeeded']}; с ошибкой: {metrics['analysis_failed']}.")
        reasons = list(dict.fromkeys(issue["message"] for issue in trace["issues"] if issue.get("severity") == "error"))
        if reasons:
            lines.append("Зафиксированные причины: " + " ".join(reasons))
        if metrics["tag_warnings"]:
            lines.append(f"Предупреждений по тегам: {metrics['tag_warnings']}. Неподтверждённые теги не сохранены; корректные оценки сохранены.")
    else:
        lines.append("Эта задача выполнена до записи подробной диагностики. По сохранённым данным нельзя установить точные причины всех отказов.")
    if report.get("stop_reason"):
        lines.append("Причина завершения: " + report["stop_reason"])
    return "\n".join(lines)


def search_explanation(report):
    """A factual user answer, never model-generated guesses or raw error logs."""
    trace = report.get("diagnostics") or {}
    counts = trace.get("final_counts") or report["counts"]
    metrics = trace.get("metrics", {})
    lines = [f"В поиске #{report['task_id']} найдено {counts['found_count']}, оценено {counts['evaluated_count']}."]
    if not trace:
        lines.append("Подробная диагностика не сохранена; точные причины установить нельзя.")
        return " ".join(lines)
    if "filtered" in metrics:
        lines.append(f"Отфильтровано: {metrics['filtered']}; отложено: {metrics.get('deferred',0)}. Ранжирование само по себе вакансии не исключает.")
    plan = trace.get("search_plan") or report.get("search_plan") or {}
    if plan.get("queries"):
        lines.append("Запросы: " + "; ".join(q["text"] + (" — " + ", ".join(q["areas"]) if q.get("areas") else "") for q in plan["queries"]) + ".")
    period = trace.get("publication_period")
    if period:
        from .period import period_label
        lines.append("Период публикации: " + period_label(period) + f". По возрасту исключено: {metrics.get('age_excluded',0)}; неизвестная дата: {metrics.get('unknown_publication_date',0)}.")
    coverage = trace.get("coverage")
    if coverage:
        stages = {"cards":"готовые карточки","raw":"сохранённые исходники","source":"этап сбора HH"}
        lines.append("Завершённые этапы: " + (", ".join(stages.get(s,s) for s in coverage["stages_completed"]) or "нет") + ".")
        lines.append(f"Запросов завершено: {coverage['completed_queries']}/{coverage['planned_queries']}; " + ("охват источника неполный." if not coverage["source_complete"] else "запланированные запросы выполнены."))
    reason = trace.get("stop_reason") or report.get("stop_reason")
    descriptions = {"SOURCE_LIMIT":"достигнут лимит сбора HH", "ANALYSIS_LIMIT":"достигнут лимит новых анализов",
                    "LLM_LIMIT":"достигнут бюджет модели", "TIMEOUT":"истекло время поиска",
                    "RESULTS_EXHAUSTED":"цель по числу результатов не достигнута", "CAPTCHA":"HH запросил ручную проверку",
                    "CONTEXT_CHANGED":"изменился поисковый контекст", "INTERRUPTED":"поиск прерван",
                    "LLM_JSON_INVALID":"модель вернула некорректные данные", "LLM_SCHEMA_INVALID":"модель нарушила формат данных",
                    "AREA_UNSUPPORTED":"регион не удалось выбрать на HH", "LOCATION_CLARIFICATION":"требуется уточнить географию",
                    "PERIOD_CLARIFICATION":"требуется уточнить период", "SOURCE_INCOMPLETE":"часть исходников прочитана не полностью"}
    if reason:
        codes = list(dict.fromkeys(item.strip().split(":",1)[0] for item in reason.split(";")))
        lines.append("Причины и ограничения: " + "; ".join(dict.fromkeys(descriptions.get(c,"зафиксирован сбой обработки; подробности в диагностике") for c in codes)) + ".")
    else:
        lines.append("Запланированная обработка завершилась без зафиксированной причины ограничения.")
    lines.append("Подробный разбор доступен в панели диагностики.")
    return " ".join(lines)
