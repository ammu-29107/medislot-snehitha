"""Focused regression tests for the consolidated Milestone 5 corrections."""

import importlib.util
import os
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Lambda modules initialise their AWS clients at import time. These inert
# credentials keep the unit tests offline; tests replace external calls when
# they need to exercise a handler.
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_USER", "test-user")
os.environ.setdefault("DB_PASSWORD_PARAMETER", "/test/database/password")
os.environ.setdefault("IDEMPOTENCY_TABLE", "test-idempotency")
os.environ.setdefault("SLOT_INVENTORY_TABLE", "test-slot-inventory")
os.environ.setdefault("SLOT_WINDOW_DAYS_PARAMETER", "/test/scheduling/slot-window-days")
os.environ.setdefault("FLASK_SECRET_KEY", "dashboard-test-secret")
os.environ.setdefault("MEDISLOT_API_BASE_URL", "https://example.test")
os.environ.setdefault("FLASK_SESSION_DIR", "/tmp/medislot-m5-correction-tests")

sys.path.insert(0, str(ROOT / "src" / "doctor"))


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


appointments_api = _load_module(
    "medislot_m5_appointments_api", ROOT / "src/appointments/api/app.py"
)
scheduling = _load_module(
    "medislot_m5_scheduling", ROOT / "src/doctor/common/scheduling.py"
)
from dashboard import app as dashboard_module


def test_refund_percentage_matches_policy_thresholds():
    now = datetime(2026, 11, 24, 10, 0)
    assert appointments_api.refund_percentage(now + timedelta(hours=49), now) == Decimal("100")
    assert appointments_api.refund_percentage(now + timedelta(hours=30), now) == Decimal("75")
    assert appointments_api.refund_percentage(now + timedelta(hours=12), now) == Decimal("50")
    assert appointments_api.refund_percentage(now + timedelta(hours=3), now) == Decimal("0")
    assert appointments_api.refund_percentage(now - timedelta(hours=1), now, no_show=True) == Decimal("0")
    assert appointments_api.refund_percentage(now + timedelta(hours=1), now, by_doctor=True) == Decimal("100")


def test_month_navigation_uses_calendar_months_not_thirty_day_steps():
    january = date(2026, 1, 1)
    february = dashboard_module.shift_calendar_month(january, 1)
    march = dashboard_module.shift_calendar_month(february, 1)
    assert february == date(2026, 2, 1)
    assert march == date(2026, 3, 1)
    assert dashboard_module.shift_calendar_month(january, -1) == date(2025, 12, 1)


class ScheduleCursor:
    """Small SQL cursor fake for the availability reconciliation helper."""

    def __init__(self, pattern, existing_slot):
        self.pattern = pattern
        self.existing_slot = existing_slot
        self.fetchall_result = []
        self.fetchone_result = None
        self.rowcount = 0
        self.lastrowid = 100
        self.inserted_slots = []
        self.statements = []

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.statements.append((compact, params))
        self.fetchall_result = []
        self.fetchone_result = None
        self.rowcount = 0

        if "FROM doctor_availability_patterns p" in compact:
            self.fetchall_result = [self.pattern]
        elif "FROM blackout_dates" in compact:
            self.fetchall_result = []
        elif "FROM slots" in compact and "FOR UPDATE" in compact:
            self.fetchall_result = [self.existing_slot]
        elif "FROM waitlist_entries" in compact:
            self.fetchall_result = []
        elif compact.startswith("SELECT inventory_version"):
            self.fetchone_result = {"inventory_version": 2}
        elif compact.startswith("INSERT INTO slots"):
            self.lastrowid += 1
            self.inserted_slots.append(params)
            self.rowcount = 1
        elif compact.startswith("UPDATE slots"):
            self.rowcount = 1
        elif compact.startswith("INSERT INTO slot_inventory_outbox"):
            self.rowcount = 1

    def fetchall(self):
        return self.fetchall_result

    def fetchone(self):
        return self.fetchone_result


def test_availability_change_redivides_future_slots_immediately():
    now = datetime(2026, 11, 24, 8, 0)
    pattern = {
        "pattern_id": 5,
        "day_of_week": now.weekday(),
        "start_time": time(9, 0),
        "end_time": time(10, 0),
        "consultation_type_id": 3,
        "capacity": 1,
        "duration_minutes": 30,
    }
    existing_slot = {
        "slot_id": 99,
        "pattern_id": 5,
        "consultation_type_id": 3,
        "start_at": datetime(2026, 11, 24, 9, 0),
        "end_at": datetime(2026, 11, 24, 10, 0),
        "capacity": 1,
        "booked_count": 0,
        "active_hold_count": 0,
        "status": "OPEN",
        "closure_reason": None,
    }
    cursor = ScheduleCursor(pattern, existing_slot)

    result = scheduling.reconcile_doctor_schedule(
        cursor, doctor_id=7, now=now, window_days=1
    )

    assert result["slots_cancelled"] == 1
    assert result["slots_created"] == 2
    assert [item[3] for item in cursor.inserted_slots] == [
        datetime(2026, 11, 24, 9, 0),
        datetime(2026, 11, 24, 9, 30),
    ]
    assert all(item[5] == 1 for item in cursor.inserted_slots)
