import asyncio
from deployra_panel.config import Config
from deployra_panel.context import Panel

async def main():
    panel = Panel(Config())
    for node_id in (9, 12):
        node = panel.node(node_id)
        if not node:
            continue
        try:
            client = panel.node_client(node)
            info = await client.get("/api/info")
            panel.db.update("nodes", node["id"], info=info, status="paired")
            print(f"Node {node['name']} (ID {node_id}) updated successfully! OS: {info.get('os')}, RAM: {info.get('ram_human')}")
        except Exception as e:
            print(f"Node {node['name']} (ID {node_id}) failed: {e}")

asyncio.run(main())
