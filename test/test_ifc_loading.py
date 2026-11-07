from types import SimpleNamespace

from MoosasPy.model.io import ifc as ifc_io


def _stub_ifcopenshell(monkeypatch, has_spaces):
    document = SimpleNamespace(
        by_type=lambda name: [object()] if name == "IfcSpace" and has_spaces else []
    )
    opened_paths = []

    def open_ifc(path):
        opened_paths.append(path)
        return document

    monkeypatch.setattr(ifc_io, "ifcopenshell", SimpleNamespace(open=open_ifc))
    return document, opened_paths


def test_load_ifc_prefers_direct_loader_when_spaces_exist(monkeypatch):
    document, opened_paths = _stub_ifcopenshell(monkeypatch, has_spaces=True)
    model = SimpleNamespace()
    calls = []

    monkeypatch.setattr(
        ifc_io,
        "_legacy_loadIfc_direct",
        lambda path, ifc: calls.append(("direct", path, ifc)) or model,
    )
    monkeypatch.setattr(
        ifc_io,
        "_loadIfc_via_geo_bridge",
        lambda path, ifc: calls.append(("bridge", path, ifc)),
    )

    result = ifc_io.loadIfc("sample.ifc")

    assert result is model
    assert opened_paths == ["sample.ifc"]
    assert calls == [("direct", "sample.ifc", document)]
    assert model.ifcHasSpaces is True
    assert model.ifcLoadMode == "space"


def test_load_ifc_prefers_geometry_bridge_without_spaces(monkeypatch):
    document, opened_paths = _stub_ifcopenshell(monkeypatch, has_spaces=False)
    model = SimpleNamespace()
    calls = []

    monkeypatch.setattr(
        ifc_io,
        "_legacy_loadIfc_direct",
        lambda path, ifc: calls.append(("direct", path, ifc)),
    )
    monkeypatch.setattr(
        ifc_io,
        "_loadIfc_via_geo_bridge",
        lambda path, ifc: calls.append(("bridge", path, ifc)) or model,
    )

    result = ifc_io.loadIfc("sample.ifc")

    assert result is model
    assert opened_paths == ["sample.ifc"]
    assert calls == [("bridge", "sample.ifc", document)]
    assert model.ifcHasSpaces is False
    assert model.ifcLoadMode == "geometry"


def test_load_ifc_fallback_reuses_open_document(monkeypatch):
    document, opened_paths = _stub_ifcopenshell(monkeypatch, has_spaces=True)
    model = SimpleNamespace()
    calls = []

    def fail_direct(path, ifc):
        calls.append(("direct", path, ifc))
        raise ValueError("direct loader failed")

    monkeypatch.setattr(ifc_io, "_legacy_loadIfc_direct", fail_direct)
    monkeypatch.setattr(
        ifc_io,
        "_loadIfc_via_geo_bridge",
        lambda path, ifc: calls.append(("bridge", path, ifc)) or model,
    )

    result = ifc_io.loadIfc("sample.ifc")

    assert result is model
    assert opened_paths == ["sample.ifc"]
    assert calls == [
        ("direct", "sample.ifc", document),
        ("bridge", "sample.ifc", document),
    ]
    assert model.ifcHasSpaces is True
    assert model.ifcLoadMode == "space"
