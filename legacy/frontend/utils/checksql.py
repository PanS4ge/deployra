import mysql.connector
from mysql.connector import Error

def check_database_connection(host : str, user : str, password : str, database : str) -> bool:
    try:
        connection = mysql.connector.connect(
            host=host,
            user=user,
            password=password,
            database=database
        )
        if connection.is_connected():
            print("Połączenie z bazą danych zostało nawiązane.")
            return True
    except Error as e:
        print("Błąd połączenia:", e)
        return False
    finally:
        if 'connection' in locals() and connection.is_connected():
            connection.close()