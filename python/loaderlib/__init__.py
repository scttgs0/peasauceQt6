"""
    Peasauce - interactive disassembler
    Copyright (C) 2012-2017 Richard Tew
    Licensed using the MIT license.
"""

from dataclasses import dataclass
import io
import logging
import os
import struct
from typing import Any, IO, List, Optional, Tuple

from . import amiga
from . import atarist
from . import binary
from . import human68k
from . import snes
from . import zxspectrum
from . import constants
from .constants import Endian


logger = logging.getLogger("loader")


systems_by_name = {}

def _generate_module_data():
    global systems_by_name
    for module in (amiga, atarist, human68k, binary, snes, zxspectrum):
        system_name = module.__name__
        systems_by_name[system_name] = module.System(system_name)
_generate_module_data()

def get_system(system_name):
    return systems_by_name[system_name]

def get_system_data_types(system_name: str) -> "DataTypes":
    system = systems_by_name[system_name]
    return DataTypes(system.endian_id)

def load_file(input_file, file_name, loader_options=None, file_offset=0, file_length=None) -> Optional[Tuple["FileInfo", "DataTypes"]]:
    for system_name, system in systems_by_name.items():
        file_info = FileInfo(system, file_name, loader_options)
        data_types = get_system_data_types(system_name)
        if system.load_input_file(input_file, file_info, data_types, f_offset=file_offset, f_length=file_length):
            return file_info, data_types

def identify_file(input_file, file_name, file_offset=0, file_length=None):
    matches = []
    for system_name, system in systems_by_name.items():
        file_info = FileInfo(system, file_name)
        data_types = get_system_data_types(system_name)
        system_matches = system.identify_input_file(input_file, file_info, data_types, f_offset=file_offset, f_length=file_length)
        matches.extend(((file_info, match, system) for match in system_matches))

    if len(matches):
        # For now take the match we are most confident in.
        matches.sort(key = lambda v: v[1].confidence)
        file_info, match, system = matches[0]

        if match.file_format_id != constants.FileFormat.UNKNOWN and match.confidence != constants.MATCH_NONE:
            result = {}
            result["processor"] = system.get_processor_id()
            result["platform"] = match.platform_id
            result["filetype"] = match.file_format_id
            result["endian"] = system.endian_id
            return file_info, result


SEGMENT_TYPE_CODE = 1
SEGMENT_TYPE_DATA = 2
SEGMENT_TYPE_BSS = 3


@dataclass
class Segment:
    type: int
    file_offset: int
    data_length: int
    length: int
    address: int
    cached_data: Any


def get_segment_type(segments, segment_id):
    return segments[segment_id].type

def get_segment_data_file_offset(segments, segment_id):
    return segments[segment_id].file_offset

def get_segment_data_length(segments, segment_id):
    return segments[segment_id].data_length

def get_segment_length(segments, segment_id):
    return segments[segment_id].length

def get_segment_address(segments, segment_id):
    return segments[segment_id].address

def get_segment_data(segments, segment_id):
    return segments[segment_id].cached_data

def is_segment_type_code(segments, segment_id):
    return segments[segment_id].type == SEGMENT_TYPE_CODE

def is_segment_type_data(segments, segment_id):
    return segments[segment_id].type == SEGMENT_TYPE_DATA

def is_segment_type_bss(segments, segment_id):
    return segments[segment_id].type == SEGMENT_TYPE_BSS

def cache_segment_data(input_file: io.RawIOBase, segments: List[Any], segment_id: int, base_file_offset: int=0) -> None:
    """
    base_file_offset: when the input file is located within a containing file.
    """
    data = None
    file_offset = get_segment_data_file_offset(segments, segment_id)
    # No data for segments that have no data..
    if file_offset != -1:
        file_length = get_segment_data_length(segments, segment_id)

        input_file.seek(base_file_offset + file_offset, os.SEEK_SET)
        file_data = bytearray(file_length)
        if input_file.readinto(file_data) == file_length:
            # NOTE(rmtew): Python 2, type(data[0]) is str. Python 3, type(data[0]) is int
            data = memoryview(file_data)
        else:
            logger.error("Unable to cache segment %d data, got %d bytes, wanted %d", segment_id, len(file_data), file_length)
    segments[segment_id].cached_data = data

def relocate_segment_data(segments, data_types, relocations, relocatable_addresses, relocated_addresses):
    for segment_id in range(len(segments)):
        # Generic longword-based relocation.
        data = get_segment_data(segments, segment_id)
        local_address = get_segment_address(segments, segment_id)
        for target_segment_id, local_offsets in relocations[segment_id]:
            target_address = get_segment_address(segments, target_segment_id)
            for local_offset in local_offsets:
                value = data_types.uint32_value(data[local_offset:local_offset+4])
                address = value + target_address
                relocated_addresses.setdefault(address, set()).add(local_address + local_offset)
                relocatable_addresses.add(local_address + local_offset)
                data[local_offset:local_offset+4] = data_types.uint32_value_as_string(address)


def has_segment_headers(system_name):
    return get_system(system_name).has_segment_headers()

def get_segment_header(system_name, segment_id, data):
    return get_system(system_name).get_segment_header(segment_id, data)

def get_data_instruction_string(system_name, segments, segment_id, data_size, with_file_data):
    segment_type = get_segment_type(segments, segment_id)
    is_bss_segment = segment_type == SEGMENT_TYPE_BSS
    return get_system(system_name).get_data_instruction_string(data_size, is_bss_segment, with_file_data)


def get_load_address(file_info):
    return file_info.load_address

def get_entrypoint_address(file_info):
    #if file_info.entrypoint_address is not None:
    #    return file_info.entrypoint_address
    return get_segment_address(file_info.segments, file_info.entrypoint_segment_id) + file_info.entrypoint_offset


class DataTypes(object):
    def __init__(self, endian_id):
        self.endian_id = endian_id
        self._endian_char = [ "<", ">" ][endian_id == Endian.BIG]

        s = b"12345"
        bs = bytearray(s)
        mv = memoryview(bs)
        if type(mv[0]) is int:
            self.uint8_value = self._uint8_value3
        else:
            self.uint8_value = self._uint8_value2

    ## Data access related operations.

    def sized_value(self, data_size, bytes, idx=None):
        if data_size == constants.DATA_TYPE_DATA32:
            return self.uint32_value(bytes, idx)
        elif data_size == constants.DATA_TYPE_DATA16:
            return self.uint16_value(bytes, idx)
        elif data_size == constants.DATA_TYPE_DATA08:
            return self.uint8_value(bytes, idx)
        raise Exception("unsupported size", data_size)

    def _uint8_value2(self, bytes, idx=0):
        return self.uint8(bytes[idx])

    def _uint8_value3(self, bytes, idx=0):
        return bytes[idx]

    def uint16_value(self, bytes, idx=0):
        return self.uint16(bytes[idx:idx+2])

    def uint32_value(self, bytes, idx=0):
        try:
            return self.uint32(bytes[idx:idx+4])
        except:
            pass

    def uint32_value_as_string(self, v):
        if self.endian_id == Endian.BIG:
            return struct.pack(">I", v)
        else:
            return struct.pack("<I", v)

    # String to value.

    def uint16(self, s):
        if self.endian_id == Endian.BIG:
            return struct.unpack(">H", s)[0]
        else:
            return struct.unpack("<H", s)[0]

    def int16(self, s):
        if self.endian_id == Endian.BIG:
            return struct.unpack(">h", s)[0]
        else:
            return struct.unpack("<h", s)[0]

    def uint32(self, s):
        if self.endian_id == Endian.BIG:
            return struct.unpack(">I", s)[0]
        else:
            return struct.unpack("<I", s)[0]

    def int32(self, s):
        if self.endian_id == Endian.BIG:
            return struct.unpack(">i", s)[0]
        else:
            return struct.unpack("<i", s)[0]

    def uint8(self, s):
        if self.endian_id == Endian.BIG:
            return struct.unpack(">B", s)[0]
        else:
            return struct.unpack("<B", s)[0]

    def int8(self, s):
        if self.endian_id == Endian.BIG:
            return struct.unpack(">b", s)[0]
        else:
            return struct.unpack("<b", s)[0]



class FileInfo(object):
    """ The custom system data for the loaded file. """
    internal_data = None # type: Any
    savefile_data = None # type: Any

    def __init__(self, system, file_name, loader_options=None):
        self.system = system
        self.file_name = file_name
        self.loader_options = loader_options

        self.segments = []
        self.relocations_by_segment_id = []
        self.symbols_by_segment_id = []

        if loader_options is not None and loader_options.is_binary_file:
            self.load_address = loader_options.load_address
        else:
            self.load_address = 0

        """ The segment id and offset in that segment of the program entrypoint. """
        if loader_options is not None:
            self.entrypoint_segment_id = loader_options.entrypoint_segment_id
            self.entrypoint_offset = loader_options.entrypoint_offset
        else:
            self.entrypoint_segment_id = 0
            self.entrypoint_offset = 0

    ## Query..

    def has_file_name_suffix(self, suffix):
        return self.file_name.lower().endswith("."+ suffix.lower())

    ## Segment registration related operations

    def set_internal_data(self, file_data):
        self.internal_data = file_data

    def get_internal_data(self):
        return self.internal_data

    def set_savefile_data(self, file_data):
        self.savefile_data = file_data

    def get_savefile_data(self):
        return self.savefile_data

    def print_summary(self):
        self.system.print_summary(self)

    def add_code_segment(self, file_offset, data_length, segment_length, relocations, symbols):
        logger.debug("Added code segment %d %d %d #relocs %d", file_offset, data_length, segment_length, len(relocations))
        self.add_segment(SEGMENT_TYPE_CODE, file_offset, data_length, segment_length, relocations, symbols)

    def add_data_segment(self, file_offset, data_length, segment_length, relocations, symbols):
        logger.debug("Added data segment %d %d %d #relocs %d", file_offset, data_length, segment_length, len(relocations))
        self.add_segment(SEGMENT_TYPE_DATA, file_offset, data_length, segment_length, relocations, symbols)

    def add_bss_segment(self, file_offset, data_length, segment_length, relocations, symbols):
        logger.debug("Added bss segment %d %d %d #relocs %d", file_offset, data_length, segment_length, len(relocations))
        self.add_segment(SEGMENT_TYPE_BSS, file_offset, data_length, segment_length, relocations, symbols)

    def add_segment(self, segment_type, file_offset, data_length, segment_length, relocations, symbols):
        segment_id = len(self.segments)
        segment_address = self.load_address
        if segment_id > 0:
            segment_address = get_segment_address(self.segments, segment_id-1) + get_segment_length(self.segments, segment_id-1)

        segment = Segment(segment_type, file_offset, data_length, segment_length, segment_address, None)
        self.segments.append(segment)

        self.relocations_by_segment_id.append(relocations)
        self.symbols_by_segment_id.append(symbols)

    def set_entrypoint(self, segment_id, offset):
        self.entrypoint_segment_id = segment_id
        self.entrypoint_offset = offset

    ## Segment querying related operations


class BinaryFileOptions:
    is_binary_file = True
    processor_id: Optional[int] = None
    load_address: Optional[int] = None
    entrypoint_segment_id: int = 0
    entrypoint_offset: Optional[int] = None
