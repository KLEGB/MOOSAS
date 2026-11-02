"""Typed weather-domain data."""

from __future__ import annotations

from dataclasses import dataclass, fields

import numpy as np

from ..contracts import Location

@dataclass(frozen=True, slots=True)
class WeatherData:
    """One year of numeric hourly weather data and its source metadata."""

    location: Location
    weather_file: str
    hour_of_year: np.ndarray
    temperature: np.ndarray
    humidity_ratio: np.ndarray
    global_radiation: np.ndarray
    diffuse_radiation: np.ndarray
    ground_temperature: np.ndarray
    sky_temperature: np.ndarray  # Kelvin, as stored in MOOSAS/DeST CSV column 8.
    wind_speed: np.ndarray
    wind_direction: np.ndarray
    pressure: np.ndarray

    HOURS_PER_YEAR = 8760

    def __post_init__(self):
        object.__setattr__(self, "weather_file", str(self.weather_file))
        for field in fields(self)[2:]:
            values = np.array(getattr(self, field.name), dtype=float, copy=True)
            if values.shape != (self.HOURS_PER_YEAR,):
                raise ValueError(
                    f"{field.name} must contain {self.HOURS_PER_YEAR} hourly values, got {values.shape}"
                )
            values.setflags(write=False)
            object.__setattr__(self, field.name, values)
