"""
    Peasauce - interactive disassembler
    Copyright (C) 2012-2017 Richard Tew
    Licensed using the MIT license.
"""

import os

from .. import constants
from ..constants import Endian, Processor
from ..system import BaseSystem


class System(BaseSystem):
    endian_id = Endian.BIG
    processor_id = Processor.UNKNOWN

    def set_processor_id(self, processor_id: Processor) -> None:
        self.processor_id = processor_id

    def load_input_file(self, input_file, file_info, data_types, f_offset=0, f_length=None):
        if file_info.loader_options is None or not file_info.loader_options.is_binary_file:
            return False
        self.set_processor_id(file_info.loader_options.processor_id)

        if f_length is None:
            file_offset2 = input_file.tell()
            input_file.seek(0, os.SEEK_END)
            f_length = input_file.tell()
            input_file.seek(file_offset2, os.SEEK_SET)

        file_size = f_length
        relocations = []
        symbols = []
        file_info.add_code_segment(0, file_size, file_size, relocations, symbols)
        return True

    def identify_input_file(self, input_file, file_info, data_types, f_offset=0, f_length=None):
        """ User selected files should not be identified as binary. """
        return []

    def load_project_data(self, f):
        return None

    def save_project_data(self, f, data):
        return None

    def print_summary(self, file_info):
        pass

    def has_segment_headers(self):
        return False

    def get_segment_header(self, file_info, segment_id):
        return "this section header should never be seen"

    def get_data_instruction_string(self, data_size, is_bss_segment, with_file_data):
        suffix_by_size = {
            constants.DATA_TYPE_DATA08: "B",
            constants.DATA_TYPE_DATA16: "W",
            constants.DATA_TYPE_DATA32: "L",
        }
        suffix = "."+ suffix_by_size[data_size]
        if is_bss_segment:
            return "DS"+ suffix
        return "DC"+ suffix
