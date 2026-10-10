from abc import ABC, abstractmethod
from typing import Any

class DNSProvider(ABC):
    @abstractmethod
    def get_domains(self) -> list[str]:
        pass
    
    @abstractmethod
    def get_records(self, domain: str) -> list[Any]:
        pass
    
    @abstractmethod
    def add_record(self, domain: str, record: Any) -> bool:
        pass
    
    @abstractmethod
    def delete_record(self, domain: str, record: Any) -> bool:
        pass
    
    @abstractmethod
    def update_record(self, domain: str, record: Any) -> bool:
        pass