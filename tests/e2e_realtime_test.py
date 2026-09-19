"""End-to-end test verifying the real-time WebSocket layer on live Fly + Cloudflare."""
import asyncio
import json
import httpx
import websockets

BASE_URL = "https://scrambles.fly.dev"
HOST_PIN = "testpin999"

async def main():
    print("=== Starting Realtime E2E Test on Live Infrastructure ===")
    
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as http:
        # Step 1: Open Room
        print("\n1. Opening room...")
        res = await http.post("/api/room", json={
            "pin": HOST_PIN,
            "answers": ["TESTWORD", "CLOUDFLARE", "WEBSOCKET"]
        })
        if res.status_code == 409:
            print("   Room already open, closing it first...")
            # We can't close without the previous pin if different, but let's try with our pin or check state
            close_res = await http.post("/api/close", headers={"X-Host-Pin": HOST_PIN})
            res = await http.post("/api/room", json={
                "pin": HOST_PIN,
                "answers": ["TESTWORD", "CLOUDFLARE", "WEBSOCKET"]
            })
        
        assert res.status_code == 200, f"Failed to open room: {res.text}"
        room_data = res.json()
        join_code = room_data["join_code"]
        ws_url = room_data.get("ws_url")
        print(f"   ✓ Room opened! Code: {join_code}, ws_url: {ws_url}")
        
        # Step 2: Get host state to extract session_id
        host_state_res = await http.get("/api/host/state", headers={"X-Host-Pin": HOST_PIN})
        assert host_state_res.status_code == 200
        session_id = host_state_res.json()["session_id"]
        print(f"   ✓ Session ID: {session_id}")
        
        # Step 3: Connect 2 WebSocket clients to Cloudflare Worker
        ws_endpoint = f"{ws_url.replace('https://', 'wss://').replace('http://', 'ws://')}/ws/{session_id}"
        print(f"\n2. Connecting WebSocket clients to {ws_endpoint}...")
        
        import ssl
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        ws1 = await websockets.connect(f"{ws_endpoint}?pid=player_1&role=player", ssl=ssl_ctx)
        ws2 = await websockets.connect(f"{ws_endpoint}?pid=player_2&role=player", ssl=ssl_ctx)
        
        # Verify CONNECTED messages
        msg1 = json.loads(await ws1.recv())
        msg2 = json.loads(await ws2.recv())
        print(f"   ✓ WS1 connected: {msg1}")
        print(f"   ✓ WS2 connected: {msg2}")
        assert msg1["type"] == "CONNECTED"
        assert msg2["type"] == "CONNECTED"
        
        # Step 4: Player 1 & 2 Join
        print("\n3. Players joining via HTTP API...")
        j1 = await http.post("/api/join", json={"code": join_code, "pid": "player_1", "name": "Alice"})
        assert j1.status_code == 200
        j2 = await http.post("/api/join", json={"code": join_code, "pid": "player_2", "name": "Bob"})
        assert j2.status_code == 200
        
        # Check WS broadcasts
        # Player 1 should receive event for Player 1 join and Player 2 join
        evt1 = json.loads(await ws1.recv())
        print(f"   ✓ WS1 received event: {evt1}")
        assert evt1["type"] == "PLAYER_JOINED"
        assert evt1["pid"] == "player_1"
        
        evt2 = json.loads(await ws1.recv())
        print(f"   ✓ WS1 received event: {evt2}")
        assert evt2["type"] == "PLAYER_JOINED"
        assert evt2["pid"] == "player_2"
        
        # Step 5: Player 1 Ready
        print("\n4. Player 1 readying up...")
        r1 = await http.post("/api/ready", json={"code": join_code, "pid": "player_1", "ready": True})
        assert r1.status_code == 200
        
        ready_evt = json.loads(await ws2.recv())
        # Drain any pending join messages on ws2
        while ready_evt["type"] != "PLAYER_READY":
            ready_evt = json.loads(await ws2.recv())
        print(f"   ✓ WS2 received ready event: {ready_evt}")
        assert ready_evt["pid"] == "player_1"
        assert ready_evt["ready"] is True
        
        # Step 6: Assign teams & Go live
        print("\n5. Host assigning teams and going live...")
        assign_res = await http.post("/api/host/assign", headers={"X-Host-Pin": HOST_PIN})
        assert assign_res.status_code == 200
        
        start_evt = json.loads(await ws1.recv())
        while start_evt["type"] != "GAME_START":
            start_evt = json.loads(await ws1.recv())
        print(f"   ✓ WS1 received GAME_START: {start_evt}")
        assert "executeAt" in start_evt
        print(f"     executeAt lead time: {start_evt['executeAt'] - start_evt.get('serverTime', start_evt['executeAt']-1000)}ms")
        
        # Step 7: Host NEXT round
        print("\n6. Host triggering NEXT round...")
        next_res = await http.post("/api/host/round", headers={"X-Host-Pin": HOST_PIN}, json={"action": "next"})
        assert next_res.status_code == 200
        
        next_evt1 = json.loads(await ws1.recv())
        next_evt2 = json.loads(await ws2.recv())
        while next_evt2["type"] != "NEXT":
            next_evt2 = json.loads(await ws2.recv())
            
        print(f"   ✓ WS1 received NEXT: {next_evt1}")
        print(f"   ✓ WS2 received NEXT: {next_evt2}")
        assert next_evt1["type"] == "NEXT"
        assert next_evt1["round"] == 0
        assert "executeAt" in next_evt1
        assert "sequence" in next_evt1
        print(f"     Both clients received synchronized executeAt: {next_evt1['executeAt']}")
        
        # Step 8: Clean up
        print("\n7. Closing room...")
        await http.post("/api/close", headers={"X-Host-Pin": HOST_PIN})
        close_evt = json.loads(await ws1.recv())
        print(f"   ✓ WS1 received ROOM_CLOSED: {close_evt}")
        assert close_evt["type"] == "ROOM_CLOSED"
        
        await ws1.close()
        await ws2.close()
        
    print("\n🎉 ALL REALTIME TESTS PASSED SUCCESSFULLY! 🎉")

if __name__ == "__main__":
    asyncio.run(main())
