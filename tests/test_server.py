"""
Tests for hub/server.py over a real WebSocket, with the service talking to
the fake lamp.

Starlette's TestClient runs the app (and its lifespan, so the service
starts and stops) in a background thread. Tests talk to it like a browser
would: JSON in, JSON out. The lamp is never touched from the test thread
directly; everything goes through the socket.
"""

import time
from contextlib import contextmanager

from fastapi.testclient import TestClient

from hub.server import create_app
from tests.service_helpers import make_service

WHITE = {"on": True, "brightness": 255, "rgb": [255, 255, 255], "mode": "normal"}


@contextmanager
def running_app(**service_options):
    service, fakes, _ = make_service(**service_options)
    with TestClient(create_app(service)) as client:
        yield client, service, fakes


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met in time")
        time.sleep(0.01)


def recv_until(ws, predicate, limit: int = 10) -> dict:
    """Read messages until one satisfies predicate (skips e.g. an early 'disconnected')."""
    for _ in range(limit):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("expected message never arrived")


def connected_state(ws) -> dict:
    return recv_until(ws, lambda m: m["type"] == "state" and m["connected"])


def test_client_gets_the_lamp_state_on_connect():
    with running_app() as (client, _, _), client.websocket_connect("/ws") as ws:
        assert connected_state(ws)["lamp"] == WHITE


def test_valid_command_is_acked_and_the_new_state_is_pushed():
    with running_app() as (client, _, fakes), client.websocket_connect("/ws") as ws:
        connected_state(ws)
        ws.send_json({"id": 7, "type": "color", "r": 255, "g": 0, "b": 0})
        msgs = [ws.receive_json() for _ in range(2)]
        assert {"type": "ack", "id": 7} in msgs
        state = next(m for m in msgs if m["type"] == "state")
        assert state["lamp"]["rgb"] == [255, 0, 0]
        assert fakes[0].state["rgb"] == [255, 0, 0]


def test_invalid_values_get_an_error_and_never_reach_the_lamp():
    with running_app() as (client, _, fakes), client.websocket_connect("/ws") as ws:
        connected_state(ws)
        ws.send_json({"id": 1, "type": "color", "r": 999, "g": 0, "b": 0})
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["id"] == 1
        assert "r" in msg["message"]
        assert fakes[0].state["rgb"] == [255, 255, 255]


def test_garbage_gets_an_error_without_an_id():
    with running_app() as (client, _, _), client.websocket_connect("/ws") as ws:
        connected_state(ws)
        ws.send_text("not json")
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["id"] is None


def test_unknown_mode_name_is_rejected_with_the_known_names():
    with running_app() as (client, _, _), client.websocket_connect("/ws") as ws:
        connected_state(ws)
        ws.send_json({"id": 2, "type": "mode", "mode": "disco"})
        msg = ws.receive_json()
        assert msg["type"] == "error" and "rainbow" in msg["message"]


def test_command_while_the_lamp_is_away_gets_an_error_not_a_hang():
    with running_app(fail_first=1000) as (client, _, _), client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first == {"type": "state", "connected": False, "lamp": None, "sun": None}
        ws.send_json({"id": 5, "type": "power", "on": True})
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["id"] == 5
        assert "not connected" in msg["message"]


def test_every_client_sees_changes_made_by_another():
    with (
        running_app() as (client, _, _),
        client.websocket_connect("/ws") as a,
        client.websocket_connect("/ws") as b,
    ):
        connected_state(a), connected_state(b)
        a.send_json({"type": "brightness", "value": 42})
        state = recv_until(b, lambda m: m["type"] == "state")
        assert state["lamp"]["brightness"] == 42


def test_clients_that_leave_are_forgotten():
    with running_app() as (client, service, _):
        with client.websocket_connect("/ws") as ws:
            connected_state(ws)
            wait_until(lambda: service.subscriber_count == 1)
        wait_until(lambda: service.subscriber_count == 0)


def test_the_ui_is_served_at_the_root():
    with running_app() as (client, _, _):
        page = client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "i_Lamp" in page.text
        assert client.get("/app.js").status_code == 200


def test_the_ring_is_controlled_and_reported_over_the_socket():
    with running_app() as (client, _, fakes), client.websocket_connect("/ws") as ws:
        state = connected_state(ws)
        assert state["sun"] == {"on": False, "temperature": 128, "level": 16}
        ws.send_json({"id": 1, "type": "sun", "on": True})
        msgs = [ws.receive_json() for _ in range(3)]  # ack + ring state + rgb-off state
        assert {"type": "ack", "id": 1} in msgs
        last = [m for m in msgs if m["type"] == "state"][-1]
        assert last["sun"]["on"] is True and last["lamp"]["on"] is False
        assert fakes[0].sun["power"] == 1
        ws.send_json({"id": 2, "type": "sun_temperature", "value": 0})
        msgs = [ws.receive_json() for _ in range(2)]
        assert next(m for m in msgs if m["type"] == "state")["sun"]["temperature"] == 0


def test_ring_level_over_the_socket():
    with running_app() as (client, _, fakes), client.websocket_connect("/ws") as ws:
        connected_state(ws)
        ws.send_json({"id": 3, "type": "sun_level", "value": 2})
        msgs = [ws.receive_json() for _ in range(2)]
        assert next(m for m in msgs if m["type"] == "state")["sun"]["level"] == 2
        assert fakes[0].sun["level"] == 2
