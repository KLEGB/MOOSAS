import numpy as np
import copy
from eppy import ext_field_functions as extff
from eppy.bunchhelpers import makefieldname

# IDF-specific settings models.
SpaceDefault = {
    'space_height': 3.0,  #
    'zone_area': 10.0,  #
    'outside_area': 10.0,  #
    'facade_area': 10.0,  #
    'window_area': 10.0,  #
    'roof_area': 10.0,  #
    'skylight_area': 10.0,  #
    'floor_area': 10.0,  #
    'summer_solar': 0.0,  #
    'winter_solar': 0.0,  #

    'zone_wallU': 0.5, # construction
    'zone_winU': 2.4, # construction
    'zone_win_SHGC': 0.6, # construction
    'zone_c_temp': 26,#'coolingSchedule'
    'zone_c_hum': 0.4,
    'zone_h_temp': 18,#'heatingSchedule'
    'zone_collingEER': 2.5,
    'zone_HeatingEER': 2,
    'zone_work_start': 0,  #'OccSchedule'
    'zone_work_end': 23,  #'OccSchedule'
    'zone_ppsm': 0.0196,  #ZonePeopleDefault
    'zone_pfav': 30,  #ZoneOutdoorAirDefault
    'zone_popheat': 88,  #pHeatSchedule
    'zone_equipment': 3.8,  #ZoneEquipmentDefault
    'zone_lighting': 5,  #ZoneLightDefault
    'zone_infiltration': 0.5,  #ZoneInfiltrationDefault
}
FaceDefault = {
    'key': 'BuildingSurface:Detailed',
    "Name": "",
    "Surface_Type": "Floor",
    "Construction_Name": "Office_External_Wall",
    "Zone_Name": "",
    "Outside_Boundary_Condition": "Outdoors",
    "Sun_Exposure": 'SunExposed',
    "Wind_Exposure": 'WindExposed',
    "View_Factor_to_Ground": "AutoCalculate",
    "Number_of_Vertices": 4,
    "Vertex_1_Xcoordinate": 0.0,
    "Vertex_1_Ycoordinate": 0.0,
    "Vertex_1_Zcoordinate": 0.0,
    "Vertex_2_Xcoordinate": 0.0,
    "Vertex_2_Ycoordinate": 0.0,
    "Vertex_2_Zcoordinate": 0.0,
    "Vertex_3_Xcoordinate": 0.0,
    "Vertex_3_Ycoordinate": 0.0,
    "Vertex_3_Zcoordinate": 0.0,
    "Vertex_4_Xcoordinate": 0.0,
    "Vertex_4_Ycoordinate": 0.0,
    "Vertex_4_Zcoordinate": 0.0,
}
WindowDefault = {
    'key': 'FenestrationSurface:Detailed',
    "Name": "",
    "Surface_Type": "Window",
    "Construction_Name": "Office_External_Window",
    "Building_Surface_Name": "",
    "View_Factor_to_Ground": "AutoCalculate",
    "Multiplier": 1,
    "Number_of_Vertices": 3,
    "Vertex_1_Xcoordinate": 0.0,
    "Vertex_1_Ycoordinate": 0.0,
    "Vertex_1_Zcoordinate": 0.0,
    "Vertex_2_Xcoordinate": 0.0,
    "Vertex_2_Ycoordinate": 0.0,
    "Vertex_2_Zcoordinate": 0.0,
    "Vertex_3_Xcoordinate": 0.0,
    "Vertex_3_Ycoordinate": 0.0,
    "Vertex_3_Zcoordinate": 0.0,
}
ZoneObjectDefault = {
    'key': 'ZONE',
    'Name': "",
    'Direction_of_Relative_North': 0,
    'X_Origin': 0, 'Y_Origin': 0, 'Z_Origin': 0,
    'Type': 1,
    'Multiplier': 1,
    'Volume': 0.0,
    'Floor_Area': 0.0,
    'Zone_Inside_Convection_Algorithm': "TARP",
    'Part_of_Total_Floor_Area': "Yes"
}
ZoneInfiltrationDefault = {
    "key": "ZoneInfiltration:DesignFlowRate",
    'Name': "",  # Block2:Zone5 Infiltration
    'Zone_or_ZoneList_Name': "",
    "Schedule_Name": "On",
    "Design_Flow_Rate_Calculation_Method": "Flow/zone",
    "Design_Flow_Rate": 0.0, # m3/s
}
ZoneVentilationDefault = {
    "key": "ZoneVentilation:DesignFlowRate",
    'Name': "",  # Block2:Zone5 Ventilation
    'Zone_or_ZoneList_Name': "",
    "Schedule_Name": "Office_MainRoom_OnOff",
    "Flow_Rate_per_Person": "Flow/Person",
    "Design_Flow_Rate": 0.0,  # m3/s/person
}
ZoneEquipmentDefault = {
    "key": "OtherEquipment",
    'Name': "",
    "Fuel_Type": "Electricity",
    'Zone_or_ZoneList_Name': "",
    "Schedule_Name": "Office_MainRoom_Occ",
    "Design_Level_Calculation_Method": "Watts/Area",
    "Power_per_Zone_Floor_Area": 15.0,
}
ZoneElectricEquipmentDefault = {
    "key": "ElectricEquipment",
    'Name': "",
    'Zone_or_ZoneList_Name': "",
    "Schedule_Name": "Office_MainRoom_Occ",
    "Design_Level_Calculation_Method": "Watts/Area",
    "Watts_per_Zone_Floor_Area": 15.0,
}
ZonePeopleDefault = {
    "key": "People",
    'Name': "",  # Block2:Zone5 People
    'Zone_or_ZoneList_Name': "",
    'Number_of_People_Schedule_Name': 'Office_MainRoom_OnOff',
    'Number_of_People_Calculation_Method': 'Watts/Area',
    'People_per_Zone_Floor_Area': .02,
    'Activity_level_schedule_Name': 'Office_MainRoom_OnOff'
}
ZoneLightDefault = {
    "key": "Lights",
    'Name': "",  # Block2:Zone5 Lighting
    'Zone_or_ZoneList_Name': "",
    'Schedule_Name': 'Office_MainRoom_Occ',
    'Design_Level_Calculation_Method': 'People/Area',
    'Watts_per_Zone_Floor_Area': .02,
}
ZoneSizingDefault = {
    "key": "Sizing:Zone",
    'Zone_or_ZoneList_Name': "",
    'Design_Specification_Outdoor_Air_Object_Name': '',  # same as zone name
    'Design_Specification_Zone_Air_Distribution_Object_Name': '',  # same as zone name
}
ZoneAirDistributionDefault = {
    "key": "DesignSpecification:ZoneAirDistribution",
    "Name": ''  # same as zone name
}
ZoneOutdoorAirDefault = {
    "key": "DesignSpecification:OutdoorAir",
    'Name': "",  # same as zone name
    'Outdoor_Air_Method': 'Flow/Person',
    'Outdoor_Air_Flow_per_Person': '',
    'Outdoor_Air_Schedule_Name': 'Office_MainRoom_Occ'
}
ZoneControlDefault = {
    "key": "ZoneControl:Thermostat",
    'Name': "",# Block2:Zone5 Thermostat
    'Zone_or_ZoneList_Name': "",
    'Control_1_Object_Type': 'ThermostatSetpoint:DualSetpoint',
    'Control_1_Name': ''  # Dual Setpoint - Zone Block2:Zone5
}
ZoneThermostatSetpointDefault = {
    "key": "ThermostatSetpoint:DualSetpoint",
    'Name': "",  # Dual Setpoint - Zone Block2:Zone5
    'Heating_Setpoint_Temperature_Schedule_Name': 'Office_MainRoom_HeatingSetPoint',
    'Cooling_Setpoint_Temperature_Schedule_Name': 'Office_MainRoom_CoolingSetPoint'
}
ZoneEquipmentConnectionsDefault = {
    "key": "ZoneHVAC:EquipmentConnections",
    'Zone_Name': "",
    'Zone_Conditioning_Equipment_List_Name': "",  # Block1:Zone3 EquipmentList
    'Zone_Air_Inlet_Node_or_NodeList_Name': "",  # Block1:Zone3 Inlets
    'Zone_Air_Node_Name': "",  # Node Block1:Zone3 Zone
    'Zone_Return_Air_Node_or_NodeList_Name': "" # Node Block1:Zone3 Out
}
ZoneEquipmentListDefault = {
    "key": "ZoneHVAC:EquipmentList",
    'Name':'',# Block1:Zone3 EquipmentList
    'Zone_Equipment_1_Object_Type': 'ZoneHVAC:IdealLoadsAirSystem',
    'Zone_Equipment_1_Name':"" # Block1:Zone3 Ideal Loads Air
}
ZoneHVACDefault = {
    "key": "ZoneHVAC:IdealLoadsAirSystem",
    'Name': "", # Block1:Zone3 Ideal Loads Air
    'Availability_Schedule_Name': 'Office_MainRoom_Occ',
    'Zone_Supply_Air_Node_Name': '', # Node Block1:Zone3 In
    'Heating_Availability_Schedule_Name': 'Office_MainRoom_OnOff',
    'Cooling_Availability_Schedule_Name': 'Office_MainRoom_OnOff',
    'Design_Specification_Outdoor_Air_Object_Name': ''# same as zone name
}
ZoneNodeListDefault = {
    "key": "NodeList",
    'Name': "",  # Block1:Zone3 Inlets
    'Node_1_Name': "", # Node Block1:Zone3 In
}

'Office_MainRoom_HeatingSetPoint'
'Office_MainRoom_CoolingSetPoint'
'Office_MainRoom_Occ'
'Office_MainRoom_OnOff'
"Office_External_Window"
"Office_External_Wall"
"Office_External_Roof"
"Office_External_GroundFloor"
"Office_Internal_Window"
"Office_Internal_Wall"
"Office_Internal_Floor"


class MoosasSettings(object):
    __slots__ = ['id', 'params']

    def __init__(self, default=None, **kwargs):
        """
        Initialize the object with default parameters and optional keyword arguments.
        
        Parameters
        ----------
        default : SpaceDefault, optional
            The default parameter set to copy. If None, defaults to SpaceDefault.
        **kwargs : dict
            Additional keyword arguments to update the parameters; may include 'id'.
        
        Returns
        -------
        None
        """
        if default is None:
            default = SpaceDefault
        self.params = copy.copy(default)
        self.updateParams(**kwargs)
        if 'id' in kwargs.keys():
            self.id = kwargs['id']

    @classmethod
    def fromIdfObject(cls, idfObject):
        """
        Create a new instance from an IDF object.
        
        Parameters
        ----------
        cls : type
            The class constructor, used to instantiate the new object.
        idfObject : object
            An object containing IDF data with attributes 'objls' and key-value access 
            via indexing. Keys in 'objls' are used to extract non-empty values.
        
        Returns
        -------
        cls
            A new instance of the class initialized with extracted keyword arguments.
        """
        kwargs = {}
        for key in idfObject.objls:
            if idfObject[key]!='':
                kwargs[key] = idfObject[key]
        return cls({}, **kwargs)

    def updateParams(self, **kwargs):
        """
        Update the parameters of the object with the provided keyword arguments.
        
        Parameters
        ----------
        **kwargs : dict
            Arbitrary keyword arguments representing the parameters to be updated.
            Each key-value pair will be added or updated in the `self.params` dictionary.
        
        Returns
        -------
        self : object
            Returns the instance of the object with updated parameters, enabling method chaining.
        """
        for key in kwargs.keys():
            self.params[key] = kwargs[key]
        return self

    def paramToString(self):
        """
        Convert parameter values to a comma-separated string.
        
        Parameters
        ----------
        self : object
            The instance containing a `params` attribute, which is a dictionary 
            mapping parameter names to their values.
        
        Returns
        -------
        str
            A comma-separated string representation of the parameter values.
        """
        return ','.join(np.array(list(self.params.values())).astype(str))

    def paramTags(self):
        """Get a string of parameter keys prefixed with an exclamation mark.
        
        Parameters
        ----------
        self : object
            The instance of the class containing a `params` dictionary attribute.
        
        Returns
        -------
        str
            A string starting with '!' followed by comma-separated parameter keys.
        """
        return '!' + ','.join(np.array(list(self.params.keys())).astype(str))

    def applyToIDF(self, idf, rename: dict = None):
        """
        Apply parameters to an IDF object, optionally renaming fields.
        
        Parameters
        ----------
        idf : pyenergyplus.idf.IDF
            The IDF object to which the parameters will be applied.
        rename : dict, optional
            A dictionary mapping parameter names to new field names in the IDF object.
            If not provided, no renaming is performed.
        
        Returns
        -------
        None
            This function does not return a value. It modifies the `idf` object in place.
        """
        if 'key' not in self.params.keys():
            return None
        if rename is None:
            rename = {}
        key = self.params['key'].upper()
        key_i = idf.model.dtls.index(key)
        objidd = idf.idd_info[key_i]
        objls = {makefieldname(comm['field'][0]) for comm in objidd[1:]}
        extensible = extff.getextensible(objidd)
        fields = {}
        for _name in self.params.keys():
            if _name in rename.keys():
                _objName = rename[_name]
            else:
                _objName = _name
            if _name != 'key' and (_name in objls
                                   or (extensible and extff.islegalextensiblefield(objidd, _name))):
                if str(self.params[_name]).isdecimal():
                    try:
                        self.params[_name] = float(self.params[_name])
                        self.params[_name] = np.round(self.params[_name],2)
                    except:
                        pass
                fields[_objName] = self.params[_name]
        missing = [name for name in fields if name not in objls]
        if missing:
            # Grow the IDD to the highest extensible index used (e.g. Vertex_178_* on large surfaces).
            last = extff.extfieldint(objidd[-1]['field'][0], sep=' ')
            extra = max(extff.extfieldint(name) for name in missing) - last
            extff.increaseIDDfields(idf.block, idf.idd_info, key_i, key, extra * extensible)
        return idf.newidfobject(self.params['key'], **fields)

    def __repr__(self):
        """String representation of the ThermalSettings object.
        
        Parameters
        ----------
        self : ThermalSettings
            The instance of ThermalSettings to represent as a string.
        
        Returns
        -------
        str
            A string representation of the ThermalSettings object, specifically the string representation of its `params` attribute.
        """
        return self.params.__repr__()

    def __getitem__(self, key):
        if key in self.params.keys():
            return self.params[key]
        else:
            raise KeyError(key)

    def __setitem__(self, key, value):
        self.params[key] = value

    def __delitem__(self, key):
        del self.params[key]

class ThermalSettings(MoosasSettings):
    __slots__ = ['load']

    def __init__(self, **kwargs):
        """
        Initialize the ThermalSettings object with default space settings and load values.
        
        Parameters
        ----------
        **kwargs : dict
            Additional keyword arguments to be passed to the parent class constructor.
        
        Returns
        -------
        None
            This method does not return any value.
        """
        super(ThermalSettings, self).__init__(SpaceDefault, **kwargs)
        self.load = {
            'total': 0.0,
            'cooling': 0.0,
            'heating': 0.0,
            'lighting': 0.0
        }
    def __repr__(self):
        """Return string representations of the params and load attributes.
        
        Parameters
        ----------
        self : object
            The instance of the class containing `params` and `load` attributes.
        
        Returns
        -------
        str
            Concatenated string representation of `self.params` and `self.load`.
        """
        return self.params.__repr__() + self.load.__repr__()
