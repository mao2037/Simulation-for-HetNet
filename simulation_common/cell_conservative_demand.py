from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.io import loadmat
from scipy.ndimage import maximum_filter

from .core import CHECKERBOARD_SEED_NAMES, Scenario, number_text, project_path


CELL_CONSERVATIVE_DEMAND_VERSION = "mat_od_cell_conservative_multilinear_v1"


def _validated_density(density: np.ndarray, citysize: float) -> np.ndarray:
    density = np.asarray(density, dtype=float)
    if density.ndim != 4 or density.shape[:2] != density.shape[2:] or min(density.shape) < 2:
        raise ValueError("lambda_OD 必须是 (n_x, n_y, n_x, n_y) 的四维数组")
    if not np.all(np.isfinite(density)) or np.any(density < 0):
        raise ValueError("lambda_OD 必须有限且非负")
    if not np.isfinite(citysize) or citysize <= 0:
        raise ValueError("城市边长必须为有限正值")
    return density


class CellConservativeDemand:
    def __init__(self, density: np.ndarray, citysize: float):
        density = _validated_density(density, citysize)
        self.source_shape = density.shape
        total = float(density.sum())
        if not np.isfinite(total) or total <= 0:
            raise ValueError("原始四维 OD 节点总质量必须为正且有限")

        self._probabilities = (density / total).ravel()
        x, y = (np.linspace(0, citysize, size) for size in density.shape[:2])
        axes = (x, y, x, y)
        self._edges = tuple(np.r_[0, (axis[:-1] + axis[1:]) / 2, citysize] for axis in axes)
        self._interpolate = RegularGridInterpolator(axes, density)

        self._upper = maximum_filter(density, size=3, mode="constant", cval=0)

    def sample(self, count: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
        drawn = rng.choice(self._probabilities.size, size=count, p=self._probabilities)
        indices = np.unravel_index(drawn, self.source_shape)
        lower = np.column_stack([edges[index] for edges, index in zip(self._edges, indices)])
        upper = np.column_stack([edges[index + 1] for edges, index in zip(self._edges, indices)])
        bounds = self._upper[indices]
        points = np.empty((count, 4))
        pending = np.arange(count)
        while pending.size:
            candidates = rng.uniform(lower[pending], upper[pending])
            accepted = rng.random(len(pending)) * bounds[pending] < self._interpolate(candidates)
            points[pending[accepted]] = candidates[accepted]
            pending = pending[~accepted]
        return points[:, :2], points[:, 2:]



def demand_source_identity(source: dict) -> dict:
    return {key: source[key] for key in ("demand_version", "demand_source_sha256")}


@lru_cache(maxsize=2)
def _load(path: Path, citysize: float, demand: int, file_version: tuple):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
        stream.seek(0)
        data = loadmat(stream, variable_names=("lambda_OD", "D"))
    if "lambda_OD" not in data or np.asarray(data.get("D", [])).size != 1:
        raise ValueError(f"需求 MAT 缺少 lambda_OD 或标量 D：{path}")
    if not np.isclose(float(data["D"].item()), demand, rtol=1e-9, atol=1e-8):
        raise ValueError(f"需求 MAT 中的 D 与场景人数 {demand} 不一致：{path}")
    metadata = {"demand_version": CELL_CONSERVATIVE_DEMAND_VERSION, "demand_source": path.relative_to(project_path()).as_posix(),
                "demand_source_sha256": sha.hexdigest(), "normalized_demand": demand,
                "coordinate_order": "x_o,y_o,x_d,y_d"}
    od = CellConservativeDemand(data["lambda_OD"], citysize)
    metadata.update(cell_partition="midpoints between CA nodes, clipped to city boundary",
                    cell_pair_mass="original CA OD nodes normalized with equal quadrature weights",
                    within_cell_sampling="continuous, conditional on the four-dimensional linear interpolant",
                    demand_node_shape=list(od.source_shape))
    return od, metadata


def load_cell_conservative_demand(scenario: Scenario):
    filename = f"lambda_{scenario.demand}_citysize_{number_text(scenario.citysize)}.mat"
    if scenario.city in CHECKERBOARD_SEED_NAMES:
        filename = f"{scenario.city}_{filename}"
    path = project_path("inputs", "demand", scenario.city, filename)
    if not path.is_file():
        raise FileNotFoundError(f"Missing OD demand MAT: {path}")
    stat = path.stat()
    return _load(path, scenario.citysize, scenario.demand, (stat.st_size, stat.st_mtime_ns))
