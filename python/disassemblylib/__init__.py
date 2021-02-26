"""
    Peasauce - interactive disassembler
    Copyright (C) 2012-2017 Richard Tew
    Licensed using the MIT license.
"""

import logging
from typing import List

from . import constants
from ..loaderlib.constants import Processor

logger = logging.getLogger("disassemblylib")


def get_processor_ids() -> List[Processor]:
    return [
        Processor.M680x0,
        Processor.MIPS,
        Processor.P65c816,
    ]

def get_processor(processor_id):
    if processor_id == Processor.P65c816:
        from .arch65c816 import Arch65c816 as ArchClass
        from .arch65c816 import instruction_table
        from .arch65c816 import operand_type_table
    elif processor_id == Processor.M680x0:
        from .archm68k import ArchM68k as ArchClass
        from .archm68k import instruction_table
        from .archm68k import operand_type_table
    elif processor_id == Processor.MIPS:
        from .archmips import ArchMIPS as ArchClass
        from .archmips import instruction_table
        from .archmips import operand_type_table
    elif processor_id == Processor.Z80:
        from .archz80 import ArchZ80 as ArchClass
        from .archz80 import instruction_table
        from .archz80 import operand_type_table
    else:
        logger.error("get_processor: %s unknown", processor_id)
        raise Exception("...")

    arch = ArchClass()
    arch.set_operand_type_table(operand_type_table)
    arch.set_instruction_table(instruction_table)
    return arch
