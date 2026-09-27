from __future__ import annotations

from app.network.rules import DEFAULT_RULES

SEGMENTS = [
    {"code": "seg-01", "name": "广州南至虎门", "sequence_no": 1, "expected_dwell_seconds": 900, "capacity_mbps": 1200},
    {"code": "seg-02", "name": "虎门至深圳北", "sequence_no": 2, "expected_dwell_seconds": 900, "capacity_mbps": 1200},
    {"code": "seg-03", "name": "深圳北至福田", "sequence_no": 3, "expected_dwell_seconds": 600, "capacity_mbps": 1200},
    {"code": "seg-04", "name": "福田至西九龙", "sequence_no": 4, "expected_dwell_seconds": 600, "capacity_mbps": 10},
]

ALLOCATED_DOWNLINK = 16.0
ALLOCATED_UPLINK = 8.0


def sample_payload(**overrides):
    payload = {
        "sample_key": "sample-migration-0001",
        "scenario_code": "gdh-rail",
        "segment_code": "seg-01",
        "app_code": "video-call",
        "subscriber_hash": "subscriber-000000000001",
        "device_class": "phone",
        "train_speed_kmh": 300,
        "latency_ms": 350,
        "packet_loss": 0.08,
        "downlink_mbps": 1.5,
        "uplink_mbps": 0.5,
        "observed_at": "2026-09-26T05:30:00Z",
    }
    payload.update(overrides)
    return payload


def prepare(client):
    scenario = client.post(
        "/api/network/scenarios",
        json={
            "code": "gdh-rail",
            "name": "广深港高铁",
            "scene_type": "railway",
            "timezone": "Asia/Shanghai",
            "max_concurrent_sessions": 10,
            "capacity_mbps": 3000,
        },
    )
    assert scenario.status_code == 201, scenario.text
    segments = {}
    for item in SEGMENTS:
        response = client.post("/api/network/scenarios/gdh-rail/segments", json=item)
        assert response.status_code == 201, response.text
        segments[item["code"]] = response.json()
    app = client.post(
        "/api/network/applications",
        json={
            "app_code": "video-call",
            "name": "视频通话",
            "category": "video_call",
            "latency_target_ms": 100,
            "packet_loss_target": 0.01,
            "min_downlink_mbps": 8,
            "min_uplink_mbps": 4,
            "default_priority": 70,
        },
    )
    assert app.status_code == 201, app.text
    policy = client.post("/api/network/scenarios/gdh-rail/policies", json={"rules": DEFAULT_RULES, "actor": "tests"})
    assert policy.status_code == 201, policy.text
    published = client.post(
        f"/api/network/policies/{policy.json()['id']}/publish",
        json={"actor": "tests", "effective_from": "2026-01-01T00:00:00Z"},
    )
    assert published.status_code == 200, published.text
    entitlement = client.post(
        "/api/network/entitlements",
        json={
            "subscriber_hash": sample_payload()["subscriber_hash"],
            "scenario_code": "gdh-rail",
            "product_code": "rail-boost-day",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": "2099-01-01T00:00:00Z",
            "source_order_id": "order-migration-0001",
        },
    )
    assert entitlement.status_code == 201, entitlement.text
    return {"scenario": scenario.json(), "segments": segments}


def start_session(client, **sample_overrides):
    sample = client.post("/api/network/samples", json=sample_payload(**sample_overrides))
    assert sample.status_code == 202, sample.text
    started = client.post(f"/api/network/incidents/{sample.json()['incident_id']}/accelerate", json={"actor": "tests"})
    assert started.status_code == 200, started.text
    return started.json()


def observe(client, session_id, observation_key, segment_code, observed_at):
    return client.post(
        f"/api/network/sessions/{session_id}/migrations",
        json={"observation_key": observation_key, "segment_code": segment_code, "observed_at": observed_at, "actor": "location-feed"},
    )


def held_capacity(client):
    snapshot = client.get("/api/network/analytics/capacity")
    assert snapshot.status_code == 200, snapshot.text
    return {item["segment_code"]: item["held_downlink_mbps"] for item in snapshot.json()["items"] if item["segment_code"]}


def assert_conserved(client, current_segment):
    held = held_capacity(client)
    for code in held:
        expected = ALLOCATED_DOWNLINK if code == current_segment else 0.0
        assert held[code] == expected, f"{code} 持有容量 {held[code]}，期望 {expected}"
    assert sum(held.values()) == ALLOCATED_DOWNLINK


def test_forward_migration_moves_reservation_and_conserves_capacity(client):
    prepared = prepare(client)
    session = start_session(client)
    assert session["segment_id"] == prepared["segments"]["seg-01"]["id"]
    assert_conserved(client, "seg-01")

    migrated = observe(client, session["id"], "obs-0000a1", "seg-02", "2099-01-01T00:01:00Z")
    assert migrated.status_code == 201, migrated.text
    record = migrated.json()
    assert record["movement"] == "forward"
    assert record["result"] == "migrated"
    assert record["from_segment_code"] == "seg-01"
    assert record["to_segment_code"] == "seg-02"
    assert record["observed_at"] == "2099-01-01T00:01:00+00:00"
    assert record["downlink_mbps"] == ALLOCATED_DOWNLINK
    assert record["uplink_mbps"] == ALLOCATED_UPLINK
    assert_conserved(client, "seg-02")

    detail = client.get(f"/api/network/sessions/{session['id']}").json()
    assert detail["segment_id"] == prepared["segments"]["seg-02"]["id"]
    assert detail["reservation"]["segment_id"] == prepared["segments"]["seg-02"]["id"]
    assert detail["reservation"]["state"] == "held"
    assert [event["event_type"] for event in detail["events"]] == ["started", "migrated"]
    assert len(detail["migrations"]) == 1
    entry = detail["migrations"][0]
    assert entry["from_segment_code"] == "seg-01"
    assert entry["to_segment_code"] == "seg-02"
    assert entry["result"] == "migrated"
    assert entry["observed_at"] == "2099-01-01T00:01:00+00:00"


def test_repeated_observation_does_not_double_deduct_capacity(client):
    prepare(client)
    session = start_session(client)
    first = observe(client, session["id"], "obs-0000b1", "seg-02", "2099-01-01T00:01:00Z")
    assert first.status_code == 201, first.text
    replay = observe(client, session["id"], "obs-0000b1", "seg-02", "2099-01-01T00:01:00Z")
    assert replay.status_code == 201, replay.text
    assert replay.json()["duplicate"] is True
    assert replay.json()["id"] == first.json()["id"]
    assert_conserved(client, "seg-02")
    detail = client.get(f"/api/network/sessions/{session['id']}").json()
    assert len(detail["migrations"]) == 1
    assert [event["event_type"] for event in detail["events"]] == ["started", "migrated"]

    changed = observe(client, session["id"], "obs-0000b1", "seg-03", "2099-01-01T00:01:00Z")
    assert changed.status_code == 409
    assert_conserved(client, "seg-02")


def test_back_swing_out_of_order_and_regression_do_not_move_session(client):
    prepare(client)
    session = start_session(client)
    assert observe(client, session["id"], "obs-0000c1", "seg-02", "2099-01-01T00:01:00Z").json()["result"] == "migrated"

    swing = observe(client, session["id"], "obs-0000c2", "seg-01", "2099-01-01T00:01:10Z")
    assert swing.status_code == 201, swing.text
    assert swing.json()["movement"] == "back_swing"
    assert swing.json()["result"] == "ignored"
    assert_conserved(client, "seg-02")

    stale = observe(client, session["id"], "obs-0000c3", "seg-03", "2099-01-01T00:01:05Z")
    assert stale.status_code == 201, stale.text
    assert stale.json()["movement"] == "out_of_order"
    assert stale.json()["result"] == "rejected"
    assert_conserved(client, "seg-02")

    assert observe(client, session["id"], "obs-0000c4", "seg-03", "2099-01-01T00:02:00Z").json()["result"] == "migrated"
    backward = observe(client, session["id"], "obs-0000c5", "seg-02", "2099-01-01T00:03:00Z")
    assert backward.json()["movement"] == "regression"
    assert backward.json()["result"] == "rejected"
    assert_conserved(client, "seg-03")

    detail = client.get(f"/api/network/sessions/{session['id']}").json()
    assert [item["movement"] for item in detail["migrations"]] == ["forward", "back_swing", "out_of_order", "forward", "regression"]
    assert [event["event_type"] for event in detail["events"]] == ["started", "migrated", "migrated"]


def test_skip_migration_held_when_capacity_insufficient_keeps_old_reservation(client):
    prepared = prepare(client)
    session = start_session(client)
    assert observe(client, session["id"], "obs-0000d1", "seg-02", "2099-01-01T00:01:00Z").json()["result"] == "migrated"

    held = observe(client, session["id"], "obs-0000d2", "seg-04", "2099-01-01T00:02:00Z")
    assert held.status_code == 201, held.text
    assert held.json()["movement"] == "skip"
    assert held.json()["result"] == "held"
    assert "容量不足" in held.json()["reason"]
    assert_conserved(client, "seg-02")

    detail = client.get(f"/api/network/sessions/{session['id']}").json()
    assert detail["segment_id"] == prepared["segments"]["seg-02"]["id"]
    assert detail["reservation"]["segment_id"] == prepared["segments"]["seg-02"]["id"]
    assert detail["reservation"]["state"] == "held"
    assert detail["status"] == "active"
    assert "migration_held" in [event["event_type"] for event in detail["events"]]

    retry = observe(client, session["id"], "obs-0000d3", "seg-03", "2099-01-01T00:03:00Z")
    assert retry.json()["result"] == "migrated"
    assert_conserved(client, "seg-03")


def test_same_segment_observation_is_ignored(client):
    prepare(client)
    session = start_session(client)
    same = observe(client, session["id"], "obs-0000e1", "seg-01", "2099-01-01T00:01:00Z")
    assert same.status_code == 201, same.text
    assert same.json()["movement"] == "same_segment"
    assert same.json()["result"] == "ignored"
    assert_conserved(client, "seg-01")
    detail = client.get(f"/api/network/sessions/{session['id']}").json()
    assert [event["event_type"] for event in detail["events"]] == ["started"]


def test_full_journey_capacity_conservation_and_audit_trail(client):
    prepare(client)
    session = start_session(client)
    steps = [
        ("obs-0000f1", "seg-02", "2099-01-01T00:01:00Z", "migrated", "seg-02"),
        ("obs-0000f2", "seg-01", "2099-01-01T00:01:10Z", "ignored", "seg-02"),
        ("obs-0000f3", "seg-03", "2099-01-01T00:01:05Z", "rejected", "seg-02"),
        ("obs-0000f4", "seg-04", "2099-01-01T00:02:00Z", "held", "seg-02"),
        ("obs-0000f5", "seg-03", "2099-01-01T00:03:00Z", "migrated", "seg-03"),
        ("obs-0000f6", "seg-02", "2099-01-01T00:04:00Z", "rejected", "seg-03"),
        ("obs-0000f7", "seg-03", "2099-01-01T00:05:00Z", "ignored", "seg-03"),
    ]
    assert_conserved(client, "seg-01")
    for key, segment, observed_at, result, current in steps:
        response = observe(client, session["id"], key, segment, observed_at)
        assert response.status_code == 201, response.text
        assert response.json()["result"] == result, response.json()
        assert_conserved(client, current)

    detail = client.get(f"/api/network/sessions/{session['id']}").json()
    assert len(detail["migrations"]) == len(steps)
    for entry, (key, segment, observed_at, result, _current) in zip(detail["migrations"], steps, strict=True):
        assert entry["observation_key"] == key
        assert entry["to_segment_code"] == segment
        assert entry["result"] == result
        assert entry["observed_at"] == observed_at.replace("Z", "+00:00")
        assert entry["reason"]
    assert [event["event_type"] for event in detail["events"]] == ["started", "migrated", "migration_held", "migrated"]

    finished = client.post(f"/api/network/sessions/{session['id']}/finish", json={"actor": "tests", "reason": "旅程结束", "result": "completed"})
    assert finished.status_code == 200, finished.text
    assert sum(held_capacity(client).values()) == 0.0


def test_migration_requires_active_session_and_replay_still_works(client):
    prepare(client)
    session = start_session(client)
    first = observe(client, session["id"], "obs-0000g1", "seg-02", "2099-01-01T00:01:00Z")
    assert first.json()["result"] == "migrated"
    finished = client.post(f"/api/network/sessions/{session['id']}/finish", json={"actor": "tests", "reason": "体验恢复", "result": "completed"})
    assert finished.status_code == 200, finished.text

    replay = observe(client, session["id"], "obs-0000g1", "seg-02", "2099-01-01T00:01:00Z")
    assert replay.status_code == 201
    assert replay.json()["duplicate"] is True
    late = observe(client, session["id"], "obs-0000g2", "seg-03", "2099-01-01T00:02:00Z")
    assert late.status_code == 409
    assert sum(held_capacity(client).values()) == 0.0


def test_migration_validates_session_segment_and_observed_at(client):
    prepare(client)
    session = start_session(client)
    missing = observe(client, 999999, "obs-0000h1", "seg-02", "2099-01-01T00:01:00Z")
    assert missing.status_code == 404
    unknown = observe(client, session["id"], "obs-0000h2", "seg-99", "2099-01-01T00:01:00Z")
    assert unknown.status_code == 404
    invalid = observe(client, session["id"], "obs-0000h3", "seg-02", "not-a-time")
    assert invalid.status_code == 422
    assert_conserved(client, "seg-01")


def test_scenario_level_session_binds_segment_on_first_observation(client):
    prepare(client)
    session = start_session(client, sample_key="sample-migration-0002", segment_code=None)
    assert session["segment_id"] is None
    bound = observe(client, session["id"], "obs-0000i1", "seg-02", "2099-01-01T00:01:00Z")
    assert bound.status_code == 201, bound.text
    assert bound.json()["movement"] == "forward"
    assert bound.json()["result"] == "migrated"
    assert bound.json()["from_segment_code"] is None
    assert_conserved(client, "seg-02")


def test_maintenance_window_holds_migration(client):
    prepare(client)
    session = start_session(client)
    window = client.post(
        "/api/network/operations/maintenance",
        json={
            "scenario_code": "gdh-rail",
            "segment_code": "seg-02",
            "code": "mw-seg-02",
            "reason": "区间设备检修",
            "starts_at": "2026-01-01T00:00:00Z",
            "ends_at": "2099-01-01T00:00:00Z",
            "drain_mode": "block_new",
            "actor": "tests",
        },
    )
    assert window.status_code == 201, window.text
    held = observe(client, session["id"], "obs-0000j1", "seg-02", "2099-01-01T00:01:00Z")
    assert held.status_code == 201, held.text
    assert held.json()["result"] == "held"
    assert "维护窗口" in held.json()["reason"]
    assert_conserved(client, "seg-01")
