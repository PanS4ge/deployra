import sqlite3

conn = sqlite3.connect('/var/lib/deployra-panel/deployra.sqlite')
conn.row_factory = sqlite3.Row
conn.execute("UPDATE nodes SET api_url = 'https://frog01.pansage.xyz', gateway_id = NULL WHERE id = 9")
conn.execute("UPDATE nodes SET api_url = 'https://frog02.pansage.xyz', gateway_id = NULL WHERE id = 12")
conn.commit()

for r in conn.execute('SELECT id, name, api_url, gateway_id, status FROM nodes').fetchall():
    print(dict(r))
