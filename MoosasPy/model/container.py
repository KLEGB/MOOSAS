"""Model-level geometry and space collection management."""
from __future__ import annotations

from .space import MoosasSpace
from .boundary import MoosasEdge, MoosasFloor
from ..transform.geometry.element import (
    MoosasElement,
    MoosasFace,
    MoosasGeometry,
    MoosasGlazing,
    MoosasSkylight,
    MoosasWall,
)
from ..transform.geometry.geos import Vector, faceNormal
from ..utils import mixItemListToList, np, shapely


class MoosasContainer(object):
    """Define all the global variables needed for Moosas+.

    This class does not have slots for the sake of flexible attributes.

    Attributes:
        geoId (str): Geometries' identification.
        geometryList (List): List of all geometries.
        faceList (List): List of MoosasFace objects.
        wallList (List): List of MoosasWall objects.
        skylightList (List): List of MoosasSkylight objects.
        glazingList (List): List of MoosasGlazing objects.
        levelList (List): List of all levels in the model.
        boundaryList (List): List of recognized boundaries.
        floorList (List): List of floors as MoosasFloor objects.
        edgeList (List): List of MoosasEdge objects.
        ceilingList (List): List of ceilings as MoosasFloor objects.
        spaceList (List): List of valid spaces as MoosasSpace objects.
        voidList (List): List of void spaces as MoosasSpace objects.
        weather (WeatherData): Hourly weather series and location metadata, default is None.
        builtData (Object): Data used to construct space manually.

    Properties:
        spaceIdDict (dict): A dictionary recording spaceId: MoosasSpace.

    Methods:
        fromDict(cls, spaceDict: dict) -> MoosasSpace: Create MoosasSpace from a dictionary.
        update(self) -> None: update self.builtData, which is used to record current elements and glazing when creating space manually.
        getAllFaces(self) -> List: Get all elements in the model.
        includeGeo(self, geo: shapely.Geometry, normal: shapely.Geometry | Vector | np.ndarray = None, cat: int = 0,
                   holes=None) -> str: Include a shapely.Geometry to the library.
        findFace(self, faceId) -> list[MoosasGeometry]: Find a geometry by its geoId.
    """

    def __init__(self):
        """
        Initialize the MoosasModel with default lists and assign appropriate types to these lists.
        
        Parameters
        ----------
        self : object
            The instance of the MoosasModel class being initialized. This method sets up all the 
            internal list attributes used to store geometric and structural components of the model.
        
        Returns
        -------
        None
            This method does not return any value.
        """
        """initialize the MoosasModel with default list, and apply type to these list"""
        self.geoId = []
        self.geometryList: list[MoosasGeometry] = []
        self.newIndex = 0

        # horizontalVerticalPlaneSet
        self.faceList: list[MoosasFace] = []
        self.wallList: list[MoosasWall] = []
        self.shadingList: list[MoosasElement] = []
        self.wall_remain: list[MoosasWall] = []
        self.face_remain: list[MoosasFace] = []

        # Identify the result set
        self.levelList: list[float] = []
        self.boundaryList = []

        # envelope floor space set
        self.floorList: list[MoosasFloor] = []
        self.ceilingList: list[MoosasFloor] = []
        self.glazingList: list[MoosasGlazing] = []
        self.skylightList: list[MoosasSkylight] = []
        self.edgeList: list[MoosasEdge] = []
        self.spaceList: list[MoosasSpace] = []
        self.voidList: list[MoosasSpace] = []

        # object used to construct a space
        self.builtData = object()

    @property
    def spaceIdDict(self) -> dict:
        """space id dictionary for all spaces in self.spaceList

        Returns:
            dict: {spaceId:MoosasSpace}
        """
        return {space.id: space for space in self.spaceList}

    def fromDict(self, spaceDict: dict) -> MoosasSpace:
        """construct a space from a dictionary
        the space will be added to self.spaceList automatically,
        and the space topology will be automatically recalculate.
        for more information please refer to MoosasSpace.fromDict()

        Args:
            spaceDict (dict): Dictionary to construct space from.

        Returns:
            MoosasSpace: created MoosasSpace object.
        """
        if not hasattr(self.builtData, 'elements') or not hasattr(self.builtData, 'glazing'):
            self.update()
        space = MoosasSpace.fromDict(spaceDict, self)
        for void in self.voidList:
            if shapely.contains(space.force_2d(), void.force_2d()):
                space.voidList.append(void)
        self.spaceList.append(space)
        return space

    def update(self) -> None:
        """
        Update the builtData attribute to reflect current elements and glazing.
        
        This method initializes the element and glazing dictionaries in builtData if they do not exist,
        then populates them with face and glazing data from the instance's glazingList, skylightList,
        and all faces obtained via getAllFaces.
        
        Parameters
        ----------
        self : object
            The instance of the class containing the method. It is expected to have the following attributes:
            - builtData: an object that will be updated with 'element' and 'glazing' dictionaries.
            - glazingList: a list of objects, each having a 'glazingId' attribute.
            - skylightList: a list of objects, each having a 'glazingId' attribute.
            - getAllFaces(): a method returning a collection of face objects, each with a 'faceId' attribute.
            - mixItemListToList: a function used to convert faceId items into a flat list.
        
        Returns
        -------
        None
        """
        """update self.builtData, which is used to record current elements and glazing when creating space manually.
        builtData.element is a dictionary: {MoosasElement.faceId:MoosasElement}
        builtData.glazing is a dictionary: {MoosasGlazing.faceId:MoosasElement}
        """
        if not hasattr(self.builtData, 'elements') or not hasattr(self.builtData, 'glazing'):
            self.builtData.element = {}
            self.builtData.glazing = {}
        for gls in self.glazingList:
            for glsId in gls.glazingId:
                self.builtData.glazing[glsId] = gls
        for gls in self.skylightList:
            for glsId in gls.glazingId:
                self.builtData.glazing[glsId] = gls
        for element in self.getAllFaces():
            for eleId in mixItemListToList(element.faceId):
                self.builtData.element[eleId] = element

    def getAllFaces(self, dumpUseless=False) -> list[MoosasElement] | dict:
        """get all MoosasElement in the model as a list
        the elements in the list will not change their type hence you can test which element it is by isinstance()

        Returns:
            list[MoosasElement]: all MoosasElement in the model
        """
        if not dumpUseless:
            faces = []
            for elementList in [self.wallList, self.faceList, self.glazingList, self.skylightList, self.shadingList]:
                faces = np.append(faces, elementList)
            return list(faces)
        else:
            mElements = {'MoosasFace': set(), 'MoosasSkylight': set(), 'MoosasWall': set(), 'MoosasGlazing': set()}
            for space in self.spaceList + self.voidList:
                elementDict = space.getAllFaces(to_dict=True)

                mElements['MoosasFace'] = mElements['MoosasFace'] | set(
                    elementDict['MoosasFloor'] + elementDict['MoosasCeiling'])
                mElements['MoosasWall'] = mElements['MoosasWall'] | set(
                    elementDict['MoosasWall'] + elementDict['InternalMass'])
                mElements['MoosasSkylight'] = mElements['MoosasSkylight'] | set(elementDict['MoosasSkylight'])
                mElements['MoosasGlazing'] = mElements['MoosasGlazing'] | set(elementDict['MoosasGlazing'])
            return mElements

    def includeGeo(self, geo: shapely.Geometry, normal: shapely.Geometry | Vector | np.ndarray = None, cat: int = 0,
                   holes=None) -> str:
        """Include a geometry into the geometry library.

        Args:
            geo (shapely.Geometry): The polygon to include.
            normal (shapely.Geometry, optional): The normal vector of the polygon. Defaults to None.
            cat (int, optional): Category of the geometry (opaque == 0, transparent == 1, aperture == 2). Defaults to 0.
            holes (List[shapely.Geometry], optional): The inner holes of the geometry. Defaults to None.

        Returns:
            str: GeoId of the geometry, can be used to construct faces.
        """
        if holes is None:
            holes = []
        if normal is None:
            normal = faceNormal(geo)
        rings = shapely.get_rings(geo)
        if len(rings) > 1:
            geo = shapely.polygons(rings[0])
            holes += [shapely.polygons(r) for r in rings[1:]]
        faceId = f"n{self.newIndex}"
        self.newIndex += 1

        self.geometryList.append(MoosasGeometry(geo, faceId, normal, cat, holes))
        self.geoId = list(np.append(self.geoId, [faceId]))
        return self.geoId[-1]

    def removeGeo(self, geo: MoosasGeometry | shapely.Geometry | str):
        """
        Remove a geometry from the internal geometry list.
        
        Parameters
        ----------
        geo : MoosasGeometry or shapely.Geometry or str
            The geometry to be removed. Can be a MoosasGeometry object, a shapely.Geometry object, 
            or a string representing the face ID of the geometry.
        
        Returns
        -------
        None
        """
        if isinstance(geo, shapely.Geometry):
            for geoItems in self.geometryList:
                if geoItems.face == geo:
                    geo = geoItems
        if isinstance(geo, str):
            geo = self.geometryList[self.geoId.index(geo)]
        if isinstance(geo, MoosasGeometry):
            self.geometryList.remove(geo)
            self.geoId.remove(geo.faceId)

    def findFace(self, faceId: str | list[str]) -> list[MoosasGeometry]:
        """find a geometry in the library
        it will test the validation of the identification automatically, and skip invalid geometry

        Args:
            faceId (str|list[str]): The id of the face in the geo file or library

        Returns:
            list[MoosasGeometry]: a list of MoosasGeometry object of the face
        """
        if isinstance(faceId, str):
            faceId = [faceId]
        _faceId = []
        for idd in faceId:
            try:
                _faceId.append(self.geoId.index(idd))
            except:
                print(f"the geo: {idd} not in the geometry library.")
        return [self.geometryList[idd] for idd in _faceId]

    def setCategory(self, reset=False):
        """
        Returns
        -------
        int
            The category code:
            - -2: Ignore faces (excluded from calculations)
            - -1: Shading faces (included as shading elements)
            -  0: Opaque surface
            -  1: Translucent surface
            -  2: Air wall
            -  3: Wall element (MoosasWall)
            -  4: Plane element (MoosasFace)
            -  5: Glazing element (MoosasGlazing)
            -  6: Skylight element (MoosasSkylight)
        """
        res = 0
        if reset:
            for geo in self.getAllFaces():
                geo.setCategory()
        else:
            for i in range(len(self.geoId)):
                if self.geometryList[i].category != 2:
                    self.geometryList[i].setCategory(-1)
            almoface = self.getAllFaces(dumpUseless=True)
            refs = {'MoosasFace': 4, 'MoosasSkylight': 6, 'MoosasWall': 3, 'MoosasGlazing': 5}
            for key in almoface.keys():
                for item in almoface[key]:
                    if mixItemListToList(item.category)[0] != 2:
                        item.setCategory(refs[key])
                    elif key == 'MoosasWall':
                        res += 1

    def removeSpace(self, space: str | MoosasSpace):
        """
        safely delete a space and change all the boundary conditions of reset of the faces.

        Parameters
        -------
        spaceId : str
        space id or space object to remove
        """
        if isinstance(space, str):
            space = self.spaceIdDict[space]
        for moElement in space.getAllFaces(False):
            try:
                moElement.isOuter = True
                moElement.space.remove(space.id)
            except:
                pass
        try:
            self.spaceList = list(self.spaceList)
            self.spaceList.remove(space)
        except:
            pass
        try:
            self.voidList = list(self.voidList)
            self.voidList.remove(space)
        except:
            pass
