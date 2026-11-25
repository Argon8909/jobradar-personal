"""Conservative checks for proven hard-filter conflicts."""

import re
from .period import date_state


def preliminary_mismatch(raw, criteria, card=None, period=None):
    """Shared gate before any paid operation. Missing evidence never means no."""
    if raw.get("status") in ("closed","archived"):
        return "Объявление подтверждённо закрыто."
    if period and date_state(raw,period) == "excluded":
        return "Дата публикации вне выбранного периода."
    if card is not None:
        return hard_mismatch(card,criteria)
    # Only an explicit, standalone field is proof. Mentions of an office in
    # duties/benefits and descriptions of clients' working conditions are not.
    formats = {"офис":"office","офисный":"office","на месте работодателя":"office",
               "удалённо":"remote","удаленно":"remote","удалённая работа":"remote",
               "удаленная работа":"remote","гибрид":"hybrid","гибридный":"hybrid"}
    values = re.findall(r"^\s*формат работы\s*:\s*([^\n]+)$",raw.get("description") or "",re.I|re.M)
    normalized = [value.strip().casefold().rstrip(".;!") for value in values]
    if normalized and all(value in formats for value in normalized):
        observed = {formats[value] for value in normalized}
        if len(observed) == 1:
            return hard_mismatch({"work_format":observed.pop()},criteria)
    return None


def candidate_priority(raw, terms):
    """Soft ordering only: zero is neither a rejection nor a permanent label."""
    text = " ".join(str(raw.get(key) or "") for key in ("title","employer","description")).casefold()
    return sum(term.casefold() in text for term in terms)

def hard_mismatch(card, criteria):
    """Exclude only proven conflicts; unknown values still go to analysis."""
    formats = criteria.get("work_formats",[])
    if not formats and any(value.strip().casefold() in ("удалённо","удаленно","удалёнка","удаленка","remote") for value in criteria.get("locations",[])):
        formats = ["remote"]
    if formats and card.get("work_format") not in (None,"unknown") and card["work_format"] not in formats:
        return "Формат работы не соответствует указанным обязательным условиям."
    # For geography do not guess whether an arbitrary city belongs to a country.
    cities = ("москва","санкт-петербург","казань","новосибирск","екатеринбург","нижний новгород")
    location = (card.get("area") or "").casefold()
    requested = [value.casefold() for value in criteria.get("locations",[])]
    if requested and all(value in cities for value in requested):
        observed = [city for city in cities if city in location]
        if observed and not any(value in observed for value in requested):
            return "География не соответствует указанным обязательным условиям."
    salary = criteria.get("salary") or {}
    if (salary.get("strict") and salary.get("min") is not None and card.get("salary_to") is not None
            and salary.get("currency") == card.get("salary_currency") and card["salary_to"] < salary["min"]):
        return "Указанная зарплата ниже явно установленной обязательной границы."
    return None
