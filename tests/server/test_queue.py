"""The waitlist gate: FIFO per model, one in flight, queued events with ETA, cancel; plus /queue routes."""

from __future__ import annotations

import asyncio

import pytest

from compute.engine.coordinator.queue import Ticket, Waitlist, cancelled_event, eta_seconds
from srv_helpers import admin_token, bearer, register, services


def runner(name: str, order: list, gates: dict):
    async def run():
        order.append(name)
        await gates[name].wait()
        yield {"type": "final", "name": name}
    return run


async def consume(wl: Waitlist, events: dict, name: str, model: str, max_tokens: int, order: list, gates: dict):
    async for ev in wl.admit(model, f"u-{name}", max_tokens, runner(name, order, gates)):
        events[name].append(ev)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


def test_eta_defaults_to_ten_tokens_per_second():
    assert eta_seconds(100, None) == 10.0
    assert eta_seconds(100, 50.0) == 2.0


async def test_fifo_one_in_flight_eta_and_cancel():
    wl = Waitlist(tok_s_fn=lambda _m: 20.0, tick_s=5.0)
    order: list = []
    gates = {n: asyncio.Event() for n in "abce"}
    events = {n: [] for n in "abce"}
    ta = asyncio.create_task(consume(wl, events, "a", "m", 100, order, gates))
    await settle()
    tb = asyncio.create_task(consume(wl, events, "b", "m", 40, order, gates))
    await settle()
    tc = asyncio.create_task(consume(wl, events, "c", "m", 60, order, gates))
    te = asyncio.create_task(consume(wl, events, "e", "other", 10, order, gates))
    await settle()
    assert order == ["a", "e"]                      # another model is not blocked
    assert events["a"] == [] and events["e"] == []  # nothing queued for requests that start at once
    qb, qc = events["b"][-1], events["c"][-1]
    assert qb["type"] == "queued" and qb["position"] == 1 and qb["eta_s"] == pytest.approx(5.0, abs=0.2)
    assert qc["position"] == 2 and qc["eta_s"] == pytest.approx(7.0, abs=0.2)   # (100 + 40) / 20
    snap = wl.snapshot("u-c")["models"]
    assert [(e["position"], e["mine"]) for e in snap["m"]] == [(1, False), (2, True)]
    assert wl.length() == 2

    gates["a"].set()
    await ta
    await settle()
    assert order == ["a", "e", "b"]
    assert events["c"][-1]["position"] == 1          # re-emitted when the position changed
    assert wl.cancel(qc["queue_id"]) is True
    await tc
    assert events["c"][-1] == cancelled_event()
    gates["b"].set()
    gates["e"].set()
    await asyncio.gather(tb, te)
    assert order == ["a", "e", "b"]
    assert wl.length() == 0 and wl.running == {}


async def test_queued_event_repeats_while_waiting():
    wl = Waitlist(tick_s=0.05)
    order: list = []
    gates = {n: asyncio.Event() for n in "ab"}
    events = {n: [] for n in "ab"}
    ta = asyncio.create_task(consume(wl, events, "a", "m", 10, order, gates))
    await settle()
    tb = asyncio.create_task(consume(wl, events, "b", "m", 10, order, gates))
    await asyncio.sleep(0.3)
    assert len(events["b"]) >= 3 and all(e["position"] == 1 for e in events["b"])
    gates["a"].set()
    gates["b"].set()
    await asyncio.gather(ta, tb)
    assert events["b"][-1] == {"type": "final", "name": "b"}


async def test_cancel_at_the_moment_the_slot_opens_does_not_start():
    wl = Waitlist(tick_s=5.0)
    order: list = []
    gates = {n: asyncio.Event() for n in "ab"}
    events = {n: [] for n in "ab"}
    real = wl._can_start

    def opens_cancelled(t):
        ready = real(t)
        if t.user_id == "u-b" and ready:
            t.cancelled = True
        return ready

    wl._can_start = opens_cancelled
    ta = asyncio.create_task(consume(wl, events, "a", "m", 10, order, gates))
    await settle()
    tb = asyncio.create_task(consume(wl, events, "b", "m", 10, order, gates))
    await settle()
    gates["a"].set()
    await asyncio.gather(ta, tb)
    assert order == ["a"]
    assert events["b"][-1] == cancelled_event()


async def test_slot_is_released_when_the_consumer_goes_away():
    wl = Waitlist(tick_s=5.0)
    order: list = []
    gates = {n: asyncio.Event() for n in "ab"}
    events = {n: [] for n in "ab"}
    ta = asyncio.create_task(consume(wl, events, "a", "m", 10, order, gates))
    await settle()
    ta.cancel()                                      # e.g. the client disconnected mid-stream
    await asyncio.gather(ta, return_exceptions=True)
    assert wl.running == {}
    gates["b"].set()
    await consume(wl, events, "b", "m", 10, order, gates)
    assert order == ["a", "b"]


def test_queue_routes(client):
    sv = services(client)
    owner = register(client, "a@lan.test")
    other = register(client, "b@lan.test")
    uid = client.get("/auth/me", headers=bearer(owner)).json()["id"]
    sv.waitlist.waiting["m"] = [Ticket("q-1", "m", uid, 50), Ticket("q-2", "m", "someone", 50)]
    view = client.get("/queue", headers=bearer(owner)).json()["models"]["m"]
    assert [(e["queue_id"], e["position"], e["mine"]) for e in view] == [("q-1", 1, True), ("q-2", 2, False)]
    assert view[1]["eta_s"] == 5.0                   # 50 tokens ahead at the default 10 tok/s
    assert client.delete("/queue/q-1", headers=bearer(other)).status_code == 403
    assert client.delete("/queue/q-1", headers=bearer(owner)).json() == {"ok": True}
    assert sv.waitlist.find("q-1").cancelled
    assert client.delete("/queue/q-2", headers=bearer(admin_token(client))).json() == {"ok": True}
    assert client.delete("/queue/q-9", headers=bearer(owner)).status_code == 404
    assert client.get("/admin/api/overview", headers=bearer(admin_token(client))).json()["queue_length"] == 2
