import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from railassist.domain.errors import RailAssistError
from railassist.domain.models import (
    Availability, OrderStatus, QuerySpec, TaskRecord, TaskStatus, Ticket, TicketSnapshot,
    WaitlistStatus, utc_now,
)
from railassist.domain.states import (
    require_order_transition, require_transition, require_waitlist_transition,
)

SCHEMA_VERSION = 4

_V2_TABLES = """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, config TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_result TEXT,
        next_run_at TEXT
    );
    CREATE TABLE IF NOT EXISTS state_events (
        id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
        from_state TEXT, to_state TEXT NOT NULL, reason_code TEXT, occurred_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ticket_snapshots (
        id INTEGER PRIMARY KEY,
        query_key TEXT NOT NULL, train_code TEXT NOT NULL, seat TEXT NOT NULL,
        availability TEXT NOT NULL, count INTEGER, price_fen INTEGER,
        departure_time TEXT, arrival_time TEXT,
        observed_at TEXT NOT NULL, source TEXT NOT NULL, validity TEXT NOT NULL DEFAULT 'VALID'
    );
    CREATE INDEX IF NOT EXISTS idx_snapshots_query ON ticket_snapshots(query_key, observed_at DESC);
    CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY,
        event_id TEXT NOT NULL, task_id TEXT, channel TEXT NOT NULL, message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING', retry_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL, delivered_at TEXT, created_at TEXT NOT NULL,
        UNIQUE(channel, event_id)
    );
    CREATE TABLE IF NOT EXISTS sale_times (
        station TEXT NOT NULL, date TEXT NOT NULL,
        sale_time TEXT, source TEXT NOT NULL, queried_at TEXT NOT NULL,
        manual INTEGER NOT NULL DEFAULT 0, trusted INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (station, date)
    );
"""

_V3_TABLES = """
    CREATE TABLE IF NOT EXISTS authorizations (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_revision TEXT NOT NULL,
        actions TEXT NOT NULL, passenger_refs TEXT NOT NULL, candidate_scope TEXT NOT NULL,
        max_total_amount_fen INTEGER NOT NULL, max_prepayment_fen INTEGER NOT NULL,
        allow_no_seat INTEGER NOT NULL DEFAULT 0, accept_added_trains INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT, confirmed_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_authorizations_task ON authorizations(task_id, confirmed_at DESC);
    CREATE TABLE IF NOT EXISTS order_attempts (
        id TEXT PRIMARY KEY,
        goal_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        action TEXT NOT NULL, status TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_checked_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_attempts_goal ON order_attempts(goal_id, created_at DESC);
    -- 幂等键与购票目标互斥均只约束"活动"尝试：终态后允许重新下单（设计文档 8）。
    CREATE UNIQUE INDEX IF NOT EXISTS ux_active_idempotency ON order_attempts(idempotency_key)
        WHERE status IN ('PREPARED','SUBMITTING','QUEUED','PENDING_PAYMENT',
                         'OUTCOME_UNKNOWN','RECONCILING','NEEDS_USER_ACTION',
                         'WAITLIST_PENDING_PAYMENT','WAITLIST_ACTIVE');
    -- 同一购票目标同时只允许一个未终结的订单流程（含候补），数据库层强制互斥。
    CREATE UNIQUE INDEX IF NOT EXISTS ux_active_attempt_per_goal ON order_attempts(goal_id)
        WHERE status IN ('PREPARED','SUBMITTING','QUEUED','PENDING_PAYMENT',
                         'OUTCOME_UNKNOWN','RECONCILING','NEEDS_USER_ACTION',
                         'WAITLIST_PENDING_PAYMENT','WAITLIST_ACTIVE');
    CREATE TABLE IF NOT EXISTS order_events (
        id INTEGER PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES order_attempts(id),
        from_state TEXT, to_state TEXT NOT NULL, reason_code TEXT, occurred_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS waitlist_orders (
        attempt_id TEXT PRIMARY KEY REFERENCES order_attempts(id),
        combination TEXT NOT NULL, prepayment_fen INTEGER, deadline TEXT,
        refund_status TEXT
    );
    CREATE TABLE IF NOT EXISTS capabilities (
        name TEXT PRIMARY KEY, adapter TEXT NOT NULL, verified INTEGER NOT NULL,
        verified_at TEXT, source TEXT
    );
"""

_ACTIVE_ATTEMPT_STATUSES = {
    OrderStatus.PREPARED, OrderStatus.SUBMITTING, OrderStatus.QUEUED,
    OrderStatus.PENDING_PAYMENT, OrderStatus.OUTCOME_UNKNOWN, OrderStatus.RECONCILING,
    OrderStatus.NEEDS_USER_ACTION, WaitlistStatus.WAITLIST_PENDING_PAYMENT,
    WaitlistStatus.WAITLIST_ACTIVE,
}

# 这些失败原因能确定“从未触达官方提交动作”，因此可以安全地本地取消/删除：
# - prepare_error:*      打开/核对确认页阶段就失败（未到达提交按钮）
# - needs_user_prepare   确认页阶段转人工（席别不符、金额读不到、页面过期等）
# - dry_run              演练模式，设计上就停在提交之前
# - user_abandoned       用户主动放弃
_NEVER_SENT_REASON_PREFIXES = ("prepare_error", "needs_user_prepare", "dry_run", "user_abandoned")
# 注意 "dry_run" 前缀同时覆盖 dry_run 与 dry_run_failed


class SQLiteTaskRepository:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")  # GUI 与后台线程并发读写
        except sqlite3.DatabaseError:
            pass
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.connection.close()
            raise RailAssistError("数据库版本高于当前程序，请升级程序。")
        if version == SCHEMA_VERSION:
            return
        with self.connection:
            if version == 0:
                self.connection.executescript(f"""
                    BEGIN;
                    {_V2_TABLES}
                    {_V3_TABLES}
                    PRAGMA user_version={SCHEMA_VERSION};
                    COMMIT;
                """)
            elif version == 1:
                self.connection.executescript(f"""
                    BEGIN;
                    ALTER TABLE tasks ADD COLUMN next_run_at TEXT;
                    ALTER TABLE state_events ADD COLUMN reason_code TEXT;
                    {_V2_TABLES}
                    {_V3_TABLES}
                    PRAGMA user_version={SCHEMA_VERSION};
                    COMMIT;
                """)
            elif version == 2:
                self.connection.executescript(f"""
                    BEGIN;
                    {_V3_TABLES}
                    PRAGMA user_version={SCHEMA_VERSION};
                    COMMIT;
                """)
            elif version == 3:
                # v3→v4：order_attempts 的幂等键从全表 UNIQUE 改为仅约束活动尝试。
                self.connection.executescript("""
                    PRAGMA foreign_keys=OFF;
                    CREATE TABLE order_attempts_v4 (
                        id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                        action TEXT NOT NULL, status TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_checked_at TEXT
                    );
                    INSERT INTO order_attempts_v4
                        SELECT id, goal_id, idempotency_key, action, status, payload,
                               created_at, updated_at, last_checked_at
                        FROM order_attempts;
                    DROP TABLE order_attempts;
                    ALTER TABLE order_attempts_v4 RENAME TO order_attempts;
                    PRAGMA user_version=4;
                """)
                self.connection.executescript(_V3_TABLES)  # 重建活动互斥/幂等部分索引
                self.connection.execute("PRAGMA foreign_keys=ON")

    def _decode(self, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"], config=json.loads(row["config"]), status=TaskStatus(row["status"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            next_run_at=row["next_run_at"],
            last_result=json.loads(row["last_result"]) if row["last_result"] else None,
        )

    def create(self, config: dict) -> TaskRecord:
        task_id, now = uuid4().hex, utc_now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO tasks(id, config, status, created_at, updated_at, last_result, next_run_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (task_id, json.dumps(config, ensure_ascii=False), TaskStatus.READY, now, now, None),
            )
            self.connection.execute(
                "INSERT INTO state_events(task_id, from_state, to_state, occurred_at) VALUES (?, ?, ?, ?)",
                (task_id, None, TaskStatus.READY, now),
            )
        return self.get(task_id)

    def get(self, task_id: str) -> TaskRecord:
        row = self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise RailAssistError(f"任务不存在：{task_id}")
        return self._decode(row)

    def list_tasks(self) -> list[TaskRecord]:
        return [self._decode(row) for row in self.connection.execute("SELECT * FROM tasks ORDER BY created_at")]

    def update(self, task_id: str, status: TaskStatus, result: dict | None = None,
               reason_code: str | None = None) -> TaskRecord:
        current = self.get(task_id)
        require_transition(current.status, status)
        now = utc_now()
        payload = current.last_result if result is None else result
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET status=?, updated_at=?, last_result=? WHERE id=?",
                (status, now, json.dumps(payload, ensure_ascii=False) if payload is not None else None, task_id),
            )
            self.connection.execute(
                "INSERT INTO state_events(task_id, from_state, to_state, reason_code, occurred_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, current.status, status, reason_code, now),
            )
        return self.get(task_id)

    def set_next_run(self, task_id: str, next_run_at: str | None) -> None:
        with self.connection:
            self.connection.execute("UPDATE tasks SET next_run_at=? WHERE id=?", (next_run_at, task_id))

    def save_snapshot(self, snapshot: TicketSnapshot) -> None:
        rows = [
            (snapshot.query.query_key, ticket.train_code, ticket.seat, ticket.availability.value,
             ticket.count, ticket.price_fen, ticket.departure_time, ticket.arrival_time,
             snapshot.observed_at, snapshot.source, snapshot.validity)
            for ticket in snapshot.tickets
        ]
        with self.connection:
            self.connection.executemany(
                "INSERT INTO ticket_snapshots(query_key, train_code, seat, availability, count, price_fen, "
                "departure_time, arrival_time, observed_at, source, validity) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def latest_snapshots(self, query_keys: tuple[str, ...]) -> dict[str, TicketSnapshot]:
        """每个查询键只取最近一次 observed_at 的完整快照。"""
        result: dict[str, TicketSnapshot] = {}
        for key in query_keys:
            header = self.connection.execute(
                "SELECT query_key, observed_at, source, validity FROM ticket_snapshots "
                "WHERE query_key=? ORDER BY observed_at DESC LIMIT 1", (key,),
            ).fetchone()
            if header is None:
                continue
            rows = self.connection.execute(
                "SELECT train_code, seat, availability, count, price_fen, departure_time, arrival_time "
                "FROM ticket_snapshots WHERE query_key=? AND observed_at=?",
                (key, header["observed_at"]),
            ).fetchall()
            tickets = tuple(
                Ticket(row["train_code"], row["seat"], Availability(row["availability"]),
                       row["count"], row["price_fen"], row["departure_time"] or "", row["arrival_time"] or "")
                for row in rows
            )
            result[key] = TicketSnapshot(
                query=QuerySpec.from_query_key(key), tickets=tickets,
                observed_at=header["observed_at"], source=header["source"], validity=header["validity"],
            )
        return result

    def save_sale_time(self, station: str, date: str, sale_time: str | None, source: str,
                       manual: bool = False, trusted: bool = True) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO sale_times(station, date, sale_time, source, queried_at, manual, trusted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(station, date) DO UPDATE SET sale_time=excluded.sale_time, "
                "source=excluded.source, queried_at=excluded.queried_at, "
                "manual=excluded.manual, trusted=excluded.trusted",
                (station, date, sale_time, source, utc_now(), int(manual), int(trusted)),
            )

    def get_sale_time(self, station: str, date: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM sale_times WHERE station=? AND date=?", (station, date),
        ).fetchone()
        if row is None:
            return None
        return self._decode_sale_time(row)

    def list_sale_times(self) -> list[dict]:
        rows = self.connection.execute("SELECT * FROM sale_times ORDER BY date, station").fetchall()
        return [self._decode_sale_time(row) for row in rows]

    @staticmethod
    def _decode_sale_time(row: sqlite3.Row) -> dict:
        return {
            "station": row["station"], "date": row["date"], "sale_time": row["sale_time"],
            "source": row["source"], "queried_at": row["queried_at"],
            "manual": bool(row["manual"]), "trusted": bool(row["trusted"]),
        }

    # ---------- 授权快照 ----------

    def save_authorization(self, record) -> dict:
        with self.connection:
            self.connection.execute(
                "INSERT INTO authorizations(id, task_id, task_revision, actions, passenger_refs, "
                "candidate_scope, max_total_amount_fen, max_prepayment_fen, allow_no_seat, "
                "accept_added_trains, expires_at, confirmed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (record.id, record.task_id, record.task_revision,
                 json.dumps(list(record.actions)), json.dumps(list(record.passenger_refs)),
                 json.dumps(record.candidate_scope, ensure_ascii=False),
                 record.max_total_amount_fen, record.max_prepayment_fen,
                 int(record.allow_no_seat), int(record.accept_added_trains),
                 record.expires_at, record.confirmed_at),
            )
        return self.get_authorization(record.id)

    def get_authorization(self, auth_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM authorizations WHERE id=?", (auth_id,)).fetchone()
        if row is None:
            raise RailAssistError(f"授权记录不存在：{auth_id}")
        return self._decode_authorization(row)

    def latest_authorization(self, task_id: str, action: str) -> dict | None:
        rows = self.connection.execute(
            "SELECT * FROM authorizations WHERE task_id=? ORDER BY confirmed_at DESC", (task_id,),
        ).fetchall()
        for row in rows:
            if action in json.loads(row["actions"]):
                return self._decode_authorization(row)
        return None

    @staticmethod
    def _decode_authorization(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "task_id": row["task_id"], "task_revision": row["task_revision"],
            "actions": json.loads(row["actions"]), "passenger_refs": json.loads(row["passenger_refs"]),
            "candidate_scope": json.loads(row["candidate_scope"]),
            "max_total_amount_fen": row["max_total_amount_fen"],
            "max_prepayment_fen": row["max_prepayment_fen"],
            "allow_no_seat": bool(row["allow_no_seat"]),
            "accept_added_trains": bool(row["accept_added_trains"]),
            "expires_at": row["expires_at"], "confirmed_at": row["confirmed_at"],
        }

    # ---------- 订单尝试 ----------

    def create_attempt(self, goal_id: str, idempotency_key: str, action: str,
                       status: str, payload: dict) -> dict:
        attempt_id, now = uuid4().hex, utc_now()
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO order_attempts(id, goal_id, idempotency_key, action, status, "
                    "payload, created_at, updated_at, last_checked_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (attempt_id, goal_id, idempotency_key, action, status,
                     json.dumps(payload, ensure_ascii=False), now, now),
                )
                self.connection.execute(
                    "INSERT INTO order_events(attempt_id, from_state, to_state, occurred_at) "
                    "VALUES (?, NULL, ?, ?)", (attempt_id, status, now),
                )
        except sqlite3.IntegrityError as exc:
            raise RailAssistError(self._goal_conflict_message(goal_id)) from exc
        return self.get_attempt(attempt_id)

    def create_booking_attempt(self, task_id: str, goal_id: str, idempotency_key: str,
                               action: str, payload: dict) -> dict:
        """Atomically move a task to BOOKING and create its PREPARED attempt."""
        attempt_id, now = uuid4().hex, utc_now()
        try:
            with self.connection:
                row = self.connection.execute(
                    "SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row is None:
                    raise RailAssistError(f"任务不存在：{task_id}")
                current = TaskStatus(row["status"])
                path: list[TaskStatus] = []
                if current in (TaskStatus.READY, TaskStatus.WAITING_SALE):
                    path.append(TaskStatus.MONITORING)
                if current is not TaskStatus.BOOKING:
                    path.append(TaskStatus.BOOKING)
                state = current
                for target in path:
                    require_transition(state, target)
                    self.connection.execute(
                        "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                        (target.value, now, task_id))
                    self.connection.execute(
                        "INSERT INTO state_events(task_id, from_state, to_state, reason_code, occurred_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (task_id, state.value, target.value, "booking_started", now))
                    state = target
                self.connection.execute(
                    "INSERT INTO order_attempts(id, goal_id, idempotency_key, action, status, "
                    "payload, created_at, updated_at, last_checked_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (attempt_id, goal_id, idempotency_key, action, OrderStatus.PREPARED.value,
                     json.dumps(payload, ensure_ascii=False), now, now),
                )
                self.connection.execute(
                    "INSERT INTO order_events(attempt_id, from_state, to_state, occurred_at) "
                    "VALUES (?, NULL, ?, ?)", (attempt_id, OrderStatus.PREPARED.value, now),
                )
        except sqlite3.IntegrityError as exc:
            raise RailAssistError(self._goal_conflict_message(goal_id)) from exc
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM order_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise RailAssistError(f"订单尝试不存在：{attempt_id}")
        return self._decode_attempt(row)

    def list_attempts(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM order_attempts"
        if active_only:
            marks = ",".join("?" for _ in _ACTIVE_ATTEMPT_STATUSES)
            sql += f" WHERE status IN ({marks})"
            rows = self.connection.execute(sql, tuple(s.value for s in _ACTIVE_ATTEMPT_STATUSES)).fetchall()
        else:
            rows = self.connection.execute(sql + " ORDER BY created_at DESC").fetchall()
        return [self._decode_attempt(row) for row in rows]

    def update_attempt(self, attempt_id: str, target_status: str, reason_code: str | None = None,
                       payload_patch: dict | None = None, bump_check: bool = False) -> dict:
        current = self.get_attempt(attempt_id)
        if current["action"] == "waitlist":
            require_waitlist_transition(WaitlistStatus(current["status"]), WaitlistStatus(target_status))
        else:
            require_order_transition(OrderStatus(current["status"]), OrderStatus(target_status))
        now = utc_now()
        payload = dict(current["payload"])
        if payload_patch:
            payload.update(payload_patch)
        last_checked = now if bump_check else current["last_checked_at"]
        with self.connection:
            self.connection.execute(
                "UPDATE order_attempts SET status=?, payload=?, updated_at=?, last_checked_at=? WHERE id=?",
                (target_status, json.dumps(payload, ensure_ascii=False), now, last_checked, attempt_id),
            )
            self.connection.execute(
                "INSERT INTO order_events(attempt_id, from_state, to_state, reason_code, occurred_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (attempt_id, current["status"], target_status, reason_code, now),
            )
        return self.get_attempt(attempt_id)

    @staticmethod
    def _decode_attempt(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "goal_id": row["goal_id"], "idempotency_key": row["idempotency_key"],
            "action": row["action"], "status": row["status"],
            "payload": json.loads(row["payload"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "last_checked_at": row["last_checked_at"],
        }

    # ---------- 候补明细 ----------

    def save_waitlist_detail(self, attempt_id: str, combination: dict, prepayment_fen: int | None,
                             deadline: str | None, refund_status: str | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO waitlist_orders(attempt_id, combination, prepayment_fen, deadline, refund_status) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(attempt_id) DO UPDATE SET "
                "combination=excluded.combination, prepayment_fen=excluded.prepayment_fen, "
                "deadline=excluded.deadline, refund_status=excluded.refund_status",
                (attempt_id, json.dumps(combination, ensure_ascii=False),
                 prepayment_fen, deadline, refund_status),
            )

    def get_waitlist_detail(self, attempt_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM waitlist_orders WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            return None
        return {
            "attempt_id": row["attempt_id"], "combination": json.loads(row["combination"]),
            "prepayment_fen": row["prepayment_fen"], "deadline": row["deadline"],
            "refund_status": row["refund_status"],
        }

    def update_waitlist_refund(self, attempt_id: str, refund_status: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE waitlist_orders SET refund_status=? WHERE attempt_id=?",
                (refund_status, attempt_id),
            )

    # ---------- 能力登记 ----------

    def set_capability(self, name: str, adapter: str, verified: bool, source: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO capabilities(name, adapter, verified, verified_at, source) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "adapter=excluded.adapter, verified=excluded.verified, "
                "verified_at=excluded.verified_at, source=excluded.source",
                (name, adapter, int(verified), utc_now(), source),
            )

    def list_capabilities(self) -> list[dict]:
        rows = self.connection.execute("SELECT * FROM capabilities ORDER BY name").fetchall()
        return [{
            "name": row["name"], "adapter": row["adapter"], "verified": bool(row["verified"]),
            "verified_at": row["verified_at"], "source": row["source"],
        } for row in rows]

    # ---------- 删除 ----------

    def delete_task(self, task_id: str) -> None:
        """删除任务及其状态事件与授权记录；不触碰订单尝试（其关联按 task_id 由调用方处理）。"""
        with self.connection:
            self.connection.execute("DELETE FROM state_events WHERE task_id=?", (task_id,))
            self.connection.execute("DELETE FROM authorizations WHERE task_id=?", (task_id,))
            cursor = self.connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        if cursor.rowcount == 0:
            raise RailAssistError(f"任务不存在：{task_id}")

    def _goal_conflict_message(self, goal_id: str) -> str:
        """购票目标冲突时给出可操作的信息：点名是哪条记录挡住了重试。"""
        active = [status.value for status in _ACTIVE_ATTEMPT_STATUSES]
        placeholders = ",".join("?" * len(active))
        rows = self.connection.execute(
            "SELECT id, status FROM order_attempts WHERE goal_id=? "
            f"AND status IN ({placeholders})", (goal_id, *active)).fetchall()
        detail = "、".join(f"{row['id'][:8]}({row['status']})" for row in rows)
        return ("该购票目标（" + goal_id + "）已有未决订单"
                + ("：" + detail if detail else "（幂等键重复）")
                + "；请先处理该记录再重试（未向官方提交过的可用 order cancel 本地取消）。")

    def attempt_may_have_created_order(self, attempt_id: str) -> bool:
        """该尝试是否**可能**已在官方侧产生订单（决定能否安全本地取消/删除）。

        设计上“先落库 SUBMITTING，再执行提交动作”（崩溃恢复需要），所以只要
        进入过 SUBMITTING 就必须保守处理；只有“最后一次状态变更的失败原因”
        明确属于“从未触达官方提交动作”的那几类（见 _NEVER_SENT_REASON_PREFIXES），
        才能确定官方侧不可能有订单，可以安全本地取消/删除。
        """
        entered_submitting = self.connection.execute(
            "SELECT 1 FROM order_events WHERE attempt_id=? AND to_state='SUBMITTING' LIMIT 1",
            (attempt_id,)).fetchone() is not None
        if not entered_submitting:
            return False                     # 从未进入提交流程
        row = self.connection.execute(
            "SELECT reason_code FROM order_events WHERE attempt_id=? ORDER BY id DESC LIMIT 1",
            (attempt_id,)).fetchone()
        reason = (row["reason_code"] or "") if row is not None else ""
        return not any(reason.startswith(prefix) for prefix in _NEVER_SENT_REASON_PREFIXES)

    def delete_attempt(self, attempt_id: str) -> None:
        """删除订单尝试记录（含事件与候补明细）；不取消任何官方订单。"""
        attempt = self.get_attempt(attempt_id)
        if (attempt["status"] in {status.value for status in _ACTIVE_ATTEMPT_STATUSES}
                and self.attempt_may_have_created_order(attempt_id)):
            raise RailAssistError(
                "该订单记录可能已在官方产生订单，不能删除；请先通过官方订单页核对到明确终态。")
        with self.connection:
            self.connection.execute("DELETE FROM order_events WHERE attempt_id=?", (attempt_id,))
            self.connection.execute("DELETE FROM waitlist_orders WHERE attempt_id=?", (attempt_id,))
            cursor = self.connection.execute("DELETE FROM order_attempts WHERE id=?", (attempt_id,))
        if cursor.rowcount == 0:
            raise RailAssistError(f"订单尝试不存在：{attempt_id}")

    def close(self) -> None:
        self.connection.close()
