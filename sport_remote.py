#!/usr/bin/env python3
"""Sport Remote desktop bridge.

Talks to a Focus V CARTA Sport over Bluetooth (via BlueZ/bleak) and serves the
Sport Remote web UI on http://localhost:8766, relaying frames over a WebSocket.
This lets the UI run in any browser, including Firefox, which has no Web Bluetooth.
"""
import asyncio
import json
import os
import signal
import sys
import webbrowser
from pathlib import Path

from aiohttp import web, WSMsgType
from bleak import BleakClient, BleakScanner

HOST, PORT = "127.0.0.1", 8766
SVC = "1011123e-8535-b5a0-7140-a304d2495cb7"
NOTIFY = "1011123e-8535-b5a0-7140-a304d2495cb8"
WRITE = "1011123e-8535-b5a0-7140-a304d2495cb9"
IDLE_EXIT_SECONDS = 30  # quit once no browser tab has been open this long
HERE = Path(__file__).resolve().parent


class Bridge:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()
        self.client: BleakClient | None = None
        self.name = ""
        self.want = False           # user wants a connection (drives auto-reconnect)
        self.write_no_resp = True
        self.task: asyncio.Task | None = None
        self.state = {"type": "status", "state": "disconnected"}

    async def broadcast(self, msg):
        if msg.get("type") == "status":
            self.state = msg
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def status(self, state, **extra):
        print(f"[bridge] {state} {extra.get('message', '')}".rstrip(), flush=True)
        await self.broadcast({"type": "status", "state": state, "name": self.name, **extra})

    # ---- BLE ----
    async def find_device(self):
        await self.status("scanning")
        found = await BleakScanner.find_device_by_filter(
            lambda d, ad: SVC in [u.lower() for u in ad.service_uuids]
            or (d.name or ad.local_name or "").upper().startswith("CARTA"),
            timeout=12.0,
        )
        return found

    async def connect_loop(self):
        attempt = 0
        while self.want:
            try:
                dev = await self.find_device()
                if not dev:
                    await self.status("error", message="No CARTA Sport found. Is it on (press V 5x) and nearby?")
                    self.want = False
                    return
                self.name = dev.name or "CARTA SPORT"
                await self.status("connecting")
                done = asyncio.Event()
                client = BleakClient(dev, disconnected_callback=lambda _c: done.set())
                await client.connect(timeout=15.0)
                self.client = client
                props = client.services.get_characteristic(WRITE).properties
                self.write_no_resp = "write-without-response" in props
                await client.start_notify(NOTIFY, self.on_notify)
                attempt = 0
                await self.status("connected")
                await done.wait()           # until the link drops
                self.client = None
                if self.want:
                    await self.status("reconnecting")
            except Exception as e:  # BLE errors are many and varied; report and retry
                self.client = None
                attempt += 1
                if attempt > 5:
                    await self.status("error", message=f"Connection failed: {e}")
                    self.want = False
                    return
                await self.status("reconnecting", message=str(e))
                await asyncio.sleep(min(2 * attempt, 8))
        await self.status("disconnected")

    def on_notify(self, _char, data: bytearray):
        asyncio.get_running_loop().create_task(self.broadcast({"type": "frame", "data": list(data)}))

    async def connect(self):
        if self.task and not self.task.done():
            await self.broadcast(self.state)
            return
        self.want = True
        self.task = asyncio.create_task(self.connect_loop())

    async def disconnect(self):
        self.want = False
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        elif self.task and not self.task.done():
            self.task.cancel()
            await self.status("disconnected")

    async def write(self, data):
        if not (self.client and self.client.is_connected):
            await self.broadcast({"type": "error", "message": "Not connected"})
            return
        try:
            await self.client.write_gatt_char(WRITE, bytes(data), response=not self.write_no_resp)
        except Exception as e:
            await self.broadcast({"type": "error", "message": f"Write failed: {e}"})


bridge = Bridge()


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    bridge.clients.add(ws)
    await ws.send_json(bridge.state)
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            m = json.loads(msg.data)
        except ValueError:
            continue
        t = m.get("type")
        if t == "connect":
            await bridge.connect()
        elif t == "disconnect":
            await bridge.disconnect()
        elif t == "write" and isinstance(m.get("data"), list):
            await bridge.write([int(b) & 0xFF for b in m["data"]])
    bridge.clients.discard(ws)
    return ws


async def index(_request):
    return web.FileResponse(HERE / "index.html", headers={"Cache-Control": "no-store"})


async def ping(_request):
    return web.json_response({"bridge": True})


async def idle_watchdog(app):
    idle = 0
    while True:
        await asyncio.sleep(5)
        idle = 0 if bridge.clients else idle + 5
        if idle >= IDLE_EXIT_SECONDS:
            print("[bridge] no open tabs, shutting down", flush=True)
            await bridge.disconnect()
            # GracefulExit raised inside a task is swallowed; SIGINT makes run_app shut down cleanly
            os.kill(os.getpid(), signal.SIGINT)
            return


async def on_startup(app):
    app["watchdog"] = asyncio.create_task(idle_watchdog(app))


def main():
    url = f"http://localhost:{PORT}/"
    # If a bridge is already running, just open another tab on it.
    import socket
    with socket.socket() as s:
        if s.connect_ex((HOST, PORT)) == 0:
            webbrowser.open(url)
            return
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/bridge", ping)
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(on_startup)
    if "--no-browser" not in sys.argv:
        async def open_browser(_app):
            asyncio.get_running_loop().call_later(0.5, webbrowser.open, url)
        app.on_startup.append(open_browser)
    print(f"[bridge] Sport Remote running at {url}", flush=True)
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
