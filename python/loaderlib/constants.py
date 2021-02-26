from enum import IntEnum


# Returned from file identification to rate confidence in guessed or recognised file format.
MATCH_NONE = 0
MATCH_POSSIBLE = 1
MATCH_PROBABLE = 2
MATCH_CERTAIN = 3

class Processor(IntEnum):
    UNKNOWN = 0
    M680x0 = 100
    M68000 = 101
    M68010 = 102
    M68020 = 103
    M68030 = 104
    M68040 = 105
    M68060 = 106
    MIPS = 200
    P65c816 = 300       # This gets a P prefix to account for the numeric name.
    Z80 = 400


processor_names = {
    Processor.UNKNOWN: "Unknown",
    Processor.P65c816: "65c816",
    Processor.M680x0: "M680x0",
    Processor.M68000: "M68000",
    Processor.M68010: "M68010",
    Processor.M68020: "M68020",
    Processor.M68030: "M68030",
    Processor.M68040: "M68040",
    Processor.M68060: "M68060",
    Processor.MIPS: "MIPS",
    Processor.Z80: "Z80",
}

def lookup_processor_id_by_name(specified_processor_name):
    specified_processor_name = specified_processor_name.lower()
    for processor_id, processor_name in processor_names.items():
        if processor_name.lower() == specified_processor_name:
            return processor_id

# The supported platforms.
PLATFORM_UNKNOWN = 0
PLATFORM_AMIGA = 1000
PLATFORM_ATARIST = 2000
PLATFORM_SNES = 6000
PLATFORM_X68000 = 7000
PLATFORM_ZXSPECTRUM = 8000

platform_names = {
    PLATFORM_UNKNOWN: "Unknown",
    PLATFORM_AMIGA: "Amiga",
    PLATFORM_ATARIST: "Atari ST",
    PLATFORM_SNES: "SNES",
    PLATFORM_X68000: "X68000",
    PLATFORM_ZXSPECTRUM: "ZX Spectrum",
}

# The supported file formats.
class FileFormat(IntEnum):
    UNKNOWN = 0
    AMIGA_HUNK_EXECUTABLE = PLATFORM_AMIGA + 1
    AMIGA_HUNK_LIBRARY = PLATFORM_AMIGA + 2
    ATARIST_GEMDOS_EXECUTABLE = PLATFORM_ATARIST + 1
    SNES_SMC = PLATFORM_SNES + 1
    X68000_X_EXECUTABLE = PLATFORM_X68000 + 1
    ZXSPECTRUM_Z80_1 = PLATFORM_ZXSPECTRUM + 1
    ZXSPECTRUM_Z80_2 = PLATFORM_ZXSPECTRUM + 2
    ZXSPECTRUM_Z80_3 = PLATFORM_ZXSPECTRUM + 3


file_format_names = {
    FileFormat.UNKNOWN: "Unknown",
    FileFormat.AMIGA_HUNK_EXECUTABLE: "Hunk executable",
    FileFormat.AMIGA_HUNK_LIBRARY: "Hunk library",
    FileFormat.ATARIST_GEMDOS_EXECUTABLE: "GEMDOS executable",
    FileFormat.SNES_SMC: "SMC rom",
    FileFormat.X68000_X_EXECUTABLE: "X executable",
    FileFormat.ZXSPECTRUM_Z80_1: "Z80 snapshot v1",
    FileFormat.ZXSPECTRUM_Z80_2: "Z80 snapshot v2",
    FileFormat.ZXSPECTRUM_Z80_3: "Z80 snapshot v3",
}


class Endian(IntEnum):
    UNKNOWN = 0
    BIG = 1
    LITTLE = 2


endian_names = {
    Endian.UNKNOWN: "Unknown",
    Endian.BIG: "Big",
    Endian.LITTLE: "Little",
}


class MatchResult(object):
    confidence = MATCH_NONE
    platform_id = PLATFORM_UNKNOWN
    file_format_id = FileFormat.UNKNOWN


def _count_bits(v):
    count = 0
    while v:
        count += 1
        v >>= 1
    return count

def _make_bitmask(bitcount):
    mask = 0
    while bitcount:
        bitcount -= 1
        mask |= 1<<bitcount
    return mask


DATA_TYPE_CODE          = 1
DATA_TYPE_ASCII         = 2
DATA_TYPE_DATA08        = 3
DATA_TYPE_DATA16        = 4
DATA_TYPE_DATA32        = 5
DATA_TYPE_BIT0          = DATA_TYPE_CODE - 1
DATA_TYPE_BITCOUNT      = _count_bits(DATA_TYPE_DATA32)
DATA_TYPE_BITMASK       = _make_bitmask(DATA_TYPE_BITCOUNT)

DATA_TYPE_SIZES = [
    (DATA_TYPE_DATA32, 4),
    (DATA_TYPE_DATA16, 2),
    (DATA_TYPE_DATA08, 1),
]
