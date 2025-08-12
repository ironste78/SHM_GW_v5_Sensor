from modules.tools.log import log
from modules.sensor import Sensor
from modules.connections.http_client import Request
from io import BufferedWriter
import time
import os

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
        """Save the accelerometer data"""
        try:
            byte_timestamp = self.__validate_timestamps(delta_timestamp)

            if (byte_timestamp is None):
                raise ValueError(f"[AccelerometerStorer] Invalid timestamp {delta_timestamp}")
            
            # Convert delta_timestamp to bytes and add it to the data list joining the lists values
            data = byte_timestamp + b''.join(data)

            # Save the data
            self.__setup_output_stream(rxTstamp)
            if self.output_stream:
                self.output_stream.write(data)
                self.output_stream_counter += len(data)
                
                # If the counter is greater than len(data) * Frequency * 60 * 5 (5 minutes), close the output stream
                if self.output_stream_counter >= (len(data) * self.frequency * self.file_duration):
                    self.__close_file()
            else:
                raise ValueError('[AccelerometerStorer] Output stream is not available')
        except Exception as e:
            raise ValueError(f"[AccelerometerStorer] Some error: {e}")

    def __validate_timestamps(self, current_delta_timestamp: int):
        """Validate the timestamps"""
        if not self.start_delta_timestamp:
            self.start_delta_timestamp = current_delta_timestamp
            #log(f"[AccelerometerStorer] INFO timestamp first data - v1: {current_delta_timestamp}")

        # soglie in ms/us
        # ATTENZIONE: con 200 Hz, 2 periodi = 10 ms: troppo aggressivo per ruotare file!
        micro_gap_us = int(2_000_000)  # 2s in microsecondi
        milli_gap_ms = int(2_000)      # 2s in millisecondi

        if (current_delta_timestamp > 1748262041001092):
            # us timestamp unit
            if self.last_delta_timestamp and (current_delta_timestamp - self.last_delta_timestamp) > micro_gap_us:
                self.__close_file()
                self.start_delta_timestamp = current_delta_timestamp
                #log(f"[AccelerometerStorer] INFO timestamp first data - v2: {current_delta_timestamp}")

        elif(current_delta_timestamp > 1748262041001):
            # ms timestamp unit
            if self.last_delta_timestamp and (current_delta_timestamp - self.last_delta_timestamp) > milli_gap_ms:
                self.__close_file()
                self.start_delta_timestamp = current_delta_timestamp
                #log(f"[AccelerometerStorer] INFO timestamp first data - v2: {current_delta_timestamp}")

        else:
            # start got from 1 Jan 1970. Maybe the file should be deleted---- TODO!!
            log(f"[AccelerometerStorer] INFO timestamp Jan70 -- {current_delta_timestamp} > {self.last_delta_timestamp}")
            return None
            #if self.last_delta_timestamp and (current_delta_timestamp - self.last_delta_timestamp) > 2:
            #    self.__close_file()
            #    self.start_delta_timestamp = current_delta_timestamp
            #    log(f"[AccelerometerStorer] INFO timestamp Jan70 -- {current_delta_timestamp} > {self.last_delta_timestamp}")

        self.last_delta_timestamp = current_delta_timestamp

        return (current_delta_timestamp - self.start_delta_timestamp).to_bytes(4, byteorder='little')

    def __setup_output_stream(self, rxTstamp: int):
        """Get the output stream"""
        # 1. Create the temp directory if it does not exist
        if not os.path.exists(self.temp_directory):
            os.makedirs(self.temp_directory)

        if not self.output_stream:
            # 2. Get the file path. 
            # OPTION1: USE rxTstamp. Account for time of first data in the report is time of rx report - numReport(10) * tSampling 
            #self.begin_tStamp_filename = rxTstamp - int(int(Sensor.get('nreports', 0)) * (1000/self.frequency))
            # OPTION2: USE START_SAMPLING_TIMESTAMP
            #self.begin_tStamp_filename = self.START_SAMPLING_TIMESTAMP + self.start_delta_timestamp
            # OPTION3: The board is synchronized through the Discovery, then it sends directly the posix time
            self.begin_tStamp_filename = self.start_delta_timestamp
            #self.begin_tStamp_filename = rxTstamp or self.start_delta_timestamp

            filepath = os.path.join(self.temp_directory, f'shm_{self.mac}_05_{self.begin_tStamp_filename}.dat')
            #filepath = os.path.join(self.temp_directory, f'shm_{self.mac}_05_{self.START_SAMPLING_TIMESTAMP + self.start_delta_timestamp}.dat')

            # 3. Open the file in append mode
            self.output_stream = open(filepath, 'ab')

    def __close_file(self):
        """Close the output stream and move the file to the data directory"""
        # Reset the output stream
        if self.output_stream:
            # Move the file to the data directory
            filename = f'shm_{self.mac}_05_{self.begin_tStamp_filename}.dat'

            # OPTION1: TODO When using rxTstamp, here put a timing reference to be computed more accurately later as the rxTime of the last sample
            #self.end_tStamp_filename = int(time.time() * 1000)
            # OPTION2: When using START_SAMPLING_TIMESTAMP, just put the last delta timestamp
            #self.end_tStamp_filename = self.START_SAMPLING_TIMESTAMP + self.last_delta_timestamp
            # OPTION3: The board is synchronized through the Discovery, then it sends directly the posix time
            self.end_tStamp_filename = self.last_delta_timestamp

            dst_filename = f'shm_{self.mac}_05_{self.begin_tStamp_filename}_{(self.end_tStamp_filename)}'

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
            os.replace(os.path.join(self.temp_directory, filename), os.path.join(self.directory, dst_filename))
            
            
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