from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from .core import Network


DISTANCE_ATOL_KM = 1e-8

DISTANCE_RTOL = 1e-10


def _same_distance(left: Any, right: Any) -> Any:
    if not isinstance(left, np.ndarray) and not isinstance(right, np.ndarray):
        return bool(left == right or abs(left - right) <= DISTANCE_ATOL_KM + DISTANCE_RTOL * abs(right))
    return np.isclose(left, right, rtol=DISTANCE_RTOL, atol=DISTANCE_ATOL_KM)


def _downstream_minimum_indices(values: np.ndarray) -> np.ndarray:


    count = len(values)
    choices = np.full(count, -1, dtype=np.int32)
    if count < 2:
        return choices

    suffix_best = np.minimum.accumulate(values[::-1])[::-1]

    is_record = np.isfinite(values) & _same_distance(values, suffix_best)

    candidate_indices = np.where(is_record, np.arange(count, dtype=np.int32), count)

    suffix_indices = np.minimum.accumulate(candidate_indices[::-1])[::-1]
    choices[:-1] = np.where(suffix_indices[1:] < count, suffix_indices[1:], -1)
    return choices


@dataclass(frozen=True)
class Plan:


    options: Mapping[tuple[int, int], int]
    best_distance_km: float
    reachable: bool


@dataclass(frozen=True)
class _DestinationData:


    direct: np.ndarray
    earliest: np.ndarray
    shortest: np.ndarray
    direct_best: np.ndarray
    earliest_best: np.ndarray
    shortest_best: np.ndarray


class _CompactOptions(Mapping):


    __slots__ = ("_index", "_origin", "_choices", "_size")

    def __init__(self, index: "RoutingIndex", origin: int, choices: np.ndarray) -> None:
        self._index = index
        self._origin = origin
        self._choices = choices
        self._size = None

    def _choice_at(self, visit: int) -> int:
        return int(self._choices[self._index._origin_positions[visit]])

    def __getitem__(self, key: tuple[int, int]) -> int:
        if not isinstance(key, tuple) or len(key) != 2:
            raise KeyError(key)

        route_index, sequence = key
        if not isinstance(route_index, (int, np.integer)) or not isinstance(sequence, (int, np.integer)):
            raise KeyError(key)
        if route_index < 0 or route_index >= len(self._index._lengths):
            raise KeyError(key)
        if sequence < 0 or sequence >= self._index._lengths[route_index]:
            raise KeyError(key)

        visit = int(self._index._offsets[route_index]) + int(sequence)
        if self._index._stops[visit] != self._origin:
            raise KeyError(key)

        alight = self._choice_at(visit)
        if alight < 0:
            raise KeyError(key)
        return alight

    def __iter__(self) -> Iterator[tuple[int, int]]:
        for key, _ in self.items():
            yield key

    def items(self) -> Iterator[tuple[tuple[int, int], int]]:
        for visit, choice in zip(self._index._occurrences[self._origin], self._choices):

            alight = int(choice)
            if alight >= 0:
                yield (int(self._index._visit_routes[visit]), int(self._index._visit_sequences[visit])), alight

    def __len__(self) -> int:
        if self._size is None:
            self._size = sum(1 for _ in self.items())
        return self._size


class RoutingIndex:


    def __init__(self, network: Network, cache_destinations: int = 16,
                 cache_plans: int = 8192, cache_transfers: int = 8192) -> None:

        if cache_destinations < 1 or cache_plans < 0 or cache_transfers < 0:
            raise ValueError("目的站缓存必须为正整数，站对及换乘候选缓存不得为负数")
        self.network = network
        self.cache_destinations = int(cache_destinations)
        self.cache_plans = int(cache_plans)
        self.cache_transfers = int(cache_transfers)
        self._station_count = len(network.stations)
        self._lengths = np.asarray([len(route.stop_ids) for route in network.routes], dtype=np.int64)
        self._offsets = np.r_[0, np.cumsum(self._lengths)]
        self._stops = np.empty(int(self._offsets[-1]), dtype=np.int32)
        self._distances = np.empty(len(self._stops), dtype=np.float64)
        self._visit_routes = np.empty(len(self._stops), dtype=np.int32)
        self._visit_sequences = np.empty(len(self._stops), dtype=np.int32)
        self._active_visits = np.zeros(len(self._stops), dtype=bool)
        self._choice_dtype = np.int8 if max(self._lengths, default=0) <= 128 else np.int16 if max(self._lengths, default=0) <= 32768 else np.int32
        self._active_routes = []
        for route_index, route in enumerate(network.routes):

            stops = np.asarray(route.stop_ids)
            distances = np.asarray(route.distances_km, dtype=float)
            if stops.ndim != 1 or distances.ndim != 1 or len(stops) != len(distances):
                raise ValueError(f"线路 {route.route_id} 的站序和里程必须是等长一维数组")
            if not np.issubdtype(stops.dtype, np.integer) or np.any(stops < 0) or np.any(stops >= self._station_count):
                raise ValueError(f"线路 {route.route_id} 含非法站索引")
            if not np.all(np.isfinite(distances)) or np.any(distances < 0) or np.any(np.diff(distances) < 0):
                raise ValueError(f"线路 {route.route_id} 的累计里程必须有限且不递减")
            if not np.isfinite(route.frequency_veh_h) or route.frequency_veh_h < 0:
                raise ValueError(f"线路 {route.route_id} 的频率必须有限且非负")

            start, end = self._offsets[route_index:route_index + 2]
            self._stops[start:end] = stops
            self._distances[start:end] = distances
            self._visit_routes[start:end] = route_index
            self._visit_sequences[start:end] = np.arange(len(stops), dtype=np.int32)
            if route.frequency_veh_h > 0 and len(stops) >= 2:
                self._active_routes.append(route_index)
                self._active_visits[start:end] = True

        sorted_visits = np.argsort(self._stops, kind="stable")
        bounds = np.searchsorted(self._stops[sorted_visits], np.arange(self._station_count + 1))
        self._occurrences = tuple(sorted_visits[bounds[station]:bounds[station + 1]] for station in range(self._station_count))
        self._origin_positions = np.empty(len(self._stops), dtype=np.int32)
        for visits in self._occurrences:
            self._origin_positions[visits] = np.arange(len(visits), dtype=np.int32)
        self._destinations: OrderedDict[int, _DestinationData] = OrderedDict()
        self._plans: OrderedDict[tuple[int, int, int, str], Plan] = OrderedDict()
        self._transfers: OrderedDict[tuple[int, int, int, bool], np.ndarray] = OrderedDict()
        self._direct_reachable_bits = None
        self._one_transfer_reachable_bits = [None] * self._station_count

    def _direct_reachability(self) -> list[int]:


        if self._direct_reachable_bits is None:

            reachable = [0] * self._station_count
            for route_index in self._active_routes:
                suffix = 0
                for station in self.network.routes[route_index].stop_ids[::-1]:
                    reachable[int(station)] |= suffix
                    suffix |= 1 << int(station)
            self._direct_reachable_bits = reachable
        return self._direct_reachable_bits

    def one_transfer_reachable(self, origins: np.ndarray, destinations: np.ndarray) -> np.ndarray:


        origins, destinations = np.asarray(origins), np.asarray(destinations)
        if origins.ndim != 1 or destinations.shape != origins.shape:
            raise ValueError("批量起终站必须为等长的一维整数序列")
        if not len(origins):
            return np.empty(0, dtype=bool)
        if (not np.issubdtype(origins.dtype, np.integer) or not np.issubdtype(destinations.dtype, np.integer)
                or np.any(origins < 0) or np.any(destinations < 0)
                or np.any(origins >= self._station_count) or np.any(destinations >= self._station_count)):
            raise ValueError("批量起终站索引必须为网络范围内的整数")
        direct = self._direct_reachability()
        for value in np.unique(origins):
            origin = int(value)
            if self._one_transfer_reachable_bits[origin] is not None:
                continue
            reachable = direct[origin] | (1 << origin)
            transfers = direct[origin]
            while transfers:
                bit = transfers & -transfers
                reachable |= direct[bit.bit_length() - 1]
                transfers ^= bit
            self._one_transfer_reachable_bits[origin] = reachable
        return np.fromiter((bool(self._one_transfer_reachable_bits[int(origin)] & (1 << int(destination)))
                            for origin, destination in zip(origins, destinations)), dtype=bool, count=len(origins))

    def transfer_candidates(self, route_index: int, board_sequence: int,
                            destination: int) -> np.ndarray:


        if any(isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer))
               for value in (route_index, board_sequence, destination)):
            raise ValueError("换乘的线路、上车站序和终点必须为整数索引")
        if not 0 <= route_index < len(self._lengths) or not 0 <= destination < self._station_count:
            raise ValueError("换乘的线路或终点超出网络范围")
        if not 0 <= board_sequence < self._lengths[route_index]:
            raise ValueError("换乘上车站序超出线路范围")

        key = (int(route_index), int(board_sequence), int(destination))
        if key in self._transfers:
            self._transfers.move_to_end(key)
            return self._transfers[key]

        candidates = []
        if self._active_visits[int(self._offsets[route_index]) + int(board_sequence)]:

            route = self.network.routes[route_index]
            reachable = self._direct_reachability()
            destination_bit = 1 << int(destination)
            for sequence in range(int(board_sequence) + 1, len(route.stop_ids)):

                station = int(route.stop_ids[sequence])
                if station == destination or not reachable[station] & destination_bit:
                    continue
                candidates.append(sequence)

        result = np.asarray(candidates, dtype=np.int32)
        result.flags.writeable = False
        if self.cache_transfers:
            self._transfers[key] = result
            if len(self._transfers) > self.cache_transfers:
                self._transfers.popitem(last=False)
        return result

    def _segmented_downstream_minima(self, values: np.ndarray) -> np.ndarray:


        count = len(values)
        choices = np.full(count, -1, dtype=np.int64)
        if count < 2:
            return choices

        unique_values, ranks = np.unique(values, return_inverse=True)

        base = len(unique_values) + 1
        encoded = self._visit_routes.astype(np.int64) * base + ranks

        suffix_encoded = np.minimum.accumulate(encoded[::-1])[::-1]
        suffix_values = unique_values[suffix_encoded % base]

        is_record = np.isfinite(values) & self._active_visits & _same_distance(values, suffix_values)

        record_visits = np.where(is_record, np.arange(count), count)
        suffix_visits = np.minimum.accumulate(record_visits[::-1])[::-1]
        choices[:-1] = suffix_visits[1:]

        valid = (choices >= 0) & (choices < count) & self._active_visits
        valid[valid] &= self._visit_routes[choices[valid]] == self._visit_routes[valid]
        choices[~valid] = -1
        return choices

    def _direct_visits(self, origin_visits: np.ndarray, destination: int) -> np.ndarray:


        destination_visits = self._occurrences[destination]

        result = np.full(len(origin_visits), -1, dtype=np.int64)

        following = np.searchsorted(destination_visits, origin_visits, side="right")

        valid = following < len(destination_visits)
        result[valid] = destination_visits[following[valid]]
        valid[valid] &= self._visit_routes[result[valid]] == self._visit_routes[origin_visits[valid]]
        valid &= self._active_visits[origin_visits]
        result[~valid] = -1
        return result

    def _build_destination(self, destination: int) -> _DestinationData:


        visits = np.arange(len(self._stops))
        direct_visits = self._direct_visits(visits, destination)

        direct_best = np.full(self._station_count, np.inf)

        direct_board = np.flatnonzero(direct_visits >= 0)
        np.minimum.at(direct_best, self._stops[direct_board],
                      self._distances[direct_visits[direct_board]] - self._distances[direct_board])
        direct_best[destination] = 0.0

        tail = direct_best[self._stops]

        earliest_visits = self._segmented_downstream_minima(tail)
        earliest_visits[direct_board] = direct_visits[direct_board]

        shortest_visits = self._segmented_downstream_minima(self._distances + tail)

        tied_direct = _same_distance(self._distances[direct_visits[direct_board]],
                                     (self._distances + tail)[shortest_visits[direct_board]])
        shortest_visits[direct_board[tied_direct]] = direct_visits[direct_board[tied_direct]]

        choices_arrays = []
        best_arrays = [direct_best]
        for position, choices in enumerate((direct_visits, earliest_visits, shortest_visits)):

            local = np.full(len(visits), -1, dtype=self._choice_dtype)

            board = np.flatnonzero(choices >= 0)
            alight = choices[board]
            local[board] = self._visit_sequences[alight]
            choices_arrays.append(local)
            if position:

                best = np.full(self._station_count, np.inf)
                np.minimum.at(best, self._stops[board],
                              self._distances[alight] - self._distances[board] + tail[alight])
                best[destination] = 0.0
                best_arrays.append(best)
        for array in (*choices_arrays, *best_arrays):
            array.flags.writeable = False
        return _DestinationData(*choices_arrays, *best_arrays)

    def _destination(self, destination: int) -> _DestinationData:
        if destination in self._destinations:
            self._destinations.move_to_end(destination)
            return self._destinations[destination]


        result = self._build_destination(destination)
        self._destinations[destination] = result
        if len(self._destinations) > self.cache_destinations:
            self._destinations.popitem(last=False)
        return result

    def _make_plan(self, origin: int, destination: int, remaining_transfers: int,
                   strategy: str, data: _DestinationData | None = None) -> Plan:


        if origin == destination:
            return Plan({}, 0.0, True)

        visits = self._occurrences[origin]
        if remaining_transfers == 0:

            alight_visits = self._direct_visits(visits, destination)

            valid = alight_visits >= 0
            distances = np.full(len(visits), np.inf)
            distances[valid] = self._distances[alight_visits[valid]] - self._distances[visits[valid]]

            best = float(np.min(distances, initial=np.inf))

            choices = np.full(len(visits), -1, dtype=self._choice_dtype)
            choices[valid] = self._visit_sequences[alight_visits[valid]]
        else:
            if data is None:
                data = self._destination(destination)

            source = data.earliest if strategy == "earliest" else data.shortest
            best = float((data.earliest_best if strategy == "earliest" else data.shortest_best)[origin])
            choices = source[visits].copy()

            valid = choices >= 0

            alight_visits = self._offsets[self._visit_routes[visits[valid]]] + choices[valid]
            distances = np.full(len(visits), np.inf)
            distances[valid] = self._distances[alight_visits] - self._distances[visits[valid]] + data.direct_best[self._stops[alight_visits]]
        if not np.isfinite(best):
            return Plan({}, np.inf, False)
        if strategy == "shortest":
            choices[~_same_distance(distances, best)] = -1
        choices.flags.writeable = False
        return Plan(_CompactOptions(self, origin, choices), best, True)

    def get_plan(self, origin: int, destination: int, remaining_transfers: int,
                 strategy: str) -> Plan:


        if not isinstance(origin, (int, np.integer)) or not isinstance(destination, (int, np.integer)):
            raise ValueError("起终站必须为整数内部索引")
        if not 0 <= origin < self._station_count or not 0 <= destination < self._station_count:
            raise ValueError("起终站索引超出网络范围")
        if remaining_transfers not in (0, 1) or strategy not in ("earliest", "shortest"):
            raise ValueError("剩余换乘次数只能为 0/1，策略只能为 earliest/shortest")

        key = (int(origin), int(destination), remaining_transfers, strategy)
        if key in self._plans:
            self._plans.move_to_end(key)
            return self._plans[key]

        result = self._make_plan(int(origin), int(destination), remaining_transfers, strategy)
        if self.cache_plans:
            self._plans[key] = result
            if len(self._plans) > self.cache_plans:
                self._plans.popitem(last=False)
        return result

    def prepare_pairs(self, origins: Iterable[int], destinations: Iterable[int],
                      remaining_transfers: int, strategy: str) -> dict[tuple[int, int], Plan]:


        pairs = np.column_stack((np.asarray(list(origins) if not isinstance(origins, np.ndarray) else origins),
                                 np.asarray(list(destinations) if not isinstance(destinations, np.ndarray) else destinations)))
        if pairs.ndim != 2 or pairs.shape[1] != 2 or (len(pairs) and not np.issubdtype(pairs.dtype, np.integer)):
            raise ValueError("批量起终站必须为等长的一维整数序列")
        if np.any(pairs < 0) or np.any(pairs >= self._station_count):
            raise ValueError("批量起终站索引超出网络范围")
        if remaining_transfers not in (0, 1) or strategy not in ("earliest", "shortest"):
            raise ValueError("剩余换乘次数只能为 0/1，策略只能为 earliest/shortest")

        unique_pairs = np.unique(pairs[:, ::-1], axis=0)

        result: dict[tuple[int, int], Plan] = {}
        current_destination, data = None, None
        for destination, origin in unique_pairs:
            if remaining_transfers and origin != destination and destination != current_destination:
                data = self._destination(int(destination))
                current_destination = destination
            result[(int(origin), int(destination))] = self._make_plan(int(origin), int(destination), remaining_transfers, strategy, data)
        return result
