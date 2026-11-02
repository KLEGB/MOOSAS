"""Energy workflows that require radiation-domain results."""

from __future__ import annotations

from ..energy.runner import EnergyRunner
from ..weather import WeatherData


def run_energy_with_radiation(
    model,
    *,
    weather: WeatherData,
    irradiance_by_surface,
    on_stage=None,
    **energy_options,
):
    """Run energy with explicit hourly incident shortwave, in W/m² per surface.

    Irradiance is before glazing optics; seasonal space gains and SHGC-weighted
    totals cannot reconstruct this boundary and are intentionally not accepted.
    """
    if on_stage is not None:
        on_stage('energy')
    return EnergyRunner(
        model=model,
        weather=weather,
        require_radiation=True,
        irradiance_by_surface=irradiance_by_surface,
        **energy_options,
    ).run()
