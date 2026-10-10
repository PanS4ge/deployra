from abc import ABC, abstractmethod
from typing import Any

class Storage(ABC):
    @abstractmethod
    def does_db_exist(self, db: str) -> bool:
        pass

    @abstractmethod
    def create_db(self, db: str, **kwargs) -> bool:
        pass

    @abstractmethod
    def save_value(self, db: str, key: str, **kwargs) -> bool:
        pass

    @abstractmethod
    def update_value(self, db: str, key: str, **kwargs) -> bool:
        pass

    @abstractmethod
    def get_value(self, db: str, value: str, column: str) -> Any:
        pass

    @abstractmethod
    def get_all_databases(self) -> list[str]:
        pass

    @abstractmethod
    def list_keys(self, db: str) -> list[str]:
        pass

    @abstractmethod
    def delete_db(self, db: str) -> bool:
        pass
    
    @abstractmethod
    def delete_value(self, db: str, key: str) -> bool:
        pass