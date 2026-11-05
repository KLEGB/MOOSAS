from __future__ import annotations

import copy
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from xml.etree import ElementTree

from eppy.modeleditor import IDF
import numpy as np
import pytest

from MoosasPy.model import MoosasModel
from MoosasPy.model.io.idf import exportIDF
from MoosasPy.model.io.idf.adapter import IDFtoXml, _idf_build_artifacts
from MoosasPy.model.io.idf.input import createThermalSurface, encodeFace
from MoosasPy.model.io.idf.model import FaceDefault, MoosasSettings
from MoosasPy.model.io.idf.parser import ZoneTemplate
from MoosasPy.model.io.idf.version import bundled_template_idf_path, configure_idd
from MoosasPy.model import configure_model_resources
from MoosasPy.transform import transform
from MoosasPy.transform.geometry.element import MoosasElement, MoosasGeometry, MoosasSkylight
from MoosasPy.transform.geometry.geos import Vector
from MoosasPy.utils import shapely


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_FIXTURE = PROJECT_ROOT / "test" / "caseFile" / "test3_geomove.geo"


@pytest.fixture(scope="module")
def semantic_model() -> MoosasModel:
    return transform(str(GEOMETRY_FIXTURE), input_type="geo", stdout=StringIO())


def test_model_initialization_does_not_load_external_resources():
    model = MoosasModel()

    assert model.buildingTemplate == {}
    assert model.schedule == {}
    assert not hasattr(model, "weather")
    assert not hasattr(model, "cumSky")
    assert not hasattr(model, "idfZoneTemplate")
    assert not hasattr(model, "loadSchedule")
    assert not hasattr(model, "loadWeatherData")


def test_resource_service_configures_a_domain_model():
    model = configure_model_resources(MoosasModel())

    assert model.buildingTemplate
    assert model.schedule == {}


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_rdf_streaming_parser_preserves_large_literals(tmp_path, newline):
    from MoosasPy.model.io.rdf import MoosasRDF
    from rdflib import URIRef
    value = 'x' * 1048576 + '\n"metadata"'
    target = tmp_path / 'large.rdf'
    triples = [
        f'<urn:space> <urn:metadata> {json.dumps(value)} .',
        '<urn:space> <urn:type> <urn:room> .',
    ]
    target.write_bytes(newline.join(triples).encode('utf-8'))
    graph = MoosasRDF.load(str(target))
    assert len(graph) == len(MoosasRDF()) + 2
    assert str(graph.value(URIRef('urn:space'), URIRef('urn:metadata'))) == value


def test_full_iri_turtle_remains_supported(tmp_path):
    from MoosasPy.model.io.rdf import MoosasRDF
    target = tmp_path / 'full-iri.rdf'
    target.write_text('<urn:space> <urn:type> <urn:room> ; <urn:name> "Room" .', encoding='utf-8')
    assert len(MoosasRDF.load(str(target))) == len(MoosasRDF()) + 2


@pytest.mark.slow
def test_large_rdf_export_threshold_and_relative_iris(tmp_path, monkeypatch):
    from MoosasPy.model.io import rdf
    from rdflib import Graph, Literal, URIRef
    graph = Graph()
    predicate = URIRef('urn:value')
    for index in range(100000):
        graph.add((URIRef(f'urn:space:{index}'), predicate, Literal(index)))
    formats = []
    serialize = graph.serialize

    def record_serialization(destination, **kwargs):
        formats.append(kwargs['format'])
        return serialize(destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rdf, 'MoosasRDF', lambda *_: graph)
        patch.setattr(graph, 'serialize', record_serialization)
        rdf.writeRDF(None, str(tmp_path / 'threshold.rdf'))
        graph.add((URIRef('relative-space'), URIRef('relative-predicate'), Literal('value')))
        target = tmp_path / 'large.rdf'
        rdf.writeRDF(None, str(target))

    assert formats == ['turtle', 'nt']
    restored = rdf.MoosasRDF.load(str(target))
    assert len(restored) == len(graph) + len(rdf.MoosasRDF())
    assert set(graph) <= set(restored)
    from urllib.parse import urljoin
    assert (URIRef(urljoin(target.as_uri(), 'relative-space')),
            URIRef(urljoin(target.as_uri(), 'relative-predicate')), Literal('value')) in restored


@pytest.mark.parametrize("suffix", (".geo", ".obj", ".stl"))
def test_load_rejects_raw_geometry_sources(suffix: str):
    with pytest.raises(ValueError, match="Unsupported model load format"):
        MoosasModel.load(f"raw-geometry{suffix}")


@pytest.mark.parametrize("suffix", (".geo", ".obj", ".stl"))
def test_save_rejects_raw_geometry_targets(suffix: str):
    model = MoosasModel()
    with pytest.raises(ValueError, match="Unsupported model save format"):
        model.save(f"raw-geometry{suffix}")


@pytest.mark.parametrize("suffix", (".graph.json",))
def test_load_rejects_save_only_model_formats(suffix: str):
    with pytest.raises(ValueError, match="Unsupported model load format"):
        MoosasModel.load(f"model{suffix}")


@pytest.mark.parametrize("suffix", (".rdf", ".xml", ".json"))
def test_semantic_model_formats_round_trip(semantic_model: MoosasModel, suffix: str):
    with TemporaryDirectory() as directory:
        file_path = Path(directory) / f"model{suffix}"
        save_result = semantic_model.save(file_path)
        restored = MoosasModel.load(file_path)

    assert save_result.primary_path == file_path
    assert isinstance(restored, MoosasModel)
    assert len(restored.geometryList) == len(semantic_model.geometryList)
    assert len(restored.spaceList) == len(semantic_model.spaceList)
    assert len(restored.wallList) == len(semantic_model.wallList)


def test_json_round_trip_preserves_empty_shading(semantic_model: MoosasModel):
    original_shading = semantic_model.shadingList
    semantic_model.shadingList = []
    try:
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / "model.json"
            semantic_model.save(file_path)
            document = json.loads(file_path.read_text(encoding="utf-8"))
            document["shading"] = None
            file_path.write_text(json.dumps(document), encoding="utf-8")
            restored = MoosasModel.load(file_path)
    finally:
        semantic_model.shadingList = original_shading

    assert restored.shadingList == []


def _single_room_model(tmp_path):
    from MoosasPy.transform.importers.geo import writeGeo

    rings = [
        [(0, 0, 0), (0, 4, 0), (4, 4, 0), (4, 0, 0)],
        [(0, 0, 3), (4, 0, 3), (4, 4, 3), (0, 4, 3)],
        [(0, 0, 0), (4, 0, 0), (4, 0, 3), (0, 0, 3)],
        [(4, 0, 0), (4, 4, 0), (4, 4, 3), (4, 0, 3)],
        [(4, 4, 0), (0, 4, 0), (0, 4, 3), (4, 4, 3)],
        [(0, 4, 0), (0, 0, 0), (0, 0, 3), (0, 4, 3)],
        [(0, 1, 1), (0, 1, 2), (0, 2, 2), (0, 2, 1)],
    ]
    normals = [(0, 0, -1), (0, 0, 1), (0, -1, 0), (1, 0, 0),
               (0, 1, 0), (-1, 0, 0), (-1, 0, 0)]
    geometry = [
        MoosasGeometry(shapely.Polygon(ring), f"g_{index}", normal,
                       0 if index < 6 else 1, errors="raise")
        for index, (ring, normal) in enumerate(zip(rings, normals))
    ]
    source = tmp_path / "single.geo"
    writeGeo(str(source), geoList=geometry)
    model = transform(str(source), input_type="geo", stdout=StringIO())
    assert len(model.spaceList) == 1
    assert len(model.glazingList) == 1
    return model


def test_new_spaces_do_not_select_a_national_standard(tmp_path):
    model = _single_room_model(tmp_path)
    space = model.spaceList[0]
    assert space.settings['zone_template'] is None
    assert '_thermal_template' not in space.settings
    assert 'zone_ppsm' not in space.settings
    with pytest.raises(ValueError, match='complete namespaced id'):
        space.applySettings('RESIDENTIAL')


@pytest.mark.parametrize('suffix', ['.ttl', '.xml', '.json'])
def test_unbundled_standard_snapshot_round_trip(tmp_path, suffix):
    from copy import deepcopy
    from MoosasPy.model.thermal_templates import apply_thermal_template, template_program

    model = _single_room_model(tmp_path)
    record = deepcopy(model.buildingTemplate['CN/GB55015-2021/public_A_cold/OFFICE'])
    # This synthetic standard is deliberately absent from the bundled catalog.
    template_id = 'ZZ/TEST-2026/climate-1/OFFICE'
    record['standard'] = 'ZZ/TEST-2026'
    record['_thermal_template']['id'] = template_id
    record['_thermal_template']['standard'] = dict(country='ZZ', id='TEST-2026', title='Synthetic')
    space = model.spaceList[0]
    apply_thermal_template(space, record, template_id)
    target = tmp_path / ('standard-snapshot' + suffix)
    model.save(target)
    restored = MoosasModel.load(target)
    restored_space = next(s for s in restored.spaceList if s.id == space.id)
    assert restored_space.settings['_thermal_template'] == space.settings['_thermal_template']
    assert restored_space.settings['zone_template'] == template_id
    np.testing.assert_array_equal(
        template_program(restored_space.settings, 'zone_ppsm', .1),
        template_program(space.settings, 'zone_ppsm', .1))


@pytest.mark.parametrize('suffix', ['.ttl', '.xml', '.json'])
def test_solver_settings_and_model_metadata_round_trip(tmp_path, suffix):
    model = _single_room_model(tmp_path)
    space = model.spaceList[0]
    window = model.glazingList[0]
    model.energy_settings = {'validation': {'enabled': True, 'hours': 24}, 'latent': {'enabled': True}}
    model.schedule = {'probe_daily': {'type': 'Daily', 'value': [1.] * 24},
                      'person_moisture': {'type': 'Daily', 'value': [.04] * 12 + [.08] * 12, 'value_mode': 'absolute'}}
    space.conditioned = False
    space.settings['daylight_occupied_mask'] = [True] * 8760
    space.settings['zone_popmoisture'] = 'person_moisture'
    space.settings['zone_c_hum'] = .55
    space.explicit_settings = ['daylight_occupied_mask', 'zone_popmoisture', 'zone_c_hum']
    window.settings.update(visible_transmittance=.61, operable=.37, shgc=.42,
                           numeric_string='001', flags=np.array([True, False]))
    window.explicit_settings = {'visible_transmittance'}
    owners = list(window.space)
    outer = window.isOuter
    target = tmp_path / ('solver-model' + suffix)
    model.save(target)
    restored = MoosasModel.load(target)
    result = next(w for w in restored.glazingList if w.Uid == window.Uid)
    restored_space = next(s for s in restored.spaceList if s.id == space.id)
    assert restored.energy_settings == model.energy_settings
    assert restored.schedule == model.schedule
    assert restored_space.conditioned is False
    assert restored_space.settings['daylight_occupied_mask'] == [True] * 8760
    assert restored_space.settings['zone_popmoisture'] == 'person_moisture'
    assert restored_space.settings['zone_c_hum'] == .55
    assert sorted(restored_space.explicit_settings) == sorted(space.explicit_settings)
    assert result.settings['numeric_string'] == '001'
    assert result.settings['flags'] == [True, False]
    assert result.SHGC == .42 and result.operable == .37
    assert result.explicit_settings == {'visible_transmittance'}
    assert result.space == owners and result.isOuter == outer


@pytest.mark.parametrize('suffix', ['.ttl', '.xml', '.json'])
def test_skylight_settings_reach_properties_and_airflow(tmp_path, suffix):
    from MoosasPy.simulation.airflow.network import AfnPath

    model = _single_room_model(tmp_path)
    space = model.spaceList[0]
    geometry = MoosasGeometry(
        shapely.Polygon([(1, 1, 3), (2, 1, 3), (2, 2, 3), (1, 2, 3)]),
        'probe-skylight-geometry', [0, 0, 1], 5, errors='raise',
    )
    model.geometryList.append(geometry)
    model.geoId.append(geometry.faceId)
    skylight = MoosasSkylight(model, geometry, uid='probe-skylight', space=[space.id])
    space.getAllFaces(to_dict=True)['MoosasCeiling'][0].add_glazing(skylight)
    model.skylightList.append(skylight)
    skylight.settings.update(shgc=.42, operable=.37, visible_transmittance=.61,
                             airflow_element={'cd': .61})
    skylight.explicit_settings = {'visible_transmittance'}
    assert skylight.SHGC == .42 and skylight.operable == .37
    target = tmp_path / ('skylight-model' + suffix)
    model.save(target)
    restored = MoosasModel.load(target)
    result, = restored.skylightList
    assert result.SHGC == .42 and result.operable == .37
    assert result.explicit_settings == {'visible_transmittance'}
    path = AfnPath.fromElement(result)
    assert path.element['operable'] == .37 and path.element['shgc'] == .42
    assert path.element['cd'] == .61


@pytest.mark.parametrize("ground_method", ["GroundFCfactorMethod", "Ground", "Foundation"])
def test_idf_export_matches_template_ground_and_prunes_unused_materials(tmp_path, ground_method):
    model = _single_room_model(tmp_path)
    geometry_before = [list(g.face.exterior.coords) for g in model.geometryList]
    configure_idd()
    template = IDF(str(bundled_template_idf_path()))
    zone_name = template.idfobjects["ZONE"][0].Name
    for surface in template.idfobjects["BUILDINGSURFACE:DETAILED"]:
        if surface.Zone_Name == zone_name and surface.Surface_Type.lower() == "floor":
            surface.Outside_Boundary_Condition = "Outdoors"
    template.newidfobject(
        "Material", Name="test slab layer", Roughness="Rough", Thickness=0.2,
        Conductivity=1.5, Density=2000, Specific_Heat=1000,
    )
    template.newidfobject("Construction", Name="test slab", Outside_Layer="test slab layer")
    template.newidfobject("Construction", Name="unused test slab", Outside_Layer="unused test layer")
    template.newidfobject(
        "Material", Name="unused test layer", Roughness="Rough", Thickness=0.1,
        Conductivity=1, Density=1000, Specific_Heat=1000,
    )
    template.newidfobject(
        "Construction:FfactorGroundFloor", Name="test foundation",
        FFactor=1.26, Area=1000, PerimeterExposed=300,
    )
    template.newidfobject(
        "BuildingSurface:Detailed", Name="template ground floor", Zone_Name=zone_name,
        Surface_Type="Floor", Outside_Boundary_Condition=ground_method,
        Construction_Name="test foundation" if ground_method == "GroundFCfactorMethod" else "test slab",
    )
    temperature_key = ("Site:GroundTemperature:FCfactorMethod" if ground_method == "GroundFCfactorMethod"
                       else "Site:GroundTemperature:BuildingSurface")
    template.idfobjects[temperature_key] = []
    temperatures = template.newidfobject(temperature_key)
    for index, month in enumerate((
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )):
        temperatures[f"{month}_Ground_Temperature"] = 10 + index
    expected_temperatures = list(temperatures.obj)
    template_path = tmp_path / "ground-template.idf"
    output_path = tmp_path / "ground-export.idf"
    template.saveas(str(template_path))
    kwargs = dict(idfTemplatePath=str(template_path),
                  zoneNameToSpaceDict={zone_name: [str(model.spaceList[0].id)]})
    if ground_method == "Foundation":
        with pytest.raises(ValueError, match="Unsupported template ground method"):
            exportIDF(model, str(output_path), **kwargs)
        assert not output_path.exists()
        return

    exportIDF(model, str(output_path), **kwargs)

    exported = IDF(str(output_path))
    floor = next(s for s in exported.idfobjects["BUILDINGSURFACE:DETAILED"]
                 if s.Surface_Type.lower() == "floor")
    assert floor.Outside_Boundary_Condition == ground_method
    assert list(exported.idfobjects[temperature_key][0].obj) == expected_temperatures
    if ground_method == "GroundFCfactorMethod":
        foundation, = exported.idfobjects["CONSTRUCTION:FFACTORGROUNDFLOOR"]
        assert floor.Construction_Name == foundation.Name
        assert (foundation.FFactor, foundation.Area, foundation.PerimeterExposed) == pytest.approx((1.26, 16, 16))
    else:
        assert floor.Construction_Name == "test slab"
        assert any(m.Name == "test slab layer" for m in exported.idfobjects["MATERIAL"])
        assert not exported.idfobjects["CONSTRUCTION:FFACTORGROUNDFLOOR"]
    assert not any(c.Name == "unused test slab" for c in exported.idfobjects["CONSTRUCTION"])
    assert not any(m.Name == "unused test layer" for m in exported.idfobjects["MATERIAL"])
    assert [list(g.face.exterior.coords) for g in model.geometryList] == geometry_before


@pytest.mark.parametrize("suffix", (".xml", ".json", pytest.param(".idf", marks=pytest.mark.slow)))
def test_single_room_formats_round_trip(tmp_path, suffix):
    model = _single_room_model(tmp_path)
    target = tmp_path / f"single{suffix}"
    model.save(target)
    restored = MoosasModel.load(target)
    assert len(restored.spaceList) == 1
    assert len(restored.glazingList) == 1
    assert restored.spaceList[0].area == pytest.approx(16)
    # The 0.1 m inset window survives IDF export at full precision.
    expected_area = 0.8 ** 2 if suffix == ".idf" else 1.0
    assert restored.glazingList[0].area == pytest.approx(expected_area)


@pytest.mark.parametrize("suffix", (".rdf", ".xml", ".json"))
def test_semantic_formats_preserve_shading_and_void_area(
    semantic_model: MoosasModel,
    suffix: str,
):
    source_geometry = semantic_model.geometryList[0]
    shading_geometry = MoosasGeometry(
        source_geometry.face,
        "roundtrip-shading-geometry",
        source_geometry.normal,
        -1,
    )
    original_shading = semantic_model.shadingList
    semantic_model.geometryList.append(shading_geometry)
    semantic_model.geoId.append(shading_geometry.faceId)
    shading = MoosasElement(semantic_model, shading_geometry, uid="roundtrip-shading")
    semantic_model.shadingList = [shading]
    try:
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / f"model{suffix}"
            semantic_model.save(file_path)
            restored = MoosasModel.load(file_path)
    finally:
        semantic_model.shadingList = original_shading
        semantic_model.geometryList.pop()
        semantic_model.geoId.pop()

    assert [(item.Uid, list(item.faceId)) for item in restored.shadingList] == [
        (shading.Uid, list(shading.faceId))
    ]
    assert sum(space.area for space in restored.spaceList) == pytest.approx(
        sum(space.area for space in semantic_model.spaceList)
    )


def test_rdf_round_trip_loads_spaces_in_stable_uri_order(semantic_model: MoosasModel):
    with TemporaryDirectory() as directory:
        file_path = Path(directory) / "model.rdf"
        semantic_model.save(file_path)
        restored = MoosasModel.load(file_path)

    restored_ids = [space.id for space in restored.spaceList]
    assert restored_ids == sorted(restored_ids, key=lambda value: f"Space_{value}")


def test_rdf_round_trip_preserves_air_boundaries_as_walls(semantic_model: MoosasModel):
    source_air_walls = [wall for wall in semantic_model.wallList if wall.is_air_boundary]
    assert source_air_walls

    with TemporaryDirectory() as directory:
        file_path = Path(directory) / "air-boundaries.rdf"
        semantic_model.save(file_path)
        restored = MoosasModel.load(file_path)

    restored_air_walls = [wall for wall in restored.wallList if wall.is_air_boundary]
    assert len(restored_air_walls) == len(source_air_walls)
    assert all(glazing.category != 2 for glazing in restored.glazingList)


@pytest.mark.slow
def test_idf_air_boundaries_use_native_simple_mixing(semantic_model: MoosasModel):
    with TemporaryDirectory() as directory:
        file_path = Path(directory) / "air-boundaries.idf"
        semantic_model.save(file_path)
        configure_idd()
        idf = IDF(str(file_path))

    air_boundaries = list(idf.idfobjects["CONSTRUCTION:AIRBOUNDARY"])
    air_boundary = air_boundaries[0]
    air_surfaces = [
        surface
        for surface in idf.idfobjects["BUILDINGSURFACE:DETAILED"]
        if surface.Construction_Name == "Moosas Air Boundary"
    ]

    assert len(air_boundaries) == 1
    assert air_boundary.Name == "Moosas Air Boundary"
    assert air_boundary.Air_Exchange_Method == "SimpleMixing"
    assert air_boundary.Simple_Mixing_Air_Changes_per_Hour == pytest.approx(0.5)
    assert air_boundary.Simple_Mixing_Schedule_Name == ""
    assert air_surfaces
    assert len(idf.idfobjects["ZONEMIXING"]) == 0


def test_idf_face_encoding_removes_unused_vertex_fields():
    settings = MoosasSettings(FaceDefault)
    triangle = shapely.polygons(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)))

    encodeFace(settings, triangle, Vector(0.0, 0.0, 1.0))

    assert settings.params["Number_of_Vertices"] == 3
    assert "Vertex_4_Xcoordinate" not in settings.params
    assert "Vertex_4_Ycoordinate" not in settings.params
    assert "Vertex_4_Zcoordinate" not in settings.params


def test_idf_face_encoding_preserves_millimetre_coordinates():
    configure_idd()
    idf = IDF(StringIO("Version,26.1;"))
    settings = MoosasSettings(FaceDefault)
    points = np.array([
        (0.003, -0.007, 3.123),
        (1.003, -0.007, 3.123),
        (0.003, 1.007, 3.123),
    ])

    encodeFace(settings, shapely.Polygon(points), Vector(0, 0, 1))
    settings.applyToIDF(idf)
    reloaded = IDF(StringIO(idf.idfstr()))
    surface = reloaded.idfobjects["BUILDINGSURFACE:DETAILED"][0]
    actual = np.array([
        [float(surface[f"Vertex_{i}_{axis}coordinate"]) for axis in "XYZ"]
        for i in range(1, 4)
    ])

    assert actual == pytest.approx(points, abs=1e-12)


def _idf_vertex_normal(surface):
    points = np.array([
        [float(surface[f"Vertex_{index}_{axis}coordinate"]) for axis in "XYZ"]
        for index in range(1, int(surface["Number_of_Vertices"]) + 1)
    ])
    points -= points[0]
    normal = np.cross(points, np.roll(points, -1, axis=0)).sum(axis=0)
    return normal / np.linalg.norm(normal)


@pytest.mark.parametrize("axis_x,axis_y", [
    ((1, 0, 0), (0, 1, 0)),
    ((1, 0, 0), (0, -1, 0)),
    ((1, 0, 0), (0, 0, 1)),
    ((0, 1, 0), (0, 0, 1)),
    ((1, 0, 0), (0, 0.8, 0.6)),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_idf_face_encoding_uses_requested_right_hand_normal(axis_x, axis_y, reverse):
    points = np.array([
        x * np.array(axis_x) + y * np.array(axis_y)
        for x, y in [(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)]
    ])
    normal = np.cross(axis_x, axis_y)
    settings = MoosasSettings(FaceDefault)

    encodeFace(settings, shapely.Polygon(points[::-1] if reverse else points), Vector(normal))

    assert _idf_vertex_normal(settings.params) == pytest.approx(normal)


@pytest.mark.parametrize("surface_type", ["Floor", "Ceiling", "Roof"])
@pytest.mark.parametrize("slope", [0.0, 0.5])
def test_idf_horizontal_and_sloped_faces_have_outward_normals(surface_type, slope):
    configure_idd()
    idf = IDF(StringIO("Version,26.1;"))
    polygon = shapely.Polygon([(0, 0, 3), (4, 0, 3),
                               (4, 4, 3 + 4 * slope), (0, 4, 3 + 4 * slope)])
    window_polygon = shapely.Polygon([
        (1, 1, 3 + slope), (3, 1, 3 + slope),
        (3, 3, 3 + 3 * slope), (1, 3, 3 + 3 * slope),
    ])
    glazing = SimpleNamespace(Uid="skylight", representation=lambda: window_polygon)
    space = SimpleNamespace(
        is_void=lambda: False,
        ceiling=SimpleNamespace(face=[]),
    )
    model = SimpleNamespace(spaceIdDict={"room": space}, levelList=[0.0, 3.0])
    element = SimpleNamespace(
        parent=model, space=["room"], Uid="face", isOuter=True, level=3.0,
        representation=lambda: polygon, glazingElement=[glazing],
    )
    if surface_type != "Floor":
        space.ceiling.face.append(element)

    createThermalSurface(idf, element, surface_type)
    surfaces = idf.idfobjects["BUILDINGSURFACE:DETAILED"]

    expected = np.array([0.0, -slope, 1.0])
    expected /= np.linalg.norm(expected)
    if surface_type == "Floor":
        expected = -expected
    assert _idf_vertex_normal(surfaces[0]) == pytest.approx(expected)
    windows = idf.idfobjects["FENESTRATIONSURFACE:DETAILED"]
    assert windows
    for window in windows:
        assert np.dot(_idf_vertex_normal(window), expected) > 0.99999


def test_idf_shared_floor_ceiling_normals_are_opposite():
    configure_idd()
    idf = IDF(StringIO("Version,26.1;"))
    polygon = shapely.Polygon([(0, 0, 3), (4, 0, 3), (4, 4, 3), (0, 4, 3)])
    lower = SimpleNamespace(is_void=lambda: False, ceiling=SimpleNamespace(face=[]))
    upper = SimpleNamespace(is_void=lambda: False, ceiling=SimpleNamespace(face=[]))
    model = SimpleNamespace(spaceIdDict={"lower": lower, "upper": upper})
    element = SimpleNamespace(
        parent=model, space=["lower", "upper"], Uid="shared", isOuter=False,
        representation=lambda: polygon, glazingElement=[],
    )
    lower.ceiling.face.append(element)

    createThermalSurface(idf, element, "Ceiling", encodeWindow=False)
    ceiling, floor = idf.idfobjects["BUILDINGSURFACE:DETAILED"]

    assert ceiling.Surface_Type == "Ceiling"
    assert floor.Surface_Type == "Floor"
    assert _idf_vertex_normal(ceiling) == pytest.approx((0, 0, 1))
    assert _idf_vertex_normal(floor) == pytest.approx((0, 0, -1))
    assert ceiling.Outside_Boundary_Condition_Object == floor.Name
    assert floor.Outside_Boundary_Condition_Object == ceiling.Name


@pytest.mark.parametrize("reverse", [False, True])
def test_idf_interzone_wall_and_windows_have_opposite_outward_normals(reverse):
    configure_idd()
    idf = IDF(StringIO("Version,26.1;"))
    wall_points = [(0, 0, 0), (4, 0, 0), (4, 0, 3), (0, 0, 3)]
    window_points = [(1, 0, 1), (3, 0, 1), (3, 0, 2), (1, 0, 2)]
    wall_polygon = shapely.Polygon(wall_points[::-1] if reverse else wall_points)
    window_polygon = shapely.Polygon(window_points[::-1] if reverse else window_points)
    glazing = SimpleNamespace(Uid="window", representation=lambda: window_polygon)
    edge = SimpleNamespace(wall=[], FactorOfWall=[Vector(0, -1, 0)])
    south = SimpleNamespace(is_void=lambda: False, edge=edge)
    north = SimpleNamespace(is_void=lambda: False)
    model = SimpleNamespace(spaceIdDict={"south": south, "north": north})
    wall = SimpleNamespace(
        parent=model, space=["south", "north"], Uid="wall", isOuter=False,
        representation=lambda: wall_polygon, glazingElement=[glazing],
    )
    edge.wall.append(wall)

    createThermalSurface(idf, wall, "Wall")

    first, second = idf.idfobjects["BUILDINGSURFACE:DETAILED"]
    assert _idf_vertex_normal(first) == pytest.approx((0, -1, 0))
    assert _idf_vertex_normal(second) == pytest.approx((0, 1, 0))
    windows = {window.Name: window for window in idf.idfobjects["FENESTRATIONSURFACE:DETAILED"]}
    assert windows
    for window in windows.values():
        host = first if window.Building_Surface_Name == first.Name else second
        assert _idf_vertex_normal(window) == pytest.approx(_idf_vertex_normal(host))
        paired_window = windows[window.Outside_Boundary_Condition_Object]
        assert paired_window.Outside_Boundary_Condition_Object == window.Name
        assert _idf_vertex_normal(paired_window) == pytest.approx(-_idf_vertex_normal(window))


@pytest.mark.parametrize("vertex_direction", ["Counterclockwise", "Clockwise", None])
def test_idf_export_surface_and_window_winding(tmp_path, vertex_direction):
    model = _single_room_model(tmp_path)
    configure_idd()
    template = IDF(str(bundled_template_idf_path()))
    if vertex_direction is None:
        template.idfobjects["GLOBALGEOMETRYRULES"] = []
    else:
        template.idfobjects["GLOBALGEOMETRYRULES"][0].Vertex_Entry_Direction = vertex_direction
    template_path = tmp_path / "template.idf"
    template.saveas(str(template_path))
    output_path = tmp_path / "oriented.idf"

    exportIDF(model, str(output_path), idfTemplatePath=str(template_path))

    exported = IDF(str(output_path))
    assert exported.idfobjects["GLOBALGEOMETRYRULES"][0].Vertex_Entry_Direction == "Counterclockwise"
    surfaces = {surface.Name: surface for surface in exported.idfobjects["BUILDINGSURFACE:DETAILED"]}
    assert surfaces
    for surface in surfaces.values():
        normal = _idf_vertex_normal(surface)
        if surface.Surface_Type == "Floor":
            assert normal[2] < 0
        elif surface.Surface_Type in {"Ceiling", "Roof"}:
            assert normal[2] > 0
        else:
            space = model.spaceIdDict[surface.Zone_Name]
            element = next(wall for wall in space.edge.wall if surface.Name == f"{space.id}-{wall.Uid}")
            start, end = shapely.get_coordinates(element.force_2d())
            midpoint = (start + end) / 2
            footprint = space.force_2d()
            assert not shapely.contains(footprint, shapely.Point(midpoint + normal[:2] * 0.05))
            assert shapely.contains(footprint, shapely.Point(midpoint - normal[:2] * 0.05))
        if surface.Outside_Boundary_Condition == "Surface":
            neighbor = surfaces[surface.Outside_Boundary_Condition_Object]
            assert _idf_vertex_normal(neighbor) == pytest.approx(-normal)
    windows = exported.idfobjects["FENESTRATIONSURFACE:DETAILED"]
    assert windows
    for window in windows:
        assert _idf_vertex_normal(window) == pytest.approx(
            _idf_vertex_normal(surfaces[window.Building_Surface_Name])
        )


@pytest.mark.parametrize("method,hvac_mode", [
    ("Flow/Person", "ideal_loads"),
    ("Flow/Area", "ideal_loads"),
    ("Flow/Zone", "ideal_loads"),
    ("AirChanges/Hour", "ideal_loads"),
    ("Sum", "ideal_loads"),
    ("Maximum", "ideal_loads"),
    ("Sum", "simple"),
])
def test_idf_template_outdoor_air_keeps_method_and_all_flow_components(semantic_model, method, hvac_mode):
    configure_idd()
    idf = IDF(str(bundled_template_idf_path()))
    template = ZoneTemplate.fromIDF(idf, zoneName="Room_4", hvac_mode=hvac_mode)
    expected = {
        "Outdoor_Air_Method": method,
        "Outdoor_Air_Flow_per_Person": 0.0169901079552,
        "Outdoor_Air_Flow_per_Zone_Floor_Area": 0.0003,
        "Outdoor_Air_Flow_per_Zone": 0.02,
        "Outdoor_Air_Flow_Air_Changes_per_Hour": 0.5,
    }
    template.objectList["DesignSpecification:OutdoorAir"] = MoosasSettings(
        {"key": "DesignSpecification:OutdoorAir", "Name": "template outdoor air", **expected}
    )
    zone = copy.deepcopy(semantic_model.spaceList[0])
    applied = template.appliedToZone(zone)
    applied.applyToIDF(idf)
    exported = next(o for o in idf.idfobjects["DESIGNSPECIFICATION:OUTDOORAIR"] if o.Name == zone.id)

    for field, value in expected.items():
        assert exported[field] == value
    assert zone.settings["zone_pfav"] == pytest.approx(expected["Outdoor_Air_Flow_per_Person"] * 3600)


def test_idf_export_template_person_outdoor_air_is_not_overwritten_by_zero_ach(tmp_path):
    model = _single_room_model(tmp_path)
    configure_idd()
    template = IDF(str(bundled_template_idf_path()))
    zone_name = template.idfobjects["ZONE"][0].Name
    template.idfobjects["DESIGNSPECIFICATION:OUTDOORAIR"] = []
    template.newidfobject(
        "DesignSpecification:OutdoorAir", Name="regression outdoor air",
        Outdoor_Air_Method="Sum", Outdoor_Air_Flow_per_Person=0.0169901079552,
        Outdoor_Air_Flow_per_Zone_Floor_Area=0, Outdoor_Air_Flow_per_Zone=0,
        Outdoor_Air_Flow_Air_Changes_per_Hour=0,
    )
    for sizing in template.idfobjects["SIZING:ZONE"]:
        sizing.Design_Specification_Outdoor_Air_Object_Name = "regression outdoor air"
    template_path = tmp_path / "outdoor-air-template.idf"
    output_path = tmp_path / "outdoor-air-export.idf"
    template.saveas(str(template_path))

    exportIDF(model, str(output_path), idfTemplatePath=str(template_path),
              zoneNameToSpaceDict={zone_name: [str(model.spaceList[0].id)]})

    exported = IDF(str(output_path))
    outdoor_air, = exported.idfobjects["DESIGNSPECIFICATION:OUTDOORAIR"]
    assert outdoor_air.Outdoor_Air_Method == "Sum"
    assert outdoor_air.Outdoor_Air_Flow_per_Person == pytest.approx(0.0169901079552)
    assert outdoor_air.Outdoor_Air_Flow_Air_Changes_per_Hour == 0
    system, = exported.idfobjects["ZONEHVAC:IDEALLOADSAIRSYSTEM"]
    assert system.Design_Specification_Outdoor_Air_Object_Name == outdoor_air.Name


def test_new_idf_constructions_have_unique_deterministic_names():
    configure_idd()
    template_idf = IDF(str(bundled_template_idf_path()))
    zone_template = ZoneTemplate.fromIDF(template_idf, zoneName="Room_4")

    template_construction = next(
        item for item in zone_template.constructionList if item.type == "opaque"
    )
    assert zone_template.getConstruction(
        "opaque",
        template_construction.UFactor,
    ) is template_construction

    first = zone_template.getConstruction("opaque", 0.731)
    second = zone_template.getConstruction("opaque", 0.847)
    repeated = zone_template.getConstruction("opaque", 0.731)

    assert first is repeated
    assert first.params["Name"] != second.params["Name"]
    assert first.layers[0].params["Name"] != second.layers[0].params["Name"]
    assert len({
        str(obj["Name"]).strip()
        for obj in template_idf.idfobjects["CONSTRUCTION"]
        if str(obj["Name"]).strip()
    }) == len(template_idf.idfobjects["CONSTRUCTION"])
    assert len({
        str(obj["Name"]).strip()
        for obj in template_idf.idfobjects["MATERIAL"]
        if str(obj["Name"]).strip()
    }) == len(template_idf.idfobjects["MATERIAL"])


@pytest.mark.slow
def test_idf_export_clones_simple_baseboard_hvac_per_zone(semantic_model: MoosasModel):
    configure_idd()
    with TemporaryDirectory() as directory:
        directory = Path(directory)
        template_path = directory / "baseboard-template.idf"
        output_path = directory / "baseboard-export.idf"
        template = IDF(str(bundled_template_idf_path()))
        connection = template.idfobjects["ZONEHVAC:EQUIPMENTCONNECTIONS"][0]
        zone_name = str(connection.Zone_Name)
        equipment_list = next(
            item for item in template.idfobjects["ZONEHVAC:EQUIPMENTLIST"]
            if str(item.Name) == str(connection.Zone_Conditioning_Equipment_List_Name)
        )
        equipment_list.Zone_Equipment_1_Object_Type = "ZoneHVAC:Baseboard:Convective:Electric"
        equipment_list.Zone_Equipment_1_Name = "template electric baseboard"
        template.newidfobject(
            "ZONEHVAC:BASEBOARD:CONVECTIVE:ELECTRIC",
            Name="template electric baseboard",
            Availability_Schedule_Name="Always On",
            Heating_Design_Capacity_Method="HeatingDesignCapacity",
            Heating_Design_Capacity=1000.0,
            Efficiency=1.0,
        )
        template.saveas(str(template_path))

        exportIDF(
            semantic_model,
            str(output_path),
            idfTemplatePath=str(template_path),
            zoneNameToSpaceDict={zone_name: [str(space.id) for space in semantic_model.spaceList]},
            hvac_mode="simple",
        )
        exported = IDF(str(output_path))

    equipment_lists = exported.idfobjects["ZONEHVAC:EQUIPMENTLIST"]
    baseboards = exported.idfobjects["ZONEHVAC:BASEBOARD:CONVECTIVE:ELECTRIC"]
    baseboards_by_name = {str(baseboard.Name): baseboard for baseboard in baseboards}

    assert len(equipment_lists) == len(semantic_model.spaceList)
    assert len(baseboards_by_name) == len(semantic_model.spaceList)
    assert not exported.idfobjects["SURFACEPROPERTY:INCIDENTSOLARMULTIPLIER"]
    assert all(
        equipment_list.Zone_Equipment_1_Object_Type == "ZoneHVAC:Baseboard:Convective:Electric"
        and str(equipment_list.Zone_Equipment_1_Name) in baseboards_by_name
        for equipment_list in equipment_lists
    )


def test_idf_export_uses_ideal_loads_for_complex_office_template(semantic_model: MoosasModel):
    template_path = PROJECT_ROOT / "temp" / "90032007981091.idf"
    if not template_path.is_file():
        pytest.skip("office template attachment is not present")

    with TemporaryDirectory() as directory:
        output_path = Path(directory) / "office-ideal-loads.idf"
        exportIDF(
            semantic_model,
            str(output_path),
            idfTemplatePath=str(template_path),
            zoneNameToSpaceDict={"Space 0 ZN": [str(space.id) for space in semantic_model.spaceList]},
        )
        configure_idd()
        exported = IDF(str(output_path))

    assert not exported.idfobjects["AIRLOOPHVAC"]
    assert not exported.idfobjects["PLANTLOOP"]
    assert not exported.idfobjects["ZONEHVAC:AIRDISTRIBUTIONUNIT"]
    ideal_loads = {
        str(equipment.Name): equipment
        for equipment in exported.idfobjects["ZONEHVAC:IDEALLOADSAIRSYSTEM"]
    }
    assert len(ideal_loads) == len(semantic_model.spaceList)
    assert all(
        equipment_list.Zone_Equipment_1_Object_Type == "ZoneHVAC:IdealLoadsAirSystem"
        and str(equipment_list.Zone_Equipment_1_Name) in ideal_loads
        for equipment_list in exported.idfobjects["ZONEHVAC:EQUIPMENTLIST"]
    )


@pytest.mark.slow
def test_idf_export_removes_template_zone_groups_and_system_sizing(semantic_model: MoosasModel):
    configure_idd()
    with TemporaryDirectory() as directory:
        directory = Path(directory)
        template_path = directory / "stale-template.idf"
        output_path = directory / "stale-export.idf"
        template = IDF(str(bundled_template_idf_path()))
        zone_name = str(template.idfobjects["ZONE"][0].Name)
        template.newidfobject(
            "ZONELIST",
            Name="stale zone group",
            Zone_1_Name=zone_name,
        )
        template.newidfobject(
            "SIZING:SYSTEM",
            AirLoop_Name="stale air loop",
        )
        template.saveas(str(template_path))

        exportIDF(
            semantic_model,
            str(output_path),
            idfTemplatePath=str(template_path),
            zoneNameToSpaceDict={
                zone_name: [str(space.id) for space in semantic_model.spaceList]
            },
        )
        exported = IDF(str(output_path))

    assert not exported.idfobjects["ZONELIST"]
    assert not exported.idfobjects["SIZING:SYSTEM"]


@pytest.mark.slow
def test_idf_export_preserves_shading_geometry(semantic_model: MoosasModel):
    source_geometry = semantic_model.geometryList[0]
    shading_geometry = MoosasGeometry(
        source_geometry.face,
        "idf-shading-geometry",
        source_geometry.normal,
        -1,
    )
    shading = MoosasElement(semantic_model, shading_geometry, uid="idf-shading")
    original_shading = semantic_model.shadingList
    semantic_model.geometryList.append(shading_geometry)
    semantic_model.geoId.append(shading_geometry.faceId)
    semantic_model.shadingList = [shading]
    try:
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / "shading.idf"
            semantic_model.save(file_path)
            configure_idd()
            idf = IDF(str(file_path))
    finally:
        semantic_model.shadingList = original_shading
        semantic_model.geometryList.pop()
        semantic_model.geoId.pop()

    exported = idf.idfobjects["SHADING:BUILDING:DETAILED"]
    expected_vertices = [
        tuple(point)
        for point in source_geometry.face.exterior.coords[:-1]
    ]
    actual_vertices = [
        (
            float(exported[0][f"Vertex_{index}_Xcoordinate"]),
            float(exported[0][f"Vertex_{index}_Ycoordinate"]),
            float(exported[0][f"Vertex_{index}_Zcoordinate"]),
        )
        for index in range(1, int(exported[0].Number_of_Vertices) + 1)
    ]

    assert [item.Name for item in exported] == ["idf-shading"]
    assert len(actual_vertices) == len(expected_vertices)
    for actual, expected in zip(sorted(actual_vertices), sorted(expected_vertices)):
        assert actual == pytest.approx(expected)
    expected_normal = Vector(source_geometry.normal).unit().array
    assert _idf_vertex_normal(exported[0]) == pytest.approx(expected_normal)


@pytest.mark.slow
def test_idf_import_serializes_detailed_shading_geometry(semantic_model: MoosasModel):
    base_vertices = (
        (0.0, 0.0),
        (2.0, 0.0),
        (2.0, 2.0),
        (0.0, 2.0),
    )
    expected_vertices = []
    with TemporaryDirectory() as directory:
        file_path = Path(directory) / "shading-source.idf"
        semantic_model.save(file_path)
        configure_idd()
        idf = IDF(str(file_path))
        base_surface_name = idf.idfobjects["BUILDINGSURFACE:DETAILED"][0].Name
        for object_index, object_type in enumerate((
            "Shading:Site:Detailed",
            "Shading:Building:Detailed",
            "Shading:Zone:Detailed",
        )):
            vertices = tuple(
                (x, y, 5.0 + object_index)
                for x, y in base_vertices
            )
            expected_vertices.append(vertices)
            fields = {
                "Name": object_type,
                "Number_of_Vertices": len(vertices),
            }
            if object_type == "Shading:Zone:Detailed":
                fields["Base_Surface_Name"] = base_surface_name
            for index, (x, y, z) in enumerate(vertices, start=1):
                fields[f"Vertex_{index}_Xcoordinate"] = x
                fields[f"Vertex_{index}_Ycoordinate"] = y
                fields[f"Vertex_{index}_Zcoordinate"] = z
            idf.newidfobject(object_type, **fields)
        idf.save()

        records, faces, _, _ = _idf_build_artifacts(str(file_path))
        xml_path = Path(directory) / "shading-source.xml"
        IDFtoXml(str(file_path), str(xml_path))
        shading_nodes = ElementTree.parse(xml_path).getroot().findall("shading")

    shading_faces = [face for face in faces if face.cat == -1]
    assert len([record for record in records if record.family == "shading"]) == 3
    assert len(shading_faces) == 3
    assert {node.findtext("faceId") for node in shading_nodes} == {
        face.geo_id for face in shading_faces
    }
    assert {tuple(face.vertices) for face in shading_faces} == set(expected_vertices)


@pytest.mark.slow
def test_unconditioned_space_exports_only_zone_and_surfaces(semantic_model: MoosasModel):
    core = semantic_model.spaceList[0]
    original_conditioned = core.conditioned
    core.conditioned = False
    prohibited = (
        "ZONEINFILTRATION:DESIGNFLOWRATE",
        "ZONEVENTILATION:DESIGNFLOWRATE",
        "ZONEVENTILATION:WINDANDSTACKOPENAREA",
        "OTHEREQUIPMENT",
        "ELECTRICEQUIPMENT",
        "PEOPLE",
        "LIGHTS",
        "SIZING:ZONE",
        "DESIGNSPECIFICATION:OUTDOORAIR",
        "DESIGNSPECIFICATION:ZONEAIRDISTRIBUTION",
        "ZONECONTROL:THERMOSTAT",
        "THERMOSTATSETPOINT:DUALSETPOINT",
        "ZONEHVAC:EQUIPMENTCONNECTIONS",
        "ZONEHVAC:EQUIPMENTLIST",
        "ZONEHVAC:IDEALLOADSAIRSYSTEM",
        "NODELIST",
    )
    try:
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / "unconditioned-core.idf"
            semantic_model.save(file_path)
            configure_idd()
            idf = IDF(str(file_path))
    finally:
        core.conditioned = original_conditioned

    assert any(zone.Name == core.id for zone in idf.idfobjects["ZONE"])
    assert any(
        surface.Zone_Name == core.id
        for surface in idf.idfobjects["BUILDINGSURFACE:DETAILED"]
    )
    zone_names = {zone.Name for zone in idf.idfobjects["ZONE"]}
    assert all(
        sizing.Zone_or_ZoneList_Name in zone_names
        for sizing in idf.idfobjects["SIZING:ZONE"]
    )
    for object_type in prohibited:
        assert all(core.id not in map(str, obj.fieldvalues) for obj in idf.idfobjects[object_type])


@pytest.mark.slow
def test_all_unconditioned_spaces_remove_template_hvac(semantic_model: MoosasModel):
    original = [space.conditioned for space in semantic_model.spaceList]
    prohibited = (
        "SIZING:ZONE",
        "ZONECONTROL:THERMOSTAT",
        "ZONEHVAC:EQUIPMENTCONNECTIONS",
        "ZONEHVAC:EQUIPMENTLIST",
        "ZONEHVAC:IDEALLOADSAIRSYSTEM",
        "NODELIST",
    )
    try:
        for space in semantic_model.spaceList:
            space.conditioned = False
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / "all-unconditioned.idf"
            semantic_model.save(file_path)
            configure_idd()
            idf = IDF(str(file_path))
    finally:
        for space, conditioned in zip(semantic_model.spaceList, original):
            space.conditioned = conditioned

    assert len(idf.idfobjects["ZONE"]) == len(semantic_model.spaceList)
    assert all(not idf.idfobjects[object_type] for object_type in prohibited)


@pytest.mark.parametrize("suffix", (".graph.json", ".gbxml"))
def test_save_only_model_projections(semantic_model: MoosasModel, suffix: str):
    with TemporaryDirectory() as directory:
        file_path = Path(directory) / f"model{suffix}"
        result = semantic_model.save(file_path)

        assert result.generated_paths == (file_path,)
        assert file_path.is_file()
