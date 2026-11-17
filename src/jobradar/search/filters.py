"""Conservative checks for proven hard-filter conflicts."""

def hard_mismatch(card, criteria):
    """Exclude only proven conflicts; unknown values still go to analysis."""
    formats = criteria.get("work_formats",[])
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
