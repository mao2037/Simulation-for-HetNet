from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import heapq
from math import floor
from numbers import Integral

import numpy as np

from .core import Network
from .routing import RoutingIndex


TIME_ATOL_S = 1e-8


@dataclass(frozen=True)
class TransferChoice:


    alight_sequence: int
    board_s: float
    next_trip_id: str
    next_route_index: int
    next_board_sequence: int
    destination_arrival_s: float


@dataclass
class _HourData:


    departures: dict = field(default_factory=dict)
    trip_ids: dict = field(default_factory=dict)
    covered: set = field(default_factory=set)
    complete: bool = False


class TimetableTransferSelector:


    def __init__(self, network: Network, schedule, routing: RoutingIndex,
                 cache_hours: int = 64, cache_choices: int = 8192,
                 transfer_s: float = 60.0) -> None:

        if cache_hours < 0 or cache_choices < 0 or int(cache_hours) != cache_hours or int(cache_choices) != cache_choices:
            raise ValueError("时刻表缓存容量必须为非负整数")
        self.network, self.schedule, self.routing = network, schedule, routing
        self.cache_hours, self.cache_choices = int(cache_hours), int(cache_choices)
        self.link_s = float(transfer_s)
        if not np.isfinite(self.link_s) or self.link_s < 0:
            raise ValueError("固定换乘衔接时间必须为非负有限秒数")
        self._hours = OrderedDict()
        self._choices = OrderedDict()

    def _hour(self, hour: int, route_index: int) -> _HourData:


        data = self._hours.get(hour)
        if data is not None:
            self._hours.move_to_end(hour)
            if data.complete or route_index in data.covered:
                return data
        else:
            data = _HourData()
        sparse = getattr(self.schedule, "hour_trips_for_routes", None)

        available = sparse(hour, {route_index}) if sparse is not None else None
        if available is not None and not available:
            data.covered.add(route_index)
        else:

            grouped = {}
            for trip in self.schedule.hour_trips(hour):
                grouped.setdefault(int(trip.route_index), []).append((float(trip.departure_s), str(trip.trip_id)))
            for route, trips in grouped.items():
                trips.sort()

                departures = np.asarray([item[0] for item in trips], dtype=float)
                departures.flags.writeable = False
                data.departures[route] = departures
                data.trip_ids[route] = tuple(item[1] for item in trips)
            data.complete = True
        if self.cache_hours:
            self._hours[hour] = data
            self._hours.move_to_end(hour)
            if len(self._hours) > self.cache_hours:
                self._hours.popitem(last=False)
        return data

    def select(self, trip, board_sequence: int, destination: int, strategy: str) -> TransferChoice:


        if strategy not in ("earliest", "shortest") or not np.isfinite(trip.departure_s):
            raise ValueError("时刻表查询需要earliest/shortest策略及有限首班发车时刻")
        if any(isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral)
               for value in (trip.route_index, board_sequence, destination)):
            raise ValueError("时刻表线路、上车站序和终点必须为整数内部索引")
        if (not 0 <= trip.route_index < len(self.network.routes)
                or not 0 <= board_sequence < len(self.network.routes[trip.route_index].stop_ids)
                or not 0 <= destination < len(self.network.stations)):
            raise ValueError("时刻表线路、上车站序或终点超出网络范围")

        key = (str(trip.trip_id), int(trip.route_index), float(trip.departure_s), board_sequence, destination, strategy)
        if key in self._choices:
            self._choices.move_to_end(key)
            return self._choices[key]

        candidates = self.routing.transfer_candidates(trip.route_index, board_sequence, destination)
        if not len(candidates):
            raise ValueError("首车下游不存在一次换乘可达终点的站次")

        heap = []
        final_offsets = {}
        first_connections = {}

        def lower_bound(hour, route, board, ready):
            arrival_offset = float(self.schedule.arrival_offsets[route][board])
            return max(ready, hour * 3600 + arrival_offset - TIME_ATOL_S)

        def can_improve(lower, sequence):
            first = first_connections.get(sequence)
            return first is None or lower <= first[1].board_s + TIME_ATOL_S


        first_route = self.network.routes[trip.route_index]
        for sequence in candidates:

            alight = float(trip.departure_s + self.schedule.arrival_offsets[trip.route_index][sequence])
            ready = alight + self.link_s
            station = int(first_route.stop_ids[sequence])

            plan = self.routing.get_plan(station, destination, 0, strategy)
            for (route, board), last_alight in plan.options.items():
                final_offsets[route, board] = float(self.schedule.arrival_offsets[route][last_alight])

                departure_offset = float(self.schedule.departure_offsets[route][board])

                hour = floor((ready - departure_offset - TIME_ATOL_S) / 3600)
                lower = lower_bound(hour, route, board, ready)
                heapq.heappush(heap, (lower, int(sequence), route, board, hour, alight, ready))
        while heap:

            lower, sequence, route, board, hour, alight, ready = heapq.heappop(heap)
            if not can_improve(lower, sequence):
                continue

            data = self._hour(hour, route)
            departures, ids = data.departures.get(route), data.trip_ids.get(route, ())

            found = False
            if departures is not None:

                arrival_offset = float(self.schedule.arrival_offsets[route][board])
                departure_offset = float(self.schedule.departure_offsets[route][board])
                position = int(np.searchsorted(departures, ready - departure_offset - TIME_ATOL_S, side="left"))
                while position < len(departures):

                    departure, trip_id = float(departures[position]), ids[position]
                    position += 1

                    if trip_id == str(trip.trip_id) or departure + departure_offset < ready:
                        continue

                    boarding = max(ready, departure + arrival_offset)
                    choice = TransferChoice(sequence, boarding, trip_id, route, board,
                                            departure + final_offsets[route, board])
                    rank = (boarding, departure + arrival_offset, trip_id, route, board)
                    first = first_connections.get(sequence)
                    if first is None or rank < first[0]:
                        first_connections[sequence] = (rank, choice)
                    found = True
                    break
            if not found:
                hour += 1

                lower = lower_bound(hour, route, board, ready)
                if can_improve(lower, sequence):
                    heapq.heappush(heap, (lower, sequence, route, board, hour, alight, ready))
        if not first_connections:
            raise RuntimeError("No compatible connecting trip found")
        best = min((item[1] for item in first_connections.values()), key=lambda choice: (
            choice.destination_arrival_s, choice.alight_sequence, choice.board_s,
            choice.next_trip_id, choice.next_route_index, choice.next_board_sequence))
        if self.cache_choices:
            self._choices[key] = best
            if len(self._choices) > self.cache_choices:
                self._choices.popitem(last=False)
        return best
