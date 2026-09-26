import asyncio
import os
import sys

# Ensure scramble root is in sys.path
sys.path.insert(0, "/Users/ashish/Documents/PERSONAL/scramble")

from fastapi.testclient import TestClient
from main import app, _mem

client = TestClient(app)

def test_routes():
    print("Testing basic page routes...")
    # /
    r = client.get("/")
    assert r.status_code == 200, f"GET / failed: {r.status_code}"
    print("  ✓ GET / OK")

    # /host
    r = client.get("/host")
    assert r.status_code == 200, f"GET /host failed: {r.status_code}"
    assert "scramble" in r.text
    assert "host console" in r.text
    print("  ✓ GET /host OK")

    # /projector
    r = client.get("/projector")
    assert r.status_code == 200, f"GET /projector failed: {r.status_code}"
    assert "scramble" in r.text
    assert "projector" in r.text
    print("  ✓ GET /projector OK")

    # /api/projector/state with no room
    r = client.get("/api/projector/state")
    assert r.status_code == 200
    d = r.json()
    assert d.get("exists") is False
    print("  ✓ GET /api/projector/state (empty) OK")

    # Open room
    pin = "4321"
    answers = ["REDESIGN", "SUCCESS"]
    r = client.post("/api/room", json={"pin": pin, "answers": answers})
    if r.status_code == 409:
        # Close room first and re-open
        print("  Room already open, closing first...")
        # Unlock or close
        client.post("/api/close", headers={"x-host-pin": pin})
        r = client.post("/api/room", json={"pin": pin, "answers": answers})
    
    assert r.status_code == 200, f"Failed to open room: {r.text}"
    room_data = r.json()
    code = room_data["join_code"]
    print(f"  ✓ Room created with code: {code}")

    # Check /api/projector/state with room open
    r = client.get("/api/projector/state")
    assert r.status_code == 200
    proj_data = r.json()
    assert proj_data.get("exists") is True
    assert proj_data.get("join_code") == code
    assert proj_data.get("phase") == "lobby"
    assert "answers" not in proj_data, "SECURITY ISSUE: answers leaked in projector state!"
    print("  ✓ /api/projector/state (lobby) OK, answers securely excluded")

    # Join 2 players
    p1 = {"code": code, "pid": "pid-111", "name": "Alice"}
    p2 = {"code": code, "pid": "pid-222", "name": "Bob"}
    r1 = client.post("/api/join", json=p1)
    r2 = client.post("/api/join", json=p2)
    assert r1.status_code == 200 and r2.status_code == 200
    print("  ✓ 2 players joined")

    # Ready up with fit reporting (Part C)
    r = client.post("/api/ready", json={"code": code, "pid": "pid-111", "ready": True, "fit": {"w": 360, "h": 780}})
    assert r.status_code == 200
    r = client.post("/api/ready", json={"code": code, "pid": "pid-222", "ready": True})
    assert r.status_code == 200

    # Dedicated /api/fit endpoint test
    r = client.post("/api/fit", json={"code": code, "pid": "pid-222", "w": 412, "h": 915})
    assert r.status_code == 200
    r = client.post("/api/fit", json={"code": "WRONG", "pid": "pid-222", "w": 412, "h": 915})
    assert r.status_code == 403
    print("  ✓ /api/fit endpoint and fit in /api/ready verified")

    # Host state
    r = client.get("/api/host/state", headers={"x-host-pin": pin})
    assert r.status_code == 200
    hs = r.json()
    assert len(hs["players"]) == 2
    assert hs["answers"] == answers
    assert hs.get("settings", {}).get("piece_orient") == "auto"
    print("  ✓ /api/host/state OK with answers, players, and default piece_orient='auto'")

    # Check player state includes piece_orient
    r = client.get(f"/api/state?code={code}&pid=pid-111")
    assert r.status_code == 200
    ps = r.json()
    assert ps.get("settings", {}).get("piece_orient") == "auto"
    print("  ✓ /api/state OK with default piece_orient='auto'")

    # Update host settings (Part B)
    r = client.post("/api/host/settings", json={"edge_marks": False, "allow_flips": True, "piece_orient": "portrait"}, headers={"x-host-pin": pin})
    assert r.status_code == 200
    print("  ✓ /api/host/settings updated to piece_orient='portrait'")

    # Verify updated settings in host and player state
    r = client.get("/api/host/state", headers={"x-host-pin": pin})
    assert r.json()["settings"]["piece_orient"] == "portrait"
    assert r.json()["settings"]["edge_marks"] is False
    r = client.get(f"/api/state?code={code}&pid=pid-111")
    assert r.json()["settings"]["piece_orient"] == "portrait"
    print("  ✓ Updated piece_orient reflected in host state and player state")

    # Assign colors & go live
    r = client.post("/api/host/assign", headers={"x-host-pin": pin})
    assert r.status_code == 200
    print("  ✓ Colors assigned, phase -> live")

    # Verify projector state in live phase
    r = client.get("/api/projector/state")
    proj_data = r.json()
    assert proj_data.get("phase") == "live"
    assert proj_data.get("current_round") == -1  # Team screen
    assert len(proj_data["players"]) == 2
    assert proj_data["players"][0]["color_idx"] >= 0
    print("  ✓ Projector state reflects live team screen with color assignments")

    # Deal Round 1 ("REDESIGN" -> > 4 letters -> pieces mode)
    r = client.post("/api/host/round", json={"action": "next"}, headers={"x-host-pin": pin})
    assert r.status_code == 200
    print("  ✓ Round 1 dealt")

    # Verify fit_scale in round snapshot and player states (Part C)
    r = client.get("/api/host/state", headers={"x-host-pin": pin})
    assert r.status_code == 200
    hs_live = r.json()
    snap = hs_live["snapshot"]
    assert "fit_scale" in snap
    team_fit_scale = snap["fit_scale"].get("0")
    assert team_fit_scale is not None and team_fit_scale > 0
    print(f"  ✓ Snapshot calculated team fit_scale: {team_fit_scale}")

    # Verify player 1 and player 2 receive fit_scale in /api/state
    r = client.get(f"/api/state?code={code}&pid=pid-111")
    assert r.status_code == 200
    p1_state = r.json()
    assert p1_state["round"]["fit_scale"] == team_fit_scale
    r = client.get(f"/api/state?code={code}&pid=pid-222")
    assert r.status_code == 200
    p2_state = r.json()
    assert p2_state["round"]["fit_scale"] == team_fit_scale
    print("  ✓ Player state /api/state exposes round.fit_scale cap correctly")

    # Verify projector state after round dealt
    r = client.get("/api/projector/state")
    proj_data = r.json()
    assert proj_data.get("current_round") == 0
    assert "answers" not in proj_data
    print("  ✓ Projector state reflects round 0")

    # Clean up / close room
    client.post("/api/close", headers={"x-host-pin": pin})
    print("  ✓ Room closed cleanly")
    print("\nALL VERIFICATION TESTS PASSED SUCCESSFULLY! 🎉")

if __name__ == "__main__":
    test_routes()
