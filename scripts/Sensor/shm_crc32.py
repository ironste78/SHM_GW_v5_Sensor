# scripts/Sensor/shm_crc32.py
import zlib
from typing import ByteString

def crc32_ieee(data: ByteString) -> int:
    """CRC-32 (IEEE 802.3): poly 0xEDB88320, init 0xFFFFFFFF, xorout 0xFFFFFFFF, reflected."""
    return zlib.crc32(memoryview(data)) & 0xFFFFFFFF

def crc32_header_without_crc(header: bytes) -> int:
    """
    Compute CRC-32 over the header excluding the trailing 4-byte CRC field.
    Assumes the header layout ends with a uint32_t CRC (little-endian on MCU).
    """
    return crc32_ieee(header[:-4])
