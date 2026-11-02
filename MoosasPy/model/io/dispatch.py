"""Strict file-format dispatch for complete ``MoosasModel`` objects."""

from __future__ import annotations

from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

from .result import SaveResult
from ...utils import path as path_utils


def _model_format(file_path: str | Path) -> str:
    path = Path(file_path)
    name = path.name.lower()
    if name.endswith(".graph.json"):
        return "graph"
    return path.suffix.lower().lstrip(".")


def _geo_sidecar(file_path: Path) -> Path:
    return file_path.with_suffix(".geo")


def _looks_like_gbxml(file_path: Path) -> bool:
    try:
        root = ET.parse(file_path).getroot()
    except (OSError, ET.ParseError):
        return False
    return root.tag.rsplit("}", 1)[-1].lower() == "gbxml"


def load_model(file_path: str | Path):
    """Load RDF, XML, JSON, IDF, or gbXML into a complete ``MoosasModel``."""
    source = Path(file_path)
    model_format = _model_format(source)

    # Many tools emit gbXML with a generic .xml suffix.  Detect that form
    # before dispatching to the Moosas XML+GEO sidecar loader.
    if model_format == "xml" and _looks_like_gbxml(source):
        model_format = "gbxml"

    if model_format in {"rdf", "ttl"}:
        from .rdf import loadRDF

        return loadRDF(str(source), fileFormat="turtle")
    if model_format == "xml":
        from .xml import loadXml

        return loadXml(str(source), str(_geo_sidecar(source)))
    if model_format == "json":
        from .json import loadJson

        return loadJson(str(source), str(_geo_sidecar(source)))
    if model_format == "idf":
        from .idf import readIDF

        return readIDF(str(source)).model
    if model_format == "gbxml":
        # The gbXML adapter normalizes the exchange format into the same BOT /
        # MOOSAS RDF graph used by the native model loader.  Keeping this
        # bridge here means callers can use the ordinary ``MoosasModel.load``
        # and receive a fully populated model that supports ``save``.
        from .gbxml import convert_gbxml_to_rdf
        from .rdf import loadRDF

        with tempfile.TemporaryDirectory(prefix=".moosas_gbxml_load_", dir=source.parent) as directory:
            rdf_path = Path(directory) / "model.ttl"
            convert_gbxml_to_rdf(source, rdf_path, rdf_format="turtle")
            return loadRDF(str(rdf_path), fileFormat="turtle")

    raise ValueError(f"Unsupported model load format: {model_format or source}")


def save_model(model, file_path: str | Path, **format_options) -> SaveResult:
    """Save a model using the capabilities defined for the target format."""
    target = Path(file_path)
    model_format = _model_format(target)
    if model_format == "foam":
        raise ValueError(
            "OpenFOAM export requires MoosasPy.model.io.foam.exportFoam"
        )
    supported_formats = {"rdf", "ttl", "xml", "json", "idf", "graph", "gbxml"}
    if model_format not in supported_formats:
        raise ValueError(f"Unsupported model save format: {model_format or target}")
    if format_options:
        raise TypeError(f"{model_format} save does not accept format options")
    if model_format == 'idf' and getattr(model,'energy_settings',{}).get('external_thermal_model'):
        raise ValueError('Source-backed IDF export requires the source-aligned EnergyPlus exporter; the legacy writer cannot preserve external thermal settings')
    path_utils.checkBuildDir(str(target))

    if model_format in {"rdf", "ttl"}:
        from .rdf import writeRDF

        writeRDF(model, str(target), fileFormat="turtle")
        generated = (target,)
    elif model_format == "xml":
        from .xml import writeXml
        from ...transform.importers.geo import writeGeo

        sidecar = _geo_sidecar(target)
        writeXml(str(target), model)
        writeGeo(str(sidecar), geoList=model.geometryList)
        generated = (target, sidecar)
    elif model_format == "json":
        from .json import writeJson
        from ...transform.importers.geo import writeGeo

        sidecar = _geo_sidecar(target)
        writeJson(str(target), model)
        writeGeo(str(sidecar), geoList=model.geometryList)
        generated = (target, sidecar)
    elif model_format == "idf":
        from .idf import writeIDF

        writeIDF(model, str(target))
        generated = (target,)
    elif model_format == "graph":
        from .graph import writeGraph

        writeGraph(str(target), model)
        generated = (target,)
    elif model_format == "gbxml":
        from .gbxml import convert_rdf_to_gbxml
        from .rdf import writeRDF

        with tempfile.TemporaryDirectory(prefix=".moosas_gbxml_", dir=target.parent) as directory:
            rdf_path = Path(directory) / "model.ttl"
            writeRDF(model, str(rdf_path), fileFormat="turtle")
            convert_rdf_to_gbxml(rdf_path, target, rdf_format="turtle")
        generated = (target,)
    return SaveResult(primary_path=target, generated_paths=generated)
