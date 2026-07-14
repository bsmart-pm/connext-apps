import math
import time
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

import rti.connextdds as dds
import rti.types as idl


DOMAIN_DRONES = 10
DOMAIN_COMMAND = 20
FLEET_ID = "warehouse-fleet"
DRONE_IDS = [f"drone-{index:02d}" for index in range(1, 6)]
TOPIC_CACHE: Dict[Tuple[int, str, type], dds.Topic] = {}
COMMAND_ACTIONS = ("HOVER", "PAUSE", "RESUME", "RETURN_TO_CHARGE", "REROUTE", "MAINTENANCE")
PARTICIPANT_NAMES = {
	DOMAIN_DRONES: "Drone Ops Telemetry",
	DOMAIN_COMMAND: "Cloud Command",
}

_default_provider = dds.QosProvider.default
_default_provider.participant_factory_qos.monitoring = dds.Monitoring.disabled


def create_participant(domain_id: int) -> dds.DomainParticipant:
	qos = dds.DomainParticipantQos()
	qos.transport_builtin.mask = dds.TransportBuiltinMask.UDPv4
	participant_name = PARTICIPANT_NAMES.get(domain_id, FLEET_ID)
	qos.participant_name.name = participant_name
	qos.property.set("dds.sys_info.executable_filepath", participant_name)
	return dds.DomainParticipant(domain_id, qos=qos)


DRONE_PARTICIPANT = create_participant(DOMAIN_DRONES)
COMMAND_PARTICIPANT = create_participant(DOMAIN_COMMAND)


@idl.struct(member_annotations={"drone_id": [idl.key]})
class DroneTelemetry:
	drone_id: str = ""
	drone_name: str = ""
	x: float = 0.0
	y: float = 0.0
	battery_pct: float = 100.0
	speed_mps: float = 0.0
	mode: str = "PATROL"
	command_state: str = "LISTENING"
	last_command_action: str = ""
	last_command_sec: float = 0.0
	active_stop_id: str = ""
	active_stop_name: str = ""
	route_progress_pct: float = 0.0
	last_update_sec: float = 0.0


@idl.struct(member_annotations={"command_id": [idl.key]})
class DroneCommand:
	command_id: str = ""
	target_drone_id: str = ""
	action: str = ""
	stop_id: str = ""
	stop_name: str = ""
	source_role: str = "cloud_command"
	created_sec: float = 0.0


@dataclass(frozen=True)
class Stop:
	stop_id: str
	name: str
	x: float
	y: float


WAREHOUSE_DEPOT = Stop("depot", "Depot", 3, 7)
WAREHOUSE_STOPS = [
	Stop("stop-1", "A", 16, 4),
	Stop("stop-2", "B", 28, 4),
	Stop("stop-3", "C", 28, 11),
	Stop("stop-4", "D", 16, 11),
	Stop("stop-5", "Inspection Pad", 5, 11),
]
STOP_BY_ID = {stop.stop_id: stop for stop in WAREHOUSE_STOPS}

CHARGING_BAY_POSITIONS = [
	(0.8, 8.0),
	(4.2, 8.0),
	(7.6, 8.0),
	(0.8, 5.8),
	(4.2, 5.8),
]


def clamp(value: float, low: float, high: float) -> float:
	return max(low, min(high, value))


def distance(a_x: float, a_y: float, b_x: float, b_y: float) -> float:
	return math.hypot(b_x - a_x, b_y - a_y)


def move_toward(
	x: float,
	y: float,
	target_x: float,
	target_y: float,
	step_size: float,
) -> Tuple[float, float, float, bool]:
	delta_x = target_x - x
	delta_y = target_y - y
	remaining = math.hypot(delta_x, delta_y)
	if remaining <= step_size or remaining == 0:
		return target_x, target_y, remaining, True
	ratio = step_size / remaining
	return x + delta_x * ratio, y + delta_y * ratio, remaining, False


def pretty_number(value: float) -> str:
	return f"{value:5.1f}"


def create_topic(participant: dds.DomainParticipant, name: str, type_cls):
	cache_key = (id(participant), name, type_cls)
	topic = TOPIC_CACHE.get(cache_key)
	if topic is None:
		topic = dds.Topic(participant, name, type_cls)
		TOPIC_CACHE[cache_key] = topic
	return topic


def make_telemetry(
	drone_id: str,
	drone_name: str,
	x: float,
	y: float,
	battery_pct: float,
	speed_mps: float,
	mode: str,
	command_state: str,
	last_command_action: str,
	last_command_sec: float,
	active_stop_id: str,
	active_stop_name: str,
	route_progress_pct: float,
) -> DroneTelemetry:
	return DroneTelemetry(
		drone_id=drone_id,
		drone_name=drone_name,
		x=x,
		y=y,
		battery_pct=battery_pct,
		speed_mps=speed_mps,
		mode=mode,
		command_state=command_state,
		last_command_action=last_command_action,
		last_command_sec=last_command_sec,
		active_stop_id=active_stop_id,
		active_stop_name=active_stop_name,
		route_progress_pct=route_progress_pct,
		last_update_sec=time.monotonic(),
	)


def make_command(
	command_id: str,
	target_drone_id: str,
	action: str,
	stop_id: str = "",
	stop_name: str = "",
	source_role: str = "cloud_command",
) -> DroneCommand:
	return DroneCommand(
		command_id=command_id,
		target_drone_id=target_drone_id,
		action=action,
		stop_id=stop_id,
		stop_name=stop_name,
		source_role=source_role,
		created_sec=time.monotonic(),
	)


def build_demo_positions() -> List[Tuple[float, float]]:
	return list(CHARGING_BAY_POSITIONS)


def normalize_action(action: str) -> str:
	return action.strip().upper().replace(" ", "_")


def is_broadcast_target(target_drone_id: str) -> bool:
	return target_drone_id in {"", "*"}
