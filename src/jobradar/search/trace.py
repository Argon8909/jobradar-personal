"""Small local per-task diagnostic files, separate from the logical domain schema."""

import json

from jobradar.diagnostics import record


class TaskTrace:
    def __init__(self, database, task_id):
        self.path = database.path.parent / "search-diagnostics" / f"task-{task_id}.json"
        self.data = {"task_id":task_id,"metrics":{"analysis_attempts":0,"analysis_succeeded":0,
            "analysis_failed":0,"cache_hits":0,"reassessments":0,"repair_requests":0,
            "tag_warnings":0,"source_skipped":0,"duplicate_skipped":0},"issues":[]}
        self.context = {}

    def save(self):
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            record("search","diagnostics_write_failed",task_id=self.data["task_id"])

    def count(self, name, amount=1):
        self.data["metrics"][name] += amount

    def issue(self, item):
        item = {**self.context,**item}
        if item["code"] == "REPAIR_REQUESTED":
            self.count("repair_requests")
        if item.get("severity") == "warning" and str(item.get("stage","")).startswith("tag"):
            self.count("tag_warnings")
        self.data["issues"].append(item)
        record("search","processing_issue",task_id=self.data["task_id"],**item)
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
    def existing(cls, database, task_id):
        data = cls.load(database,task_id)
        if data is None:
            return None
        trace = cls(database,task_id)
        trace.data = data
        return trace


def attach_diagnostics(report, trace):
    report["diagnostics"] = trace
    issues = trace.get("issues",[]) if trace else []
    for item in report.get("unassessed",[]):
        matching = [issue for issue in issues if issue.get("vacancy_id") == item["vacancy_id"] and issue.get("severity") == "error"]
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
