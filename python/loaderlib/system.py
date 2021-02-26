from .constants import Endian, Processor

class BaseSystem:
    endian_id: Endian = Endian.UNKNOWN
    processor_id: Processor = Processor.UNKNOWN

    def __init__(self, system_name: str) -> None:
        self.system_name = system_name

    def get_processor_id(self) -> Processor:
        return self.processor_id
