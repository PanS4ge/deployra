from abc import ABC, abstractmethod
from typing import Any
import mysql.connector
from utils.storage.storage import Storage
from utils import settingsgen

class SQL(Storage):
    def __init__(self) -> None:
        self.conn = mysql.connector.connect(
            host=settingsgen.read_settings()["mysql_host"],
            user=settingsgen.read_settings()["mysql_user"],
            password=settingsgen.read_settings()["mysql_password"],
            database=settingsgen.read_settings()["mysql_database"],
        )
        self.cursor = self.conn.cursor()

    def does_db_exist(self, db: str) -> bool:
        self.cursor.execute("SHOW TABLES")
        tables = [t[0] for t in self.cursor.fetchall()]
        return db in tables

    def create_db(self, db: str, **kwargs) -> bool:
        if not kwargs:
            raise ValueError("Musisz podać kolumny jako kwargs (np. name='VARCHAR(255)')")

        # wymuszamy kolumnę `key`
        columns = "`key` VARCHAR(255) UNIQUE," + ", ".join([f"`{name}` {type_}" for name, type_ in kwargs.items()])
        sql = f"CREATE TABLE IF NOT EXISTS `{db}` (id INT AUTO_INCREMENT PRIMARY KEY, {columns})"
        self.cursor.execute(sql)
        self.conn.commit()
        return True

    def save_value(self, db: str, key: str, **kwargs) -> bool:
        if not kwargs:
            raise ValueError("Musisz podać wartości jako kwargs (np. name='Dominika')")

        columns = ", ".join(["`key`"] + [f"`{col}`" for col in kwargs.keys()])
        placeholders = ", ".join(["%s"] * (len(kwargs) + 1))
        values = [key] + list(kwargs.values())

        sql = f"INSERT INTO `{db}` ({columns}) VALUES ({placeholders})"
        self.cursor.execute(sql, values)
        self.conn.commit()
        return True

    def update_value(self, db: str, key: str, **kwargs) -> bool:
        if not kwargs:
            return False
        update_clause = ", ".join([f"`{k}`=%s" for k in kwargs.keys()])
        values = list(kwargs.values()) + [key]
        sql = f"UPDATE `{db}` SET {update_clause} WHERE `key` = %s"
        self.cursor.execute(sql, values)
        self.conn.commit()
        return True

    def get_value(self, db: str, value: str, column: str) -> Any:
        sql = f"SELECT * FROM `{db}` WHERE `{column}` = %s"
        self.cursor.execute(sql, (value,))
        result = self.cursor.fetchone()
        return result
    
    def list_keys(self, db: str) -> list[str]:
        sql = f"SELECT `key` FROM `{db}`"
        self.cursor.execute(sql)
        results = [row[0] for row in self.cursor.fetchall()]
        return results

    def get_all_databases(self) -> list[str]:
        self.cursor.execute("SHOW TABLES")
        return [row[0] for row in self.cursor.fetchall()]

    def delete_db(self, db: str) -> bool:
        sql = f"DROP TABLE IF EXISTS `{db}`"
        self.cursor.execute(sql)
        self.conn.commit()
        return True