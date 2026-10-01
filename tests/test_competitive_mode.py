import os
import pytest
from fastapi.testclient import TestClient

from main import app, _mem, store


@pytest.fixture(autouse=True)
def clean_db():
    if not store._conn:
        store.connect()
    _mem["room"] = None
    _mem["players"] = []
    _mem["answers"] = []
    _mem["rounds"] = {}
    _mem["kicked_pids"] = set()
    _mem["guess_cooldowns"] = {}
    store.close_room()
    yield
    store.close_room()


def test_competitive_mode_lifecycle():
    client = TestClient(app)
    pin = "1234"

    # 1. Open room in competitive mode
    r = client.post("/api/room", json={
        "pin": pin,
        "answers": ["PYTHON", "JAVASCRIPT", "DOCKER"],
        "game_mode": "competitive"
    })
    assert r.status_code == 200
    join_code = r.json()["join_code"]
    assert r.json()["game_mode"] == "competitive"

    headers = {"X-Host-Pin": pin}

    # Verify host state
    hr = client.get("/api/host/state", headers=headers)
    assert hr.status_code == 200
    hdata = hr.json()
    assert hdata["game_mode"] == "competitive"
    assert hdata["scores"] == {}
    assert hdata["round_winner"] is None

    # Set team count to 3
    client.post("/api/host/teams", json={"team_count": 3}, headers=headers)

    # 2. Join 3 players
    p1 = {"code": join_code, "pid": "pid-alice", "name": "Alice"}
    p2 = {"code": join_code, "pid": "pid-bob", "name": "Bob"}
    p3 = {"code": join_code, "pid": "pid-charlie", "name": "Charlie"}
    for p in (p1, p2, p3):
        res = client.post("/api/join", json=p)
        assert res.status_code == 200

    # 3. Custom team creation & joining flow
    # Alice creates "Code Crusaders"
    res = client.post("/api/team/create", json={"code": join_code, "pid": "pid-alice", "name": "Code Crusaders"})
    assert res.status_code == 200
    assert res.json()["team"]["name"] == "Code Crusaders"
    assert res.json()["color_idx"] == 0

    # Duplicate name check
    res_dup = client.post("/api/team/create", json={"code": join_code, "pid": "pid-bob", "name": "code crusaders"})
    assert res_dup.status_code == 400

    # Bob joins "Code Crusaders"
    res = client.post("/api/team/select", json={"code": join_code, "pid": "pid-bob", "color_idx": 0})
    assert res.status_code == 200

    # Host sets team member limit to 2
    res_limit = client.post("/api/host/team_limit", json={"max_team_members": 2}, headers=headers)
    assert res_limit.status_code == 200

    # Charlie tries to join "Code Crusaders" (now full at 2/2) -> rejected
    res_full = client.post("/api/team/select", json={"code": join_code, "pid": "pid-charlie", "color_idx": 0})
    assert res_full.status_code == 400
    assert "Team is full" in res_full.json()["detail"]

    # Charlie creates team "Binary Beasts"
    res_bb = client.post("/api/team/create", json={"code": join_code, "pid": "pid-charlie", "name": "Binary Beasts"})
    assert res_bb.status_code == 200
    assert res_bb.json()["team"]["name"] == "Binary Beasts"
    charlie_team_idx = res_bb.json()["color_idx"]

    # Bob leaves "Code Crusaders" and joins "Binary Beasts"
    res_leave = client.post("/api/team/select", json={"code": join_code, "pid": "pid-bob", "color_idx": -1})
    assert res_leave.status_code == 200
    assert res_leave.json()["color_idx"] == -1

    res_join_bb = client.post("/api/team/select", json={"code": join_code, "pid": "pid-bob", "color_idx": charlie_team_idx})
    assert res_join_bb.status_code == 200

    # Verify Alice's state has custom team name
    sr = client.get("/api/state?pid=pid-alice")
    assert sr.status_code == 200
    sdata = sr.json()
    assert sdata["you"]["team_name"] == "Code Crusaders"
    assert sdata["game_mode"] == "competitive"
    assert len(sdata["custom_teams"]) == 2

    # 4. Assign colors / start game
    res = client.post("/api/host/assign", headers=headers)
    assert res.status_code == 200

    # 5. Advance to round 0 ("PYTHON")
    res = client.post("/api/host/round", json={"action": "next"}, headers=headers)
    assert res.status_code == 200

    # 6. Bob tries wrong answer
    res = client.post("/api/round/submit", json={"code": join_code, "pid": "pid-bob", "guess": "JAVA"})
    assert res.status_code == 200
    assert res.json()["correct"] is False
    assert res.json()["cooldown"] == 3.0

    # Rapid re-submit within 3s triggers cooldown
    res = client.post("/api/round/submit", json={"code": join_code, "pid": "pid-bob", "guess": "JAVA2"})
    assert res.status_code == 200
    assert "Slow down" in res.json()["message"]

    # 7. Alice submits correct answer (with different casing/spacing tolerance)
    res = client.post("/api/round/submit", json={"code": join_code, "pid": "pid-alice", "guess": "  python  "})
    assert res.status_code == 200
    assert res.json()["correct"] is True
    winner = res.json()["winner"]
    assert winner["team_idx"] == 0
    assert winner["team_name"] == "Code Crusaders"
    assert winner["player_name"] == "Alice"
    assert res.json()["scores"]["0"] == 1

    # 8. Late submission after round is won is rejected
    res = client.post("/api/round/submit", json={"code": join_code, "pid": "pid-bob", "guess": "python"})
    assert res.status_code == 200
    assert res.json()["won"] is True

    # Check projector state
    proj = client.get("/api/projector/state").json()
    assert proj["game_mode"] == "competitive"
    assert proj["scores"]["0"] == 1
    assert proj["round_winner"]["player_name"] == "Alice"

    # 9. Host manual score adjustment
    res = client.post("/api/host/scores", json={"color_idx": 1, "delta": 2}, headers=headers)
    assert res.status_code == 200
    assert res.json()["scores"]["1"] == 2

    # 10. Advance to Round 1: round_winner resets, scores persist
    res = client.post("/api/host/round", json={"action": "next"}, headers=headers)
    assert res.status_code == 200
    hr2 = client.get("/api/host/state", headers=headers).json()
    assert hr2["round_winner"] is None
    assert hr2["scores"]["0"] == 1
    assert hr2["scores"]["1"] == 2

    # 11. Mode switcher test: switch back to classic
    res = client.post("/api/host/mode", json={"mode": "classic"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["game_mode"] == "classic"

    # In classic mode, submissions are rejected
    res = client.post("/api/round/submit", json={"code": join_code, "pid": "pid-bob", "guess": "javascript"})
    assert res.status_code == 400

    # 12. Switch back to competitive and test direct score set
    res = client.post("/api/host/mode", json={"mode": "competitive"}, headers=headers)
    assert res.status_code == 200
    res = client.post("/api/host/scores", json={"color_idx": 0, "score": 10}, headers=headers)
    assert res.status_code == 200
    assert res.json()["scores"]["0"] == 10

    # 13. End game / back to lobby clears scores & round_winner
    res = client.post("/api/host/lobby", headers=headers)
    assert res.status_code == 200
    hr3 = client.get("/api/host/state", headers=headers).json()
    assert hr3["phase"] == "lobby"
    assert hr3["scores"] == {}
    assert hr3["round_winner"] is None

    # 14. Verify all HTML pages serve 200 and include new competitive elements
    r_index = client.get("/")
    assert r_index.status_code == 200
    assert "comp-answer-drawer" in r_index.text
    assert "lb-team-picker" in r_index.text
    assert "st-team-badge" in r_index.text
    assert "comp-drawer-team-pill" in r_index.text

    r_host = client.get("/host")
    assert r_host.status_code == 200
    assert "tab-competitive" in r_host.text
    assert "card-competitive" in r_host.text
    assert "nav-header-row" in r_host.text
    assert "@media (max-width: 860px)" in r_host.text

    r_proj = client.get("/projector")
    assert r_proj.status_code == 200
    assert "winner-modal" in r_proj.text
    assert "btn-fs" in r_proj.text


def test_competitive_late_joiner_team_selection():
    client = TestClient(app)
    pin = "1234"

    # 1. Open room in competitive mode
    r = client.post("/api/room", json={
        "pin": pin,
        "answers": ["FIRSTROUND", "SECONDROUND"],
        "game_mode": "competitive"
    })
    join_code = r.json()["join_code"]
    headers = {"X-Host-Pin": pin}

    # 2. Add players and create team in lobby
    client.post("/api/join", json={"code": join_code, "pid": "pid-1", "name": "Player 1"})
    client.post("/api/join", json={"code": join_code, "pid": "pid-2", "name": "Player 2"})
    client.post("/api/team/create", json={"code": join_code, "pid": "pid-1", "name": "Hackers"})
    client.post("/api/team/select", json={"code": join_code, "pid": "pid-2", "color_idx": 0})

    # 3. Start live round
    res_assign = client.post("/api/host/assign", headers=headers)
    assert res_assign.status_code == 200
    res_deal = client.post("/api/host/round", json={"action": "next"}, headers=headers)
    assert res_deal.status_code == 200

    # Verify room is live
    hr = client.get("/api/host/state", headers=headers).json()
    assert hr["phase"] == "live"

    # 4. Late joiner arrives while game is LIVE
    res_join = client.post("/api/join", json={"code": join_code, "pid": "pid-late1", "name": "Late Joiner"})
    assert res_join.status_code == 200

    # In competitive mode, late joiner is unassigned (color_idx == -1) so they can pick their team
    sr_late = client.get("/api/state?pid=pid-late1").json()
    assert sr_late["you"]["color"] is None
    assert sr_late["you"]["team_name"] is None

    # Late joiner joins existing team "Hackers" during live round
    res_sel = client.post("/api/team/select", json={"code": join_code, "pid": "pid-late1", "color_idx": 0})
    assert res_sel.status_code == 200

    sr_late_after = client.get("/api/state?pid=pid-late1").json()
    assert sr_late_after["you"]["team_name"] == "Hackers"

    # Another late joiner arrives and creates a brand new team while game is LIVE
    client.post("/api/join", json={"code": join_code, "pid": "pid-late2", "name": "Late Creator"})
    res_create = client.post("/api/team/create", json={"code": join_code, "pid": "pid-late2", "name": "Night Owls"})
    assert res_create.status_code == 200
    assert res_create.json()["team"]["name"] == "Night Owls"

    sr_late2 = client.get("/api/state?pid=pid-late2").json()
    assert sr_late2["you"]["team_name"] == "Night Owls"
