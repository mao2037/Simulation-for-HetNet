from collections import OrderedDict
from dataclasses import dataclass, replace
import heapq
from math import floor

import numpy as np

from .schedule import Trip
from .timetable import TIME_ATOL_S


@dataclass(frozen=True)
class FirstBoardingChoice:
    trip: Trip
    board_sequence: int
    alight_sequence: int
    axis: str
    arrival_s: float
    board_s: float
    departure_s: float
    destination_arrival_s: float


class AxisFirstBoardingSelector:
    def __init__(self, network, transfer_selector, cache_choices=8192):
        self.network = network
        self.transfer_selector = transfer_selector
        self.schedule = transfer_selector.schedule
        self.cache_choices = cache_choices
        self._choices = OrderedDict()

    def _first_vehicle(self, options, ready_s, excluded_trip_ids):
        queue = []
        for route_index, sequence in options:
            departure_offset = float(self.schedule.departure_offsets[route_index][sequence])
            hour = floor((ready_s - departure_offset - TIME_ATOL_S) / 3600)
            arrival_offset = float(self.schedule.arrival_offsets[route_index][sequence])
            lower = max(ready_s, hour * 3600 + arrival_offset)
            heapq.heappush(queue, (lower, route_index, sequence, hour))
        best = None
        while queue:
            lower, route_index, sequence, hour = heapq.heappop(queue)
            if best is not None and lower > best[0][0] + TIME_ATOL_S:
                break
            data = self.transfer_selector._hour(hour, route_index)
            departures = data.departures.get(route_index)
            found = False
            if departures is not None:
                arrival_offset = float(self.schedule.arrival_offsets[route_index][sequence])
                departure_offset = float(self.schedule.departure_offsets[route_index][sequence])
                position = int(np.searchsorted(departures, ready_s - departure_offset - TIME_ATOL_S))
                while position < len(departures):
                    departure = float(departures[position])
                    trip_id = data.trip_ids[route_index][position]
                    position += 1
                    closes = departure + departure_offset
                    if closes < ready_s or trip_id in excluded_trip_ids:
                        continue
                    arrival = departure + arrival_offset
                    boards = max(ready_s, arrival)
                    rank = (boards, arrival, trip_id, route_index, sequence)
                    if best is None or rank < best[0]:
                        trip = Trip(trip_id, trip_id, route_index, departure)
                        best = (rank, trip, sequence, arrival, closes)
                    found = True
                    break
            if not found:
                hour += 1
                lower = max(
                    ready_s, hour * 3600 + float(self.schedule.arrival_offsets[route_index][sequence])
                )
                if best is None or lower <= best[0][0] + TIME_ATOL_S:
                    heapq.heappush(queue, (lower, route_index, sequence, hour))
        return best

    def select(self, station, destination, ready_s, strategy, plan, excluded_trip_ids=()):
        if not np.isfinite(ready_s):
            raise ValueError("First-boarding ready time must be finite")
        excluded = frozenset(excluded_trip_ids)
        key = (int(station), int(destination), strategy)
        cached = self._choices.get(key) if not excluded else None
        if cached is not None:
            start, end, choice = cached
            if start <= ready_s <= end:
                self._choices.move_to_end(key)
                return replace(choice, board_s=max(float(ready_s), choice.arrival_s))

        options = {"EW": {}, "NS": {}}
        for (route_index, sequence), alight in plan.options.items():
            direction = self.network.routes[route_index].direction
            options["EW" if direction in ("E", "W") else "NS"][route_index, sequence] = alight
        choices = []
        valid_until = np.inf
        for axis, admitted in options.items():
            if not admitted:
                continue
            first = self._first_vehicle(admitted, ready_s, excluded)
            if first is None:
                continue
            rank, trip, sequence, arrival, closes = first
            valid_until = min(valid_until, closes)
            alight = admitted[trip.route_index, sequence]
            route = self.network.routes[trip.route_index]
            if int(route.stop_ids[alight]) == destination:
                destination_arrival = trip.departure_s + float(
                    self.schedule.arrival_offsets[trip.route_index][alight]
                )
            else:
                transfer = self.transfer_selector.select(trip, sequence, destination, strategy)
                alight = transfer.alight_sequence
                destination_arrival = transfer.destination_arrival_s
            choices.append(
                FirstBoardingChoice(
                    trip, sequence, int(alight), axis, arrival, rank[0], closes, float(destination_arrival)
                )
            )
        if not choices:
            raise ValueError("No usable first vehicle for a reachable station pair")
        choice = min(
            choices,
            key=lambda value: (
                value.destination_arrival_s,
                value.board_s,
                value.arrival_s,
                value.trip.trip_id,
                value.trip.route_index,
                value.board_sequence,
            ),
        )
        if self.cache_choices and not excluded:
            self._choices[key] = (float(ready_s), float(valid_until), choice)
            self._choices.move_to_end(key)
            if len(self._choices) > self.cache_choices:
                self._choices.popitem(last=False)
        return choice
