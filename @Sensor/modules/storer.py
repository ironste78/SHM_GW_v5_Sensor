from modules.tools.log import log
from modules.sensor import Sensor
from modules.connections.http_client import Request
from io import BufferedWriter
import time
import os
import struct

class AccelerometerStorer:

    START_SAMPLING_TIMESTAMP: int = None                    # Start sampling timestamp for the accelerometer data

    def __init__(self):
        self.mac: str = Sensor.get('mac').replace(":", "")  # MAC address of the device
        self.frequency: int = Sensor.get('frequency', 200)  # Frequency of the accelerometer data
        self.file_duration: int = int(os.getenv('SENSOR_FILE_DURATION', 300))  # Duration of the accelerometer file
        self.output_stream_counter: int = 0                 # Counter for the output stream
        self.output_stream: BufferedWriter = None           # Output stream for the accelerometer data

        self.start_delta_timestamp: int = None              # Start delta timestamp for the accelerometer data
        self.last_delta_timestamp: int = None               # Last delta timestamp for the accelerometer data
        
        self.directory = os.getenv('DATA_DIR', '../data')   # Directory to save the data
        self.temp_directory = self.directory + '/.temp'     # Temporary directory to save the data

        self.begin_tStamp_filename: int = None              # Time (ms) of file's opening
        self.end_tStamp_filename: int = None                # Time (ms) of file's closing

    @classmethod
    def set_start_sampling_timestamp(cls, timestamp: int):
        """Set the start sampling timestamp"""
        cls.START_SAMPLING_TIMESTAMP = timestamp

    ## Accelerometer data ##
    def save(self, data: list, delta_timestamp: int, rxTstamp: int):
        """Salva i campioni in BINARIO:
           record = <deltaT:uint32 LE> <ax:float32 LE> <ay:float32 LE> <az:float32 LE> <temp:float32 LE>
           dove deltaT è in microsecondi rispetto al primo campione del file (t0).
        """

        try:
            delta_us = self.__validate_timestamps(delta_timestamp)
            if delta_us is None:
                raise ValueError(f"[AccelerometerStorer] Invalid timestamp {delta_timestamp}")

            # Prepare output stream and filenames (sets t0 if needed)
            self.__setup_output_stream(rxTstamp)

            # Convert values (incoming chunks are 4B little-endian floats from PacketHandler)
            vals = data[:4]  # ax, ay, az, temp
            floats = []
            for b in vals:
                if not isinstance(b, (bytes, bytearray)) or len(b) != 4:
                    raise ValueError("[AccelerometerStorer] invalid float bytes in chunk")
                floats.append(struct.unpack('<f', b)[0])

            # Impacchetta il record binario: deltaT uint32 LE + 4 float32 LE
            # If wants bigg-endian format, just use '!Iffff'
            record = struct.pack('<Iffff', int(delta_us), floats[0], floats[1], floats[2], floats[3])

            if self.output_stream:
                self.output_stream.write(record)
                self.output_stream_counter += len(record)  # in bytes

                # Rotazione per durata: bytes_per_record (20) * freq * file_duration
                bytes_per_record = 4 + 4*4
                if self.output_stream_counter >= (bytes_per_record * self.frequency * self.file_duration):
                    self.__close_file()
                raise ValueError('[AccelerometerStorer] Output stream is not available')
        except Exception as e:
            raise ValueError(f"[AccelerometerStorer] Some error: {e}")


    def __validate_timestamps(self, current_delta_timestamp: int):
        """Validate timestamps and return deltaT in microseconds as uint32.
        Treat all inputs as UNIX epoch in microseconds. If ms is detected, convert to µs.
        Also handles file rotation on large gaps (2s) and keeps start/last absolute timestamps.
        """
        # initialize t0 with absolute timestamp
        if not self.start_delta_timestamp:
            self.start_delta_timestamp = current_delta_timestamp

        # Normalize to µs if a ms epoch slipped in
        ts_us = current_delta_timestamp
        if current_delta_timestamp < 10_000_000_000_000:          # < ~2001-09-09 in µs → probably ms
            if current_delta_timestamp > 10_000_000_000:          # >= ~2001 in ms
                ts_us = current_delta_timestamp * 1000

        # Gap-based rotation: if jump forward > 2s
        if self.last_delta_timestamp:
            diff = ts_us - self.last_delta_timestamp
            if diff > 2_000_000:  # 2s in µs
                self.__close_file()
                self.start_delta_timestamp = ts_us

        self.last_delta_timestamp = ts_us

        # deltaT from t0 in µs, rotate to avoid u32 overflow
        delta_us = max(0, ts_us - self.start_delta_timestamp)
        if delta_us > 0xFFFFFFFF:
            self.__close_file()
            self.start_delta_timestamp = ts_us
            delta_us = 0

        return delta_us

    def __setup_output_stream(self, rxTstamp: int):
        """Prepare the output stream and filenames.
           Begin/end timestamps in filename are absolute UNIX µs.
        """
        # 1. Temp dir
        if not os.path.exists(self.temp_directory):
            os.makedirs(self.temp_directory)

        if not self.output_stream:
            # Absolute begin = t0_us
            self.begin_tStamp_filename = self.start_delta_timestamp
            # file temporaneo (estensione .part)
            tmp_name = f'shm_{self.mac}_05_{self.begin_tStamp_filename}.part'
            filepath = os.path.join(self.temp_directory, tmp_name)

            # 3. Open the file (binary; we write encoded text lines)
            self.output_stream = open(filepath, 'ab')
            self.__send_status(3, "Sampling")

        # keep end updated to last absolute timestamp
        self.end_tStamp_filename = self.last_delta_timestamp

    def __close_file(self):
        """Close the output stream and move the file to the data directory"""
        # Reset the output stream
        if self.output_stream:
            # Move the file to the data directory
            # Name of the temporary file file in .temp folder
            tmp_filename = f'shm_{self.mac}_05_{self.begin_tStamp_filename}.part'

            # OPTION1: TODO When using rxTstamp, here put a timing reference to be computed more accurately later as the rxTime of the last sample
            #self.end_tStamp_filename = int(time.time() * 1000)
            # OPTION2: When using START_SAMPLING_TIMESTAMP, just put the last delta timestamp
            #self.end_tStamp_filename = self.START_SAMPLING_TIMESTAMP + self.last_delta_timestamp
            # OPTION3: The board is synchronized through the Discovery, then it sends directly the posix time
            self.end_tStamp_filename = self.last_delta_timestamp

            # Final file name in data/ (with .dat)
            dst_filename = f'shm_{self.mac}_05_{self.begin_tStamp_filename}_{self.end_tStamp_filename}.dat'

            #filename = f'shm_{self.mac}_05_{self.START_SAMPLING_TIMESTAMP + self.start_delta_timestamp}.dat'
            #dst_filename = f'shm_{self.mac}_05_{self.START_SAMPLING_TIMESTAMP + self.start_delta_timestamp}_{(self.START_SAMPLING_TIMESTAMP + self.last_delta_timestamp)}'

            # flush su disco
            import os
            self.output_stream.flush()
            os.fsync(self.output_stream.fileno())
            self.output_stream.close()
            # directory di destinazione garantita
            os.makedirs(self.directory, exist_ok=True)
            # move atomico anche cross-platform
            os.replace(
                os.path.join(self.temp_directory, tmp_filename),
                os.path.join(self.directory, dst_filename)
            )
             
            
            self.output_stream = None

            log(f"[AccelerometerStorer] Created file: {dst_filename}")
            self.__send_status(3, "Sampling")


        # Reset the variables
        self.output_stream_counter = 0
        self.start_delta_timestamp = None

    
    def __send_status(self, status: int, msg: str):
        """Send the sensor status"""
        try:
            Request.update_sensor_status({ "procStatus": status, "message": msg })
        except Exception as e:
            log(f"[AccelerometerStorer] Error while updating sensor status: {e}", "ERROR")

    ## END Accelerometer data ##