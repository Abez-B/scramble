import asyncio
import aiohttp
import time
import random
import uuid

HOST_URL = "https://scrambles.fly.dev"

async def run_latency_test():
    connector = aiohttp.TCPConnector(verify_ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        print("[Host] Cleaning up any existing room...")
        pin = "1234"
        await session.post(f"{HOST_URL}/api/close", headers={"x-host-pin": pin})
        
        print("[Host] Opening room...")
        answers = ["TESTING", "latency", "benchmark"]
        async with session.post(f"{HOST_URL}/api/room", json={"pin": pin, "answers": answers}) as resp:
            data = await resp.json()
            if "join_code" not in data:
                print(f"[Host] Failed to open room: {data}")
                return
            code = data["join_code"]
            print(f"[Host] Room created: {code}")

        print("[Players] Joining 100 players...")
        players = []
        for i in range(100):
            pid = str(uuid.uuid4())
            name = f"Bot-{i+1}"
            players.append({"pid": pid, "name": name, "code": code})
        
        successful_players = []
        for i, p in enumerate(players):
            async with session.post(f"{HOST_URL}/api/join", json=p) as resp:
                if resp.status == 200:
                    successful_players.append(p)
                else:
                    print(f"Join failed for {p['name']}: {await resp.text()}")
            # small delay to prevent overflowing the server socket backlog
            await asyncio.sleep(0.05)
            if (i+1) % 25 == 0:
                print(f"  Joined {i+1}/100")

        players = successful_players
        print(f"[Players] {len(players)} joined successfully. Marking all as ready...")
        async def ready_up(p):
            async with session.post(f"{HOST_URL}/api/ready", json={"code": p["code"], "pid": p["pid"], "ready": True}) as resp:
                pass
        
        # chunk ready_ups
        for i in range(0, 100, 20):
            await asyncio.gather(*(ready_up(p) for p in players[i:i+20]))

        print("[Host] Setting to 10 teams and assigning colors...")
        headers = {"x-host-pin": pin}
        async with session.post(f"{HOST_URL}/api/host/teams", json={"team_count": 10}, headers=headers) as resp:
            pass
        async with session.post(f"{HOST_URL}/api/host/assign", headers=headers) as resp:
            pass
            
        print("[Host] Colors assigned! Starting polling loops...")
        
        client_states = {p["pid"]: {"etag": None, "received_round": -1, "latency": 0, "color": -1} for p in players}
        all_received = asyncio.Event()
        received_count = 0
        round_trigger_time = 0
        
        async def poll_loop(p):
            nonlocal received_count
            pid = p["pid"]
            
            # Initial poll
            async with session.get(f"{HOST_URL}/api/state?pid={pid}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    client_states[pid]["etag"] = resp.headers.get("etag")
                    if data.get("you") and data["you"].get("team_number") is not None:
                        client_states[pid]["color"] = data["you"]["team_number"] - 1
            
            while not all_received.is_set():
                h = {}
                if client_states[pid]["etag"]:
                    h["If-None-Match"] = client_states[pid]["etag"]
                
                try:
                    async with session.get(f"{HOST_URL}/api/state?pid={pid}", headers=h) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            client_states[pid]["etag"] = resp.headers.get("etag")
                            r = data.get("round")
                            if r and r.get("idx") == 0:
                                if client_states[pid]["received_round"] < 0:
                                    client_states[pid]["received_round"] = 0
                                    client_states[pid]["latency"] = time.time() - round_trigger_time
                                    received_count += 1
                                    if received_count % 10 == 0:
                                        print(f"  {received_count}/100 players received round...")
                                    if received_count >= len(players):
                                        all_received.set()
                except Exception:
                    pass
                                
                # Random polling interval between 0.5s and 1.5s (average 1s)
                await asyncio.sleep(0.5 + random.random() * 1.0)

        polling_tasks = [asyncio.create_task(poll_loop(p)) for p in players]
        
        print("[Test] Letting polling stabilize for 3 seconds...")
        await asyncio.sleep(3)
        
        print("[Host] Pressing 'Next Word' (triggering round 0)...")
        round_trigger_time = time.time()
        async with session.post(f"{HOST_URL}/api/host/round", json={"action": "next"}, headers=headers) as resp:
            pass
            
        print("[Host] Triggered! Waiting for clients to receive...")
        
        try:
            await asyncio.wait_for(all_received.wait(), timeout=15.0)
            print("[Test] All clients received the update!")
        except asyncio.TimeoutError:
            print(f"[Test] Timeout! Only {received_count}/{len(players)} clients received it.")
            
        for t in polling_tasks:
            t.cancel()
            
        print("\n=== LATENCY BENCHMARK RESULTS ===")
        teams = {}
        for pid, st in client_states.items():
            if st["received_round"] == 0:
                c = st["color"]
                teams.setdefault(c, []).append(st["latency"])
                
        for c, lats in sorted(teams.items()):
            min_l = min(lats)
            max_l = max(lats)
            avg_l = sum(lats)/len(lats)
            spread = max_l - min_l
            status = "PASS" if spread <= 1.0 else "FAIL"
            print(f"Team {c} ({len(lats)} players): Avg {avg_l:.3f}s | Min {min_l:.3f}s | Max {max_l:.3f}s | Spread: {spread:.3f}s [{status}]")

asyncio.run(run_latency_test())
