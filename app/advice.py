from datetime import date

# 규칙이 말할 거리를 고르고, AI는 고른 사실을 문장으로만 바꿈. 기준값은 시연하며 정한 임의의 값
LATE_NIGHT_SHARE = 0.3
LATE_NIGHT_MIN_MINUTES = 10
LONG_SESSION_MINUTES = 20
SKIP_SHARE = 0.5
SKIP_MIN_CARDS = 3


def day_label(day: str, today: date) -> str:
    gap = (today - date.fromisoformat(day)).days
    if gap == 0:
        return "오늘"
    if gap == 1:
        return "어제"
    d = date.fromisoformat(day)
    return f"{d.month}월 {d.day}일"


def choose_facts(days: list[dict], today: date) -> tuple[str, dict] | None:
    """시청 기록이 있는 가장 최근 날 하나로 사실을 고름. 어제 기록이 있으면 오늘보다 어제를 우선함"""
    watched = [d for d in days if d.get("sessionCount", 0) > 0 and d["date"] <= today.isoformat()]
    if not watched:
        return None
    before_today = [d for d in watched if d["date"] < today.isoformat()]
    day = max(before_today or watched, key=lambda d: d["date"])
    label = day_label(day["date"], today)
    minutes = day.get("totalMinutes", 0)
    shown = day.get("cardsShown", 0)
    answered = day.get("cardsAnswered", 0)
    skipped = day.get("cardsSkipped", 0)

    if day.get("lateNightShare", 0) >= LATE_NIGHT_SHARE and minutes >= LATE_NIGHT_MIN_MINUTES:
        facts = {"기준": label, "쇼츠 시청(분)": minutes, "밤 12시 이후 비중(%)": round(day["lateNightShare"] * 100)}
    elif day.get("longestSessionMinutes", 0) >= LONG_SESSION_MINUTES:
        facts = {"기준": label, "쇼츠 시청(분)": minutes, "가장 길게 이어 본 시간(분)": day["longestSessionMinutes"]}
    elif shown >= SKIP_MIN_CARDS and skipped / shown >= SKIP_SHARE:
        facts = {"기준": label, "뜬 카드": shown, "건너뛴 카드": skipped}
    elif answered > 0:
        facts = {"기준": label, "푼 카드": answered, "맞힌 카드": day.get("correct", 0)}
    else:
        facts = {"기준": label, "쇼츠 시청(분)": minutes, "넘긴 횟수": day.get("swipes", 0)}
    return day["date"], facts
