from __future__ import annotations

import threading
from datetime import UTC, datetime

from app.core.clock import FrozenClock
from app.database import get_connection
from app.network.rules import DEFAULT_RULES
from app.network.service import NetworkAccelerationService


SUBSCRIBER = "subscriber-migration-0000001"
ENTITLEMENT_WINDOW = ("2026-01-01T00:00:00Z", "2031-01-01T00:00:00Z")


def prepare(client, capacities=(1000, 1000, 1000, 100)):
    client.post(
        "/api/network/scenarios",
        json={"code": "rail-300", "name": "三百公里高铁", "scene_type": "railway", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 100, "capacity_mbps": 5000},
    )
    for sequence, (code, name, capacity) in enumerate(
        (
            ("seg-a", "甲区段", capacities[0]),
            ("seg-b", "乙区段", capacities[1]),
            ("seg-c", "丙区段", capacities[2]),
            ("seg-d", "丁区段", capacities[3]),
        ),
        start=1,
    ):
        response = client.post(
            "/api/network/scenarios/rail-300/segments",
            json={"code": code, "name": name, "sequence_no": sequence, "expected_dwell_seconds": 300, "capacity_mbps": capacity},
        )
        assert response.status_code == 201, response.text
    client.post(
        "/api/network/applications",
        json={"app_code": "video-call", "name": "视频通话", "category": "video_call", "latency_target_ms": 100, "packet_loss_target": 0.01, "min_downlink_mbps": 8, "min_uplink_mbps": 4, "default_priority": 70},
    )
    policy = client.post("/api/network/scenarios/rail-300/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    client.post(f"/api/network/policies/{policy['id']}/publish", json={"actor": "tests", "effective_from": "2026-01-01T00:00:00Z"})
    client.post(
        "/api/network/entitlements",
        json={
            "subscriber_hash": SUBSCRIBER,
            "scenario_code": "rail-300",
            "product_code": "rail-boost-year",
            "valid_from": ENTITLEMENT_WINDOW[0],
            "valid_until": ENTITLEMENT_WINDOW[1],
            "source_order_id": "migration-order-0001",
        },
    )


def start_session(client, sample_key="mig-sample-0001", segment="seg-a", speed=300):
    sample = client.post(
        "/api/network/samples",
        json={
            "sample_key": sample_key,
            "scenario_code": "rail-300",
            "segment_code": segment,
            "app_code": "video-call",
            "subscriber_hash": SUBSCRIBER,
            "device_class": "phone",
            "train_speed_kmh": speed,
            "latency_ms": 350,
            "packet_loss": 0.08,
            "downlink_mbps": 1.5,
            "uplink_mbps": 0.5,
            "observed_at": "2026-09-27T03:00:00Z",
        },
    ).json()
    started = client.post(f"/api/network/incidents/{sample['incident_id']}/accelerate", json={"actor": "tests"})
    assert started.status_code == 200, started.text
    return started.json()


def migrate(client, session_id, segment_code, observed_at, observation_key, *, expected=200):
    response = client.post(
        f"/api/network/sessions/{session_id}/migrations",
        json={"segment_code": segment_code, "observed_at": observed_at, "observation_key": observation_key, "actor": "dispatcher"},
    )
    assert response.status_code == expected, response.text
    return response.json()


def held_by_segment(connection, scenario_code="rail-300"):
    rows = connection.execute(
        "SELECT g.code AS code, COUNT(*) AS n, COALESCE(SUM(r.downlink_mbps),0) AS downlink "
        "FROM capacity_reservations r JOIN network_scenarios n ON n.id=r.scenario_id "
        "LEFT JOIN network_segments g ON g.id=r.segment_id "
        "WHERE n.code=? AND r.state='held' GROUP BY g.code",
        (scenario_code,),
    ).fetchall()
    return {row["code"]: {"sessions": row["n"], "downlink": float(row["downlink"])} for row in rows}


def test_forward_migration_releases_old_and_holds_new_in_one_transaction(client):
    prepare(client)
    session = start_session(client)
    connection = get_connection()
    assert held_by_segment(connection) == {"seg-a": {"sessions": 1, "downlink": 16.0}}

    result = migrate(client, session["id"], "seg-b", "2026-09-27T03:02:00Z", "obs-0001")
    assert result["migration"]["result"] == "migrated"
    assert result["migration"]["direction"] == "forward"
    assert result["session"]["segment_id"] != session["segment_id"]
    assert result["session"]["segment"]["code"] == "seg-b"
    assert held_by_segment(connection) == {"seg-b": {"sessions": 1, "downlink": 16.0}}

    detail = client.get(f"/api/network/sessions/{session['id']}").json()
    ledger = detail["reservations"]
    assert [(item["state"], item["segment_id"] is not None) for item in ledger] == [("released", True), ("held", True)]
    migrations = detail["migrations"]
    assert len(migrations) == 1
    record = migrations[0]
    assert record["from_segment_code"] == "seg-a"
    assert record["to_segment_code"] == "seg-b"
    assert record["observed_at"] == "2026-09-27T03:02:00+00:00"
    assert record["result"] == "migrated"
    assert [event["event_type"] for event in detail["events"]] == ["started", "segment_migrated"]
    event_detail = detail["events"][-1]["detail"]
    assert event_detail["from_segment"] == "seg-a"
    assert event_detail["to_segment"] == "seg-b"
    assert event_detail["observation_key"] == "obs-0001"


def test_duplicate_observation_is_idempotent_and_does_not_double_charge(client):
    prepare(client)
    session = start_session(client)
    payload = ("seg-b", "2026-09-27T03:02:00Z", "obs-dup-01")
    first = migrate(client, session["id"], *payload)
    connection = get_connection()
    assert held_by_segment(connection) == {"seg-b": {"sessions": 1, "downlink": 16.0}}
    second = migrate(client, session["id"], *payload)
    assert second["migration"]["id"] == first["migration"]["id"]
    assert held_by_segment(connection) == {"seg-b": {"sessions": 1, "downlink": 16.0}}
    assert len(client.get(f"/api/network/sessions/{session['id']}").json()["migrations"]) == 1

    # 即使重复上报指向不同的当前状态，也不能再次扣减容量或产生新记录
    migrate(client, session["id"], "seg-c", "2026-09-27T03:04:00Z", "obs-dup-02")
    repeat_first = migrate(client, session["id"], *payload)
    assert repeat_first["migration"]["id"] == first["migration"]["id"]
    assert held_by_segment(connection) == {"seg-c": {"sessions": 1, "downlink": 16.0}}


def test_skip_ahead_is_allowed_but_out_of_order_observation_cannot_move_backward(client):
    prepare(client)
    session = start_session(client)

    skipped = migrate(client, session["id"], "seg-c", "2026-09-27T03:05:00Z", "obs-skip-01")
    assert skipped["migration"]["result"] == "migrated"
    assert skipped["migration"]["direction"] == "skip"
    assert skipped["session"]["segment"]["code"] == "seg-c"

    # 迟到的旧观测（观测时间早于已处理锚点）不能把会话拉回 seg-b
    stale = migrate(client, session["id"], "seg-b", "2026-09-27T03:03:00Z", "obs-late-01")
    assert stale["migration"]["result"] == "rejected"
    assert stale["migration"]["direction"] == "rewind"
    assert stale["migration"]["reason"] == "stale_observation"
    assert stale["session"]["segment"]["code"] == "seg-c"

    # 直接回退两个区段永远拒绝
    backward = migrate(client, session["id"], "seg-a", "2026-09-27T03:06:00Z", "obs-back-01")
    assert backward["migration"]["result"] == "rejected"
    assert backward["migration"]["direction"] == "backward"
    assert backward["migration"]["reason"] == "backward_movement_forbidden"
    assert backward["session"]["segment"]["code"] == "seg-c"


def test_short_rewind_inside_tolerance_window_is_accepted(client):
    prepare(client)
    session = start_session(client)
    migrate(client, session["id"], "seg-b", "2026-09-27T03:02:00Z", "obs-rw-01")
    rewind = migrate(client, session["id"], "seg-a", "2026-09-27T03:03:30Z", "obs-rw-02")
    assert rewind["migration"]["result"] == "migrated"
    assert rewind["migration"]["direction"] == "rewind"
    assert rewind["session"]["segment"]["code"] == "seg-a"

    # 回摆后再前进，锚点应推进到回摆观测
    forward = migrate(client, session["id"], "seg-b", "2026-09-27T03:04:30Z", "obs-rw-03")
    assert forward["migration"]["result"] == "migrated"
    assert forward["migration"]["direction"] == "forward"
    assert forward["session"]["segment"]["code"] == "seg-b"


def test_rewind_after_tolerance_window_is_rejected_and_capacity_stays(client):
    prepare(client)
    session = start_session(client)
    connection = get_connection()
    migrate(client, session["id"], "seg-b", "2026-09-27T03:02:00Z", "obs-late-rw-01")
    late = migrate(client, session["id"], "seg-a", "2026-09-27T03:05:00Z", "obs-late-rw-02")
    assert late["migration"]["result"] == "rejected"
    assert late["migration"]["reason"] == "rewind_window_closed"
    assert late["session"]["segment"]["code"] == "seg-b"
    assert held_by_segment(connection) == {"seg-b": {"sessions": 1, "downlink": 16.0}}


def test_capacity_shortage_keeps_explainable_state_and_old_reservation(client):
    prepare(client, capacities=(1000, 1000, 1000, 10))
    session = start_session(client)
    connection = get_connection()
    # seg-d 只有 10Mbps，无法容纳 12Mbps 的加速预留
    denied = migrate(client, session["id"], "seg-d", "2026-09-27T03:02:00Z", "obs-cap-01")
    assert denied["migration"]["result"] == "rejected"
    assert denied["migration"]["direction"] == "skip"
    assert denied["migration"]["reason"] == "target_capacity_insufficient"
    assert denied["session"]["segment"]["code"] == "seg-a"
    assert held_by_segment(connection) == {"seg-a": {"sessions": 1, "downlink": 16.0}}

    session_detail = client.get(f"/api/network/sessions/{session['id']}").json()
    record = session_detail["migrations"][-1]
    assert record["result"] == "rejected"
    assert record["detail"]["capacity_mbps"] == 10
    assert record["detail"]["requested_downlink_mbps"] == 16.0
    assert record["detail"]["held_downlink_mbps"] == 0.0
    # 拒绝后仍可继续迁到容量充足的区段
    recovered = migrate(client, session["id"], "seg-b", "2026-09-27T03:03:00Z", "obs-cap-02")
    assert recovered["migration"]["result"] == "migrated"
    assert held_by_segment(connection) == {"seg-b": {"sessions": 1, "downlink": 16.0}}


def test_total_capacity_conserved_across_fixed_migration_sequence(client):
    """固定序列 API 请求验证跨区段总容量守恒：
    每次迁移后全场景 held 下行容量恒等于单个会话的 12Mbps。"""
    prepare(client)
    session = start_session(client)
    connection = get_connection()

    def total_held():
        return float(
            connection.execute(
                "SELECT COALESCE(SUM(downlink_mbps),0) FROM capacity_reservations r "
                "JOIN network_scenarios n ON n.id=r.scenario_id WHERE n.code='rail-300' AND r.state='held'"
            ).fetchone()[0]
        )

    assert total_held() == 16.0
    sequence = [
        ("seg-b", "2026-09-27T03:02:00Z", "obs-seq-01", "migrated"),
        ("seg-c", "2026-09-27T03:04:00Z", "obs-seq-02", "migrated"),
        ("seg-b", "2026-09-27T03:04:30Z", "obs-seq-03", "migrated"),
        ("seg-a", "2026-09-27T03:20:00Z", "obs-seq-04", "rejected"),
        ("seg-a", "2026-09-27T03:04:40Z", "obs-seq-05", "migrated"),
        ("seg-d", "2026-09-27T03:03:00Z", "obs-seq-06", "rejected"),
        ("seg-c", "2026-09-27T03:05:00Z", "obs-seq-07", "migrated"),
        ("seg-b", "2026-09-27T03:05:30Z", "obs-seq-08", "migrated"),
        ("seg-b", "2026-09-27T03:05:30Z", "obs-seq-08", "migrated"),
    ]
    for segment, observed, key, expected_result in sequence:
        result = migrate(client, session["id"], segment, observed, key)
        assert result["migration"]["result"] == expected_result, (segment, observed, result["migration"])
        assert total_held() == 16.0

    assert result["session"]["segment"]["code"] == "seg-b"

    snapshot = client.get("/api/network/analytics/capacity").json()["items"]
    held_total = round(sum(item["held_downlink_mbps"] for item in snapshot if item["scenario_code"] == "rail-300"), 3)
    assert held_total == 16.0
    active = [item for item in snapshot if item["active_sessions"]]
    assert len(active) == 1
    assert active[0]["segment_code"] == "seg-b"


def test_same_segment_observation_is_unchanged_without_capacity_change(client):
    prepare(client)
    session = start_session(client)
    connection = get_connection()
    result = migrate(client, session["id"], "seg-a", "2026-09-27T03:01:00Z", "obs-stay-01")
    assert result["migration"]["result"] == "unchanged"
    assert result["migration"]["direction"] == "stay"
    assert result["session"]["segment"]["code"] == "seg-a"
    assert held_by_segment(connection) == {"seg-a": {"sessions": 1, "downlink": 16.0}}
    detail = client.get(f"/api/network/sessions/{session['id']}").json()
    assert [event["event_type"] for event in detail["events"]] == ["started"]

    # 停滞观测推进时间锚点：窗口内回摆以最近一次停滞观测为准
    rewind = migrate(client, session["id"], "seg-b", "2026-09-27T03:01:30Z", "obs-stay-02")
    # seg-a -> seg-b 是前进，不是回摆
    assert rewind["migration"]["result"] == "migrated"
    back = migrate(client, session["id"], "seg-a", "2026-09-27T03:02:00Z", "obs-stay-03")
    assert back["migration"]["result"] == "migrated"
    assert back["migration"]["direction"] == "rewind"

    # 旧于锚点的停滞观测拒绝且不动容量
    stale_stay = migrate(client, session["id"], "seg-a", "2026-09-27T03:00:30Z", "obs-stay-04")
    assert stale_stay["migration"]["result"] == "rejected"
    assert stale_stay["migration"]["reason"] == "stale_observation"
    assert held_by_segment(connection) == {"seg-a": {"sessions": 1, "downlink": 16.0}}


def test_unknown_segment_and_invalid_time_are_rejected(client):
    prepare(client)
    session = start_session(client)
    missing = client.post(
        f"/api/network/sessions/{session['id']}/migrations",
        json={"segment_code": "seg-x", "observed_at": "2026-09-27T03:02:00Z", "observation_key": "obs-miss-01", "actor": "dispatcher"},
    )
    assert missing.status_code == 404
    bad_time = client.post(
        f"/api/network/sessions/{session['id']}/migrations",
        json={"segment_code": "seg-b", "observed_at": "not-a-time", "observation_key": "obs-bad-01", "actor": "dispatcher"},
    )
    assert bad_time.status_code == 422


def test_finished_session_rejects_migration_and_keeps_history(client):
    prepare(client)
    session = start_session(client)
    client.post(
        f"/api/network/sessions/{session['id']}/finish",
        json={"actor": "tests", "reason": "体验恢复", "result": "completed"},
    )
    result = migrate(client, session["id"], "seg-b", "2026-09-27T03:10:00Z", "obs-done-01")
    assert result["migration"]["result"] == "rejected"
    assert result["migration"]["reason"] == "session_not_active"
    assert result["session"]["status"] == "completed"
    connection = get_connection()
    held = connection.execute("SELECT COUNT(*) FROM capacity_reservations WHERE state='held'").fetchone()[0]
    assert held == 0


def test_concurrent_migrations_to_tight_segment_never_oversell_capacity(client):
    prepare(client, capacities=(1000, 20, 1000, 100))
    # seg-b 容量 20Mbps：两个 16Mbps 的会话同时迁入，只能成功一个
    first = start_session(client, sample_key="mig-sample-conc-1")
    second = start_session(client, sample_key="mig-sample-conc-2")
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}

    def move(sess, key):
        service = NetworkAccelerationService(get_connection())
        barrier.wait()
        results[key] = service.migrate_session(
            sess["id"],
            {"segment_code": "seg-b", "observed_at": "2026-09-27T03:02:00Z", "observation_key": f"obs-conc-{key}", "actor": "dispatcher"},
            "dispatcher",
        )

    threads = [
        threading.Thread(target=move, args=(first, "a")),
        threading.Thread(target=move, args=(second, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    outcomes = sorted(item["migration"]["result"] for item in results.values())
    assert outcomes == ["migrated", "rejected"]
    rejected = next(item for item in results.values() if item["migration"]["result"] == "rejected")
    assert rejected["migration"]["reason"] == "target_capacity_insufficient"
    connection = get_connection()
    held = held_by_segment(connection)
    assert held["seg-b"]["downlink"] == 16.0
    assert held["seg-a"]["downlink"] == 16.0
    overbooked = client.get("/api/network/analytics/capacity").json()["items"]
    seg_b = next(item for item in overbooked if item["segment_code"] == "seg-b")
    assert seg_b["held_downlink_mbps"] == 16.0
    assert seg_b["available_downlink_mbps"] == 4.0


def test_legacy_reservation_table_is_rebuilt_with_history_preserved(client):
    prepare(client)
    session = start_session(client)
    connection = get_connection()
    reservation_id = connection.execute("SELECT id FROM capacity_reservations WHERE session_id=?", (session["id"],)).fetchone()[0]

    # 模拟旧版本库结构：UNIQUE(session_id) 的预留表
    connection.executescript(
        """
        ALTER TABLE capacity_reservations RENAME TO capacity_reservations_old;
        CREATE TABLE capacity_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            scenario_id INTEGER NOT NULL,
            segment_id INTEGER,
            downlink_mbps REAL NOT NULL,
            uplink_mbps REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'held',
            held_at TEXT NOT NULL,
            released_at TEXT,
            UNIQUE(session_id)
        );
        INSERT INTO capacity_reservations SELECT * FROM capacity_reservations_old;
        DROP TABLE capacity_reservations_old;
        """
    )
    from app.network.schema import ensure_network_schema
    ensure_network_schema(connection)

    kept = connection.execute("SELECT id,state,downlink_mbps FROM capacity_reservations WHERE session_id=?", (session["id"],)).fetchone()
    assert kept["id"] == reservation_id and kept["state"] == "held" and float(kept["downlink_mbps"]) == 16.0
    # 重建后一个会话可以拥有历史 + 当前两条预留
    result = migrate(client, session["id"], "seg-b", "2026-09-27T03:02:00Z", "obs-legacy-01")
    assert result["migration"]["result"] == "migrated"
    states = [row["state"] for row in connection.execute("SELECT state FROM capacity_reservations WHERE session_id=? ORDER BY id", (session["id"],)).fetchall()]
    assert states == ["released", "held"]


def test_duplicate_observation_is_stable_under_fixed_clock_service(client):
    prepare(client)
    frozen = FrozenClock(datetime(2026, 9, 27, 3, 1, tzinfo=UTC))
    service = NetworkAccelerationService(get_connection(), frozen)
    session = start_session(client)
    payload = {"segment_code": "seg-b", "observed_at": "2026-09-27T03:02:00Z", "observation_key": "obs-svc-dup-01"}
    first = service.migrate_session(session["id"], dict(payload, actor="dispatcher"), "dispatcher")
    second = service.migrate_session(session["id"], dict(payload, actor="dispatcher"), "dispatcher")
    assert first["migration"]["id"] == second["migration"]["id"]
    connection = get_connection()
    assert held_by_segment(connection) == {"seg-b": {"sessions": 1, "downlink": 16.0}}
