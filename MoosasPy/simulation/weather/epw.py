"""EPW parsing and explicit weather-asset generation."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import os
import tempfile
import shutil

import numpy as np

from ...utils.tools import path, native_platform
from ..contracts import Location
from ..runner import Runner
from .data import WeatherData
from .sky.cumulative import CumulativeSky, build_cumulative_skies


SUN_POSITION_FILE = os.path.join(path.dataBaseDir, "radiation", "sun_position.csv")

TREGENZA_COEFFICIENTS = np.array(
    [0.0435449227, 0.0416418006, 0.0473984151, 0.0406730411,
     0.0428934136, 0.0445221864, 0.0455168385, 0.0344199465],
    dtype=float,
)
TREGENZA_PATCHES_PER_ROW = [30, 30, 24, 24, 18, 12, 6, 1]


@dataclass(frozen=True, slots=True)
class PreparedWeather:
    """Weather and sky assets generated from one EPW file."""

    weather: WeatherData
    cumulative_sky_matrix: np.ndarray
    cumulative_skies: dict[str, CumulativeSky]
    cumulative_sky_file: str
    wea_file: str


def read_weather_csv(
    weather_file: str, location: Location, *, epw_file: str | None = None,
) -> WeatherData:
    """Read MOOSAS/DeST hourly CSV, optionally attaching its retained source EPW."""
    weather_file = os.path.abspath(weather_file)
    values = np.loadtxt(weather_file, delimiter=",", dtype=float)
    if values.shape != (WeatherData.HOURS_PER_YEAR, 13):
        raise ValueError(
            "Weather CSV must contain 8760 rows and 13 columns, "
            f"got {values.shape}"
        )
    return WeatherData(
        location=location,
        weather_file=weather_file,
        epw_file=epw_file,
        hour_of_year=values[:, 2],
        temperature=values[:, 3],
        humidity_ratio=values[:, 4] / 1000.,  # DeST CSV g/kg -> WeatherData SI kg/kg
        global_radiation=values[:, 5],
        diffuse_radiation=values[:, 6],
        ground_temperature=values[:, 7],
        sky_temperature=values[:, 8],
        wind_speed=values[:, 9],
        wind_direction=values[:, 10],
        pressure=values[:, 11],
    )


def _round_dest(values):
    values = np.asarray(values, dtype=float)
    return np.where(values >= 0, np.floor(values * 100 + 0.5), np.ceil(values * 100 - 0.5)) / 100


def read_epw_location(epw_file: str) -> Location:
    """Read location metadata and mean pressure from an EPW file."""
    with open(epw_file, encoding="utf-8") as source:
        location_row = source.readline().strip().split(",")
        for _ in range(7):
            source.readline()
        climate_rows = [source.readline().strip().split(",") for _ in range(8760)]
    if len(location_row) < 10:
        raise ValueError("Invalid EPW LOCATION row")
    if any(len(row) < 10 for row in climate_rows):
        raise ValueError("EPW must contain 8760 complete hourly rows")
    pressure = float(np.mean(np.asarray(climate_rows)[:, 9].astype(float)))
    return Location(
        station_id=location_row[5].rstrip(";"),
        city=location_row[1],
        state=location_row[2],
        latitude=location_row[6],
        longitude=location_row[7],
        altitude=location_row[9],
        pressure=pressure,
    )


def _convert_epw_columns(epw_file: str):
    """Convert EPW rows to the 13-column MOOSAS/DeST weather layout."""
    with open(epw_file, encoding="utf-8") as source:
        location_row = source.readline().strip().split(",")
        source.readline()
        source.readline()
        ground_row = source.readline().strip().split(",")
        ground_monthly = ground_row[6:18]
        if len(ground_monthly) != 12:
            raise ValueError("EPW ground-temperature row must contain 12 monthly values")
        month_days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        ground_temperature = np.array([
            float(value)
            for value, days in zip(ground_monthly, month_days)
            for _ in range(days * 24)
        ])
        for _ in range(4):
            source.readline()
        climate = np.array([source.readline().strip().split(",") for _ in range(8760)])

    if climate.shape[0] != 8760 or climate.shape[1] < 22:
        raise ValueError(f"EPW must contain 8760 complete hourly rows, got {climate.shape}")

    dew_point = climate[:, 7].astype(float)
    # Preserve the legacy CSV's g/kg unit, but derive water content from the
    # EPW dew point AND actual atmospheric pressure. The old polynomial could
    # produce negative moisture in cold weather.
    pressure_pa = climate[:, 9].astype(float)
    vapor_pressure = 610.94 * np.exp(17.625 * dew_point / (dew_point + 243.04))
    if (not np.isfinite(vapor_pressure).all() or np.any(pressure_pa <= vapor_pressure)
            or np.any(pressure_pa <= 0) or np.any(dew_point >= 99)):
        raise ValueError('EPW dew point/pressure is missing or invalid for humidity')
    humidity_ratio = 1000 * .622 * vapor_pressure / (pressure_pa - vapor_pressure)
    horizontal_infrared = climate[:, 12].astype(float)
    sky_temperature = np.power(horizontal_infrared / 5.67e-8, 0.25)
    wind_speed = climate[:, 21].astype(float)
    wind_direction_degrees = climate[:, 20].astype(float)
    wind_direction_degrees[wind_direction_degrees == 999] = 0
    wind_direction = np.round(wind_direction_degrees / 360 * 16, 0)
    wind_direction[(wind_speed != 0) & (wind_direction == 0)] = 16

    station_id = location_row[5].rstrip(";")
    return [
        [station_id] * 8760,
        np.zeros(8760),
        np.arange(8760),
        _round_dest(climate[:, 6]),
        _round_dest(humidity_ratio),
        _round_dest(climate[:, 13]),
        _round_dest(climate[:, 15]),
        _round_dest(ground_temperature),
        _round_dest(sky_temperature),
        _round_dest(wind_speed),
        wind_direction.astype(int),
        climate[:, 9].astype(float).round(2),
        np.full(8760, 9999999),
    ]


def write_epw_csv(epw_file: str, output_path: str) -> str:
    """Convert an EPW file to the MOOSAS/DeST CSV format."""
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    columns = _convert_epw_columns(epw_file)
    with open(output_path, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.writer(output_file)
        for hour_index in range(8760):
            writer.writerow([
                str(columns[0][hour_index]),
                "0",
                str(hour_index),
                *(
                    f"{float(columns[column][hour_index]):.2f}"
                    for column in range(3, 10)
                ),
                str(int(columns[10][hour_index])),
                f"{float(columns[11][hour_index]):.2f}",
                "9999999",
            ])
    return output_path


def load_epw(epw_file: str, output_dir: str) -> WeatherData:
    """Convert one user-provided EPW file into a ``WeatherData`` object."""
    if not str(epw_file).lower().endswith(".epw"):
        raise ValueError(f"Expected an EPW file: {epw_file}")
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    retained_epw = os.path.join(output_dir, 'weather.epw')
    if os.path.normcase(os.path.realpath(epw_file)) != os.path.normcase(os.path.realpath(retained_epw)):
        shutil.copyfile(epw_file, retained_epw)
    location = read_epw_location(retained_epw)
    weather_csv = write_epw_csv(retained_epw, os.path.join(output_dir, "weather.csv"))
    return read_weather_csv(weather_csv, location, epw_file=retained_epw)


def convert_epw_to_wea(
    epw_file: str | Path,
    output_path: str | Path,
) -> str:
    """Write an hourly non-leap WEA, preserving fractional UTC offsets.

    EPW records are end-of-hour; WEA uses local-standard-time midpoints.
    Missing DNI/DHI, subhourly data and incomplete/leap years are rejected.
    Used by both energy weather preparation and annual daylight.
    Conversion is local and retains the full EPW coordinate precision.
    """
    with open(epw_file, encoding='utf-8-sig', newline='') as source:
        rows = list(csv.reader(source))
    if len(rows) != 8768 or len(rows[0]) < 10 or rows[0][0] != 'LOCATION':
        raise ValueError('EPW requires a complete non-leap 8760-hour year')
    if len(rows[7]) < 3 or rows[7][0] != 'DATA PERIODS' or int(rows[7][2]) != 1:
        raise ValueError('EPW requires one record per hour')
    location = rows[0]
    latitude, longitude, offset, altitude = map(float, location[6:10])
    if not all(np.isfinite([latitude, longitude, offset, altitude])) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and -14 <= offset <= 14):
        raise ValueError('Invalid EPW location')
    lines = [f'place {location[1]}', f'latitude {latitude}',
             f'longitude {-longitude}', f'time_zone {-15 * offset}',
             f'site_elevation {altitude}', 'weather_data_file_units 1']
    for index, row in enumerate(rows[8:]):
        stamp = datetime(2001, 1, 1) + timedelta(hours=index)
        if len(row) < 16 or tuple(map(int, row[1:4])) != (stamp.month, stamp.day, stamp.hour + 1) or int(row[4]) not in (0, 30, 60):
            raise ValueError(f'Invalid/non-hourly EPW time axis at record {index}')
        dni, dhi = map(float, (row[14], row[15]))
        if not all(np.isfinite([dni, dhi])) or not (0 <= dni < 9999 and 0 <= dhi < 9999):
            raise ValueError(f'Missing/invalid DNI or DHI at EPW record {index}')
        lines.append(f'{stamp.month} {stamp.day} {stamp.hour + .5} {dni} {dhi}')
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(target)


def generate_cumulative_sky(
    wea_file: str,
    *,
    work_dir: str | None = None,
    timeout_seconds: float = 300.0,
) -> np.ndarray:
    """Generate a 145-by-8760 Tregenza sky matrix from a WEA file."""
    platform_dir, suffix = native_platform()
    executable = os.path.join(path.libDir, "radiance", platform_dir, "bin", f"gendaymtx{suffix}")
    with tempfile.TemporaryDirectory(prefix="moosas-cumsky-", dir=work_dir) as temporary_dir:
        matrix_output = os.path.join(temporary_dir, "sky.mtx")
        with open(matrix_output, "w", encoding="utf-8") as output_file:
            Runner(timeout_seconds=timeout_seconds).run_command(
                [executable, "-m", "1", "-O1", os.path.abspath(wea_file)],
                stdout=output_file,
            )
        rgb_rows = []
        with open(matrix_output, encoding="utf-8") as matrix_file:
            for line in matrix_file:
                parts = line.split()
                if len(parts) == 3:
                    try:
                        rgb_rows.append([float(value) for value in parts])
                    except ValueError:
                        continue

    rgb = np.asarray(rgb_rows, dtype=float)
    expected_shape = (146 * WeatherData.HOURS_PER_YEAR, 3)
    if rgb.shape != expected_shape:
        raise ValueError(f"Unexpected gendaymtx data shape: {rgb.shape}")
    broadband = 0.265074126 * rgb[:, 0] + 0.670114631 * rgb[:, 1] + 0.064811243 * rgb[:, 2]
    # gendaymtx row zero is ground; the following 145 rows are sky patches.
    matrix = broadband.reshape(146, WeatherData.HOURS_PER_YEAR)[1:]
    coefficients = np.repeat(TREGENZA_COEFFICIENTS, TREGENZA_PATCHES_PER_ROW).reshape(145, 1)
    return matrix * coefficients * 8760 / 1000


def calibrate_sky_radiation(
    calculated: np.ndarray,
    observed_global_radiation,
    *,
    sun_position_file: str = SUN_POSITION_FILE,
) -> np.ndarray:
    """Scale each hourly sky column to observed global radiation."""
    calculated = np.asarray(calculated, dtype=float).copy()
    observed = np.asarray(observed_global_radiation, dtype=float)
    positions = np.loadtxt(sun_position_file, delimiter=",", dtype=float)[:, 2]
    calculated_global = np.sum(calculated.T * positions, axis=1)
    scale = np.divide(
        observed,
        calculated_global,
        out=np.ones_like(observed, dtype=float),
        where=calculated_global != 0,
    )
    calculated *= scale
    return calculated


def prepare_epw(
    epw_file: str,
    output_dir: str,
    *,
    timeout_seconds: float = 300.0,
) -> PreparedWeather:
    """Retain the source EPW, weather CSV, sky MTX and intermediate WEA together."""
    output_dir = os.path.abspath(output_dir)
    weather = load_epw(epw_file, output_dir)
    assert weather.epw_file is not None
    wea_file = convert_epw_to_wea(
        weather.epw_file,
        os.path.join(output_dir, "weather.wea"),
    )
    sky_matrix = generate_cumulative_sky(
        wea_file,
        work_dir=output_dir,
        timeout_seconds=timeout_seconds,
    )
    sky_matrix = calibrate_sky_radiation(sky_matrix, weather.global_radiation)
    sky_file = os.path.join(output_dir, "cumulative_sky.mtx")
    np.savetxt(sky_file, sky_matrix, delimiter=",")
    return PreparedWeather(
        weather,
        sky_matrix,
        build_cumulative_skies(sky_matrix),
        sky_file,
        wea_file,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Convert an EPW file to MOOSAS weather CSV data.")
    parser.add_argument("input_path", help="input EPW file")
    parser.add_argument("-o", "--output", required=True, dest="output_path", help="output CSV path")
    args = parser.parse_args(argv)
    write_epw_csv(args.input_path, args.output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
