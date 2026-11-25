"""Deterministic, readable descriptions of operations; never dump tool arguments."""

import re


def text(value):
    if value is None:
        return "не указано"
    if isinstance(value,bool):
        return "да" if value else "нет"
    return re.sub(r"\\([.*_`])",r"\1",str(value))


def duration(years):
    if years is None:
        return "стаж не указан"
    months = round(years * 12)
    whole, rest = divmod(months,12)
    def word(number,forms):
        return forms[2] if 11 <= number % 100 <= 14 else forms[0] if number % 10 == 1 else forms[1] if 2 <= number % 10 <= 4 else forms[2]
    parts = []
    if whole:
        parts.append(f"{whole} {word(whole,('год','года','лет'))}")
    if rest or not whole:
        parts.append(f"{rest} {word(rest,('месяц','месяца','месяцев'))}")
    return ("≈ " if abs(months-years*12) > 0.000001 else "") + " ".join(parts)


def profile_lines(data):
    lines = []
    for key,label in (("profession","Профессия"),("specializations","Специализации"),
                      ("competencies","Навыки"),("notes","Примечание")):
        if key in data:
            value = data[key]
            lines.append(label + ": " + ("; ".join(map(text,value)) if isinstance(value,list) and value else "нет" if value == [] else text(value)))
    if "experience" in data:
        items = [text(item["domain"]) + " — " + duration(item.get("years")) for item in data["experience"]]
        lines.append("Опыт: " + ("; ".join(items) or "не указан"))
    if "qualifications" in data:
        items = [text(item["name"]) + (" (" + ", ".join(map(text,item["details"])) + ")" if item.get("details") else "") for item in data["qualifications"]]
        lines.append("Квалификация и допуски: " + ("; ".join(items) or "не указаны"))
    if "education" in data:
        items = [", ".join(text(item[key]) for key in ("level","field","qualification","institution","year") if item.get(key) is not None) for item in data["education"]]
        lines.append("Образование: " + ("; ".join(items) or "не указано"))
    if "additional_attributes" in data:
        items = [text(item["description"]) + ": " + text(item["value"]) for item in data["additional_attributes"]]
        lines.append("Дополнительные сведения: " + ("; ".join(items) or "нет"))
    return lines


def criteria_lines(data):
    lines = []
    formats = {"remote":"удалённая работа","hybrid":"гибрид","office":"офис"}
    for key,label in (("target_roles","Направления"),("locations","География"),("work_formats","Формат работы")):
        if key in data:
            lines.append(label + ": " + (", ".join(formats.get(item,text(item)) for item in data[key]) or "не ограничено"))
    if "salary" in data:
        salary = data["salary"]
        if salary is None:
            lines.append("Пожелание по зарплате: снять")
        else:
            parts = []
            if "min" in salary:
                amount = salary["min"]
                if isinstance(amount,float) and amount.is_integer():
                    amount = int(amount)
                parts.append("нижняя граница не указана" if amount is None else "от " + format(amount,",").replace(","," ") + (" " + text(salary["currency"]) if salary.get("currency") else ""))
            elif "currency" in salary:
                parts.append("валюта " + text(salary["currency"]))
            if "strict" in salary:
                parts.append("обязательное условие" if salary["strict"] else "мягкое пожелание")
            lines.append("Зарплата: " + (", ".join(parts) or "без изменений"))
    for key,label in (("experience_level","Уровень опыта"),("notes","Пожелания")):
        if key in data:
            lines.append(label + ": " + text(data[key]))
    operations = {"prefer":"предпочтительно","exclude":"исключить","require":"обязательно"}
    attributes = {"domain":"сфера","employment_type":"занятость","work_format":"формат работы","relocation":"переезд"}
    for key,label in (("preferences","Предпочтения"),("restrictions","Ограничения")):
        if key in data:
            lines.append(label + ": " + ("; ".join(attributes.get(item["attribute"],text(item["attribute"])) + " — " +
                         operations.get(item["operator"],text(item["operator"])) + ": " + text(item["value"]) for item in data[key]) or "нет"))
    from jobradar.search.period import stored_period, period_label
    period = stored_period(data)
    if period:
        lines = [line for line in lines if not line.startswith("Ограничения:")]
        others = [item for item in data.get("restrictions",[]) if item["attribute"] != "publication_period"]
        if others:
            lines.append("Ограничения: " + "; ".join(text(item["attribute"]) + " — " + text(item["operator"]) + ": " + text(item["value"]) for item in others))
        lines.append("Период публикации: " + ("стандартный из конфигурации" if period["unit"] == "default" else period_label(period)))
    return lines


def describe(name,arguments,*,context_name=None):
    if name == "create_profile":
        # Unknown/empty optional facts are not presented as qualifications gained.
        facts = {key:value for key,value in arguments.get("profile_data",{}).items() if value not in (None,[],"")}
        return "\n".join(["Создать профиль «" + text(arguments["profession"]) + "»",*profile_lines(facts)])
    if name == "create_context":
        return "\n".join(["Создать поисковый контекст «" + text(arguments["name"]) + "»",*criteria_lines(arguments.get("criteria",{}))])
    if name == "update_profile":
        return "\n".join(["Обновить профессиональные сведения",*profile_lines(arguments.get("changes",arguments))])
    if name == "update_context":
        return "\n".join(["Обновить условия поиска",*criteria_lines(arguments.get("changes",arguments))])
    if name == "switch_context":
        return "Переключиться на контекст «" + text(context_name or "№" + str(arguments["context_id"])) + "»"
    if name == "return_to_general":
        return "Вернуться в общий диалог"
    if name == "search_vacancies":
        return "Запустить поиск вакансий" + (": " + text(arguments["request"]) if arguments.get("request") else "")
    return "Выполнить запрошенное действие"


def short_confirmation(message):
    value = " ".join(message.strip().casefold().split())
    value = re.sub(r"\s*,\s*"," ",value).rstrip(".!…").strip()
    return value in {"да","подтверждаю","я подтверждаю","да, подтверждаю","да подтверждаю",
                     "да, я подтверждаю","да я подтверждаю","да согласен","да согласна","согласен","согласна","давай","давайте","ок","окей"}


def completed(name,description):
    replacements = {"create_profile":("Создать профиль","Создан профиль"),
                    "create_context":("Создать поисковый контекст","Создан поисковый контекст"),
                    "switch_context":("Переключиться на контекст","Активный контекст:"),
                    "return_to_general":("Вернуться в общий диалог","Возврат в общий диалог выполнен"),
                    "update_profile":("Обновить профессиональные сведения","Профессиональные сведения обновлены"),
                    "update_context":("Обновить условия поиска","Условия поиска обновлены")}
    before,after = replacements.get(name,("", ""))
    return "Выполнено.\n" + (after+description[len(before):] if before and description.startswith(before) else description)
