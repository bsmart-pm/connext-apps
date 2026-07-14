from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any, Deque, Dict, Optional
from urllib.parse import urlparse
import webbrowser

import rti.asyncio
import rti.connextdds as dds

from Drone_Demo.fleet_common import (
	COMMAND_PARTICIPANT,
	DRONE_IDS,
	DRONE_PARTICIPANT,
	DroneCommand,
	DroneTelemetry,
	Stop,
	STOP_BY_ID,
	WAREHOUSE_DEPOT,
	WAREHOUSE_STOPS,
	build_demo_positions,
	clamp,
	create_topic,
	distance,
	is_broadcast_target,
	make_telemetry,
	move_toward,
	normalize_action,
)


class DroneHub:
	def __init__(self):
		self._lock = Lock()
		self.telemetry: Dict[str, Dict[str, Any]] = {}
		self.recent_commands: Dict[str, Dict[str, Any]] = {}

	def record_telemetry(self, **fields: Any):
		with self._lock:
			self.telemetry[fields["drone_id"]] = dict(fields)

	def record_command(self, **fields: Any):
		with self._lock:
			command_id = fields["command_id"]
			self.recent_commands[command_id] = dict(fields)

	def remove_command(self, command_id: str):
		with self._lock:
			self.recent_commands.pop(command_id, None)

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
			active = sum(1 for drone in drones if drone["mode"] not in {"PAUSE", "HOVER"})
			return {
				"drones": drones,
				"active": active,
				"idle": len(drones) - active,
				"recent_commands": [
					command
					for command in sorted(
						self.recent_commands.values(),
						key=lambda item: float(item.get("created_sec", 0.0)),
						reverse=True,
					)
					if command.get("status") in {"queued", "active"}
				],
				"stops": [
					{"stop_id": stop.stop_id, "name": stop.name, "x": stop.x, "y": stop.y}
					for stop in WAREHOUSE_STOPS
				],
				"depot": {"name": WAREHOUSE_DEPOT.name, "x": WAREHOUSE_DEPOT.x, "y": WAREHOUSE_DEPOT.y},
			}


class DroneAgent:
	def __init__(
		self,
		drone_id: str,
		start_x: float,
		start_y: float,
		route_offset: int = 0,
		route_direction: int = 1,
		hub: Optional[DroneHub] = None,
		participant: Optional[dds.DomainParticipant] = None,
	):
		self.drone_id = drone_id
		self.drone_name = drone_id.replace("-", " ").title()
		self.hub = hub
		self.participant = participant or DRONE_PARTICIPANT
		self.telemetry_writer = dds.DataWriter(create_topic(self.participant, "DroneTelemetry", DroneTelemetry))
		self.command_reader = dds.DataReader(create_topic(self.participant, "DroneCommand", DroneCommand))
		self.x = start_x
		self.y = start_y
		self.battery_pct = 100.0
		self.mode = "PATROL"
		self.command_state = "LISTENING"
		self.last_command_action = ""
		self.last_command_sec = 0.0
		self.active_stop_id = WAREHOUSE_STOPS[0].stop_id
		self.active_stop_name = WAREHOUSE_STOPS[0].name
		self.charging_station = Stop(
			stop_id=f"charge-{drone_id}",
			name=f"Depot Bay {route_offset + 1}",
			x=start_x,
			y=start_y,
		)
		self.route_progress_pct = 0.0
		self.dwell_ticks = 0
		self.maintenance_dwell_ticks = 0
		self.patrol_index = route_offset % len(WAREHOUSE_STOPS)
		self.route_direction = 1 if route_direction >= 0 else -1
		self.command_seen: set[str] = set()
		self.pending_commands: Deque[DroneCommand] = deque()
		self.active_command_id: Optional[str] = None
		self.command_target_stop_id = ""
		self.command_target_stop_name = ""
		if self.hub is not None:
			self.hub.record_telemetry(
				drone_id=self.drone_id,
				drone_name=self.drone_name,
				x=self.x,
				y=self.y,
				battery_pct=self.battery_pct,
				speed_mps=0.0,
				mode=self.mode,
				command_state=self.command_state,
				last_command_action=self.last_command_action,
				last_command_sec=self.last_command_sec,
				active_stop_id=self.active_stop_id,
				active_stop_name=self.active_stop_name,
				route_progress_pct=self.route_progress_pct,
				last_update_sec=time.monotonic(),
			)

	def _current_patrol_stop(self):
		return WAREHOUSE_STOPS[self.patrol_index % len(WAREHOUSE_STOPS)]

	def _advance_patrol_index(self):
		self.patrol_index = (self.patrol_index + self.route_direction) % len(WAREHOUSE_STOPS)

	def _is_blocked_by_charge(self) -> bool:
		return self.mode == "RETURN_TO_CHARGE" and self.battery_pct < 99.5

	def _queue_command(self, command: DroneCommand):
		self.pending_commands.append(command)
		if self.hub is not None:
			self.hub.record_command(
				command_id=command.command_id,
				drone_id=self.drone_id,
				target_drone_id=command.target_drone_id,
				action=normalize_action(command.action),
				stop_id=command.stop_id,
				stop_name=command.stop_name,
				source_role=command.source_role,
				status="queued",
			)

	def _complete_active_command(self):
		if self.active_command_id is None:
			return
		if self.hub is not None:
			self.hub.remove_command(self.active_command_id)
		self.active_command_id = None

	def _next_queued_command(self) -> Optional[DroneCommand]:
		if not self.pending_commands:
			return None
		if self.mode in {"HOVER", "PAUSE"}:
			for _ in range(len(self.pending_commands)):
				command = self.pending_commands.popleft()
				if normalize_action(command.action) == "RESUME":
					return command
				self.pending_commands.append(command)
			return None
		return self.pending_commands.popleft()

	def _start_command(self, command: DroneCommand):
		self.active_command_id = command.command_id
		self.last_command_action = normalize_action(command.action)
		self.last_command_sec = time.monotonic()
		self.command_state = "EXECUTING"
		if self.hub is not None:
			self.hub.record_command(
				command_id=command.command_id,
				drone_id=self.drone_id,
				target_drone_id=command.target_drone_id,
				action=self.last_command_action,
				stop_id=command.stop_id,
				stop_name=command.stop_name,
				source_role=command.source_role,
				status="active",
			)
		action = self.last_command_action
		if action == "HOVER":
			self.mode = "HOVER"
			self.command_state = "HOLDING"
			self._complete_active_command()
			return
		if action == "PAUSE":
			self.mode = "PAUSE"
			self.command_state = "HOLDING"
			self._complete_active_command()
			return
		if action == "RESUME":
			self.mode = "PATROL"
			self.command_state = "LISTENING"
			self._complete_active_command()
			return
		if action == "RETURN_TO_CHARGE":
			self._enter_return_to_charge()
			return
		if action == "MAINTENANCE":
			self.mode = "MAINTENANCE"
			self.command_target_stop_id = STOP_BY_ID["stop-5"].stop_id
			self.command_target_stop_name = STOP_BY_ID["stop-5"].name
			self.command_state = "EXECUTING"
			return
		if action == "REROUTE" and command.stop_id in STOP_BY_ID:
			self.mode = "REROUTE"
			self.command_target_stop_id = command.stop_id
			self.command_target_stop_name = command.stop_name or STOP_BY_ID[command.stop_id].name
			self.command_state = "EXECUTING"
			return
		self._complete_active_command()

	def _drain_command_queue(self):
		if self.active_command_id is not None or self._is_blocked_by_charge():
			return
		command = self._next_queued_command()
		if command is None:
			return
		self._start_command(command)

	def _current_target(self):
		if self.mode == "PATROL":
			stop = self._current_patrol_stop()
			self.active_stop_id = stop.stop_id
			self.active_stop_name = stop.name
			return stop
		if self.mode == "REROUTE" and self.command_target_stop_id in STOP_BY_ID:
			stop = STOP_BY_ID[self.command_target_stop_id]
			self.active_stop_id = stop.stop_id
			self.active_stop_name = stop.name
			return stop
		if self.mode == "RETURN_TO_CHARGE":
			self.active_stop_id = self.charging_station.stop_id
			self.active_stop_name = self.charging_station.name
			return self.charging_station
		if self.mode == "MAINTENANCE":
			inspection_pad = STOP_BY_ID["stop-5"]
			self.active_stop_id = inspection_pad.stop_id
			self.active_stop_name = inspection_pad.name
			return inspection_pad
		return None

	def _enter_return_to_charge(self):
		self.mode = "RETURN_TO_CHARGE"
		self.command_state = "EXECUTING"
		self.command_target_stop_id = self.charging_station.stop_id
		self.command_target_stop_name = self.charging_station.name
		self.active_stop_id = self.charging_station.stop_id
		self.active_stop_name = self.charging_station.name

	def _publish_telemetry(self, speed_mps: float):
		if self.hub is not None:
			self.hub.record_telemetry(
				drone_id=self.drone_id,
				drone_name=self.drone_name,
				x=self.x,
				y=self.y,
				battery_pct=self.battery_pct,
				speed_mps=speed_mps,
				mode=self.mode,
				command_state=self.command_state,
				last_command_action=self.last_command_action,
				last_command_sec=self.last_command_sec,
				active_stop_id=self.active_stop_id,
				active_stop_name=self.active_stop_name,
				route_progress_pct=self.route_progress_pct,
				last_update_sec=time.monotonic(),
			)
		self.telemetry_writer.write(
			make_telemetry(
				drone_id=self.drone_id,
				drone_name=self.drone_name,
				x=self.x,
				y=self.y,
				battery_pct=self.battery_pct,
				speed_mps=speed_mps,
				mode=self.mode,
				command_state=self.command_state,
				last_command_action=self.last_command_action,
				last_command_sec=self.last_command_sec,
				active_stop_id=self.active_stop_id,
				active_stop_name=self.active_stop_name,
				route_progress_pct=self.route_progress_pct,
			)
		)

	def _apply_command(self, command: DroneCommand):
		if not (is_broadcast_target(command.target_drone_id) or command.target_drone_id == self.drone_id):
			return
		if command.command_id in self.command_seen:
			return
		self.command_seen.add(command.command_id)
		if self._is_blocked_by_charge() or self.active_command_id is not None:
			self._queue_command(command)
			return
		self._start_command(command)

	def _finish_maintenance(self):
		self.maintenance_dwell_ticks = 0
		self.mode = "PATROL"
		self.command_state = "LISTENING"
		self._complete_active_command()

	def _complete_charge_if_ready(self):
		if self.mode == "RETURN_TO_CHARGE" and self.battery_pct >= 99.5:
			self.mode = "PATROL"
			self.command_state = "LISTENING"
			self._complete_active_command()
			self._drain_command_queue()

	def _begin_maintenance_dwell(self):
		self.maintenance_dwell_ticks = 5
		self.command_state = "HOLDING"

	async def _listen_for_commands(self):
		async for command in self.command_reader.take_data_async():
			self._apply_command(command)

	def _update_route_progress(self, remaining_distance: float, total_distance: float):
		if total_distance <= 0:
			self.route_progress_pct = 100.0
			return
		self.route_progress_pct = clamp(100.0 * (1.0 - remaining_distance / total_distance), 0.0, 100.0)

	async def _fly_loop(self):
		while True:
			if self.battery_pct < 15.0 and self.mode != "RETURN_TO_CHARGE":
				self._enter_return_to_charge()

			if self.mode == "MAINTENANCE" and self.maintenance_dwell_ticks > 0:
				self.maintenance_dwell_ticks -= 1
				self.command_state = "HOLDING"
				self.route_progress_pct = 100.0
				if self.maintenance_dwell_ticks <= 0:
					self._finish_maintenance()
				self._publish_telemetry(0.0)
				await asyncio.sleep(1.0)
				continue

			speed_mps = 0.0
			target = self._current_target()

			if self.mode == "HOVER":
				self.battery_pct = clamp(self.battery_pct - 0.01, 0.0, 100.0)
				self.command_state = "HOLDING"
				self.route_progress_pct = 0.0

			elif self.mode == "PAUSE":
				self.command_state = "HOLDING"
				self.route_progress_pct = 0.0

			elif self.mode == "DWELL":
				self.dwell_ticks -= 1
				self.command_state = "LISTENING"
				self.route_progress_pct = 100.0
				if self.dwell_ticks <= 0:
					self.mode = "PATROL"
					self._advance_patrol_index()

			elif target is not None:
				step = 1.2 if self.mode == "RETURN_TO_CHARGE" else 1.05
				start_distance = distance(self.x, self.y, target.x, target.y)
				self.x, self.y, remaining, arrived = move_toward(self.x, self.y, target.x, target.y, step)
				speed_mps = step
				self._update_route_progress(remaining, max(start_distance, 0.1))
				self.battery_pct = clamp(self.battery_pct - 0.35, 0.0, 100.0)
				if arrived:
					if self.mode == "PATROL":
						self.mode = "DWELL"
						self.dwell_ticks = 2
					elif self.mode == "REROUTE":
						self.mode = "PATROL"
						self._advance_patrol_index()
						self._complete_active_command()
					elif self.mode == "RETURN_TO_CHARGE":
						self.command_state = "HOLDING"
						self.battery_pct = clamp(self.battery_pct + 1.5, 0.0, 100.0)
						self._complete_charge_if_ready()
					elif self.mode == "MAINTENANCE":
						self._begin_maintenance_dwell()

			else:
				self.command_state = "LISTENING"
				self.route_progress_pct = 0.0
				self.battery_pct = clamp(self.battery_pct + 0.08, 0.0, 100.0)

			if self.active_command_id is None:
				self._drain_command_queue()

			self._publish_telemetry(speed_mps)
			await asyncio.sleep(1.0)

	async def run(self):
		await asyncio.gather(self._listen_for_commands(), self._fly_loop())


class DroneDashboardHandler(BaseHTTPRequestHandler):
	hub: Optional[DroneHub] = None

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
		assert self.hub is not None
		if parsed.path == "/":
			self._send_html(DRONE_DASHBOARD_HTML)
			return
		if parsed.path == "/api/state":
			self._send_json(self.hub.snapshot())
			return
		self._reject(404, "Not found")

	def log_message(self, format: str, *args: Any):
		return


DRONE_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
	<meta charset="utf-8" />
	<meta name="viewport" content="width=device-width, initial-scale=1" />
	<title>Drone Operations</title>
	<style>
		:root { --bg:#08111f; --panel:rgba(13, 24, 42, 0.92); --line:rgba(145, 173, 214, 0.2); --text:#e7f1ff; --muted:#8ca3c2; --accent:#66d9ff; --good:#6ee7b7; --warn:#fbbf24; --bad:#fb7185; }
		* { box-sizing:border-box; }
		body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--text); background: radial-gradient(circle at top left, rgba(102, 217, 255, 0.12), transparent 28%), linear-gradient(180deg, #050b14 0%, #08111f 100%); min-height:100vh; }
		.shell { width:min(1440px, calc(100vw - 32px)); margin:0 auto; padding:16px 0 28px; }
		.topbar { display:grid; grid-template-columns:1.4fr 1fr; gap:16px; margin-bottom:16px; }
		.panel { background:var(--panel); border:1px solid var(--line); border-radius:20px; box-shadow:0 20px 45px rgba(0,0,0,0.28); backdrop-filter:blur(12px); }
		.brand { padding:20px 24px; }
		.brand h1 { margin:0; font-size:28px; }
		.brand p { margin:6px 0 0; color:var(--muted); }
		.summary-grid { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:10px; margin-top:16px; }
		.metric { background:rgba(19, 34, 57, 0.92); border:1px solid var(--line); border-radius:16px; padding:12px 14px; }
		.metric .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:0.12em; }
		.metric .value { font-size:24px; margin-top:6px; font-weight:700; }
		.card { padding:16px; }
		.card h2 { margin:0 0 12px; font-size:18px; }
		.muted { color:var(--muted); }
		.status-list { display:grid; gap:10px; }
		.drone-row { border:1px solid var(--line); border-radius:18px; background:linear-gradient(180deg, rgba(16, 26, 43, 0.9), rgba(10, 18, 31, 0.96)); padding:14px; }
		.drone-top { display:flex; justify-content:space-between; gap:10px; align-items:center; flex-wrap:wrap; }
		.drone-name { font-size:18px; font-weight:700; }
		.pill { display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border-radius:999px; font-size:12px; letter-spacing:0.08em; text-transform:uppercase; border:1px solid rgba(255,255,255,0.1); background:rgba(255,255,255,0.06); }
		.pill.ok { color:#d5ffe8; background:rgba(110, 231, 183, 0.11); }
		.pill.warn { color:#fff3c6; background:rgba(251, 191, 36, 0.14); }
		.pill.bad { color:#ffe0e7; background:rgba(251, 113, 133, 0.14); }
		.meta { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:10px; margin-top:10px; font-size:14px; color:var(--muted); }
		.meta strong { color:var(--text); }
		.battery { margin-top:10px; height:10px; border-radius:999px; background:rgba(255,255,255,0.08); overflow:hidden; }
		.battery > div { height:100%; border-radius:inherit; background:linear-gradient(90deg, #57e4a4, #66d9ff); }
		.command-log { display:grid; gap:10px; }
		.command-item { border:1px solid var(--line); border-radius:14px; padding:12px 14px; background:rgba(255,255,255,0.04); }
		.command-item .title { font-weight:700; }
		.command-item .meta-line { display:block; margin-top:4px; font-size:13px; color:var(--muted); }
		.content { display:grid; grid-template-columns:1.35fr 1fr; gap:16px; margin-top:16px; }
		.map-card { min-height:520px; overflow:hidden; }
		.map-caption { display:flex; justify-content:space-between; gap:12px; align-items:center; margin-bottom:12px; }
		.legend { display:flex; flex-wrap:wrap; gap:12px; font-size:13px; color:var(--muted); }
		.legend span { display:inline-flex; align-items:center; gap:7px; }
		.swatch { width:12px; height:12px; border-radius:4px; display:inline-block; }
		.battery-fill { background:linear-gradient(90deg, #ef4444 0%, #f59e0b 48%, #84cc16 100%); }
		svg { width:100%; height:460px; display:block; }
		.map-note { margin-top:10px; font-size:13px; color:var(--muted); }
		.marker-label { font-size:12px; font-weight:700; fill:#eaf5ff; text-anchor:middle; pointer-events:none; }
		.marker-id { font-size:12px; font-weight:800; fill:#06111f; text-anchor:middle; pointer-events:none; }
	</style>
</head>
<body>
	<div class="shell">
		<div class="topbar">
			<div class="panel brand">
				<h1>Drone Operations</h1>
				<p>Local routing, telemetry publishing, and command subscription in Domain 10.</p>
				<div class="summary-grid" id="summaryGrid"></div>
			</div>
			<div class="panel card">
				<h2>Command Feed</h2>
				<div class="muted">This dashboard shows incoming command events and local drone telemetry only. No command controls live here.</div>
				<div class="command-log" id="commandLog" style="margin-top:12px;"></div>
			</div>
		</div>
		<div class="content">
			<div class="panel card map-card">
				<div class="map-caption">
					<div>
						<h2 style="margin-bottom:4px;">Yard Map</h2>
						<div class="muted">Depot, stops, live drone markers, and target lines.</div>
					</div>
				</div>
				<svg id="mapSvg" viewBox="0 0 1000 460" preserveAspectRatio="none"></svg>
				<div class="map-note">The map animates between telemetry refreshes so drone motion reads smoothly.</div>
			</div>
			<div class="panel card">
				<h2>Drone Status</h2>
				<div class="status-list" id="statusList"></div>
			</div>
		</div>
	</div>
	<script>
		const droneColors = ['#880808', '#7cdaff', '#6ee7b7', '#f472b6', '#fbbf24'];
		let latestState = null;
		let currentState = null;
		let animationToken = 0;

		function esc(value) {
			return String(value ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
		}

		function clamp01(value) {
			return Math.max(0, Math.min(1, value));
		}

		function lerp(start, end, amount) {
			return start + (end - start) * amount;
		}

		function yardX(x) {
			return 80 + (x / 34) * 840;
		}

		function yardY(y) {
			return 60 + ((14 - y) / 14) * 340;
		}

		function depotSlotPoint(index, count) {
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

		function routeSequenceForDrone(droneIndex, stops) {
			if (!stops.length) return [];
			const offset = droneIndex % stops.length;
			return Array.from({ length: stops.length }, (_, step) => stops[(offset + step) % stops.length]);
		}

		function batteryClass(value) {
			if (value < 22) return 'bad';
			if (value < 45) return 'warn';
			return 'ok';
		}

		function commandClass(mode, commandState) {
			if (mode === 'HOVER' || mode === 'PAUSE') return 'warn';
			if (commandState === 'EXECUTING') return 'warn';
			return 'ok';
		}

		function normalizeState(state) {
			return {
				summary: {
					active: state.active,
					idle: state.idle,
				},
				recent_commands: state.recent_commands || [],
				drones: (state.drones || []).map(drone => ({
					drone_id: drone.drone_id,
					drone_name: drone.drone_name,
					x: Number(drone.x || 0),
					y: Number(drone.y || 0),
					battery_pct: Number(drone.battery_pct || 0),
					mode: drone.mode || 'PATROL',
					command_state: drone.command_state || 'LISTENING',
					last_command_action: drone.last_command_action || '',
					last_command_sec: Number(drone.last_command_sec || 0),
					active_stop_id: drone.active_stop_id || '',
					active_stop_name: drone.active_stop_name || '',
					route_progress_pct: Number(drone.route_progress_pct || 0),
					telemetry_age_sec: drone.telemetry_age_sec,
					command_age_sec: drone.command_age_sec,
				})),
				stops: state.stops || [],
				depot: state.depot || { name: 'Depot', x: 3, y: 7 },
			};
		}

		async function api(path) {
			const response = await fetch(path, { cache: 'no-store' });
			if (!response.ok) throw new Error(await response.text());
			return response.json();
		}

		function renderSummary(state) {
			const metrics = [
				['Drones', state.drones.length],
				['Active', state.summary.active],
				['Idle', state.summary.idle],
			];
			document.getElementById('summaryGrid').innerHTML = metrics.map(([label, value]) => `
				<div class="metric"><div class="label">${label}</div><div class="value">${esc(value)}</div></div>
			`).join('');
		}

		function renderCommands(state) {
			const container = document.getElementById('commandLog');
			if (!state.recent_commands.length) {
				container.innerHTML = '<div class="muted">No active or queued commands.</div>';
				return;
			}
			container.innerHTML = state.recent_commands.slice(0, 5).map(command => `
				<div class="command-item">
					<div class="title">${esc(command.action)} → ${esc(command.target_drone_id || 'all drones')}</div>
					<span class="meta-line">${esc(command.stop_name || command.stop_id || 'no stop')} · ${esc(command.source_role || 'cloud')} · ${esc(command.status || 'active')}</span>
				</div>
			`).join('');
		}

		function renderMap(state) {
			const svg = document.getElementById('mapSvg');
			const stops = state.stops.map((stop, index) => {
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
				const assignedStop = state.stops[index % Math.max(1, state.stops.length)] || state.depot;
				const depotSlot = depotSlotPoint(index, state.drones.length || 1);
				const slotColor = color;
				return `
					<g>
						<rect x="${depotSlot.x - 23}" y="${depotSlot.y - 16}" width="46" height="32" rx="9" fill="rgba(255,255,255,0.08)" stroke="${slotColor}" stroke-width="2"></rect>
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

		function renderDrones(state) {
			const container = document.getElementById('statusList');
			if (!state.drones.length) {
				container.innerHTML = '<div class="muted">Waiting for commands to reach the drones.</div>';
				return;
			}
			container.innerHTML = state.drones.map(drone => {
				const battery = Math.max(0, Math.min(100, drone.battery_pct || 0));
				const telemetryAge = drone.telemetry_age_sec != null ? `${drone.telemetry_age_sec.toFixed(1)} sec` : 'n/a';
				return `
					<div class="drone-row">
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
						<div style="margin-top:10px; color: var(--muted); font-size:14px;">State: ${esc(drone.command_state || 'LISTENING')}</div>
						<div class="battery"><div class="battery-fill" style="width:${battery}%;"></div></div>
					</div>
				`;
			}).join('');
		}

		function drawState(state) {
			renderSummary(state);
			renderCommands(state);
			renderMap(state);
			renderDrones(state);
		}

		async function refresh() {
			latestState = normalizeState(await api('/api/state'));
			const startState = currentState || latestState;
			const duration = 700;
			const token = ++animationToken;

			const animate = (startTime) => {
				if (token !== animationToken) return;
				const elapsed = performance.now() - startTime;
				const progress = clamp01(elapsed / duration);
				const frameState = {
					summary: latestState.summary,
					recent_commands: latestState.recent_commands,
					drones: latestState.drones.map(targetDrone => {
						const startDrone = startState.drones.find(item => item.drone_id === targetDrone.drone_id) || targetDrone;
						return {
							...targetDrone,
							x: lerp(startDrone.x, targetDrone.x, progress),
							y: lerp(startDrone.y, targetDrone.y, progress),
							battery_pct: lerp(startDrone.battery_pct, targetDrone.battery_pct, progress),
							route_progress_pct: lerp(startDrone.route_progress_pct || 0, targetDrone.route_progress_pct || 0, progress),
						};
					}),
					stops: latestState.stops,
					depot: latestState.depot,
				};
				drawState(frameState);
				if (progress < 1) {
					requestAnimationFrame(() => animate(startTime));
				} else {
					currentState = latestState;
				}
			};

			requestAnimationFrame(() => animate(performance.now()));
		}

		refresh();
		setInterval(refresh, 1000);
	</script>
</body>
</html>
"""


async def _launch_drones(hub: DroneHub):
	positions = build_demo_positions()
	drones = [
		DroneAgent(
			drone_id,
			start_x=position[0],
			start_y=position[1],
			route_offset=index,
			route_direction=1 if index % 2 == 0 else -1,
			hub=hub,
		)
		for index, (drone_id, position) in enumerate(zip(DRONE_IDS, positions))
	]
	await asyncio.gather(*(drone.run() for drone in drones))


def run_dashboard_demo():
	hub = DroneHub()
	DroneDashboardHandler.hub = hub
	server = ThreadingHTTPServer(("127.0.0.1", 0), DroneDashboardHandler)
	url = f"http://127.0.0.1:{server.server_address[1]}/"
	Thread(target=lambda: rti.asyncio.run(_launch_drones(hub)), daemon=True).start()
	webbrowser.open_new(url)
	print(f"Drone operations dashboard opened at {url}")
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		server.shutdown()


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Local drone routing and telemetry publisher for Domain 10")
	return parser.parse_args()


def main():
	parse_args()
	try:
		run_dashboard_demo()
	except KeyboardInterrupt:
		pass


if __name__ == "__main__":
	main()
