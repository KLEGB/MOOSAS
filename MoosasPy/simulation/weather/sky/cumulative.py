"""Cumulative sky data and packaged matrix readers."""

from __future__ import annotations

import os

import numpy as np

from ....transform.geometry.geos import Vector
from ....utils.tools import path


class CumulativeSky:
    """Radiation values for the 145 Tregenza sky patches."""

    __slots__ = ("_positions", "values")

    HOURS_PER_YEAR = 8760
    RADIATION_SCALE = 1000
    SUMMER_START_HOUR = 3624
    SUMMER_END_HOUR = 5832
    WINTER_START_HOUR = 8016
    WINTER_END_HOUR = 1416

    def __init__(self, values, positions=None):
        values = np.asarray(values, dtype=float)
        if values.shape != (145,):
            raise ValueError(f"Cumulative sky must contain 145 patch values, got {values.shape}")
        self.values = values
        self._positions = positions

    @property
    def positions(self):
        if self._positions is None:
            position_path = os.path.join(path.dataBaseDir, "radiation", "sun_position.csv")
            with open(position_path, encoding="utf-8") as position_file:
                self._positions = [
                    Vector(np.array(line.split(","), dtype=float))
                    for line in position_file.read().splitlines()
                    if line.strip()
                ]
        return self._positions

    @classmethod
    def from_period(cls, hourly_matrix, start_hour: int, end_hour: int):
        matrix = np.asarray(hourly_matrix, dtype=float)
        if matrix.shape != (145, cls.HOURS_PER_YEAR):
            raise ValueError(
                "Hourly sky matrix must have shape (145, 8760), "
                f"got {matrix.shape}"
            )
        start_hour = int(start_hour)
        end_hour = int(end_hour)
        if not 0 <= start_hour <= cls.HOURS_PER_YEAR:
            raise ValueError("start_hour must be between 0 and 8760")
        if not 0 <= end_hour <= cls.HOURS_PER_YEAR:
            raise ValueError("end_hour must be between 0 and 8760")
        if start_hour < end_hour:
            values = np.sum(matrix[:, start_hour:end_hour], axis=1)
        else:
            values = np.sum(matrix[:, start_hour:], axis=1) + np.sum(matrix[:, :end_hour], axis=1)
        return cls(values / cls.RADIATION_SCALE)


def read_cumulative_sky_matrix(matrix_file: str) -> np.ndarray:
    """Read a 145-by-8760 hourly cumulative-sky matrix CSV."""
    matrix = np.loadtxt(matrix_file, delimiter=",", dtype=float)
    if matrix.shape != (145, CumulativeSky.HOURS_PER_YEAR):
        raise ValueError(
            "Cumulative sky matrix must have shape (145, 8760), "
            f"got {matrix.shape}"
        )
    return matrix


def build_cumulative_skies(matrix) -> dict[str, CumulativeSky]:
    """Aggregate an hourly sky matrix into annual and seasonal sky objects."""
    return {
        "annual": CumulativeSky.from_period(matrix, 0, CumulativeSky.HOURS_PER_YEAR),
        "summer": CumulativeSky.from_period(
            matrix,
            CumulativeSky.SUMMER_START_HOUR,
            CumulativeSky.SUMMER_END_HOUR,
        ),
        "winter": CumulativeSky.from_period(
            matrix,
            CumulativeSky.WINTER_START_HOUR,
            CumulativeSky.WINTER_END_HOUR,
        ),
    }
