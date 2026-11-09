# coding=utf-8
"""Moosas datetime based on Ladybug datetime."""

from __future__ import division, annotations
from datetime import datetime, date, time


class DateTime(datetime):
    """Create Ladybug Date time.

    Args:
        month: A value for month between 1-12 (Default: 1).
        day: A value for day between 1-31 (Default: 1).
        hour: A value for hour between 0-23 (Default: 0).
        minute: A value for month between 0-59 (Default: 0).
        leap_year: A boolean to indicate if datetime is for a leap year
            (Default: False).

    Properties:
        * month
        * day
        * hour
        * leap_year
        * doy
        * hoy
        * int_hoy
        * minute
        * moy
        * float_hour
        * tzinfo
        * year
    """

    __slots__ = ()

    def __new__(cls, monthOrDateTime: int | datetime = 1, day=1, hour=0, minute=0, leap_year=False):
        """
        Create a MoosasDateTime instance from a month or datetime object, with optional day, hour, minute, and leap year settings.
        
        Parameters
        ----------
        monthOrDateTime : int or datetime, default 1
            If int, represents the month (1-12). If datetime, its month, day, hour, and minute are used.
        day : int, default 1
            Day of the month (1-31), used only if monthOrDateTime is an integer.
        hour : int or float, default 0
            Hour of the day (0-23) or decimal hour. Can be combined with minute.
        minute : int or float, default 0
            Minute of the hour (0-59) or fractional minutes. Combined with hour as total minutes.
        leap_year : bool, default False
            If True, sets the year to 2016 (leap year); otherwise, sets year to 2017.
        
        Returns
        -------
        MoosasDateTime
            A new instance of MoosasDateTime with the specified date and time components.
        """
        """Create MoosasDateTime
        """
        year = 2016 if leap_year else 2017

        try:
            if isinstance(monthOrDateTime, datetime):
                return datetime.__new__(cls, year, monthOrDateTime.month, monthOrDateTime.day,
                                        monthOrDateTime.hour, monthOrDateTime.minute)
            else:
                hour, minute = Time._calculate_hour_and_minute(hour + minute / 60.0)
                return datetime.__new__(cls, year, monthOrDateTime, day, hour, minute)
        except ValueError as e:
            raise ValueError("{}:\n\t({}/{}@{}:{})(m/d@h:m)".format(
                e, monthOrDateTime, day, hour, minute
            ))

    def __reduce_ex__(self, protocol):
        """
        Call the __new__() constructor when the class instance is unpickled.
        
        This method is necessary for the pickle.loads() call to work.
        
        Parameters
        ----------
        self : object
            The instance of the class being pickled.
        protocol : int
            The pickle protocol version used for serialization.
        
        Returns
        -------
        tuple
            A tuple containing the class type and a tuple of arguments (month, day, hour, minute) 
            required to reconstruct the instance during unpickling.
        """
        """Call the __new__() constructor when the class instance is unpickled.

        This method is necessary for the pickle.loads() call to work.
        """
        return type(self), (self.month, self.day, self.hour, self.minute)

    @classmethod
    def from_hoy(cls, hoy, leap_year=False):
        """
        Create Ladybug Datetime from an hour of the year.
        
        Parameters
        ----------
        hoy : float
            A float value representing the hour of the year, 0 <= hoy < 8760.
        leap_year : bool, optional
            Boolean to note whether the DateTime is part of a leap year. Default is False.
        
        Returns
        -------
        Ladybug Datetime
            A DateTime object corresponding to the given hour of the year.
        """
        """Create Ladybug Datetime from an hour of the year.

        Args:
            hoy: A float value 0 <= and < 8760
            leap_year: Boolean to note whether the Date Time is a part of a
                leap year. Default: False.
        """
        return cls.from_moy(round(hoy * 60), leap_year)

    @classmethod
    def from_moy(cls, moy, leap_year=False):
        """
        Create a Ladybug Datetime object from a minute of the year.
        
        Parameters
        ----------
        moy : int
            An integer representing the minute of the year, must satisfy 0 <= moy < 525600.
        leap_year : bool, optional
            Boolean indicating whether the datetime is in a leap year. Default is False.
        
        Returns
        -------
        LadybugDatetime
            A Ladybug Datetime object corresponding to the given minute of the year.
        """
        """Create Ladybug Datetime from a minute of the year.

        Args:
            moy: An integer value 0 <= and < 525600
            leap_year: Boolean to note whether the Date Time is a part of a
                leap year. Default: False.
        """
        if not leap_year:
            num_of_minutes_until_month = (0, 44640, 84960, 129600, 172800, 217440,
                                          260640, 305280, 349920, 393120, 437760,
                                          480960, 525600)
        else:
            num_of_minutes_until_month = (0, 44640, 84960 + 1440, 129600 + 1440,
                                          172800 + 1440, 217440 + 1440, 260640 + 1440,
                                          305280 + 1440, 349920 + 1440, 393120 + 1440,
                                          437760 + 1440, 480960 + 1440, 525600 + 1440)
        # find month
        moy = int(moy)
        for month_count in range(12):
            if moy < num_of_minutes_until_month[month_count + 1]:
                month = month_count + 1
                break
        try:
            day = int((moy - num_of_minutes_until_month[month - 1]) / (60 * 24)) + 1
        except UnboundLocalError:
            raise ValueError(
                "moy must be positive and smaller than 525600. Invalid input %d" % (moy)
            )
        else:
            hour = int((moy / 60) % 24)
            minute = int(moy % 60)

            return cls(month, day, hour, minute, leap_year)

    @staticmethod
    def _calculate_hour_and_minute(float_hour):
        """Calculate hour and minutes as integers from a float hour."""
        hour = int(float_hour)
        minute = int(round((float_hour - int(float_hour)) * 60))

        if minute == 60:
            return hour + 1, 0
        else:
            return hour, minute

    @classmethod
    def from_date_and_time(cls, date, time):
        """
        Create a DateTime object from a Date and a Time object.
        
        Parameters
        ----------
        date : Date
            A ladybug Date object.
        time : Time
            A ladybug Time object.
        
        Returns
        -------
        DateTime
            A new DateTime object created from the given Date and Time.
        """
        """Create Ladybug DateTime from a Date and a Time object.

        Args:
            date: A ladybug Date object.
            time: A ladybug Time object.
        """
        leap_year = True if date.year % 4 == 0 else False
        return cls(date.month, date.day, time.hour, time.minute, leap_year)

    @property
    def leap_year(self):
        """Boolean to note whether DateTime belongs to a leap year or not."""
        return self.year == 2016

    @property
    def doy(self):
        """Calculate day of the year for this date time."""
        return self.timetuple().tm_yday

    @property
    def hoy(self):
        """Calculate hour of the year for this date time."""
        return (self.doy - 1) * 24 + self.float_hour

    @property
    def moy(self):
        """Calculate minute of the year for this date time."""
        return int(self.hoy) * 60 + self.minute  # minute of the year

    @property
    def float_hour(self):
        """Get hour and minute as a float value, e.g. 6.25 for 6:15."""
        return self.hour + self.minute / 60.0


    @property
    def date(self):
        """Get a Date object associated with this DateTime."""
        return Date(self.month, self.day, self.leap_year)

    @property
    def time(self):
        """Get a Time object associated with this DateTime."""
        return Time(self.hour, self.minute)

    def __str__(self):
        """Return date time as a string."""
        return self.strftime('%d %b %H:%M')

    def ToString(self):
        """Overwrite .NET ToString."""
        return self.__str__()

    def __repr__(self):
        """Return date time as a string."""
        return self.__str__()


class Date(date):
    """Ladybug Date.

    Args:
        month: A value for month between 1-12. Default: 1.
        day: A value for day between 1-31. Default: 1.
        leap_year: A boolean to indicate if date is for a leap year. Default: False.

    Properties:
        * day
        * doy
        * leap_year
        * month
        * year
    """

    __slots__ = ()

    def __new__(cls, month=1, day=1, leap_year=False):
        """
        Create a Ladybug Date object.
        
        Parameters
        ----------
        month : int, optional
            The month of the year from 1 to 12. Default is 1.
        day : int, optional
            The day of the month from 1 to 31. Default is 1.
        leap_year : bool, optional
            Boolean to indicate whether the date is in a leap year (2016) or not (2017). Default is False.
        
        Returns
        -------
        date
            A date object representing the specified day in either a leap year (2016) or a common year (2017).
        """
        """Create Ladybug Date.
        """
        year = 2016 if leap_year else 2017
        try:
            return date.__new__(cls, year, month, day)
        except ValueError as e:
            raise ValueError("{}:\n\t({}/{})(m/d)".format(e, month, day))

    def __reduce_ex__(self, protocol):
        """
        Call the __new__() constructor when the class instance is unpickled.
        
        This method is necessary for the pickle.loads() call to work.
        
        Parameters
        ----------
        self : object
            The instance of the class being pickled.
        protocol : int
            The pickle protocol used for serialization.
        
        Returns
        -------
        tuple
            A tuple containing the class type and a tuple of arguments (month, day, leap_year) 
            to be passed to __new__ upon unpickling.
        """
        """Call the __new__() constructor when the class instance is unpickled.

        This method is necessary for the pickle.loads() call to work.
        """
        return (type(self), (self.month, self.day, self.leap_year))

    @property
    def leap_year(self):
        """Boolean to note whether Date belongs to a leap year or not."""
        return self.year == 2016

    @property
    def doy(self):
        """Calculate day of the year for this date."""
        return self.timetuple().tm_yday

    def to_array(self):
        """Return date as an array of values."""
        if not self.leap_year:
            return (self.month, self.day)
        return (self.month, self.day, 1)

    def to_dict(self):
        """Get date as a dictionary."""
        base = {'month': self.month, 'day': self.day, 'type': 'Date'}
        if self.leap_year:
            base['leap_year'] = True
        return base

    def __str__(self):
        """Return date as a string."""
        return self.strftime('%d %b')

    def __repr__(self):
        """Return date as a string."""
        return self.__str__()


class Time(time):
    """Create Ladybug Time.

    Args:
        hour: A value for hour between 0-23 (Default: 0).
        minute: A value for month between 0-59 (Default: 0).

    Properties:
        * hour
        * minute
        * mod
        * second
        * tzinfo
    """

    __slots__ = ()

    def __new__(cls, hour=0, minute=0):
        """
        Create a Ladybug Time object.
        
        Parameters
        ----------
        hour : int or float, optional
            The hour of the time, which can be an integer or a float. If a float is provided,
            it will be converted to hours and minutes. Default is 0.
        minute : int, optional
            The minute of the time. Default is 0.
        
        Returns
        -------
        time
            A new instance of Ladybug Time with the specified hour and minute.
        """
        """Create Ladybug Time.
        """
        hour, minute = cls._calculate_hour_and_minute(hour + minute / 60.0)
        try:
            return time.__new__(cls, hour, minute)
        except ValueError as e:
            raise ValueError("{}:\n\t({}:{})(h:m)".format(e, hour, minute))

    def __reduce_ex__(self, protocol):
        """
        Call the __new__() constructor when the class instance is unpickled.
        
        This method is necessary for the pickle.loads() call to work.
        
        Parameters
        ----------
        self : object
            The instance of the class being pickled.
        protocol : int
            The pickle protocol version used for serialization.
        
        Returns
        -------
        tuple
            A tuple containing the class type and a tuple of arguments (hour, minute) 
            to be passed to __new__ upon unpickling.
        """
        """Call the __new__() constructor when the class instance is unpickled.

        This method is necessary for the pickle.loads() call to work.
        """
        return (type(self), (self.hour, self.minute))

    @classmethod
    def from_dict(cls, data):
        """
        Create a time object from a dictionary.
        
        Parameters
        ----------
        data : dict
            A dictionary containing time components with the following keys:
            - 'hour' (int, optional): Hour value between 0-23. Default is 0.
            - 'minute' (int, optional): Minute value between 0-59. Default is 0.
        
        Returns
        -------
        cls
            A new instance of the class initialized with the given hour and minute.
        """
        """Create time from a dictionary.

        Args:
            data: A python dictionary in the following format

        .. code-block:: python

                {
                'hour': 0  # A value for hour between 0-23. (Default: 0)
                'minute': 0  # A value for month between 0-59. (Default: 0)
                }
        """
        hour = data['hour'] if 'hour' in data else 0
        minute = data['minute'] if 'minute' in data else 0
        return cls(hour, minute)

    @classmethod
    def from_mod(cls, mod):
        """
        Create a Ladybug Time object from a minute of the day.
        
        Parameters
        ----------
        mod : int
            An integer value representing the minute of the day, in the range 0 <= mod < 1440.
        
        Returns
        -------
        Ladybug Time
            A Time object corresponding to the given minute of the day.
        """
        """Create Ladybug Time from a minute of the day.

        Args:
            mod: An int value 0 <= and < 1440
        """
        hour, minute = cls._calculate_hour_and_minute(mod / 60.0)
        return cls(hour, minute)

    @classmethod
    def from_time_string(cls, time_string, leap_year=False):
        """
        Create a Ladybug Time object from a time string in the format 'HH:MM'.
        
        Parameters
        ----------
        time_string : str
            A string representing time in 24-hour format 'HH:MM', where HH is hours (00-23)
            and MM is minutes (00-59).
        leap_year : bool, optional
            A flag to indicate whether the time is for a leap year. This parameter does not
            affect the parsing of the time string but may be used by the class constructor.
            Default is False.
        
        Returns
        -------
        Time
            A Ladybug Time object representing the given time.
        """
        """Create Ladybug Time from a Time string.

        Usage:

        .. code-block:: python

            dt = Time.from_time_string("12:00")
        """
        try:
            dt = datetime.strptime(time_string, '%H:%M')
        except AttributeError:  # older Python version before strptime
            vals = time_string.split(':')
            dt = datetime(int(vals[0]), int(vals[1]))
        return cls(dt.hour, dt.minute)

    @classmethod
    def from_array(cls, time_array):
        """
        Create a Ladybug Time object from an array of integers.
        
        Parameters
        ----------
        time_array : array-like of int
            An array of 2 integers ordered as follows: (hour, minute).
        
        Returns
        -------
        LadybugTime
            A new instance of Ladybug Time initialized with the given hour and minute.
        """
        """Create Ladybug Time from am array of integers.

        Args:
            datetime_array: An array of 2 integers ordered as follows: (hour, minute)
        """
        return cls(*time_array)

    @property
    def mod(self):
        """Calculate minute of the day for this time."""
        return self.hour * 60 + self.minute

    @property
    def float_hour(self):
        """Get hour and minute as a float value, e.g. 6.25 for 6:15."""
        return self.hour + self.minute / 60.0

    def to_array(self):
        """Return time as an array of values."""
        return (self.hour, self.minute)

    def to_dict(self):
        """Get time as a dictionary."""
        return {'hour': self.hour, 'minute': self.minute, 'type': 'Time'}

    @staticmethod
    def _calculate_hour_and_minute(float_hour):
        """Calculate hour and minutes as integers from a float hour."""
        hour = int(float_hour)
        minute = int(round((float_hour - hour) * 60))
        if minute == 60:
            return hour + 1, 0
        else:
            return hour, minute

    def __str__(self):
        """Return time as a string."""
        return self.strftime('%H:%M')

    def ToString(self):
        """Overwrite .NET ToString."""
        return self.__str__()

    def __repr__(self):
        """Return time as a string."""
        return self.__str__()
