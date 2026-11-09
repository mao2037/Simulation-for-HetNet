from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import heapq
import math

import numpy as np
from scipy.spatial import cKDTree

from .core import Network, PassengerInput, SimulationConfig
from .demand import generate_warmup
from .routing import RoutingIndex
from .timetable import TimetableTransferSelector
from .schedule import Schedule
from .station_selection import repair_unreachable_station_pairs


ACCESS, WAITING, RIDING, TRANSFERRING, EGRESS, COMPLETED, UNREACHABLE = range(7)


TIME_FIELDS = ("access_walk_s", "egress_walk_s", "initial_wait_s", "transfer_wait_s", "initial_queue_s", "transfer_queue_s", "base_moving_s", "detour_moving_s", "onboard_dwell_s", "transfer_link_s", "onboard_total_s", "total_s")


def walking_distances(points: np.ndarray, targets: np.ndarray) -> np.ndarray:


    offsets = np.asarray(points, dtype=float) - np.asarray(targets, dtype=float)
    return np.abs(offsets).sum(axis=-1)


def nearest_stations(points: np.ndarray, stations: np.ndarray) -> tuple[np.ndarray, np.ndarray]:


    tree = cKDTree(stations)

    distances, indices = tree.query(points, p=1)
    if len(stations) > 1:

        second = tree.query(points, k=2, p=1)[0][:, 1]
        for index in np.flatnonzero(np.abs(second - distances) <= 1e-10):

            candidates = tree.query_ball_point(points[index], float(distances[index]) + 1e-10, p=1)
            indices[index] = min(candidates)
            distances[index] = walking_distances(points[index], stations[indices[index]])
    return indices.astype(np.int32), distances


@dataclass
class WaitingGroup:


    station: int
    destination: int
    stage: int
    plan: object
    passengers: deque = field(default_factory=deque)
    rejection_times: list = field(default_factory=list)
    excluded_trip_id: str | None = None

    @property
    def key(self) -> tuple:
        return ((self.destination, self.stage) if self.excluded_trip_id is None
                else (self.destination, self.stage, self.excluded_trip_id))


@dataclass
class Vehicle:


    trip: object
    occupancy: int = 0
    alighters: dict = field(default_factory=dict)
    stop_sequence: int = 0
    arrival_s: float = 0.0
    departure_s: float = 0.0


class SimulationResult:
    def __init__(self, count: int):
        self.times = np.zeros((count, len(TIME_FIELDS)))
        self.status = np.full(count, ACCESS, dtype=np.int8)

    def objective_times(self, demand: int) -> tuple:
        completed = self.status == COMPLETED
        count = int(completed.sum())
        if count == 0:
            return (None, None, None, None)
        times = self.times[completed]
        weighted_walk = float((2 * times[:, :2].sum(axis=1)).mean() / 60)
        ordinary_wait = float(times[:, 2].mean() / 60) + float(times[:, 3].mean() / 60)
        transfer = float(times[:, 9].mean() / 60)
        onboard = float(times[:, 10].mean() / 60)
        return tuple(value * count / demand for value in (weighted_walk, ordinary_wait, transfer, onboard))


class PassengerSimulation:


    def __init__(self, network: Network, formal: PassengerInput, rep: int, config: SimulationConfig, strategy: str, background: PassengerInput | None = None, schedule=None):
        if strategy not in ("earliest", "shortest"):
            raise ValueError("策略必须为 earliest 或 shortest")
        self.network, self.config, self.strategy = network, config, strategy
        self.result = SimulationResult(len(formal))
        self.formal_count = len(formal)

        background = generate_warmup(network.scenario, rep, config) if background is None else background

        count = len(formal) + len(background)
        self.inputs = PassengerInput(np.concatenate((formal.passenger_id, background.passenger_id)), np.concatenate((formal.departure_s, background.departure_s)),
                                     np.concatenate((formal.origin_xy, background.origin_xy)), np.concatenate((formal.destination_xy, background.destination_xy)))
        self.routing = RoutingIndex(network, cache_destinations=config.routing_cache_destinations)
        self.board_station, self.access_km = nearest_stations(self.inputs.origin_xy, network.stations)
        self.destination, self.egress_km = nearest_stations(self.inputs.destination_xy, network.stations)
        repair_unreachable_station_pairs(self.routing, self.inputs, self.board_station, self.destination,
                                         self.access_km, self.egress_km)
        self.walk_only = self.board_station == self.destination
        self.ready_s = self.inputs.departure_s + self.access_km / config.walk_speed_kmh * 3600

        self.ready_s[self.walk_only] = self.inputs.departure_s[self.walk_only] + walking_distances(
            self.inputs.origin_xy[self.walk_only], self.inputs.destination_xy[self.walk_only]
        ) / config.walk_speed_kmh * 3600
        self.initial_ready = self.ready_s.copy()
        self.initial_order = np.lexsort((self.inputs.passenger_id, self.initial_ready))
        self.initial_cursor = 0
        self.initial_remaining = count
        self.pending_offvehicle = 0
        self.status = np.full(count, ACCESS, dtype=np.int8)
        self.stage = np.zeros(count, dtype=np.int8)
        self.log_position = np.zeros(count, dtype=np.int32)
        self.personal_first_rejection = np.full(count, np.inf)
        self.board_time = np.zeros(count)
        self.board_distance = np.zeros(count)
        self.groups = [dict() for _ in network.stations]
        self.active_groups = [dict() for _ in network.stations]
        self.parked = [dict() for _ in network.stations]

        self.initial_plans = self.routing.prepare_pairs(
            self.board_station[~self.walk_only], self.destination[~self.walk_only], 1, strategy)
        self.unreachable_groups = set()
        self.schedule = schedule if schedule is not None else Schedule(network, rep, config)
        self.transfer_selector = TimetableTransferSelector(network, self.schedule, self.routing,
                                                           transfer_s=config.transfer_s)
        self.heap = []
        self.serial = 0
        self.demand_ended = False
        self.remaining = self.formal_count
        self.start_s = -config.warmup_s
        self.now = self.start_s
        self.occupied_seats = 0
        self.next_skip_check_s = 3600.0
        self.result.times[:, 0] = self.access_km[:self.formal_count] / config.walk_speed_kmh * 3600
        self.result.times[:, 1] = self.egress_km[:self.formal_count] / config.walk_speed_kmh * 3600
        self.result.times[self.walk_only[:self.formal_count], 0] = self.initial_ready[:self.formal_count][self.walk_only[:self.formal_count]] - formal.departure_s[self.walk_only[:self.formal_count]]
        self.result.times[self.walk_only[:self.formal_count], 1] = 0

    def push(self, when: float, priority: int, kind: str, payload=None) -> None:
        if when < self.now - 1e-7:
            raise RuntimeError(f"事件时间倒流：{kind} {when} < {self.now}")
        self.serial += 1

        passenger_tie = int(self.inputs.passenger_id[payload[0] if kind == "transfer_ready" else payload]) if kind in ("initial", "transfer_ready", "finish") else 0
        heapq.heappush(self.heap, (float(when), priority, passenger_tie, self.serial, kind, payload))

    def _next_initial(self) -> None:
        if self.initial_cursor < len(self.initial_order):

            passenger = int(self.initial_order[self.initial_cursor])
            self.initial_cursor += 1
            self.push(float(self.initial_ready[passenger]), 2, "initial", passenger)

    def _add_trip(self, trip) -> None:


        route = self.network.routes[trip.route_index]
        if not len(route.stop_ids):
            return
        arrivals = trip.departure_s + self.schedule.arrival_offsets[trip.route_index]
        departures = trip.departure_s + self.schedule.departure_offsets[trip.route_index]


        cutoff = max(self.start_s, self.now)
        first = int(np.searchsorted(departures, cutoff, side="left"))
        if first >= len(route.stop_ids):
            return
        vehicle = Vehicle(trip, stop_sequence=first)
        self.push(max(cutoff, float(arrivals[first])), 1, "arrival", vehicle)

    def _group(self, station: int, destination: int, stage: int, excluded_trip_id: str | None = None):


        key = (destination, stage) if excluded_trip_id is None else (destination, stage, excluded_trip_id)
        if (station, destination, stage) in self.unreachable_groups:
            return None
        if key not in self.groups[station]:

            plan = self.initial_plans.get((station, destination)) if stage == 0 else None
            if plan is None:
                plan = self.routing.get_plan(station, destination, 1 - stage, self.strategy)
            if not plan.reachable:
                self.unreachable_groups.add((station, destination, stage))
                return None
            self.groups[station][key] = WaitingGroup(station, destination, stage, plan, excluded_trip_id=excluded_trip_id)
        return self.groups[station][key]

    def _ready(self, passenger: int, station: int, excluded_trip_id: str | None = None) -> None:


        stage = int(self.stage[passenger])
        group = self._group(station, int(self.destination[passenger]), stage, excluded_trip_id)
        if group is None:
            self.status[passenger] = UNREACHABLE
            if passenger < self.formal_count:
                self.remaining -= 1
            return
        self.status[passenger] = WAITING
        self.ready_s[passenger] = self.now
        self.log_position[passenger] = len(group.rejection_times)
        self.personal_first_rejection[passenger] = np.inf
        group.passengers.append(passenger)
        self.active_groups[station][group.key] = group

        parked = sorted(self.parked[station].values(), key=lambda vehicle: (vehicle.arrival_s, vehicle.trip.trip_id))
        for vehicle in parked:
            if vehicle.trip.trip_id == group.excluded_trip_id:
                continue
            if group.plan.options.get((vehicle.trip.route_index, vehicle.stop_sequence)) is None:
                continue
            if vehicle.occupancy < self.config.capacity:
                self._board_waiting(vehicle, record_rejections=False)
            if self.status[passenger] != WAITING:
                break
            if vehicle.occupancy == self.config.capacity:

                self.personal_first_rejection[passenger] = min(self.personal_first_rejection[passenger], self.now)

    def _board(self, passenger: int, group: WaitingGroup, vehicle: Vehicle, alight_sequence: int) -> None:
        if group.stage == 0:
            route = self.network.routes[vehicle.trip.route_index]
            destination = int(self.destination[passenger])
            if route.stop_ids[alight_sequence] != destination:
                choice = self.transfer_selector.select(vehicle.trip, vehicle.stop_sequence, destination, self.strategy)
                alight_sequence = choice.alight_sequence

        position = int(self.log_position[passenger])
        group_denials = len(group.rejection_times) - position

        first_rejection = min(self.personal_first_rejection[passenger], group.rejection_times[position] if group_denials else np.inf)

        ordinary = (self.now if not np.isfinite(first_rejection) else first_rejection) - self.ready_s[passenger]
        capacity_queue = 0.0 if not np.isfinite(first_rejection) else self.now - first_rejection

        self.status[passenger] = RIDING
        self.board_time[passenger] = self.now
        self.board_distance[passenger] = self.network.routes[vehicle.trip.route_index].distances_km[vehicle.stop_sequence]
        vehicle.alighters.setdefault(int(alight_sequence), []).append(passenger)
        vehicle.occupancy += 1
        self.occupied_seats += 1
        if passenger < self.formal_count:

            stage = int(self.stage[passenger])
            self.result.times[passenger, 2 + stage] += ordinary
            self.result.times[passenger, 4 + stage] += capacity_queue

    def _board_waiting(self, vehicle: Vehicle, record_rejections: bool) -> None:


        route = self.network.routes[vehicle.trip.route_index]
        station = int(route.stop_ids[vehicle.stop_sequence])
        candidates = []

        eligible = []
        for group in self.active_groups[station].values():
            if not group.passengers:
                continue
            if group.excluded_trip_id == vehicle.trip.trip_id:
                continue

            alight = group.plan.options.get((vehicle.trip.route_index, vehicle.stop_sequence))
            if alight is not None:
                eligible.append(group)

                head = group.passengers[0]
                heapq.heappush(candidates, (self.ready_s[head], int(self.inputs.passenger_id[head]), len(eligible) - 1, int(alight)))
        while candidates and vehicle.occupancy < self.config.capacity:

            _, _, group_index, alight = heapq.heappop(candidates)
            group = eligible[group_index]
            passenger = group.passengers.popleft()
            self._board(passenger, group, vehicle, alight)
            if group.passengers:
                head = group.passengers[0]
                heapq.heappush(candidates, (self.ready_s[head], int(self.inputs.passenger_id[head]), group_index, alight))
            else:

                group.rejection_times.clear()
                self.active_groups[station].pop(group.key, None)
                self.groups[station].pop(group.key, None)
        if record_rejections and vehicle.occupancy == self.config.capacity:
            for group in eligible:
                if group.passengers:
                    group.rejection_times.append(self.now)

    def _arrival(self, vehicle: Vehicle) -> None:


        route = self.network.routes[vehicle.trip.route_index]
        sequence = vehicle.stop_sequence
        station = int(route.stop_ids[sequence])
        vehicle.arrival_s = float(vehicle.trip.departure_s + self.schedule.arrival_offsets[vehicle.trip.route_index][sequence])
        vehicle.departure_s = float(vehicle.trip.departure_s + self.schedule.departure_offsets[vehicle.trip.route_index][sequence])
        self.parked[station][vehicle.trip.trip_id] = vehicle
        alighters = vehicle.alighters.pop(sequence, [])
        for passenger in alighters:
            vehicle.occupancy -= 1
            self.occupied_seats -= 1

            distance = float(route.distances_km[sequence] - self.board_distance[passenger])
            moving = distance / self.config.bus_speed_kmh * 3600
            dwell = self.now - self.board_time[passenger] - moving
            if dwell < -1e-6:
                raise RuntimeError("乘客车内时间小于车辆几何行驶时间")
            if passenger < self.formal_count:
                self.result.times[passenger, 6] += moving
                self.result.times[passenger, 8] += max(0, dwell)
                self.result.times[passenger, 10] += self.now - self.board_time[passenger]
            if station == self.destination[passenger]:
                self.status[passenger] = EGRESS
                self.pending_offvehicle += 1
                self.push(self.now + self.egress_km[passenger] / self.config.walk_speed_kmh * 3600, 2, "finish", passenger)
            else:
                if self.stage[passenger] != 0:
                    raise RuntimeError("路由产生了超过一次换乘的旅程")
                self.stage[passenger] = 1
                self.status[passenger] = TRANSFERRING
                self.pending_offvehicle += 1
                if passenger < self.formal_count:
                    self.result.times[passenger, 9] = self.config.transfer_s

                excluded = None
                if (self.now + self.config.transfer_s <= vehicle.departure_s
                        or np.any(route.stop_ids[sequence + 1:] == station)):
                    excluded = vehicle.trip.trip_id
                self.push(self.now + self.config.transfer_s, 2, "transfer_ready", (passenger, station, excluded))
        self._board_waiting(vehicle, record_rejections=True)
        self.push(max(self.now, vehicle.departure_s), 3, "departure", vehicle)

    def _departure(self, vehicle: Vehicle) -> None:


        route = self.network.routes[vehicle.trip.route_index]
        station = int(route.stop_ids[vehicle.stop_sequence])
        self.parked[station].pop(vehicle.trip.trip_id, None)
        vehicle.stop_sequence += 1
        if vehicle.stop_sequence < len(route.stop_ids):
            self.push(vehicle.trip.departure_s + self.schedule.arrival_offsets[vehicle.trip.route_index][vehicle.stop_sequence], 1, "arrival", vehicle)
        else:
            if vehicle.occupancy:
                raise RuntimeError("车辆完成全部服务站后仍有乘客未下车")

    def _finish(self, passenger: int) -> None:
        self.status[passenger] = COMPLETED
        if passenger < self.formal_count:
            self.remaining -= 1
            self.result.times[passenger, 11] = self.now - self.inputs.departure_s[passenger]
            if not self.walk_only[passenger]:

                base = float(np.abs(self.network.stations[self.board_station[passenger]] - self.network.stations[self.destination[passenger]]).sum()) / self.config.bus_speed_kmh * 3600
                self.result.times[passenger, 7] = max(0.0, self.result.times[passenger, 6] - base)
                self.result.times[passenger, 6] = base

            components = self.result.times[passenger, :10].sum()
            if abs(components - self.result.times[passenger, 11]) > 1e-5:
                raise RuntimeError(f"乘客时间分项不闭合：{passenger}, {components}, {self.result.times[passenger, 11]}")

    def _skip_empty_service(self) -> None:


        from .drain import next_boarding_time

        next_time = next_boarding_time(self.schedule, self.network, self.active_groups, self.now)
        self.next_skip_check_s = next_time + 1e-6
        if next_time - self.now < 300:
            return
        self.heap.clear()
        self.parked = [dict() for _ in self.network.stations]
        self.now = next_time

        first_hour = math.floor((next_time - max(self.schedule.route_durations, default=0)) / 3600)
        last_hour = math.floor(next_time / 3600)
        for hour in range(first_hour, last_hour + 1):
            for trip in self.schedule.hour_trips(hour):
                self._add_trip(trip)
        self.push((last_hour + 1) * 3600, 0, "hour", last_hour + 1)

    def run(self) -> SimulationResult:


        for trip in self.schedule.initial_trips():
            self._add_trip(trip)
        self._next_initial()

        hour = math.floor(self.start_s / 3600)
        self.push(max(self.start_s, hour * 3600), 0, "hour", hour)
        self.push(3600, 5, "demand_end")
        while self.heap:

            self.now, _, _, _, kind, payload = heapq.heappop(self.heap)
            if kind == "hour":
                for trip in self.schedule.hour_trips(payload):
                    if trip.departure_s >= self.start_s:
                        self._add_trip(trip)
                self.push((payload + 1) * 3600, 0, "hour", payload + 1)
            elif kind == "arrival":
                self._arrival(payload)
            elif kind == "departure":
                self._departure(payload)
            elif kind == "initial":
                self.initial_remaining -= 1
                if self.walk_only[payload]:
                    self._finish(payload)
                else:
                    self._ready(payload, int(self.board_station[payload]))
                if self.initial_remaining == 0:

                    self.initial_plans.clear()
                self._next_initial()
            elif kind == "transfer_ready":
                self.pending_offvehicle -= 1
                self._ready(*payload)
            elif kind == "finish":
                self.pending_offvehicle -= 1
                self._finish(payload)
            elif kind == "demand_end":
                self.demand_ended = True
            if self.demand_ended and self.remaining == 0:
                break
            if (self.demand_ended and self.initial_remaining == 0 and self.pending_offvehicle == 0 and self.occupied_seats == 0 and self.remaining > 0
                    and self.now >= self.next_skip_check_s and hasattr(self.schedule, "hour_trips_for_routes")):
                self._skip_empty_service()
        self.result.status[:] = self.status[:self.formal_count]
        return self.result
