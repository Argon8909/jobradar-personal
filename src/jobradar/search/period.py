"""Publication eligibility, independent of card extraction and personal scoring."""

import calendar
import re
from datetime import datetime, time, timedelta, timezone

# HH's date-only and offset-less publication dates use Moscow civil time.
MOSCOW = timezone(timedelta(hours=3))
ATTRIBUTE = "publication_period"


def publication_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MOSCOW)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def normalize_date(value):
    parsed = publication_date(value)
    return parsed.isoformat() if parsed else None


def validate_period(period):
    if not isinstance(period, dict) or set(period) != {"unit", "value"}:
        raise ValueError("Период должен содержать unit и value.")
    unit, value = period["unit"], period["value"]
    if unit in ("default", "unlimited") and value is None:
        return dict(period)
    bounds = {"days":36500, "weeks":5200, "months":1200}
    if unit in bounds and type(value) is int and 1 <= value <= bounds[unit]:
        return dict(period)
    raise ValueError("Период: days 1–36500, weeks 1–5200, months 1–1200; default/unlimited требуют null.")


def stored_period(criteria):
    items = [item for item in criteria.get("restrictions", []) if item.get("attribute") == ATTRIBUTE]
    if not items:
        return None
    if len(items) != 1 or items[0].get("operator") != "within":
        raise ValueError("Ограничение publication_period должно быть единственным, с оператором within.")
    value = items[0].get("value")
    if value in ("default", "unlimited"):
        return {"unit":value, "value":None}
    match = re.fullmatch(r"(days|weeks|months):([0-9]+)", value) if isinstance(value,str) else None
    if not match:
        raise ValueError("publication_period: ожидается days:N, weeks:N, months:N, default или unlimited.")
    return validate_period({"unit":match[1], "value":int(match[2])})


def assessment_criteria(criteria):
    """A selection window must never become a personal-fit criterion."""
    return {**criteria, "restrictions":[item for item in criteria.get("restrictions", [])
                                      if item.get("attribute") != ATTRIBUTE]} if "restrictions" in criteria else dict(criteria)


def resolve_period(criteria, limits, started_at, override=None):
    default = {"unit":"days", "value":limits["vacancy_max_age_days"]} if limits.get("vacancy_max_age_days") else {"unit":"unlimited", "value":None}
    configured = stored_period(criteria)
    selected = validate_period(override) if override is not None else configured or default
    source = "request" if override is not None else "context" if configured else "configuration"
    reset = selected["unit"] == "default"
    if reset:
        selected = default
    start = publication_date(started_at)
    if start is None:
        raise ValueError("Некорректное время начала поиска.")
    day = start.astimezone(MOSCOW).date()
    unit, value = selected["unit"], selected["value"]
    if unit == "months":
        total = day.year * 12 + day.month - 1 - value
        year, month = divmod(total, 12)
        day = day.replace(year=year, month=month+1, day=min(day.day, calendar.monthrange(year,month+1)[1]))
    elif unit in ("days", "weeks"):
        day -= timedelta(days=value * (7 if unit == "weeks" else 1))
    cutoff = datetime.combine(day, time.min, MOSCOW).astimezone(timezone.utc) if unit != "unlimited" else None
    return {**selected, "source":source, "reset_to_default":reset, "started_at":start.isoformat(),
            "cutoff":cutoff.isoformat() if cutoff else None, "timezone":"Europe/Moscow", "boundary":"inclusive_calendar_date"}


def date_state(raw, period):
    parsed = publication_date(raw.get("published_at"))
    if parsed is None or period and parsed > publication_date(period["started_at"]):
        return "unknown"
    if period and period.get("cutoff") and parsed < publication_date(period["cutoff"]):
        return "excluded"
    return "included"


def availability_observed(raw, period, ended_at=None):
    checked = publication_date(raw.get("last_checked_at"))
    start = publication_date(period.get("started_at")) if period else None
    end = publication_date(ended_at) if ended_at else datetime.now(timezone.utc)
    return bool(raw.get("status") == "available" and checked and start and start <= checked <= end)


def period_label(period):
    if not period:
        return "не сохранён"
    if period["unit"] == "unlimited":
        return "без ограничения по дате"
    return f"{period['value']} " + {"days":"календарных дней", "weeks":"недель", "months":"календарных месяцев"}[period["unit"]]
