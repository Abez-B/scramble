import asyncio
import json
import ssl
import time
import uuid
import certifi
import httpx
import websockets

HOST_URL = "https://scrambles.fly.dev"
PLAYERS_COUNT = 100
TEAMS_COUNT = 10
HOST_PIN = "1234"

async def run_ws_latency_test():
    print(f"\n=======================================================")
    print(f"  SCRAMBLE 100-PLAYER REAL-TIME WEBSOCKET LATENCY TEST")
    print(f"=======================================================\n")
    
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    
    async with httpx.AsyncClient(base_url=HOST_URL, timeout=30.0) as session:
        print("[Host] Cleaning up any existing room...")
        await session.post("/api/close", headers={"x-host-pin": HOST_PIN})
        
        print("[Host] Opening room...")
        answers = ["TESTING", "latency", "benchmark", "cloudflaredurable"]
        resp = await session.post("/api/room", json={"pin": HOST_PIN, "answers": answers})
        data = resp.json()
        code = data["join_code"]
        ws_url = data["ws_url"]
        
        host_st = await session.get("/api/host/state", headers={"x-host-pin": HOST_PIN})
        session_id = host_st.json()["session_id"]
        print(f"[Host] Room created: {code} | Session: {session_id}")
        print(f"[Host] Realtime WS URL: {ws_url}")
        
        print(f"\n[Players] Joining {PLAYERS_COUNT} players via HTTP...")
        players = []
        for i in range(PLAYERS_COUNT):
            pid = str(uuid.uuid4())
            name = f"Bot-{i+1}"
            players.append({"pid": pid, "name": name, "code": code})
            
        # Join players
        for i, p in enumerate(players):
            j_resp = await session.post("/api/join", json=p)
            if (i + 1) % 25 == 0:
                print(f"  Joined {i+1}/{PLAYERS_COUNT}")
            await asyncio.sleep(0.02)
            
        print(f"[Players] Marking all players as ready...")
        async def ready_up(p):
            await session.post("/api/ready", json={"code": p["code"], "pid": p["pid"], "ready": True})
        
        for i in range(0, PLAYERS_COUNT, 25):
            await asyncio.gather(*(ready_up(p) for p in players[i:i+25]))
            
        print(f"[Host] Setting to {TEAMS_COUNT} teams and assigning colors...")
        headers = {"x-host-pin": HOST_PIN}
        await session.post("/api/host/teams", json={"team_count": TEAMS_COUNT}, headers=headers)
        await session.post("/api/host/assign", headers=headers)
        
        # Connect all 100 players via WebSocket to Cloudflare Worker
        print(f"\n[WebSocket] Connecting {PLAYERS_COUNT} WebSocket clients to Cloudflare Durable Object...")
        ws_proto = ws_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_endpoint = f"{ws_proto}/ws/{session_id}"
        
        sockets = {}
        for i, p in enumerate(players):
            ws = await websockets.connect(f"{ws_endpoint}?pid={p['pid']}&role=player", ssl=ssl_ctx)
            # Read CONNECTED
            init_msg = json.loads(await ws.recv())
            sockets[p["pid"]] = ws
            if (i + 1) % 25 == 0:
                print(f"  Connected {i+1}/{PLAYERS_COUNT} WebSockets")
                
        print("[WebSocket] All 100 WebSockets connected! Stabilizing for 2 seconds...")
        await asyncio.sleep(2)
        
        # Get team assignment for each player
        host_state = (await session.get("/api/host/state", headers=headers)).json()
        pid_to_team = {p["pid"]: p["color_idx"] for p in host_state["players"]}
        
        client_latencies = {}
        all_received = asyncio.Event()
        round_trigger_time = 0
        
        async def listen_for_next(pid, ws):
            nonlocal round_trigger_time
            while not all_received.is_set():
                try:
                    msg = json.loads(await ws.recv())
                    if msg.get("type") == "NEXT" and msg.get("round") == 0:
                        recv_time = time.time()
                        delivery_latency_ms = (recv_time - round_trigger_time) * 1000
                        execute_at = msg.get("executeAt", 0)
                        client_latencies[pid] = {
                            "delivery_latency_ms": delivery_latency_ms,
                            "executeAt": execute_at,
                            "sequence": msg.get("sequence"),
                            "team": pid_to_team.get(pid, 0)
                        }
                        if len(client_latencies) >= PLAYERS_COUNT:
                            all_received.set()
                        break
                except Exception:
                    break

        listener_tasks = [asyncio.create_task(listen_for_next(p["pid"], sockets[p["pid"]])) for p in players]
        
        print("\n[Host] 🚀 Pressing 'Next Word' (triggering round 0 broadcast)...")
        round_trigger_time = time.time()
        await session.post("/api/host/round", json={"action": "next"}, headers=headers)
        
        try:
            await asyncio.wait_for(all_received.wait(), timeout=10.0)
            print(f"[WebSocket] ⚡ All {len(client_latencies)}/{PLAYERS_COUNT} clients received the NEXT event via WebSocket!")
        except asyncio.TimeoutError:
            print(f"[WebSocket] Timeout! Received by {len(client_latencies)}/{PLAYERS_COUNT} clients.")
            
        for t in listener_tasks:
            t.cancel()
            
        # Report results immediately
        print("\n" + "=" * 70)
        print("  REAL-TIME WEBSOCKET DELIVERY BENCHMARK RESULTS (100 PLAYERS)")
        print("=" * 70)
        
        all_deliv = [v["delivery_latency_ms"] for v in client_latencies.values()]
        teams = {}
        for pid, d in client_latencies.items():
            teams.setdefault(d["team"], []).append(d["delivery_latency_ms"])
            
        print(f"\n  Overall WebSocket Delivery Latency across {len(all_deliv)} connected phones:")
        print(f"    Min:   {min(all_deliv):.1f}ms")
        print(f"    Avg:   {sum(all_deliv)/len(all_deliv):.1f}ms")
        print(f"    Max:   {max(all_deliv):.1f}ms")
        print(f"    Spread (Max - Min): {max(all_deliv) - min(all_deliv):.1f}ms")
        
        print(f"\n  Per-Team Delivery Breakdown:")
        print(f"  {'Team':<10} {'Players':<10} {'Avg Latency':<15} {'Min / Max':<20} {'Spread':<12} {'Verdict'}")
        print(f"  {'-'*10} {'-'*10} {'-'*15} {'-'*20} {'-'*12} {'-'*7}")
        
        for team_idx in sorted(teams.keys()):
            lats = teams[team_idx]
            avg_l = sum(lats) / len(lats)
            min_l, max_l = min(lats), max(lats)
            spread = max_l - min_l
            verdict = "PASS" if spread <= 1000 else "FAIL"
            print(f"  Team {team_idx:<5} {len(lats):<10} {avg_l:>8.1f}ms     {min_l:>6.1f}ms / {max_l:>6.1f}ms   {spread:>7.1f}ms    [{verdict}]")
            
        # Clean up room
        await session.post("/api/close", headers=headers)
        try:
            await asyncio.wait_for(asyncio.gather(*(ws.close() for ws in sockets.values()), return_exceptions=True), timeout=2.0)
        except Exception:
            pass
        print(f"\n[Host] Cleaned up and closed room.")
        print("=" * 70 + "\n")

if __name__ == "__main__":
    asyncio.run(run_ws_latency_test())
