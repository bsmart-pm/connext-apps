from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any, Deque, Dict, Optional
from urllib.parse import parse_qs, urlparse
import webbrowser

import rti.asyncio
import rti.connextdds as dds

from Drone_Demo.fleet_common import (
	COMMAND_ACTIONS,
	COMMAND_PARTICIPANT,
	DRONE_IDS,
	DroneCommand,
	DroneTelemetry,
	STOP_BY_ID,
	WAREHOUSE_DEPOT,
	WAREHOUSE_STOPS,
	create_topic,
	make_command,
	normalize_action,
)


class CommandHub:
	def __init__(self):
		self._lock = Lock()
		self.telemetry: Dict[str, Dict[str, Any]] = {}
		self.sent_commands: Deque[Dict[str, Any]] = deque(maxlen=20)

	def record_telemetry(self, **fields: Any):
		with self._lock:
			self.telemetry[fields["drone_id"]] = dict(fields)

	def record_command(self, **fields: Any):
		with self._lock:
			self.sent_commands.appendleft(dict(fields))

	def snapshot(self) -> Dict[str, Any]:
		with self._lock:
			now = time.monotonic()
			drones = []
			for drone_id in DRONE_IDS:
				if drone_id not in self.telemetry:
					continue
				drone = dict(self.telemetry[drone_id])
				drone["telemetry_age_sec"] = max(0.0, now - float(drone.get("last_update_sec", now)))
				drone["command_age_sec"] = max(0.0, now - float(drone.get("last_command_sec", now))) if drone.get("last_command_sec") else None
				drones.append(drone)
			active = sum(1 for drone in drones if drone["mode"] not in {"HOVER", "PAUSE"})
			battery_avg = sum(drone["battery_pct"] for drone in drones) / len(drones) if drones else 0.0
			return {
				"drones": drones,
				"active": active,
				"idle": len(drones) - active,
				"battery_avg": battery_avg,
				"sent_commands": list(self.sent_commands),
				"stops": [
					{"stop_id": stop.stop_id, "name": stop.name, "x": stop.x, "y": stop.y}
					for stop in WAREHOUSE_STOPS
				],
				"depot": {"name": WAREHOUSE_DEPOT.name, "x": WAREHOUSE_DEPOT.x, "y": WAREHOUSE_DEPOT.y},
			}


class CloudCommandModule:
	def __init__(
		self,
		hub: Optional[CommandHub] = None,
		participant: Optional[dds.DomainParticipant] = None,
	):
		self.hub = hub
		self.participant = participant or COMMAND_PARTICIPANT
		self.telemetry_reader = dds.DataReader(create_topic(self.participant, "DroneTelemetry", DroneTelemetry))
		self.command_writer = dds.DataWriter(create_topic(self.participant, "DroneCommand", DroneCommand))
		self.latest_telemetry: Dict[str, DroneTelemetry] = {}
		self.command_counter = 0

	def send_command(self, drone_id: str, action: str, stop_id: str = ""):
		action = normalize_action(action)
		stop_name = STOP_BY_ID[stop_id].name if stop_id in STOP_BY_ID else ""
		self.command_counter += 1
		command = make_command(
			command_id=f"cmd-{self.command_counter:04d}",
			target_drone_id=drone_id,
			action=action,
			stop_id=stop_id,
			stop_name=stop_name,
		)
		self.command_writer.write(command)
		if self.hub is not None:
			self.hub.record_command(
				command_id=command.command_id,
				target_drone_id=drone_id,
				action=action,
				stop_id=stop_id,
				stop_name=stop_name,
				source_role=command.source_role,
			)

	async def _watch_telemetry(self):
		async for telemetry in self.telemetry_reader.take_data_async():
			self.latest_telemetry[telemetry.drone_id] = telemetry
			if self.hub is not None:
				self.hub.record_telemetry(
					drone_id=telemetry.drone_id,
					drone_name=telemetry.drone_name,
					x=telemetry.x,
					y=telemetry.y,
					battery_pct=telemetry.battery_pct,
					speed_mps=telemetry.speed_mps,
					mode=telemetry.mode,
					command_state=telemetry.command_state,
					last_command_action=telemetry.last_command_action,
					last_command_sec=telemetry.last_command_sec,
					active_stop_id=telemetry.active_stop_id,
					active_stop_name=telemetry.active_stop_name,
					route_progress_pct=telemetry.route_progress_pct,
					last_update_sec=telemetry.last_update_sec,
				)

	async def run(self):
		await self._watch_telemetry()


class CloudDashboardHandler(BaseHTTPRequestHandler):
	hub: Optional[CommandHub] = None
	module: Optional[CloudCommandModule] = None

	def _send_json(self, payload: Dict[str, Any]):
		data = json.dumps(payload).encode("utf-8")
		self.send_response(200)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(data)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(data)

	def _send_html(self, html_text: str):
		data = html_text.encode("utf-8")
		self.send_response(200)
		self.send_header("Content-Type", "text/html; charset=utf-8")
		self.send_header("Content-Length", str(len(data)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(data)

	def _reject(self, status: int, message: str):
		data = json.dumps({"error": message}).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(data)))
		self.end_headers()
		self.wfile.write(data)

	def do_GET(self):
		parsed = urlparse(self.path)
		query = parse_qs(parsed.query)
		assert self.hub is not None and self.module is not None

		if parsed.path == "/":
			self._send_html(CLOUD_DASHBOARD_HTML)
			return
		if parsed.path == "/api/state":
			self._send_json(self.hub.snapshot())
			return
		if parsed.path == "/api/command":
			drone_id = query.get("drone_id", [""])[0]
			action = query.get("action", [""])[0]
			stop_id = query.get("stop_id", [""])[0]
			if action.upper() not in COMMAND_ACTIONS:
				self._reject(400, "Unknown action")
				return
			if action.upper() == "REROUTE" and stop_id not in STOP_BY_ID:
				self._reject(400, "Unknown stop")
				return
			if drone_id not in DRONE_IDS and drone_id not in {"", "*"}:
				self._reject(400, "Unknown drone")
				return
			self.module.send_command(drone_id, action, stop_id)
			self._send_json({"ok": True})
			return
		self._reject(404, "Not found")

	def log_message(self, format: str, *args: Any):
		return


CLOUD_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
	<meta charset="utf-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1" />
	<title>Cloud Command Module</title>
	<style>
		:root { --bg:#07131f; --panel:rgba(13, 24, 42, 0.92); --line:rgba(145, 173, 214, 0.2); --text:#e7f1ff; --muted:#8ca3c2; --accent:#66d9ff; --good:#6ee7b7; --warn:#fbbf24; --bad:#fb7185; }
		* { box-sizing:border-box; }
		body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--text); background: radial-gradient(circle at top left, rgba(102, 217, 255, 0.12), transparent 28%), linear-gradient(180deg, #05101a 0%, #07131f 100%); min-height:100vh; }
		.shell { width:min(1540px, calc(100vw - 32px)); margin:0 auto; padding:16px 0 28px; }
		.topbar { display:grid; grid-template-columns:1.4fr 1fr; gap:16px; margin-bottom:16px; }
		.content { display:grid; grid-template-columns:1.35fr 1fr; gap:16px; }
		.panel { background:var(--panel); border:1px solid var(--line); border-radius:20px; box-shadow:0 20px 45px rgba(0,0,0,0.28); backdrop-filter:blur(12px); }
		.brand { padding:20px 24px; }
		.brand h1 { margin:0; font-size:28px; }
		.brand p { margin:6px 0 0; color:var(--muted); }
		.summary-grid { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:10px; margin-top:16px; }
		.metric { background:rgba(19, 34, 57, 0.92); border:1px solid var(--line); border-radius:16px; padding:12px 14px; }
		.metric .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:0.12em; }
		.metric .value { font-size:24px; margin-top:6px; font-weight:700; }
		.card { padding:16px; }
		.card h2 { margin:0 0 12px; font-size:18px; }
		.muted { color:var(--muted); }
		.command-log, .drone-list { display:grid; gap:10px; }
		.command-item, .drone-card { border:1px solid var(--line); border-radius:16px; padding:14px; background:rgba(255,255,255,0.04); }
		.command-item .title { font-weight:700; }
		.command-item .meta-line { display:block; margin-top:4px; font-size:13px; color:var(--muted); }
		.drone-top { display:flex; justify-content:space-between; gap:10px; align-items:center; flex-wrap:wrap; }
		.drone-name { font-size:18px; font-weight:700; }
		.pill { display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border-radius:999px; font-size:12px; letter-spacing:0.08em; text-transform:uppercase; border:1px solid rgba(255,255,255,0.1); background:rgba(255,255,255,0.06); }
		.pill.ok { color:#d5ffe8; background:rgba(110, 231, 183, 0.11); }
		.pill.warn { color:#fff3c6; background:rgba(251, 191, 36, 0.14); }
		.pill.bad { color:#ffe0e7; background:rgba(251, 113, 133, 0.14); }
		.meta { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:10px; margin-top:10px; font-size:14px; color:var(--muted); }
		.meta strong { color:var(--text); }
		.battery { margin-top:10px; height:10px; border-radius:999px; background:rgba(255,255,255,0.08); overflow:hidden; }
		.battery-fill { height:100%; border-radius:inherit; background:linear-gradient(90deg, #ef4444 0%, #f59e0b 48%, #84cc16 100%); }
		.actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
		.map-card { min-height:520px; overflow:hidden; }
		svg { width:100%; height:460px; display:block; }
		.map-note { margin-top:10px; font-size:13px; color:var(--muted); }
		.hidden { display:none !important; }
		.marker-id { font-size:12px; font-weight:800; fill:#06111f; text-anchor:middle; pointer-events:none; }
		button { appearance:none; border:0; border-radius:999px; padding:10px 14px; color:white; background:#1f3356; cursor:pointer; font-weight:600; transition:transform 0.16s ease, box-shadow 0.16s ease, background 0.16s ease; }
		button:hover { transform:translateY(-1px); box-shadow:0 10px 18px rgba(0,0,0,0.22); }
		button.primary { background:linear-gradient(135deg, #2f6af7, #4ab0ff); }
		button.warn { background:linear-gradient(135deg, #cc8b12, #f7b733); }
		button.bad { background:linear-gradient(135deg, #b4234e, #ff6a88); }
		.dialog-backdrop { position:fixed; inset:0; background:rgba(3, 8, 15, 0.72); display:none; align-items:center; justify-content:center; z-index:20; }
		.dialog { width:min(540px, calc(100vw - 32px)); background:#0d1728; border:1px solid var(--line); border-radius:22px; padding:20px; box-shadow:0 30px 80px rgba(0,0,0,0.5); }
		.dialog h3 { margin:0 0 10px; }
		.dialog select { width:100%; margin-top:8px; background:#13213a; color:var(--text); border:1px solid var(--line); border-radius:12px; padding:12px; font-size:15px; }
		.dialog-actions { display:flex; justify-content:flex-end; gap:10px; margin-top:16px; }
	</style>
</head>
<body>
	<div class="shell">
		<div class="topbar">
			<div class="panel brand">
				<h1>Cloud Command Module</h1>
				<p>Domain 20 subscribes to bridged telemetry and publishes commands back toward Domain 10.</p>
				<div class="summary-grid" id="summaryGrid"></div>
			</div>
			<div class="panel card">
				<h2>Command History</h2>
				<div class="muted">Use the buttons below to emit return-to-charge, reroute, maintenance, hover, pause, or resume commands.</div>
				<div class="command-log" id="commandLog" style="margin-top:12px;"></div>
			</div>
		</div>
		<div class="content">
			<div class="panel card map-card hidden" id="mapPanel">
				<div class="map-caption">
					<div>
						<h2 style="margin-bottom:4px;">Yard Map</h2>
						<div class="muted">Depot, stops, live drone markers, and target lines.</div>
					</div>
				</div>
				<svg id="mapSvg" viewBox="0 0 1000 460" preserveAspectRatio="none"></svg>
				<div class="map-note">The map appears once telemetry starts arriving.</div>
			</div>
			<div class="panel card">
				<h2>Telemetry</h2>
				<div class="drone-list" id="droneList"></div>
			</div>
		</div>
	</div>

	<div class="dialog-backdrop" id="rerouteDialog">
		<div class="dialog">
			<h3>Reroute Drone</h3>
			<div class="muted" id="rerouteLabel">Select a destination stop.</div>
			<label for="rerouteStopSelect" class="muted" style="display:block; margin-top:12px;">Destination stop</label>
			<select id="rerouteStopSelect"></select>
			<div class="dialog-actions">
				<button onclick="closeReroute()">Cancel</button>
				<button class="primary" onclick="confirmReroute()">Send reroute</button>
			</div>
		</div>
	</div>

	<script>
		let latestState = null;
		let rerouteDroneId = null;
		const droneColors = ['#880808', '#7cdaff', '#6ee7b7', '#f472b6', '#fbbf24'];

		function esc(value) {
			return String(value ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
		}

		function commandClass(mode, state) {
			if (mode === 'HOVER' || mode === 'PAUSE') return 'warn';
			if (state === 'EXECUTING') return 'warn';
			return 'ok';
		}

		function yardX(x) {
			return 80 + (x / 34) * 840;
		}

		function yardY(y) {
			return 60 + ((14 - y) / 14) * 340;
		}

		function depotSlotPoint(index) {
			const columns = 3;
			const slotWidth = 62;
			const rowGap = 54;
			const colGap = 84;
			const leftX = 68;
			const topY = 206;
			const row = Math.floor(index / columns);
			const col = index % columns;
			return {
				x: leftX + col * colGap + slotWidth / 2,
				y: topY + row * rowGap,
			};
		}

		async function api(path) {
			const response = await fetch(path, { cache: 'no-store' });
			if (!response.ok) throw new Error(await response.text());
			return response.json();
		}

		async function postAction(path) {
			await api(path);
			await refresh();
		}

		function openReroute(droneId) {
			rerouteDroneId = droneId;
			const drone = latestState?.drones?.find(item => item.drone_id === droneId);
			document.getElementById('rerouteLabel').textContent = `Select a new destination stop for ${drone?.drone_name || droneId}.`;
			const select = document.getElementById('rerouteStopSelect');
			select.innerHTML = latestState.stops.map(stop => `<option value="${esc(stop.stop_id)}">${esc(stop.name)}</option>`).join('');
			document.getElementById('rerouteDialog').style.display = 'flex';
		}

		function closeReroute() {
			rerouteDroneId = null;
			document.getElementById('rerouteDialog').style.display = 'none';
		}

		async function confirmReroute() {
			const stopId = document.getElementById('rerouteStopSelect').value;
			if (rerouteDroneId && stopId) {
				await api(`/api/command?drone_id=${encodeURIComponent(rerouteDroneId)}&action=REROUTE&stop_id=${encodeURIComponent(stopId)}`);
				await refresh();
			}
			closeReroute();
		}

		function renderSummary(state) {
			const metrics = [
				['Telemetry', state.drones.length],
				['Active', state.active],
				['Idle', state.idle],
				['Avg Battery', `${state.battery_avg.toFixed(1)}%`],
			];
			document.getElementById('summaryGrid').innerHTML = metrics.map(([label, value]) => `
				<div class="metric"><div class="label">${label}</div><div class="value">${esc(value)}</div></div>
			`).join('');
		}

		function renderCommandLog(state) {
			const container = document.getElementById('commandLog');
			if (!state.sent_commands.length) {
				container.innerHTML = '<div class="muted">No commands sent yet.</div>';
				return;
			}
			container.innerHTML = state.sent_commands.slice(0, 6).map(command => `
				<div class="command-item">
					<div class="title">${esc(command.action)} → ${esc(command.target_drone_id || 'all drones')}</div>
					<span class="meta-line">${esc(command.stop_name || command.stop_id || 'no stop')} · ${esc(command.source_role || 'cloud_command')}</span>
				</div>
			`).join('');
		}

		function renderMap(state) {
			const panel = document.getElementById('mapPanel');
			const svg = document.getElementById('mapSvg');
			if (!state.drones.length) {
				panel.classList.add('hidden');
				svg.innerHTML = '';
				return;
			}
			panel.classList.remove('hidden');

			const stops = state.stops.map(stop => {
				const x = yardX(stop.x);
				const y = yardY(stop.y);
				const rackWidth = 112;
				const rackHeight = 54;
				return `
					<g>
						<rect x="${x - rackWidth / 2}" y="${y - rackHeight / 2}" width="${rackWidth}" height="${rackHeight}" rx="16" fill="rgba(251,191,36,0.16)" stroke="#fbbf24" stroke-width="2.5"></rect>
						<rect x="${x - rackWidth / 2 + 8}" y="${y - rackHeight / 2 + 8}" width="${rackWidth - 16}" height="${rackHeight - 16}" rx="12" fill="rgba(255,255,255,0.05)" stroke="rgba(251,191,36,0.35)" stroke-width="1"></rect>
						<text x="${x}" y="${y + 5}" class="marker-id">${esc(stop.name)}</text>
					</g>
				`;
			}).join('');

			const drones = state.drones.map((drone, index) => {
				const x = yardX(drone.x);
				const y = yardY(drone.y);
				const color = droneColors[index % droneColors.length];
				const depotSlot = depotSlotPoint(index);
				return `
					<g>
						<rect x="${depotSlot.x - 23}" y="${depotSlot.y - 16}" width="46" height="32" rx="9" fill="rgba(255,255,255,0.08)" stroke="${color}" stroke-width="2"></rect>
						<text x="${depotSlot.x}" y="${depotSlot.y + 4}" class="marker-id">${index + 1}</text>
						<g transform="translate(${x}, ${y})">
							<circle cx="0" cy="0" r="13" fill="${color}" opacity="0.98"></circle>
							<circle cx="-18" cy="-10" r="5" fill="${color}" opacity="0.9"></circle>
							<circle cx="18" cy="-10" r="5" fill="${color}" opacity="0.9"></circle>
							<circle cx="-18" cy="10" r="5" fill="${color}" opacity="0.9"></circle>
							<circle cx="18" cy="10" r="5" fill="${color}" opacity="0.9"></circle>
							<line x1="-12" y1="-7" x2="-20" y2="-12" stroke="${color}" stroke-width="2"></line>
							<line x1="12" y1="-7" x2="20" y2="-12" stroke="${color}" stroke-width="2"></line>
							<line x1="-12" y1="7" x2="-20" y2="12" stroke="${color}" stroke-width="2"></line>
							<line x1="12" y1="7" x2="20" y2="12" stroke="${color}" stroke-width="2"></line>
							<text x="0" y="4" class="marker-id" style="fill:#eaf5ff;">${index + 1}</text>
						</g>
					</g>
				`;
			}).join('');

			svg.innerHTML = `
				<defs>
					<linearGradient id="yardFill" x1="0" x2="0" y1="0" y2="1">
						<stop offset="0%" stop-color="#143252" />
						<stop offset="100%" stop-color="#0d1f36" />
					</linearGradient>
					<pattern id="grid" width="42" height="42" patternUnits="userSpaceOnUse">
						<path d="M 42 0 L 0 0 0 42" fill="none" stroke="rgba(160,190,230,0.08)" stroke-width="1" />
					</pattern>
				</defs>
				<rect x="0" y="0" width="1000" height="460" rx="18" fill="url(#yardFill)"></rect>
				<rect x="0" y="0" width="1000" height="460" rx="18" fill="url(#grid)"></rect>
				<rect x="40" y="18" width="920" height="418" rx="28" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.12)" stroke-width="2"></rect>
				<rect x="40" y="74" width="332" height="286" rx="32" fill="rgba(110,231,183,0.12)" stroke="#6ee7b7" stroke-width="2.5"></rect>
				<rect x="56" y="92" width="300" height="250" rx="24" fill="rgba(110,231,183,0.07)" stroke="rgba(110,231,183,0.22)" stroke-width="1.5"></rect>
				${stops}
				${drones}
			`;
		}

		function actionButtons(drone) {
			return `
				<button class="warn" onclick="postAction('/api/command?drone_id=${encodeURIComponent(drone.drone_id)}&action=RETURN_TO_CHARGE')">Return to charge</button>
				<button class="warn" onclick="postAction('/api/command?drone_id=${encodeURIComponent(drone.drone_id)}&action=MAINTENANCE')">Maintenance</button>
				<button class="primary" onclick="openReroute('${esc(drone.drone_id)}')">Reroute</button>
				<button onclick="postAction('/api/command?drone_id=${encodeURIComponent(drone.drone_id)}&action=HOVER')">Hover</button>
				<button onclick="postAction('/api/command?drone_id=${encodeURIComponent(drone.drone_id)}&action=PAUSE')">Pause</button>
				<button class="bad" onclick="postAction('/api/command?drone_id=${encodeURIComponent(drone.drone_id)}&action=RESUME')">Resume</button>
			`;
		}

		function renderDrones(state) {
			const container = document.getElementById('droneList');
			if (!state.drones.length) {
				container.innerHTML = '<div class="muted">Waiting for bridged telemetry from Domain 10.</div>';
				return;
			}
			container.innerHTML = state.drones.map((drone, index) => {
				const battery = Math.max(0, Math.min(100, drone.battery_pct || 0));
				const telemetryAge = drone.telemetry_age_sec != null ? `${drone.telemetry_age_sec.toFixed(1)} sec` : 'n/a';
				return `
					<div class="drone-card">
						<div class="drone-top">
							<div class="drone-name">${esc(drone.drone_name)}</div>
							<div class="pill ${commandClass(drone.mode, drone.command_state)}">${esc(drone.mode)}</div>
						</div>
						<div class="meta">
							<div><strong>Battery</strong><br>${battery.toFixed(1)}%</div>
							<div><strong>Destination</strong><br>${esc(drone.active_stop_name || '-')}</div>
							<div><strong>Last command</strong><br>${esc(drone.last_command_action || 'NONE')}</div>
							<div><strong>Telemetry age</strong><br>${telemetryAge}</div>
						</div>
						<div style="margin-top:10px; color:var(--muted); font-size:14px;">State: ${esc(drone.command_state || 'LISTENING')}</div>
						<div class="battery"><div class="battery-fill" style="width:${battery}%;"></div></div>
						<div class="actions">${actionButtons(drone)}</div>
					</div>
				`;
			}).join('');
		}

		async function refresh() {
			latestState = await api('/api/state');
			renderSummary(latestState);
			renderCommandLog(latestState);
			renderMap(latestState);
			renderDrones(latestState);
		}

		refresh();
		setInterval(refresh, 1000);
	</script>
</body>
</html>
"""


def _run_module(module: CloudCommandModule):
	return module.run()


def run_dashboard_demo():
	hub = CommandHub()
	module = CloudCommandModule(hub=hub)
	CloudDashboardHandler.hub = hub
	CloudDashboardHandler.module = module
	server = ThreadingHTTPServer(("127.0.0.1", 0), CloudDashboardHandler)
	url = f"http://127.0.0.1:{server.server_address[1]}/"
	Thread(target=lambda: rti.asyncio.run(_run_module(module)), daemon=True).start()
	webbrowser.open_new(url)
	print(f"Cloud command dashboard opened at {url}")
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		server.shutdown()


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Cloud command module for Domain 20")
	return parser.parse_args()


def main():
	parse_args()
	try:
		run_dashboard_demo()
	except KeyboardInterrupt:
		pass


if __name__ == "__main__":
	main()
