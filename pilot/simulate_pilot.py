r"""Симулятор пилота RoomBook: генерирует правдоподобные события бронирования.

ЧЕСТНО: это НЕ данные реального пилота. Офиса и пользователей нет, поэтому цифры получены
симуляцией с фиксированным seed и явно заданными параметрами. Всё, что ниже, — модель поведения,
а не наблюдение. Отчёт (pilot/pilot_report.md) помечает это первой строкой.

Зачем тогда нужно: показывает, какие метрики пилот должен собрать, и даёт каркас для настоящего
пилота — когда данные появятся, тот же отчёт строится по реальным событиям (см. pilot/README.md).

Запуск:
    python pilot/simulate_pilot.py [--seed 42] [--days 90] [--out pilot]
"""

import argparse
import csv
import random
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
SLOT_MIN = 30
WORK_START = 9
WORK_END = 19
SLOTS_PER_DAY = (WORK_END - WORK_START) * 60 // SLOT_MIN  # 20 слотов

ROOMS = [
    ("Room-1", 4, ["projector"]),
    ("Room-2", 6, ["projector", "whiteboard"]),
    ("Room-3", 6, ["whiteboard"]),
    ("Room-4", 8, ["projector", "video-conf"]),
    ("Room-5", 8, ["projector"]),
    ("Room-6", 10, ["projector", "video-conf"]),
    ("Room-7", 12, ["projector", "video-conf", "whiteboard"]),
    ("Room-8", 12, ["projector"]),
    ("Room-9", 14, ["projector", "video-conf"]),
    ("Room-10", 16, ["projector", "video-conf"]),
    ("Room-11", 20, ["projector", "video-conf"]),
    ("Room-12", 30, ["projector", "video-conf", "whiteboard"]),
]

# Вероятность начала встречи по часам: утро и после обеда загружены сильнее.
HOUR_WEIGHTS = {
    9: 0.9, 10: 1.5, 11: 1.7, 12: 1.2, 13: 0.6, 14: 1.6, 15: 1.8,
    16: 1.5, 17: 1.1, 18: 0.5,
}
DURATIONS = [(1, 0.55), (2, 0.33), (3, 0.10), (4, 0.02)]  # в слотах


def pick_hour(rng: random.Random) -> int:
    hours = list(HOUR_WEIGHTS)
    weights = [HOUR_WEIGHTS[h] for h in hours]
    return rng.choices(hours, weights=weights, k=1)[0]


def pick_duration(rng: random.Random) -> int:
    slots = [value for value, _ in DURATIONS]
    weights = [weight for _, weight in DURATIONS]
    return rng.choices(slots, weights=weights, k=1)[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--employees", type=int, default=40)
    parser.add_argument("--out", default="pilot")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    start_day = date(2026, 9, 1)
    employees = [f"user{i:02d}@example.com" for i in range(1, args.employees + 1)]
    occupied: dict[tuple[str, datetime], str] = {}  # (room, start) -> booking id
    events: list[dict] = []
    bookings: list[dict] = []
    sequence = 0

    for day_offset in range(args.days):
        current_day = start_day + timedelta(days=day_offset)
        if current_day.weekday() >= 5:  # выходные
            continue
        for employee in rng.sample(employees, k=rng.randint(8, 22)):
            sequence += 1
            booking_id = f"b{sequence:05d}"
            hour = pick_hour(rng)
            slot_index = (hour - WORK_START) * 60 // SLOT_MIN + rng.choice([0, 1])
            duration = pick_duration(rng)
            if slot_index + duration > SLOTS_PER_DAY:
                slot_index = SLOTS_PER_DAY - duration
            room_name, capacity, _ = rng.choices(ROOMS, weights=[max(1, 20 - c // 3) for _, c, _ in ROOMS], k=1)[0]
            start_local = datetime.combine(current_day, datetime.min.time()).replace(hour=WORK_START, tzinfo=MSK) \
                + timedelta(minutes=slot_index * SLOT_MIN)
            end_local = start_local + timedelta(minutes=duration * SLOT_MIN)
            start_utc = start_local.astimezone(timezone.utc)
            end_utc = end_local.astimezone(timezone.utc)
            lead_minutes = rng.choices(
                [0, 30, 60, 180, 1440, 4320, 10080],
                weights=[0.08, 0.10, 0.18, 0.22, 0.25, 0.12, 0.05],
                k=1,
            )[0]

            conflict = any(
                (room_name, start_utc + timedelta(minutes=SLOT_MIN * i)) in occupied
                for i in range(duration)
            )
            if conflict:
                events.append({
                    "booking_id": booking_id, "employee": employee, "room": room_name,
                    "start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat(),
                    "status": "REJECTED_CONFLICT",
                })
                continue

            for i in range(duration):
                occupied[(room_name, start_utc + timedelta(minutes=SLOT_MIN * i))] = booking_id

            cancelled = rng.random() < 0.12
            cancel_too_late = cancelled and rng.random() < 0.35
            events.append({
                "booking_id": booking_id, "employee": employee, "room": room_name,
                "start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat(),
                "status": "CANCELLED_TOO_LATE" if cancel_too_late else ("CANCELLED" if cancelled else "CONFIRMED"),
            })
            if cancel_too_late:
                for i in range(duration):
                    occupied.pop((room_name, start_utc + timedelta(minutes=SLOT_MIN * i)), None)
            bookings.append({
                "booking_id": booking_id, "employee": employee, "room": room_name,
                "capacity": capacity,
                "start_local": start_local.isoformat(),
                "end_local": end_local.isoformat(),
                "start_utc": start_utc.isoformat(),
                "duration_min": duration * SLOT_MIN,
                "lead_minutes": lead_minutes,
                "status": "CANCELLED" if cancelled and not cancel_too_late else "CONFIRMED",
            })

    with (out_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(events[0]))
        writer.writeheader()
        writer.writerows(events)
    with (out_dir / "bookings.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(bookings[0]))
        writer.writeheader()
        writer.writerows(bookings)

    report = build_report(events, bookings, args)
    (out_dir / "pilot_report.md").write_text(report, encoding="utf-8")
    print(f"событий: {len(events)}, броней: {len(bookings)}")
    print(f"отчёт: {out_dir / 'pilot_report.md'}")
    return 0


def build_report(events: list[dict], bookings: list[dict], args) -> str:
    total = len(events)
    statuses = {}
    for event in events:
        statuses[event["status"]] = statuses.get(event["status"], 0) + 1

    confirmed = [b for b in bookings if b["status"] == "CONFIRMED"]
    durations = [b["duration_min"] for b in confirmed]
    leads = [b["lead_minutes"] for b in confirmed]

    by_room: dict[str, int] = {}
    by_hour: dict[int, int] = {}
    by_weekday: dict[int, int] = {}
    busy_minutes: dict[str, int] = {}
    for booking in confirmed:
        by_room[booking["room"]] = by_room.get(booking["room"], 0) + 1
        start = datetime.fromisoformat(booking["start_local"])
        by_hour[start.hour] = by_hour.get(start.hour, 0) + 1
        by_weekday[start.weekday()] = by_weekday.get(start.weekday(), 0) + 1
        busy_minutes[booking["room"]] = busy_minutes.get(booking["room"], 0) + booking["duration_min"]

    working_days = len({b["start_local"][:10] for b in bookings}) or 1
    room_capacity_minutes = working_days * SLOTS_PER_DAY * SLOT_MIN
    occupancy = {room: busy_minutes.get(room, 0) / room_capacity_minutes for room, _, _ in ROOMS}

    weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт"]
    lines = [
        "# Пилот RoomBook — отчёт по событиям",
        "",
        "> **Это симуляция, а не реальный пилот.** Данные получены генератором `pilot/simulate_pilot.py`",
        f"> при seed = {args.seed}, период {args.days} дней, {args.employees} сотрудников, 12 комнат.",
        "> Цифры показывают, какие метрики пилот должен собрать, и как они считаются. Заменяются на",
        "> реальные события из `events.csv` без изменения логики отчёта (см. `pilot/README.md`).",
        "",
        "## Воронка событий",
        "",
        "| Событие | Количество | Доля |",
        "|---|---|---|",
    ]
    labels = {
        "CONFIRMED": "Бронь создана (CONFIRMED)",
        "CANCELLED": "Отменена сотрудником по правилу (> 1 ч)",
        "CANCELLED_TOO_LATE": "Поздняя отмена (отклонена правилом ≤ 1 ч)",
        "REJECTED_CONFLICT": "Отклонена из-за пересечения (409)",
    }
    for key in ("CONFIRMED", "CANCELLED", "CANCELLED_TOO_LATE", "REJECTED_CONFLICT"):
        count = statuses.get(key, 0)
        lines.append(f"| {labels[key]} | {count} | {count / total * 100:.1f}% |")

    lines += [
        "",
        "## Ключевые метрики",
        "",
        "| Метрика | Значение | Как считается |",
        "|---|---|---|",
        f"| Всего попыток бронирования | {total} | строки `events.csv` |",
        f"| Успешных броней | {len(confirmed)} | статус CONFIRMED в `bookings.csv` |",
        f"| Доля отказов из-за пересечения | {statuses.get('REJECTED_CONFLICT', 0) / total * 100:.1f}% | REJECTED_CONFLICT / все попытки |",
        f"| Медианная длительность встречи | {statistics.median(durations):.0f} мин | `duration_min` |",
        f"| 90-й процентиль длительности | {sorted(durations)[int(len(durations) * 0.9)]} мин | перцентиль по `duration_min` |",
        f"| Медианное время до встречи (lead time) | {statistics.median(leads) / 60:.1f} ч | `lead_minutes` |",
        f"| Доля броней, созданных в день встречи | {sum(1 for x in leads if x <= 60) / len(leads) * 100:.1f}% | lead ≤ 60 мин |",
        "",
        "## Загрузка по комнатам",
        "",
        "| Комната | Вместимость | Броней | Занято часов | Загрузка |",
        "|---|---|---|---|---|",
    ]
    for room_name, capacity, _ in ROOMS:
        count = by_room.get(room_name, 0)
        hours = busy_minutes.get(room_name, 0) / 60
        lines.append(f"| {room_name} | {capacity} | {count} | {hours:.1f} | {occupancy[room_name] * 100:.1f}% |")

    lines += ["", "## Загрузка по часам (начало встречи, МСК)", "", "| Час | Броней |", "|---|---|"]
    for hour in range(WORK_START, WORK_END):
        lines.append(f"| {hour:02d}:00 | {by_hour.get(hour, 0)} |")

    lines += ["", "## По дням недели", "", "| День | Броней |", "|---|---|"]
    for index, name in enumerate(weekday_names):
        lines.append(f"| {name} | {by_weekday.get(index, 0)} |")

    top = sorted(by_room.items(), key=lambda item: -item[1])[:5]
    lines += [
        "",
        "## Топ-5 комнат по числу броней",
        "",
        "| Комната | Броней |",
        "|---|---|",
    ]
    for room_name, count in top:
        lines.append(f"| {room_name} | {count} |")

    lines += [
        "",
        "## Что с этим делать в реальном пилоте",
        "",
        "1. Заменить `events.csv` на выгрузку из журнала портала: те же колонки, те же статусы.",
        "2. Пересчитать отчёт скриптом — логика не меняется.",
        "3. Сравнить с целевыми значениями: доля отказов из-за пересечений — индикатор качества поиска",
        "   слотов; доля поздних отмен — индикатор дисциплины бронирования.",
        "4. Зафиксировать фактические p95 времени ответа (в симуляции их нет — это измеряется нагрузочным тестом).",
        "",
        "## Параметры генерации (для воспроизводимости)",
        "",
        f"- seed: `{args.seed}`",
        f"- период: `{args.days}` дней с 2026-09-01",
        f"- сотрудников: `{args.employees}`",
        "- рабочие часы: 09:00–19:00 МСК, шаг 30 минут, только будни",
        "- вероятности длительности: 30 мин — 55%, 60 мин — 33%, 90 мин — 10%, 120 мин — 2%",
        "- вероятность отмены: 12%, из них 35% — поздние (правило ≤ 1 ч)",
        "- конфликты: проверяются по 30-минутным слотам; отказ фиксируется как 409",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
