# Словарь данных — RoomBook

Все временные метки — `timestamptz` (UTC). Отображение и ввод — Europe/Moscow (UTC+3).
Данные синтетические: `user@example.com`, `Room-1`. Совпадения с реальными компаниями случайны.

## User

| Поле | Тип | Обязательность | Ограничения | Описание |
|---|---|---|---|---|
| `id` | uuid | да | PK, default gen_random_uuid() | идентификатор пользователя |
| `email` | varchar(255) | да | UNIQUE, NOT NULL, CHECK содержит `@` | логин сотрудника |
| `password_hash` | varchar(255) | да | NOT NULL | bcrypt-хеш с cost ≥ 10 |
| `role` | user_role | да | NOT NULL, default `EMPLOYEE` | роль: `EMPLOYEE` или `ADMIN` |
| `created_at` | timestamptz | да | default now() | дата создания записи |

## Room

| Поле | Тип | Обязательность | Ограничения | Описание |
|---|---|---|---|---|
| `id` | uuid | да | PK | идентификатор комнаты |
| `name` | varchar(120) | да | NOT NULL, CHECK length ≥ 1 | название (`Room-1`) |
| `capacity` | smallint | да | CHECK ≥ 1 | вместимость в человек |
| `equipment` | text[] | нет | default `{}` | оборудование: проектор, доска, ВКС |
| `is_active` | boolean | да | default true | участвует ли комната в поиске |
| `created_at` | timestamptz | да | default now() | дата создания |

## Booking

| Поле | Тип | Обязательность | Ограничения | Описание |
|---|---|---|---|---|
| `id` | uuid | да | PK | идентификатор брони |
| `user_id` | uuid | да | FK → User.id, ON DELETE RESTRICT | автор брони |
| `room_id` | uuid | да | FK → Room.id, ON DELETE RESTRICT | забронированная комната |
| `start_time` | timestamptz | да | CHECK кратно 30 мин, внутри 09:00–19:00 МСК | начало интервала (UTC) |
| `end_time` | timestamptz | да | CHECK `end_time > start_time` | конец интервала (UTC) |
| `status` | booking_status | да | NOT NULL, default `CONFIRMED` | `CONFIRMED` / `CANCELLED` |
| `idempotency_key` | uuid | да | UNIQUE | ключ идемпотентности из заголовка |
| `created_at` | timestamptz | да | default now() | момент создания |
| `cancelled_at` | timestamptz | нет | NULL, пока бронь активна | момент отмены |

## Перечисления

### `user_role`

| Значение | Смысл | Права |
|---|---|---|
| `EMPLOYEE` | сотрудник | просмотр комнат и слотов, создание и отмена своих броней, `GET /bookings/my` |
| `ADMIN` | администратор | всё, что EMPLOYEE, плюс админ-эндпоинты и отмена любой брони |

### `booking_status`

| Значение | Смысл | Переходы |
|---|---|---|
| `CONFIRMED` | бронь активна | → `CANCELLED` (отмена сотрудником по правилу, отмена админом) |
| `CANCELLED` | бронь отменена | конечный статус |

### Коды ошибок API (RFC 7807)

| HTTP | `type` (URI-фрагмент) | Когда |
|---|---|---|
| 400 | `missing-idempotency-key` | `POST /bookings` без заголовка `Idempotency-Key` |
| 401 | `unauthorized` | нет или истёк JWT |
| 403 | `forbidden` | роль не даёт прав (чужая бронь, `/admin/*` для EMPLOYEE) |
| 404 | `not-found` | комната или бронь не существует |
| 409 | `slot-taken` | пересечение с существующей CONFIRMED броней |
| 409 | `cancel-too-late` | отмена позже, чем за 1 час до начала |
| 409 | `already-cancelled` | повторная отмена |
| 422 | `invalid-time-range` | интервал вне рабочих часов или не кратен 30 мин |
| 422 | `invalid-period` | `from > to` в отчёте |
| 500 | `internal-error` | непредвиденная ошибка |

## Ограничения целостности и место проверки

| Ограничение | Где проверяется | Как |
|---|---|---|
| `end_time > start_time` | БД + API | CHECK в DDL и валидация схемы запроса (422) |
| Кратность 30 минутам и рабочие часы | API | валидация в `ValidationLayer` (422) |
| Нет двух CONFIRMED с пересечением по комнате | БД | транзакция с `SELECT … FOR UPDATE` по комнате; при PostgreSQL — `EXCLUDE USING gist (room_id WITH =, tstzrange(start_time, end_time) WITH &&) WHERE status = 'CONFIRMED'` |
| Отмена EMPLOYEE не позднее чем за 1 час | приложение | `BookingService`, сравнение `now()` и `start_time` (409) |
| Уникальность `idempotency_key` | БД | UNIQUE-индекс; повтор возвращает существующую бронь |
| Уникальность email | БД | UNIQUE-индекс |
| Роль и доступ к `/admin/*` | приложение | middleware проверки JWT и роли (403) |
