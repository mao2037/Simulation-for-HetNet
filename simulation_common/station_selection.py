from __future__ import annotations

import numpy as np

from .core import PassengerInput
from .routing import RoutingIndex


def _rank_stations(point: np.ndarray, stations: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    offsets = point - stations
    distances = np.abs(offsets).sum(axis=1)
    order = np.argsort(distances, kind="stable")
    sorted_distances = distances[order]
    if np.any(np.diff(sorted_distances) <= 1e-10):
        start = 0
        while start < len(order):
            end = int(np.searchsorted(sorted_distances, sorted_distances[start] + 1e-10, side="right"))
            order[start:end] = np.sort(order[start:end])
            start = end
    return order, distances


def repair_unreachable_station_pairs(routing: RoutingIndex, inputs: PassengerInput,
                                     board_stations: np.ndarray, alight_stations: np.ndarray,
                                     access_km: np.ndarray, egress_km: np.ndarray) -> None:


    unresolved = np.flatnonzero(~routing.one_transfer_reachable(board_stations, alight_stations))
    stations = routing.network.stations
    for passenger in unresolved:
        passenger = int(passenger)
        boards, board_distances = _rank_stations(inputs.origin_xy[passenger], stations)
        alights, alight_distances = _rank_stations(inputs.destination_xy[passenger], stations)
        for count in range(2, len(stations) + 1):

            board_ranks = np.r_[np.arange(count), np.full(count - 1, count - 1)]
            alight_ranks = np.r_[np.full(count, count - 1), np.arange(count - 1)]
            candidate_boards, candidate_alights = boards[board_ranks], alights[alight_ranks]
            feasible = ((candidate_boards != candidate_alights)
                        & routing.one_transfer_reachable(candidate_boards, candidate_alights))
            choices = np.flatnonzero(feasible)
            if not len(choices):
                continue
            costs = board_distances[candidate_boards[choices]] + alight_distances[candidate_alights[choices]]
            choices = choices[np.abs(costs - costs.min()) <= 1e-10]
            best = choices[np.lexsort((candidate_alights[choices], candidate_boards[choices]))[0]]
            board_stations[passenger], alight_stations[passenger] = candidate_boards[best], candidate_alights[best]
            access_km[passenger] = board_distances[board_stations[passenger]]
            egress_km[passenger] = alight_distances[alight_stations[passenger]]
            break
