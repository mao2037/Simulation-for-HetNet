from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor
from pathlib import Path

import numpy as np

from .core import Network, SimulationConfig, data_path, stable_seed
from .excel_io import ExcelWriter, read_metadata
from .route_filter import filter_operating_network, route_filter_settings


SCHEDULE_VERSION = "direction_headway_stratified_v2_route_filter"


@dataclass(frozen=True)
class Trip:


    trip_id: str
    bus_id: str
    route_index: int
    departure_s: float


class Schedule:


    def __init__(self, network: Network, rep: int, config: SimulationConfig):
        network.validate()
        if rep < 1:
            raise ValueError("重复编号必须为正整数")
        self.network = network
        self.seed_key = network.scenario.seed_key
        self.rep = rep
        self.config = config
        self.arrival_offsets = []
        self.departure_offsets = []
        self.route_durations = []
        for route in network.routes:

            arrival = route.distances_km / config.bus_speed_kmh * 3600 + np.arange(len(route.stop_ids)) * config.dwell_s
            self.arrival_offsets.append(arrival)
            self.departure_offsets.append(arrival + config.dwell_s)
            self.route_durations.append(route.length_km / config.bus_speed_kmh * 3600 + len(route.stop_ids) * config.dwell_s)

        self.first_hour = floor((-config.warmup_s - max(self.route_durations, default=0)) / 3600)
        self._groups = {}
        self._route_positions = {}
        for direction in ("E", "W", "N", "S"):

            indices = np.array([index for index, route in enumerate(network.routes) if route.direction == direction], dtype=np.int32)
            if not len(indices):
                continue

            frequencies = np.array([network.routes[index].frequency_veh_h for index in indices])
            total = float(frequencies.sum())

            rng = np.random.default_rng(stable_seed(config.base_seed, self.seed_key, rep, "phase", direction))
            phase = float(rng.random())
            self._groups[direction] = (indices, np.cumsum(frequencies) / total, total, phase)
            for position, index in enumerate(indices):
                self._route_positions[int(index)] = (direction, position)

    def hour_trips(self, hour: int) -> list[Trip]:


        if int(hour) != hour:
            raise ValueError("hour 必须为整数")

        trips = []
        for direction, (indices, cumulative, total, phase) in self._groups.items():

            candidates = np.arange(floor(hour * total - phase) - 1, ceil((hour + 1) * total - phase) + 2, dtype=np.int64)

            departures = (candidates + phase) * (3600 / total)
            inside = (departures >= hour * 3600) & (departures < (hour + 1) * 3600)
            candidates, departures = candidates[inside], departures[inside]
            if not len(candidates):
                continue

            rng = np.random.default_rng(stable_seed(self.config.base_seed, self.seed_key, self.rep, "assignment", direction, int(hour)))

            points = (np.arange(len(candidates)) + rng.random()) / len(candidates)

            assigned = indices[np.minimum(np.searchsorted(cumulative, points, side="right"), len(indices) - 1)]
            rng.shuffle(assigned)
            for sequence, departure, route_index in zip(candidates, departures, assigned):

                trip_id = f"{direction}_{int(sequence):+013d}"
                trips.append(Trip(trip_id, trip_id, int(route_index), float(departure)))
        return sorted(trips, key=lambda trip: (trip.departure_s, trip.trip_id))

    @staticmethod
    def _hour_count(hour: int, total: float, phase: float) -> int:


        first = floor(hour * total - phase) - 1
        end = ceil((hour + 1) * total - phase) + 2

        factor = 3600 / total
        start_s, end_s = hour * 3600, (hour + 1) * 3600
        while first < end and (np.float64(first) + phase) * factor < start_s:
            first += 1
        while first < end and (np.float64(end - 1) + phase) * factor >= end_s:
            end -= 1
        return end - first

    def hour_trips_for_routes(self, hour: int, route_indices: set[int]) -> list[Trip]:


        if int(hour) != hour:
            raise ValueError("hour 必须为整数")
        if not route_indices:
            return []

        requested = {}
        for route_index in route_indices:

            location = self._route_positions.get(route_index)
            if location is not None:
                requested.setdefault(location[0], []).append(location[1])
        for direction, positions in requested.items():

            indices, cumulative, total, phase = self._groups[direction]

            count = self._hour_count(hour, total, phase)
            if count == 0:
                continue

            rng = np.random.default_rng(stable_seed(self.config.base_seed, self.seed_key, self.rep, "assignment", direction, int(hour)))
            offset = float(rng.random())
            for position in positions:

                lower = float(cumulative[position - 1]) if position else 0.0
                candidate = ceil(count * lower - offset)

                for stratum in (candidate - 1, candidate, candidate + 1):
                    if 0 <= stratum < count:

                        point = (np.float64(stratum) + offset) / count
                        actual = min(int(np.searchsorted(cumulative, point, side="right")), len(indices) - 1)
                        if actual == position:
                            return [trip for trip in self.hour_trips(hour) if trip.route_index in route_indices]
        return []

    def initial_trips(self):
        for hour in range(self.first_hour, ceil(-self.config.warmup_s / 3600)):
            for trip in self.hour_trips(hour):
                if trip.departure_s < -self.config.warmup_s and trip.departure_s + self.route_durations[trip.route_index] >= -self.config.warmup_s:
                    yield trip

    def scene_trips(self):
        for hour in range(self.first_hour, 1):
            for trip in self.hour_trips(hour):
                if trip.departure_s + self.route_durations[trip.route_index] >= -self.config.warmup_s:
                    yield trip

def _network_signature(network: Network) -> int:
    return stable_seed(network.stations.tolist(), [(route.route_id, route.direction, route.frequency_veh_h,
                        route.length_km, route.stop_ids.tolist(), route.distances_km.tolist()) for route in network.routes])


def build_scene(network: Network, rep: int, config: SimulationConfig) -> Path:
    network = filter_operating_network(network, config)
    path = data_path("scene", network.scenario, rep)
    schedule = Schedule(network, rep, config)
    scene_settings = {name: getattr(config, name)
                      for name in ("bus_speed_kmh", "dwell_s", "base_seed", "warmup_s", "sheet_rows")}
    signature = str(stable_seed(SCHEDULE_VERSION, _network_signature(network), rep,
                                scene_settings, route_filter_settings(config)))
    if path.is_file() and read_metadata(path).get("scene_signature") == signature:
        return path
    metadata = {**network.scenario.to_dict(), "rep": rep, **scene_settings,
                "schedule_version": SCHEDULE_VERSION, "scene_signature": signature,
                "route_filter_settings": route_filter_settings(config),
                "first_departure_hour": schedule.first_hour, "last_departure_hour": 0,
                "time_unit": "s", "coordinate_unit": "km", "frequency_unit": "veh/h"}
    with ExcelWriter(path, config.sheet_rows) as writer:
        writer.metadata(metadata)
        writer.table("routes", ["route_id", "direction", "frequency_veh_h", "length_km", "stop_count"],
                     ((route.route_id, route.direction, route.frequency_veh_h, route.length_km, len(route.stop_ids)) for route in network.routes))
        writer.table("stations", ["station_id", "x_km", "y_km"],
                     ((index + 1, *position) for index, position in enumerate(network.stations)))
        writer.table("route_stops", ["route_id", "sequence", "station_id", "distance_km", "arrival_offset_s", "departure_offset_s"],
                     ((route.route_id, stop + 1, station + 1, route.distances_km[stop], schedule.arrival_offsets[index][stop], schedule.departure_offsets[index][stop])
                      for index, route in enumerate(network.routes) for stop, station in enumerate(route.stop_ids)))
        writer.table("trips", ["trip_id", "bus_id", "route_id", "departure_s", "route_end_s"],
                     ((trip.trip_id, trip.bus_id, network.routes[trip.route_index].route_id, trip.departure_s, trip.departure_s + schedule.route_durations[trip.route_index]) for trip in schedule.scene_trips()))
        writer.table("stop_times", ["trip_id", "bus_id", "route_id", "sequence", "station_id", "arrival_s", "departure_s"],
                     ((trip.trip_id, trip.bus_id, network.routes[trip.route_index].route_id, stop + 1, station + 1,
                       trip.departure_s + schedule.arrival_offsets[trip.route_index][stop], trip.departure_s + schedule.departure_offsets[trip.route_index][stop])
                      for trip in schedule.scene_trips() for stop, station in enumerate(network.routes[trip.route_index].stop_ids)))
    return path
