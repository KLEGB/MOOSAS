"""Preserve gbXML exchange information that BOT geometry alone cannot express.

The RDF contains the document envelope, per-entity XML extensions and queryable
construction/material properties. Export always overlays live RDF geometry and
properties; it does not read the original file or a sidecar.
"""
from __future__ import annotations

from copy import deepcopy
import math
import xml.etree.ElementTree as ET

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

EX = Namespace('https://moosas#gbxml/')
DOC = URIRef('urn:moosas:gbxml:document')
NS = 'http://www.gbxml.org/schema'
MO = Namespace('https://moosas#')
BOT = Namespace('https://w3id.org/bot#')
BES = Namespace('http://www.hkust.edu.hk/zhaojiwu/performance_based_generative_design#')
SCALARS = {'Thickness': EX.thickness, 'Conductivity': EX.conductivity,
           'Density': EX.density, 'SpecificHeat': EX.specificHeat, 'U-value': EX.uValue}


def q(tag):
    return f'{{{NS}}}{tag}'


def capture_exchange(root, graph):
    """Capture non-BOT data and ordered ID/IDREF relations in the same graph."""
    graph.bind('gbx', EX)
    graph.add((DOC, RDF.type, EX.Document))
    skeleton = deepcopy(root)
    for node in list(skeleton.iter()):
        if node.tag in {q('Space'), q('Surface')}:
            node[:] = []
            node.text = None
    graph.add((DOC, EX.xmlSkeleton, Literal(ET.tostring(skeleton, encoding='unicode'))))
    lookup = {str(uid): subject for subject, uid in graph.subject_objects(MO.Uid)}
    for node in root.iter():
        if not node.get('id'):
            continue
        ident = node.get('id')
        tag = node.tag.rsplit('}', 1)[-1]
        subject = lookup.get(ident, URIRef(f'urn:moosas:gbxml:resource:{ident}'))
        graph.add((subject, EX.sourceId, Literal(ident)))
        graph.add((subject, EX.sourceTag, Literal(tag)))
        if tag in {'Space', 'Surface', 'Opening', 'Material', 'Layer', 'Construction', 'WindowType'}:
            graph.add((subject, EX.xml, Literal(ET.tostring(node, encoding='unicode'))))
        if tag in {'Material', 'Layer', 'Construction', 'WindowType'}:
            graph.add((subject, RDF.type, EX[tag]))
            name = node.findtext(q('Name'))
            if name:
                graph.add((subject, RDFS.label, Literal(name)))
            for child in node:
                pred = SCALARS.get(child.tag.rsplit('}', 1)[-1])
                if pred is not None and child.text:
                    graph.add((subject, pred, Literal(float(child.text), datatype=XSD.double)))
    ids = {str(ident): subject for subject, ident in graph.subject_objects(EX.sourceId)}
    for node in root.iter():
        if node.get('id') not in ids:
            continue
        subject = ids[node.get('id')]
        for attr, pred in [('constructionIdRef', EX.construction), ('windowTypeIdRef', EX.windowType)]:
            if node.get(attr):
                graph.add((subject, pred, ids[node.get(attr)]))
        # Preserve repeated layers/materials and their order via indexed nodes.
        for i, child in enumerate(node):
            ref = child.get('layerIdRef') or child.get('materialIdRef')
            if ref:
                link = URIRef(f'{subject}/component/{i}')
                graph.add((subject, EX.component, link))
                graph.add((link, EX.order, Literal(i, datatype=XSD.integer)))
                graph.add((link, EX.resource, ids[ref]))


def _set_text(node, tag, value):
    if value is None:
        return
    child = node.find(q(tag))
    if child is None:
        child = ET.SubElement(node, q(tag))
    text = str(value)
    # Keep the original lexical precision when the numeric value is unchanged.
    try:
        if child.text and float(child.text) == float(text):
            return
    except ValueError:
        pass
    child.text = text


def restore_exchange(graph):
    """Rebuild source-dialect gbXML and overlay graph changes; None for native RDF."""
    from . import _get_first_coords, _polyloop_coords, _add_polyloop, _collect_interface_links
    template = graph.value(DOC, EX.xmlSkeleton)
    if template is None:
        return None
    ET.register_namespace('', NS)
    root = ET.fromstring(str(template))
    from .energy import units
    length_scale, area_scale, volume_scale = units(root)
    subjects = {str(ident): s for s, ident in graph.subject_objects(EX.sourceId)}
    source_ids = {s: ident for ident, s in subjects.items()}
    links, _, _ = _collect_interface_links(graph)
    changed_geometry = False
    for parent in list(root.iter()):
        for index, placeholder in enumerate(list(parent)):
            if placeholder.tag not in {q('Space'), q('Surface')}:
                continue
            subject = subjects.get(placeholder.get('id'))
            typ = BOT.Space if placeholder.tag == q('Space') else BOT.Element
            if subject is None or (subject, RDF.type, typ) not in graph:
                parent.remove(placeholder)
                continue
            fragment = graph.value(subject, EX.xml)
            node = ET.fromstring(str(fragment)) if fragment is not None else deepcopy(placeholder)
            if node.tag == q('Space'):
                _set_text(node, 'Name', graph.value(subject, RDFS.label))
                for tag, predicate, scale in [('Area', BES.hasFloorArea_m2, area_scale), ('Volume', BES.hasVolume_m3, volume_scale)]:
                    value = graph.value(subject, predicate)
                    _set_text(node, tag, float(value)/scale if value is not None else None)
            else:
                for element, owner in [(node, subject)]+[(o, subjects.get(o.get('id'))) for o in node.findall(q('Opening'))]:
                    if owner is None:
                        continue
                    _set_text(element, 'Name', graph.value(owner, RDFS.label))
                    coords = _get_first_coords(graph, owner)
                    if coords:
                        coords = [tuple(v/length_scale for v in p) for p in coords]
                    if coords and coords != _polyloop_coords(element):
                        old = element.find(q('PlanarGeometry'))
                        position = list(element).index(old) if old is not None else len(element)
                        if old is not None:
                            element.remove(old)
                        _add_polyloop(element, coords)
                        planar = element.find(q('PlanarGeometry'))
                        element.remove(planar); element.insert(position, planar)
                        # Existing local rectangular coordinates cannot describe
                        # an edited arbitrary polygon. PlanarGeometry is authoritative.
                        rectangular = element.find(q('RectangularGeometry'))
                        if rectangular is not None:
                            element.remove(rectangular)
                        changed_geometry = True
                    for pred, attr in [(EX.construction, 'constructionIdRef'), (EX.windowType, 'windowTypeIdRef')]:
                        ref = graph.value(owner, pred)
                        if ref is not None:
                            element.set(attr, source_ids[ref])
                actual = [source_ids[s] for s in links.get(subject, []) if s in source_ids]
                existing = [a.get('spaceIdRef') for a in node.findall(q('AdjacentSpaceId'))]
                if set(actual) != set(existing):
                    for adj in node.findall(q('AdjacentSpaceId')):
                        node.remove(adj)
                    for j, sid in enumerate(sorted(actual)):
                        node.insert(j, ET.Element(q('AdjacentSpaceId'), spaceIdRef=sid))
            parent.remove(placeholder); parent.insert(index, node)
    # Overlay material edits while retaining units, layer order, glazing/gap data.
    for node in root.iter():
        subject = subjects.get(node.get('id'))
        if subject is None or node.tag not in {q('Material'), q('Construction'), q('WindowType')}:
            continue
        _set_text(node, 'Name', graph.value(subject, RDFS.label))
        for tag, pred in SCALARS.items():
            value = graph.value(subject, pred)
            if value is not None:
                _set_text(node, tag, value)
    if changed_geometry:
        # Rebuild the optional shell geometry from current adjacency, not stale
        # source polygons, after a semantic geometry edit.
        by_space = {s.get('id'): [] for s in root.iter(q('Space'))}
        for surface in root.iter(q('Surface')):
            poly = surface.find(f'{q("PlanarGeometry")}/{q("PolyLoop")}')
            if poly is None:
                continue
            for adj in surface.findall(q('AdjacentSpaceId')):
                by_space.setdefault(adj.get('spaceIdRef'), []).append(poly)
        for space in root.iter(q('Space')):
            shell = space.find(q('ShellGeometry'))
            if shell is None:
                continue
            closed = shell.find(q('ClosedShell'))
            if closed is not None:
                closed[:] = [deepcopy(p) for p in by_space.get(space.get('id'), [])]
    return ET.ElementTree(root)


def validate_references(root):
    """Reject broken IDs/IDREFs before producing an interchange file."""
    ids = [n.get('id') for n in root.iter() if n.get('id')]
    if len(ids) != len(set(ids)):
        raise ValueError('gbXML contains duplicate IDs')
    known = set(ids)
    for node in root.iter():
        for attr, value in node.attrib.items():
            if attr.endswith('IdRef') and value not in known:
                raise ValueError(f'gbXML unresolved {attr}={value}')
