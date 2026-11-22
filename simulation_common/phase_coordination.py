from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from math import ceil

import numpy as np

from .core import Network, SimulationConfig, stable_seed


PHASE_COORDINATION_VERSION = "shared_stop_residual_wait_coordinate_descent_v1"
PHASE_WINDOW_S = 14400.0
PHASE_SWEEPS = 2
PHASE_GRID_SIZE = 16
PHASE_EVENT_BUDGET = 2000000
_CACHE = OrderedDict()


def phase_coordination_settings() -> dict:
    return {
        "version": PHASE_COORDINATION_VERSION,
        "window_s": PHASE_WINDOW_S,
        "sweeps": PHASE_SWEEPS,
        "grid_size": PHASE_GRID_SIZE,
        "event_budget": PHASE_EVENT_BUDGET,
    }


@dataclass
class _SharedStop:
    direction: str
    station: int
    offsets: dict[int, np.ndarray]
    times: np.ndarray | None = None
    owners: np.ndarray | None = None


def _route_events(phases: np.ndarray, headway: float, offsets: np.ndarray, window_s: float) -> np.ndarray:
    starts = (phases[:, None] + offsets[None, :]) % headway
    steps = np.arange(ceil(window_s / headway) + 1, dtype=float) * headway
    events = (starts[:, :, None] + steps[None, None, :]).reshape(len(phases), -1)
    return np.sort(events, axis=1)


def _wait_area(times: np.ndarray, window_s: float) -> float:

    ends = times[: np.searchsorted(times, window_s, side="left") + 1]
    left = np.r_[0.0, ends[:-1]]
    right = np.minimum(ends, window_s)
    widths = np.maximum(0.0, right - left)
    return float(np.sum(widths * (ends - (left + right) / 2)))


def _inserted_wait_areas(base: np.ndarray, additions: np.ndarray, window_s: float) -> np.ndarray:
    indices = np.searchsorted(base, additions, side="left")
    valid = indices < len(base)
    upper = base[np.minimum(indices, len(base) - 1)]
    lower = np.where(indices > 0, base[np.maximum(indices - 1, 0)], 0.0)
    previous = np.c_[np.zeros(len(additions)), additions[:, :-1]]
    left = np.maximum(lower, previous)
    right = np.minimum(additions, window_s)
    reduction = np.where(valid, np.maximum(right - left, 0.0) * (upper - additions), 0.0)
    return _wait_area(base, window_s) - reduction.sum(axis=1)


def _populate(group: _SharedStop, phases: np.ndarray, headways: np.ndarray, window_s: float) -> None:
    rows, owners = [], []
    for route, offsets in group.offsets.items():
        events = _route_events(phases[route : route + 1], headways[route], offsets, window_s)[0]

        events = events[: np.searchsorted(events, window_s, side="left") + 1]
        rows.append(events)
        owners.append(np.full(len(events), route, dtype=np.int32))
    times = np.concatenate(rows)
    owner_array = np.concatenate(owners)
    order = np.argsort(times, kind="stable")
    group.times, group.owners = times[order], owner_array[order]


def _group_waits(
    groups: list[_SharedStop], phases: np.ndarray, headways: np.ndarray, window_s: float
) -> np.ndarray:
    for group in groups:
        _populate(group, phases, headways, window_s)
    return np.array([_wait_area(group.times, window_s) / window_s for group in groups])


def _groups(network: Network, arrival_offsets: list[np.ndarray]) -> list[_SharedStop]:
    visits = {}
    for route_index, route in enumerate(network.routes):
        for station, offset in zip(route.stop_ids, arrival_offsets[route_index]):
            key = (route.direction, int(station))
            visits.setdefault(key, {}).setdefault(route_index, []).append(float(offset))
    return [
        _SharedStop(direction, station, {route: np.asarray(values) for route, values in members.items()})
        for (direction, station), members in sorted(visits.items())
        if len(members) > 1
    ]


def coordinate_route_phases(
    network: Network, arrival_offsets: list[np.ndarray], rep: int, config: SimulationConfig
) -> tuple[np.ndarray, dict, tuple]:
    headways = np.array([3600.0 / route.frequency_veh_h for route in network.routes])
    key = stable_seed(
        PHASE_COORDINATION_VERSION,
        PHASE_WINDOW_S,
        PHASE_SWEEPS,
        PHASE_GRID_SIZE,
        PHASE_EVENT_BUDGET,
        network.scenario.seed_key,
        rep,
        config.base_seed,
        [
            (
                route.route_id,
                route.direction,
                route.frequency_veh_h,
                route.stop_ids.tolist(),
                offsets.tolist(),
            )
            for route, offsets in zip(network.routes, arrival_offsets)
        ],
    )
    if key in _CACHE:
        _CACHE.move_to_end(key)
        phases, metadata, audit = _CACHE[key]
        return phases.copy(), dict(metadata), tuple(dict(row) for row in audit)

    initial = np.array(
        [
            np.random.default_rng(
                stable_seed(
                    config.base_seed,
                    network.scenario.seed_key,
                    rep,
                    "periodic_phase",
                    route.direction,
                    route.route_id,
                )
            ).random()
            * headway
            for route, headway in zip(network.routes, headways)
        ]
    )
    phases = initial.copy()
    groups = _groups(network, arrival_offsets)

    rate = sum(len(offsets) / headways[route] for group in groups for route, offsets in group.offsets.items())
    window_s = min(PHASE_WINDOW_S, max(3600.0, PHASE_EVENT_BUDGET / max(rate, 1.0)))
    initial_waits = _group_waits(groups, initial, headways, window_s)

    assigned = set()
    seeded = initial.copy()
    for group in sorted(groups, key=lambda item: (-len(item.offsets), item.direction, item.station)):
        routes = sorted(group.offsets, key=lambda route: network.routes[route].route_id)
        h = headways[routes[0]]
        if (
            not assigned.intersection(routes)
            and all(len(group.offsets[route]) == 1 for route in routes)
            and np.allclose(headways[routes], h, rtol=1e-12, atol=1e-9)
        ):
            anchor = (seeded[routes[0]] + group.offsets[routes[0]][0]) % h
            for rank, route in enumerate(routes):
                seeded[route] = (anchor + rank * h / len(routes) - group.offsets[route][0]) % h
            assigned.update(routes)
    seeded_waits = _group_waits(groups, seeded, headways, window_s)
    if seeded_waits.sum() < initial_waits.sum():
        phases = seeded
    else:
        _group_waits(groups, phases, headways, window_s)

    memberships = [[] for _ in network.routes]
    for group_index, group in enumerate(groups):
        for route in group.offsets:
            memberships[route].append(group_index)
    route_order = sorted(
        range(len(network.routes)),
        key=lambda route: (-len(memberships[route]), network.routes[route].route_id),
    )
    accepted = 0
    sweeps = 0
    for sweep in range(PHASE_SWEEPS):
        changed = False
        for route in route_order:
            related = memberships[route]
            if not related:
                continue
            h = headways[route]
            candidates = [phases[route], *list(np.arange(PHASE_GRID_SIZE) * h / PHASE_GRID_SIZE)]

            primary = groups[max(related, key=lambda index: len(groups[index].offsets))]
            base = primary.times[primary.owners != route]
            ends = base[: np.searchsorted(base, window_s, side="left") + 1]
            starts = np.r_[0.0, ends[:-1]]
            gaps = np.minimum(ends, window_s) - starts
            for position in np.argsort(-gaps, kind="stable")[:8]:
                middle = (starts[position] + ends[position]) / 2
                candidates.append((middle - primary.offsets[route][0]) % h)
            candidates = np.array(list(dict.fromkeys(float(value % h) for value in candidates)))
            costs = np.zeros(len(candidates))
            bases = []
            for group_index in related:
                group = groups[group_index]
                base = group.times[group.owners != route]
                additions = _route_events(candidates, h, group.offsets[route], window_s)
                costs += _inserted_wait_areas(base, additions, window_s) / window_s
                bases.append((base, group.owners[group.owners != route]))
            best = int(np.argmin(costs))
            if costs[best] < costs[0] - 1e-8:
                phases[route] = candidates[best]
                for group_index, (base, base_owners) in zip(related, bases):
                    group = groups[group_index]
                    events = _route_events(phases[route : route + 1], h, group.offsets[route], window_s)[0]
                    events = events[: np.searchsorted(events, window_s, side="left") + 1]
                    times = np.r_[base, events]
                    owners = np.r_[base_owners, np.full(len(events), route, dtype=np.int32)]
                    order = np.argsort(times, kind="stable")
                    group.times, group.owners = times[order], owners[order]
                changed = True
                accepted += 1
        sweeps = sweep + 1
        if not changed:
            break

    final_waits = _group_waits(groups, phases, headways, window_s)

    if final_waits.sum() > initial_waits.sum() + 1e-7:
        phases = initial.copy()
        final_waits = _group_waits(groups, phases, headways, window_s)
        accepted = 0
    audit = tuple(
        {
            "direction": group.direction,
            "station_id": group.station + 1,
            "route_count": len(group.offsets),
            "combined_headway_s": 1
            / sum(len(offsets) / headways[route] for route, offsets in group.offsets.items()),
            "ideal_half_headway_s": 0.5
            / sum(len(offsets) / headways[route] for route, offsets in group.offsets.items()),
            "initial_mean_wait_s": float(before),
            "coordinated_mean_wait_s": float(after),
            "improvement_s": float(before - after),
        }
        for group, before, after in zip(groups, initial_waits, final_waits)
    )
    metadata = {
        "version": PHASE_COORDINATION_VERSION,
        "objective": "sum_of_shared_stop_uniform_arrival_residual_wait_seconds",
        "window_start_s": 0.0,
        "window_end_s": window_s,
        "shared_stop_group_count": len(groups),
        "sweeps": sweeps,
        "accepted_phase_changes": accepted,
        "objective_before_s": float(initial_waits.sum()),
        "objective_after_s": float(final_waits.sum()),
        "groups_improved": int(np.sum(final_waits < initial_waits - 1e-7)),
        "groups_worsened": int(np.sum(final_waits > initial_waits + 1e-7)),
        "phase_signature": str(stable_seed(phases.tolist())),
    }
    _CACHE[key] = (phases.copy(), dict(metadata), tuple(dict(row) for row in audit))
    while len(_CACHE) > 4:
        _CACHE.popitem(last=False)
    return phases.copy(), metadata, audit
