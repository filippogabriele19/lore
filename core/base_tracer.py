from __future__ import annotations
from abc import ABC, abstractmethod

class BaseTaintTracer(ABC):
    @abstractmethod
    def trace(self) -> dict:
        """
        Execute variable dataflow taint analysis on the source code.
        Returns:
            dict containing:
                "flows": list of detected source-to-sink flow dicts
                "outgoing_calls": list of caller argument details for external functions
                "tainted_returns": list of function names returning tainted values (optional)
        """
        pass
