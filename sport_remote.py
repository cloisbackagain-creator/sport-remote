#!/usr/bin/env python3
"""Sport Remote desktop bridge.

Talks to a Focus V CARTA Sport over Bluetooth (via BlueZ/bleak) and serves the
Sport Remote web UI on http://localhost:8766, relaying frames over a WebSocket.
This lets the UI run in any browser, including Firefox, which has no Web Bluetooth.

Sharing: the host can open a Cloudflare quick tunnel to a second, guest-only port (8767).
Guests need a personal invite link carrying a random 256-bit key; they can only start or
cancel sessions (or just watch), never change settings, presets, lights or power.

Relay: when the PC can't reach the Carta (out of range), a guest who can control sessions may
connect it from their phone or PC (Web Bluetooth). Their page then carries the link for everyone:
it forwards the Carta's frames here and performs the writes this bridge sends it.
"""
import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import sys
import time
import webbrowser
from pathlib import Path

from aiohttp import web, WSMsgType
from bleak import BleakClient, BleakScanner

HOST, PORT, GUEST_PORT = "127.0.0.1", 8766, 8767
SVC = "1011123e-8535-b5a0-7140-a304d2495cb7"
NOTIFY = "1011123e-8535-b5a0-7140-a304d2495cb8"
WRITE = "1011123e-8535-b5a0-7140-a304d2495cb9"
# Device Information (180A), User Data (181C: the official app keeps serial + color code here), Battery (180F)
INFO_CHARS = {"model": "2a24", "serial": "2a25", "hardware": "2a27", "firmware": "2a28",
              "manufacturer": "2a29", "userSerial": "2a8a", "colorCode": "2a90", "battery": "2a19"}
RELEASE_SECONDS = 5     # no host window this long: let go of the Carta and stop sharing (covers F5 reloads)
IDLE_EXIT_SECONDS = 30  # quit once no host window/tab has been open this long
HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sport-remote"
GUESTS_FILE = CONFIG / "guests.json"
CLOUDFLARED = shutil.which("cloudflared") or "/home/linuxbrew/.linuxbrew/bin/cloudflared"

# What each guest role may send to the device, by command byte. Reads: 0xDD clock sync (makes the
# device dump its counters) and 0xFA session-log read. 0xCC is the session command (set/start/cancel).
GUEST_WRITES = {"watch": {0xDD, 0xFA}, "control": {0xDD, 0xFA, 0xCC}}
FAIL_LIMIT, FAIL_WINDOW = 5, 15 * 60       # per address: 5 wrong keys -> locked out for 15 min
GLOBAL_FAIL_LIMIT = 30                     # all addresses together, same window
GUEST_MSGS_PER_SEC = 10
MAX_GUEST_SOCKETS = 20                     # open guest connections (joined or still proving their key)
# Sent with every page: only the app's own script may run, nothing loads from elsewhere,
# and no other site may embed the page.
SEC_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self' ws: wss:; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cache-Control": "no-store",
}
guest_sockets = 0


def is_carta(d, ad):
    """Match a CARTA Sport even if the user renamed it.

    It advertises Device Information (180A) plus manufacturer data like b'{S3TG|<serial>...}',
    where S3xx is the Sport model code (D9xx = CARTA 2, E5xx = AERIS).
    """
    if SVC in [u.lower() for u in ad.service_uuids]:
        return True
    if "carta" in (d.name or ad.local_name or "").lower():
        return True
    return any(v[:3] in (b"{S3", b"{D9", b"{E5") for v in ad.manufacturer_data.values())


class Bridge:
    def __init__(self):
        self.clients: dict[web.WebSocketResponse, dict] = {}   # ws -> {"role": host|control|watch, ...}
        self.client: BleakClient | None = None
        self.name = ""
        self.want = False           # someone wants a connection (drives auto-reconnect)
        self.write_no_resp = True
        self.task: asyncio.Task | None = None
        self.state = {"type": "status", "state": "disconnected"}
        self.relay: web.WebSocketResponse | None = None   # guest page carrying the Bluetooth link
        self.relay_name = ""

    async def send_to(self, targets, msg):
        dead = []
        for ws in targets:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.pop(ws, None)

    async def broadcast(self, msg):
        if msg.get("type") == "status":
            self.state = msg
        await self.send_to(list(self.clients), msg)

    async def to_hosts(self, msg):
        await self.send_to([ws for ws, c in self.clients.items() if c["role"] == "host"], msg)

    def hosts(self):
        return sum(c["role"] == "host" for c in self.clients.values())

    async def status(self, state, **extra):
        print(f"[bridge] {state} {extra.get('message', '')}".rstrip(), flush=True)
        await self.broadcast({"type": "status", "state": state, "name": self.name, **extra})

    # ---- BLE ----
    async def find_device(self):
        await self.status("scanning")
        return await BleakScanner.find_device_by_filter(is_carta, timeout=8.0)

    async def connect_loop(self):
        """Keep the Carta connected for as long as a window wants it: wait for it to be
        switched on, reconnect after it sleeps or drops, never give up on its own."""
        attempt = 0
        while self.want:
            if self.relay:              # a guest's phone holds the Carta; the PC can't connect anyway
                await asyncio.sleep(1)
                continue
            try:
                dev = await self.find_device()
                if self.relay:
                    continue
                if not dev:
                    await self.status("waiting", message="Waiting for the Carta. Press V 5x to turn it on.")
                    await asyncio.sleep(2)
                    continue
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
                await done.wait()           # until the link drops (sleep, power off, out of range)
                self.client = None
                if self.want:
                    await self.status("waiting", message="Carta disconnected. Reconnecting when it's back.")
            except Exception as e:  # BLE errors are many and varied; report and retry
                self.client = None
                attempt += 1
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

    # ---- relay through a guest's phone ----
    async def relay_up(self, ws, who, device_name, kind):
        self.relay, self.relay_name = ws, f"{who}'s {kind}"
        self.name = device_name or "CARTA SPORT"
        await self.status("connected", via=self.relay_name)

    async def relay_down(self, message=None):
        if not self.relay:
            return
        who, self.relay = self.relay_name, None
        if self.want:
            await self.status("waiting", message=message or f"The Carta dropped from {who}. Reconnecting when it's back.")
        else:
            await self.status("disconnected")

    async def release_relay(self):
        """Ask the relaying phone to let go, e.g. so the PC can take the Carta back."""
        if self.relay:
            await self.send_to([self.relay], {"type": "relay", "action": "release"})

    async def disconnect(self):
        self.want = False
        if self.relay:
            await self.release_relay()
            return
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        elif self.task and not self.task.done():
            self.task.cancel()
            await self.status("disconnected")

    async def info(self, ws=None):
        """Read the standard GATT info characteristics (model, serial, firmware...)."""
        if self.relay or not (self.client and self.client.is_connected):
            return
        out = {}
        for key, short in INFO_CHARS.items():
            try:
                raw = await self.client.read_gatt_char(f"0000{short}-0000-1000-8000-00805f9b34fb")
                out[key] = raw[0] if key == "battery" else raw.decode("utf-8", "replace").strip("\x00 ")
            except Exception:
                pass  # not every unit exposes every characteristic
        msg = {"type": "info", "data": out}
        await (self.send_to([ws], msg) if ws else self.broadcast(msg))

    async def write(self, data, ws=None):
        async def err(m):
            await (self.send_to([ws], {"type": "error", "message": m}) if ws else self.broadcast({"type": "error", "message": m}))
        if self.relay:
            await self.send_to([self.relay], {"type": "write", "data": data})
            return
        if not (self.client and self.client.is_connected):
            await err("Not connected")
            return
        try:
            await self.client.write_gatt_char(WRITE, bytes(data), response=not self.write_no_resp)
        except Exception as e:
            await err(f"Write failed: {e}")


bridge = Bridge()


# ---------- Guests (invite links) ----------
class Guests:
    """Invites live in ~/.config/sport-remote/guests.json (readable only by you).
    Each holds a random key; the link carries it after '#', so it never reaches a server log."""

    def __init__(self):
        try:
            self.items = json.loads(GUESTS_FILE.read_text())["guests"]
        except Exception:
            self.items = {}
        self.fails: dict[str, list[float]] = {}
        self.all_fails: list[float] = []

    def save(self):
        CONFIG.mkdir(parents=True, exist_ok=True)
        tmp = GUESTS_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"guests": self.items}, f, indent=1)
        os.replace(tmp, GUESTS_FILE)

    def add(self, name, role, days):
        gid = secrets.token_hex(4)
        self.items[gid] = {"name": (name or "Guest").strip()[:24] or "Guest",
                           "role": "watch" if role == "watch" else "control",
                           "key": secrets.token_urlsafe(32),
                           "created": int(time.time()),
                           "expires": int(time.time() + days * 86400) if days else 0,
                           "last": 0}
        self.save()
        return gid

    def remove(self, gid):
        if self.items.pop(gid, None):
            self.save()

    def valid(self, gid):
        g = self.items.get(gid)
        return bool(g) and (not g["expires"] or g["expires"] > time.time())

    def check(self, key):
        """Constant-time compare against every invite; returns the guest id or None."""
        found = None
        if isinstance(key, str) and 20 <= len(key) <= 100:
            for gid, g in self.items.items():
                if secrets.compare_digest(g["key"].encode(), key.encode()) and self.valid(gid):
                    found = gid
        return found

    def _prune(self, now):
        self.all_fails = [t for t in self.all_fails if now - t < FAIL_WINDOW]
        for ip in list(self.fails):
            self.fails[ip] = [t for t in self.fails[ip] if now - t < FAIL_WINDOW]
            if not self.fails[ip]:
                del self.fails[ip]

    def locked(self, ip):
        self._prune(time.time())
        return len(self.fails.get(ip, [])) >= FAIL_LIMIT or len(self.all_fails) >= GLOBAL_FAIL_LIMIT

    def fail(self, ip):
        now = time.time()
        self.fails.setdefault(ip, []).append(now)
        self.all_fails.append(now)
        print(f"[share] wrong invite key from {ip}", flush=True)


guests = Guests()


# ---------- Cloudflare quick tunnel ----------
class Tunnel:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.url = ""
        self.error = ""

    @property
    def running(self):
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        if self.running:
            return
        if not Path(CLOUDFLARED).exists():
            self.error = "cloudflared isn't installed. Run: brew install cloudflared"
            return
        self.url, self.error = "", ""
        # A config file would turn this into a named tunnel; point it at an empty one.
        self.proc = await asyncio.create_subprocess_exec(
            CLOUDFLARED, "tunnel", "--no-autoupdate", "--config", "/dev/null",
            "--url", f"http://127.0.0.1:{GUEST_PORT}",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        asyncio.create_task(self.watch(self.proc))

    async def watch(self, proc):
        # The address is printed before Cloudflare can route to it (530 errors until then);
        # only hand out links once a connection is registered.
        url = ""
        async for line in proc.stderr:
            m = re.search(rb"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not url:
                url = m.group().decode()
            if url and not self.url and b"Registered tunnel connection" in line:
                self.url = url
                print(f"[share] tunnel up at {self.url}", flush=True)
                await push_share()
        await proc.wait()
        if self.proc is proc:
            if not self.url:
                self.error = "Couldn't reach Cloudflare. Check the internet connection and try again."
            self.proc, self.url = None, ""
            print("[share] tunnel closed", flush=True)
            await push_share()
            await kick_guests()

    async def stop(self):
        if self.running:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()


tunnel = Tunnel()


def share_state():
    online = {c.get("id") for c in bridge.clients.values() if c["role"] != "host"}
    return {"type": "share", "running": tunnel.running, "url": tunnel.url, "error": tunnel.error,
            "guests": [{"id": gid, "name": g["name"], "role": g["role"], "expires": g["expires"],
                        "expired": not guests.valid(gid), "online": gid in online,
                        "link": f"{tunnel.url}/#k={g['key']}" if tunnel.url else ""}
                       for gid, g in sorted(guests.items.items(), key=lambda kv: kv[1]["created"])]}


async def push_share():
    await bridge.to_hosts(share_state())


async def kick_guests(gid=None):
    for ws, c in list(bridge.clients.items()):
        if c["role"] != "host" and (gid is None or c.get("id") == gid):
            bridge.clients.pop(ws, None)
            await ws.close(message=b"removed")


async def handle_share(ws, m):
    action = m.get("action")
    if action == "start":
        await tunnel.start()
    elif action == "stop":
        await tunnel.stop()
        await kick_guests()
    elif action == "add":
        try:
            days = max(0, min(int(m.get("days", 1)), 365))
        except (TypeError, ValueError):
            days = 1
        guests.add(str(m.get("name", "")), m.get("role"), days)
    elif action == "remove":
        guests.remove(str(m.get("id")))
        await kick_guests(str(m.get("id")))
    elif action == "qr":
        g = share_state()["guests"]
        link = next((x["link"] for x in g if x["id"] == m.get("id")), "")
        if link:
            import io
            import segno
            buf = io.BytesIO()
            segno.make(link, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff", xmldecl=False)
            await ws.send_json({"type": "qr", "id": m.get("id"), "svg": buf.getvalue().decode()})
        return
    await push_share()


# ---------- Web ----------
def local_only(request):
    """The host port controls everything, so refuse other sites (a WebSocket isn't bound by CORS)
    and DNS-rebinding tricks: only pages served from this bridge may talk to it."""
    host = request.host.split(":")[0]
    if host not in ("localhost", "127.0.0.1"):
        return False
    origin = request.headers.get("Origin")
    return origin is None or origin in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}")


async def ws_handler(request):
    if not local_only(request):
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    bridge.clients[ws] = {"role": "host"}
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
        elif t == "info":
            await bridge.info()
        elif t == "write" and isinstance(m.get("data"), list):
            await bridge.write([int(b) & 0xFF for b in m["data"]])
        elif t == "reclaim":           # use the PC's own Bluetooth again
            await bridge.release_relay()
            await bridge.connect()
        elif t == "share":
            try:
                await handle_share(ws, m)
            except Exception as e:  # keep the host's connection alive whatever goes wrong here
                print(f"[share] {m.get('action')} failed: {e!r}", flush=True)
                await ws.send_json({"type": "error", "message": f"Sharing: {e}"})
    bridge.clients.pop(ws, None)
    return ws


async def guest_ws(request):
    """Guest socket (reached through the tunnel). The first message must be a valid invite key."""
    global guest_sockets
    if guest_sockets >= MAX_GUEST_SOCKETS:
        raise web.HTTPServiceUnavailable()
    guest_sockets += 1
    try:
        return await guest_session(request)
    finally:
        guest_sockets -= 1


async def guest_session(request):
    ip = request.headers.get("Cf-Connecting-IP") or request.remote or "?"
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
    await ws.prepare(request)
    if guests.locked(ip):
        await ws.send_json({"type": "auth", "ok": False, "message": "Too many wrong attempts. Try again in 15 minutes."})
        await ws.close()
        return ws
    try:
        first = await ws.receive(timeout=10)
        key = json.loads(first.data).get("key") if first.type == WSMsgType.TEXT else None
    except Exception:
        key = None
    if not isinstance(key, str):
        # no key at all (page closed or reloaded before joining): not a guess, so no lockout
        await ws.close()
        return ws
    gid = guests.check(key)
    if not gid:
        guests.fail(ip)
        await asyncio.sleep(1)
        await ws.send_json({"type": "auth", "ok": False, "message": "This invite link isn't valid anymore. Ask for a new one."})
        await ws.close()
        return ws

    g = guests.items[gid]
    g["last"] = int(time.time())
    guests.save()
    bridge.clients[ws] = {"role": g["role"], "id": gid, "name": g["name"]}
    print(f"[share] {g['name']} joined ({g['role']})", flush=True)
    await ws.send_json({"type": "auth", "ok": True, "role": g["role"], "name": g["name"]})
    await ws.send_json(bridge.state)
    await push_share()
    await bridge.to_hosts({"type": "guest", "event": "joined", "name": g["name"]})

    stamps: list[float] = []
    async for msg in ws:
        if msg.type != WSMsgType.TEXT or ws not in bridge.clients:
            continue
        try:
            m = json.loads(msg.data)
        except ValueError:
            continue
        t = m.get("type")
        if t == "frame":
            # the Carta speaking through this guest's phone
            if bridge.relay is ws and isinstance(m.get("data"), list) and 0 < len(m["data"]) <= 32:
                await bridge.broadcast({"type": "frame", "data": [int(b) & 0xFF for b in m["data"]]})
            continue
        now = time.monotonic()
        stamps = [t for t in stamps if now - t < 1] + [now]
        if len(stamps) > GUEST_MSGS_PER_SEC or not guests.valid(gid):
            continue
        if t == "relay" and g["role"] == "control":
            if m.get("action") == "up":
                if (bridge.client and bridge.client.is_connected) or (bridge.relay and bridge.relay is not ws):
                    await ws.send_json({"type": "relay", "action": "release", "message": "The Carta is already connected."})
                else:
                    kind = m.get("kind") if m.get("kind") in ("phone", "tablet", "PC") else "device"
                    print(f"[share] {g['name']}'s {kind} is carrying the Carta", flush=True)
                    name = re.sub(r"[^\w .'-]", "", str(m.get("name", "")))[:32]   # plain text only
                    await bridge.relay_up(ws, g["name"], name, kind)
            elif m.get("action") == "down" and bridge.relay is ws:
                await bridge.relay_down()
        elif t == "connect":
            await bridge.connect()
        elif t == "info":
            await bridge.info(ws)
        elif t == "write" and isinstance(m.get("data"), list) and 0 < len(m["data"]) <= 32:
            data = [int(b) & 0xFF for b in m["data"]]
            if data[0] in GUEST_WRITES[g["role"]]:
                await bridge.write(data, ws)
            else:
                await ws.send_json({"type": "error", "message": "Only the host can change that."})
    if bridge.relay is ws:
        await bridge.relay_down(f"{g['name']} left, so the Carta disconnected. Reconnecting when it's back.")
    if bridge.clients.pop(ws, None):
        print(f"[share] {g['name']} left", flush=True)
    await push_share()
    return ws


def page_file(name):
    async def handler(request):
        if request.app["host"] and not local_only(request):
            raise web.HTTPForbidden()
        return web.FileResponse(HERE / name, headers=SEC_HEADERS)
    return handler


async def ping(request):
    return web.json_response({"bridge": True, "guest": not request.app["host"]})


async def idle_watchdog(stop: asyncio.Event):
    idle = 0
    while True:
        await asyncio.sleep(1)
        idle = 0 if bridge.hosts() else idle + 1
        if idle == RELEASE_SECONDS and (bridge.want or bridge.relay or tunnel.running):
            # the window is closed: stop holding the Carta so a phone can use it right away
            print("[bridge] window closed, releasing the Carta", flush=True)
            await tunnel.stop()
            await kick_guests()
            await bridge.disconnect()
        if idle >= IDLE_EXIT_SECONDS:
            print("[bridge] no open windows, shutting down", flush=True)
            stop.set()
            return


def make_app(host: bool):
    app = web.Application()
    app["host"] = host
    app.router.add_get("/", page_file("index.html"))
    app.router.add_get("/app.js", page_file("app.js"))
    app.router.add_get("/bridge", ping)
    app.router.add_get("/ws", ws_handler if host else guest_ws)
    return app


async def serve(open_browser: bool):
    url = f"http://localhost:{PORT}/"
    runners = []
    for host, port in ((True, PORT), (False, GUEST_PORT)):
        runner = web.AppRunner(make_app(host), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, HOST, port).start()
        runners.append(runner)
    print(f"[bridge] Sport Remote running at {url}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    watchdog = asyncio.create_task(idle_watchdog(stop))
    if open_browser:
        loop.call_later(0.5, webbrowser.open, url)
    await stop.wait()

    watchdog.cancel()
    await tunnel.stop()
    await bridge.disconnect()
    for ws in list(bridge.clients):
        await ws.close()
    for runner in runners:
        await runner.cleanup()


def main():
    # If a bridge is already running, just open another tab on it.
    import socket
    with socket.socket() as s:
        if s.connect_ex((HOST, PORT)) == 0:
            webbrowser.open(f"http://localhost:{PORT}/")
            return
    asyncio.run(serve("--no-browser" not in sys.argv))


if __name__ == "__main__":
    main()
