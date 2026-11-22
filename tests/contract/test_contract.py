"""Контрактные тесты RoomBook: спецификация против эталонной реализации.

Два уровня проверки:

1. **Контракт сам по себе** — все `example`/`examples` из `contracts/openapi.yaml` валидны против
   своих схем; ошибки описаны через `application/problem+json`; `operationId` уникальны.
2. **Реализация против контракта** — эталонный мини-сервис (FastAPI, in-memory) на 7 эндпоинтов:
   тесты проверяют коды и формы ответов ровно так, как записано в спецификации.

Запуск:
    python -m pytest tests/contract -q
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

SPEC_PATH = Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
SCHEMAS = SPEC["components"]["schemas"]


# --------------------------------------------------------------------------------------------
# Уровень 1: контракт проверяет сам себя
# --------------------------------------------------------------------------------------------

def resolve(pointer: str):
    if not pointer.startswith("#/"):
        raise ValueError(pointer)
    node = SPEC
    for part in pointer[2:].split("/"):
        node = node[part]
    return node


def deref(schema: dict) -> dict:
    """Подставить $ref верхнего уровня."""
    if "$ref" in schema:
        return resolve(schema["$ref"])
    return schema


def deep_resolve(node):
    """Рекурсивно раскрыть все внутренние $ref, чтобы jsonschema мог валидировать без registry."""
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
            return deep_resolve(resolve(node["$ref"]))
        return {key: deep_resolve(value) for key, value in node.items()}
    if isinstance(node, list):
        return [deep_resolve(item) for item in node]
    return node


def collect_examples(node, path="root"):
    """Найти все example/examples в спецификации вместе с их схемой."""
    if isinstance(node, dict):
        schema = None
        if "schema" in node and isinstance(node["schema"], dict):
            schema = deep_resolve(deref(node["schema"]))
        if "example" in node and schema is not None:
            yield path, schema, node["example"]
        if "examples" in node and isinstance(node["examples"], dict) and schema is not None:
            for name, example in node["examples"].items():
                value = example.get("value") if isinstance(example, dict) else example
                yield f"{path}.{name}", schema, value
        for key, value in node.items():
            if key in ("example", "examples"):
                continue
            yield from collect_examples(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from collect_examples(item, f"{path}[{index}]")


def test_examples_valid_against_schemas():
    checked = 0
    failures = []
    for path, schema, value in collect_examples(SPEC):
        try:
            Draft202012Validator(schema).validate(value)
            checked += 1
        except Exception as error:  # noqa: BLE001
            failures.append(f"{path}: {error.message if hasattr(error, 'message') else error}")
    assert checked >= 10, f"слишком мало проверенных примеров: {checked}"
    assert not failures, "невалидные примеры:\n" + "\n".join(failures)


def test_operation_ids_unique():
    ids = [
        operation["operationId"]
        for path in SPEC["paths"].values()
        for method, operation in path.items()
        if method in ("get", "post", "patch", "put", "delete")
    ]
    assert len(ids) == len(set(ids)) == 7


def test_error_responses_use_problem_json():
    for path, methods in SPEC["paths"].items():
        for method, operation in methods.items():
            if method not in ("get", "post", "patch", "put", "delete"):
                continue
            for code, response in operation["responses"].items():
                if str(code).startswith("2"):
                    continue
                resolved = deref(response) if "$ref" in response else response
                assert "application/problem+json" in resolved["content"], f"{method} {path} {code}"


def test_idempotency_header_is_required():
    parameters = SPEC["paths"]["/bookings"]["post"]["parameters"]
    header = next(item for item in parameters if item["name"] == "Idempotency-Key")
    assert header["required"] is True


# --------------------------------------------------------------------------------------------
# Уровень 2: эталонная реализация против контракта
# --------------------------------------------------------------------------------------------

def problem(status: int, type_slug: str, title: str, detail: str, instance: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"https://api.roombook.example/problems/{type_slug}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": instance,
        },
    )


def now_utc() -> datetime:
    override = os.environ.get("ROOMBOOK_NOW")
    if override:
        return datetime.fromisoformat(override.replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def build_app() -> FastAPI:
    app = FastAPI()
    rooms = [
        {"id": "r1", "name": "Room-1", "capacity": 6, "equipment": ["projector"], "is_active": True},
        {"id": "r2", "name": "Room-2", "capacity": 14, "equipment": ["projector", "video-conf"], "is_active": True},
        {"id": "r3", "name": "Room-3", "capacity": 3, "equipment": [], "is_active": False},
    ]
    bookings: list[dict] = []
    keys: dict[str, dict] = {}
    state = {"seq": 0}

    def auth(request: Request) -> dict:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status_code=401)
        token = header.removeprefix("Bearer ").strip()
        if token == "employee":
            return {"email": "user@example.com", "role": "EMPLOYEE"}
        if token == "admin":
            return {"email": "admin@example.com", "role": "ADMIN"}
        raise HTTPException(status_code=401)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        mapping = {
            401: ("unauthorized", "Authentication required", "Токен отсутствует или истёк"),
            403: ("forbidden", "Access denied", "Недостаточно прав"),
            404: ("not-found", "Resource not found", "Ресурс не найден"),
        }
        slug, title, detail = mapping.get(exc.status_code, ("internal-error", "Error", "Ошибка"))
        return problem(exc.status_code, slug, title, detail, request.url.path)

    def parse_interval(payload: dict):
        start = datetime.fromisoformat(payload["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(payload["end_time"].replace("Z", "+00:00"))
        msk = timezone(timedelta(hours=3))
        local_start = start.astimezone(msk)
        local_end = end.astimezone(msk)
        if end <= start:
            return None, "конец раньше начала"
        if local_start.minute % 30 or local_end.minute % 30:
            return None, "время не кратно 30 минутам"
        if local_start.hour < 9 or local_end.hour > 19 or (local_end.hour == 19 and local_end.minute > 0):
            return None, "вне рабочих часов"
        return (start, end), None

    @app.get("/rooms")
    async def list_rooms(request: Request, capacity_min: int | None = None, q: str | None = None):
        auth(request)
        items = [room for room in rooms if room["is_active"]]
        if capacity_min is not None:
            items = [room for room in items if room["capacity"] >= capacity_min]
        if q:
            items = [room for room in items if any(q.lower() in str(e).lower() for e in room["equipment"])]
        return items

    @app.get("/rooms/{room_id}/slots")
    async def list_slots(room_id: str, date: str, request: Request):
        auth(request)
        room = next((item for item in rooms if item["id"] == room_id), None)
        if room is None:
            raise HTTPException(status_code=404)
        msk = timezone(timedelta(hours=3))
        base = datetime.fromisoformat(f"{date}T09:00:00+03:00")
        slots = []
        for index in range(20):
            start = base + timedelta(minutes=30 * index)
            busy = any(
                booking["room_id"] == room_id
                and booking["status"] == "CONFIRMED"
                and datetime.fromisoformat(booking["start_time"]) <= start.astimezone(timezone.utc)
                < datetime.fromisoformat(booking["end_time"])
                for booking in bookings
            )
            slots.append({
                "start_time": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "end_time": (start + timedelta(minutes=30)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "status": "busy" if busy else "free",
            })
        return slots

    @app.post("/bookings", status_code=201)
    async def create_booking(payload: dict, request: Request, idempotency_key: str | None = Header(default=None)):
        user = auth(request)
        if not idempotency_key:
            return problem(400, "missing-idempotency-key", "Idempotency-Key header is required",
                           "Заголовок Idempotency-Key обязателен", "/bookings")
        if idempotency_key in keys:
            return keys[idempotency_key]
        if payload.get("roomId") not in {room["id"] for room in rooms}:
            raise HTTPException(status_code=404)
        interval, error = parse_interval(payload)
        if error:
            return problem(422, "invalid-time-range", "Invalid booking interval", error, "/bookings")
        start, end = interval
        for booking in bookings:
            if booking["room_id"] != payload["roomId"] or booking["status"] != "CONFIRMED":
                continue
            other_start = datetime.fromisoformat(booking["start_time"])
            other_end = datetime.fromisoformat(booking["end_time"])
            if start < other_end and other_start < end:
                return problem(409, "slot-taken", "Slot is already booked", "Интервалы пересекаются", "/bookings")
        state["seq"] += 1
        booking = {
            "id": f"b{state['seq']}",
            "room": {"id": payload["roomId"], "name": next(r["name"] for r in rooms if r["id"] == payload["roomId"])},
            "user_email": user["email"],
            "start_time": start.isoformat().replace("+00:00", "Z"),
            "end_time": end.isoformat().replace("+00:00", "Z"),
            "status": "CONFIRMED",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        bookings.append({**booking, "room_id": payload["roomId"], "owner": user["email"]})
        keys[idempotency_key] = booking
        return booking

    @app.get("/bookings/my")
    async def my_bookings(request: Request, status: str | None = None):
        user = auth(request)
        items = [b for b in bookings if b["owner"] == user["email"]]
        if status:
            items = [b for b in items if b["status"] == status]
        return [{k: v for k, v in b.items() if k not in ("room_id", "owner")} for b in items]

    @app.patch("/bookings/{booking_id}/cancel")
    async def cancel(booking_id: str, request: Request, payload: dict | None = None):
        user = auth(request)
        booking = next((b for b in bookings if b["id"] == booking_id), None)
        if booking is None:
            raise HTTPException(status_code=404)
        if booking["status"] == "CANCELLED":
            return problem(409, "already-cancelled", "Booking already cancelled", "Бронь уже отменена", "/bookings")
        if user["role"] != "ADMIN" and booking["owner"] != user["email"]:
            raise HTTPException(status_code=403)
        start = datetime.fromisoformat(booking["start_time"])
        if user["role"] != "ADMIN" and start - now_utc() < timedelta(hours=1):
            return problem(409, "cancel-too-late", "Cancellation window is over",
                           "Отмена возможна не позднее чем за 1 час", "/bookings")
        booking["status"] = "CANCELLED"
        booking["cancelled_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return {k: v for k, v in booking.items() if k not in ("room_id", "owner")}

    @app.post("/admin/rooms", status_code=201)
    async def create_room(payload: dict, request: Request):
        user = auth(request)
        if user["role"] != "ADMIN":
            raise HTTPException(status_code=403)
        if not payload.get("name") or payload.get("capacity", 0) < 1:
            return problem(422, "invalid-room", "Invalid room", "Нужны название и вместимость ≥ 1", "/admin/rooms")
        room = {
            "id": f"r{len(rooms) + 1}",
            "name": payload["name"],
            "capacity": payload["capacity"],
            "equipment": payload.get("equipment", []),
            "is_active": True,
        }
        rooms.append(room)
        return room

    @app.get("/admin/reports/load")
    async def load_report(request: Request, from_: str = Query(alias="from"), to: str = Query(default="")):
        user = auth(request)
        if user["role"] != "ADMIN":
            raise HTTPException(status_code=403)
        csv_body = "room;bookings;busy_hours\nRoom-1;12;8.5\nRoom-2;4;3.0\n"
        return PlainTextResponse(csv_body, media_type="text/csv")

    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(build_app())


EMPLOYEE = {"Authorization": "Bearer employee"}
ADMIN = {"Authorization": "Bearer admin"}


def validate(instance: dict, schema_name: str) -> None:
    schema = copy.deepcopy(SCHEMAS[schema_name])
    Draft202012Validator(schema).validate(instance)


def test_unauthorized_without_token(client):
    response = client.get("/rooms")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    validate(response.json(), "Problem")


def test_rooms_filtering(client):
    response = client.get("/rooms?capacity_min=10", headers=EMPLOYEE)
    assert response.status_code == 200
    rooms = response.json()
    assert [room["name"] for room in rooms] == ["Room-2"]
    for room in rooms:
        validate(room, "Room")


def test_slots_shape(client):
    response = client.get("/rooms/r1/slots?date=2026-12-01", headers=EMPLOYEE)
    assert response.status_code == 200
    slots = response.json()
    assert len(slots) == 20
    validate(slots[0], "Slot")


def test_create_booking_contract(client):
    payload = {"roomId": "r1", "start_time": "2026-12-01T07:00:00Z", "end_time": "2026-12-01T07:30:00Z"}
    response = client.post("/bookings", json=payload, headers={**EMPLOYEE, "Idempotency-Key": "k-1"})
    assert response.status_code == 201
    body = response.json()
    validate(body, "Booking")
    assert body["status"] == "CONFIRMED"


def test_conflict_on_overlap(client):
    first = {"roomId": "r1", "start_time": "2026-12-02T07:00:00Z", "end_time": "2026-12-02T08:00:00Z"}
    second = {"roomId": "r1", "start_time": "2026-12-02T07:30:00Z", "end_time": "2026-12-02T08:30:00Z"}
    assert client.post("/bookings", json=first, headers={**EMPLOYEE, "Idempotency-Key": "k-2"}).status_code == 201
    response = client.post("/bookings", json=second, headers={**EMPLOYEE, "Idempotency-Key": "k-3"})
    assert response.status_code == 409
    validate(response.json(), "Problem")


def test_adjacent_intervals_are_allowed(client):
    first = {"roomId": "r2", "start_time": "2026-12-03T07:00:00Z", "end_time": "2026-12-03T08:00:00Z"}
    second = {"roomId": "r2", "start_time": "2026-12-03T08:00:00Z", "end_time": "2026-12-03T09:00:00Z"}
    assert client.post("/bookings", json=first, headers={**EMPLOYEE, "Idempotency-Key": "k-4"}).status_code == 201
    assert client.post("/bookings", json=second, headers={**EMPLOYEE, "Idempotency-Key": "k-5"}).status_code == 201


def test_idempotency_returns_same_booking(client):
    payload = {"roomId": "r1", "start_time": "2026-12-04T07:00:00Z", "end_time": "2026-12-04T07:30:00Z"}
    first = client.post("/bookings", json=payload, headers={**EMPLOYEE, "Idempotency-Key": "same-key"})
    second = client.post("/bookings", json=payload, headers={**EMPLOYEE, "Idempotency-Key": "same-key"})
    assert first.json()["id"] == second.json()["id"]


def test_missing_idempotency_key_is_400(client):
    payload = {"roomId": "r1", "start_time": "2026-12-05T07:00:00Z", "end_time": "2026-12-05T07:30:00Z"}
    response = client.post("/bookings", json=payload, headers=EMPLOYEE)
    assert response.status_code == 400
    validate(response.json(), "Problem")


def test_invalid_interval_is_422(client):
    payload = {"roomId": "r1", "start_time": "2026-12-06T07:15:00Z", "end_time": "2026-12-06T07:45:00Z"}
    response = client.post("/bookings", json=payload, headers={**EMPLOYEE, "Idempotency-Key": "k-6"})
    assert response.status_code == 422
    validate(response.json(), "Problem")


def test_outside_working_hours_is_422(client):
    payload = {"roomId": "r1", "start_time": "2026-12-07T15:30:00Z", "end_time": "2026-12-07T17:00:00Z"}
    response = client.post("/bookings", json=payload, headers={**EMPLOYEE, "Idempotency-Key": "k-7"})
    assert response.status_code == 422


def test_employee_cannot_create_room(client):
    response = client.post("/admin/rooms", json={"name": "Room-X", "capacity": 5}, headers=EMPLOYEE)
    assert response.status_code == 403
    validate(response.json(), "Problem")


def test_admin_creates_room(client):
    response = client.post("/admin/rooms", json={"name": "Room-X", "capacity": 5}, headers=ADMIN)
    assert response.status_code == 201
    validate(response.json(), "Room")


def test_admin_report_is_csv(client):
    response = client.get("/admin/reports/load?from=2026-11-01&to=2026-12-01", headers=ADMIN)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines()[0] == "room;bookings;busy_hours"


def test_employee_cannot_read_report(client):
    response = client.get("/admin/reports/load?from=2026-11-01&to=2026-12-01", headers=EMPLOYEE)
    assert response.status_code == 403


def test_employee_cancels_own_future_booking(client):
    payload = {"roomId": "r1", "start_time": "2026-12-08T07:00:00Z", "end_time": "2026-12-08T07:30:00Z"}
    created = client.post("/bookings", json=payload, headers={**EMPLOYEE, "Idempotency-Key": "k-8"}).json()
    response = client.patch(f"/bookings/{created['id']}/cancel", headers=EMPLOYEE)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_cancel_too_late_is_409(monkeypatch):
    """Поздняя отмена: часы фиксированы, чтобы тест не зависел от времени суток."""
    monkeypatch.setenv("ROOMBOOK_NOW", "2026-12-09T06:00:00Z")
    client = TestClient(build_app())
    payload = {"roomId": "r1", "start_time": "2026-12-09T06:30:00Z", "end_time": "2026-12-09T07:00:00Z"}
    created = client.post("/bookings", json=payload, headers={**EMPLOYEE, "Idempotency-Key": "k-9"})
    assert created.status_code == 201
    response = client.patch(f"/bookings/{created.json()['id']}/cancel", headers=EMPLOYEE)
    assert response.status_code == 409
    validate(response.json(), "Problem")
