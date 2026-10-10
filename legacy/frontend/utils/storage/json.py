from utils.storage.storage import Storage
from loguru import logger
from typing import Any
import json
import os
import glob

class JSON(Storage):
    def __init__(self) -> None:
        super().__init__()
        self.storage_path = os.path.join(os.getcwd(), "storage")
        if not os.path.exists(self.storage_path):
            os.makedirs(self.storage_path)

    def does_db_exist(self, db: str) -> bool:
        return os.path.isfile(os.path.join(self.storage_path, f"{db}.json"))

    def create_db(self, db: str, **kwargs) -> bool:
        if not os.path.exists(self.storage_path):
            os.makedirs(self.storage_path)
        db_path = os.path.join(self.storage_path, f"{db}.json")
        if os.path.exists(db_path):
            logger.warning(f"Baza `{db}` już istnieje.")
            return False
        with open(db_path, "w") as f:
            json.dump({}, f)
        logger.info(f"Utworzono nową bazę `{db}`.")
        return True

    def save_value(self, db: str, key: str, **kwargs) -> bool:
        if not isinstance(kwargs, dict):
            logger.error("Wartość musi być typu dict (fieldy w kluczu).")
            return False

        db_path = os.path.join(self.storage_path, f"{db}.json")
        data = {}
        try:
            with open(db_path, "r") as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.error(f"Baza `{db}` nie istnieje, zostanie utworzona.")
        
        data[key] = kwargs

        with open(db_path, "w") as f:
            json.dump(data, f, indent=4)

        logger.info(f"Zapisano klucz `{key}` w `{db}`.")
        return True

    def update_value(self, db: str, key: str, **kwargs) -> bool:
        path = os.path.join(self.storage_path, f"{db}.json")
        if not os.path.exists(path):
            return False
        with open(path, "r") as f:
            data = json.load(f)
        if key not in data:
            return False
        for field, new_value in kwargs.items():
            data[key][field] = new_value
        with open(path, "w") as f:
            json.dump(data, f, indent=4)
        return True

    def get_value(self, db: str, value: str, column: str) -> Any:
        db_path = os.path.join(self.storage_path, f"{db}.json")
        try:
            data : dict = {}
            with open(db_path, "r") as f:
                data = json.loads(f.read())
            return data[value][column]
        except FileNotFoundError:
            logger.error(f"Baza `{db}` nie istnieje.")
            return None

    def list_keys(self, db: str) -> list[str]:
        db_path = os.path.join(self.storage_path, f"{db}.json")
        try:
            data : dict = {}
            with open(db_path, "r") as f:
                data = json.load(f)
            return data.keys()
        except FileNotFoundError:
            logger.error(f"Baza `{db}` nie istnieje.")
            return None
    
    def get_all_databases(self) -> list[str]:
        files = glob.glob(os.path.join(self.storage_path, "*.json"))
        databases = [os.path.splitext(os.path.basename(file))[0] for file in files]
        return databases

    def delete_db(self, db: str) -> bool:
        db_path = os.path.join(self.storage_path, f"{db}.json")
        if not os.path.isfile(db_path):
            logger.warning(f"Baza `{db}` nie istnieje.")
            return False
        os.remove(db_path)
        logger.info(f"Baza `{db}` została usunięta.")
        return True
    
    def delete_value(self, db: str, key: str) -> bool:
        db_path = os.path.join(self.storage_path, f"{db}.json")
        data = {}
        try:
            with open(db_path, "r") as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.error(f"Baza `{db}` nie istnieje, zostanie utworzona.")
        
        del data[key]

        with open(db_path, "w") as f:
            json.dump(data, f, indent=4)

        logger.info(f"Zapisano klucz `{key}` w `{db}`.")
        return True