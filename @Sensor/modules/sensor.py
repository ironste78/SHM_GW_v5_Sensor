class Sensor:

    SENSOR = None

    @classmethod
    def is_none(cls):
        return cls.SENSOR is None

    @classmethod
    def get(cls, key: str, default=None):
        """Safe getter for sensor:
        - If sensor is None, returns empty dictionary
        - If key is None, returns empty dictionary"""
        if (cls.SENSOR is None) or (key is None):
            return default if default is not None else {}
        return cls.SENSOR.get(key, default)
    
    @classmethod
    def set(cls, json):
        cls.SENSOR = json

    @classmethod
    def update(cls, key: str, value):
        cls.SENSOR[key] = value

    @classmethod
    def print(cls):
        print(cls.SENSOR)
