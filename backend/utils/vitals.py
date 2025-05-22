import psutil
from loguru import logger

def get_cpu_core_count():
    """Returns the number of physical CPU cores on the host."""
    core_count = psutil.cpu_count(logical=False)  # Physical cores
    logger.info(f"Physical CPU cores: {core_count}")
    return core_count

def get_logical_cpu_count():
    """Returns the number of logical CPU cores on the host (including hyper-threading)."""
    logical_cores = psutil.cpu_count(logical=True)  # Logical cores (with HT, if available)
    logger.info(f"Logical CPU cores (with HT if applicable): {logical_cores}")
    return logical_cores

def get_cpu_usage_percentage():
    """Returns the average CPU usage percentage on the host."""
    cpu_usage = psutil.cpu_percent(interval=1)  # CPU usage percentage for the host
    logger.info(f"Average CPU usage: {cpu_usage}%")
    return cpu_usage

def get_cpu_usages_percentage():
    """Returns a list of CPU usage percentages per core."""
    cpu_usages = psutil.cpu_percent(interval=1, percpu=True)  # CPU usage per core
    logger.info(f"CPU usage per core: {cpu_usages}")
    return cpu_usages

def get_ram_usage_percentage():
    """Returns the percentage of RAM usage on the host."""
    memory = psutil.virtual_memory()
    ram_usage = memory.percent  # Percentage of used RAM
    logger.info(f"RAM usage: {ram_usage}%")
    return ram_usage

def get_ram_amount():
    """Returns the total amount of RAM on the host in a human-readable format."""
    memory = psutil.virtual_memory()
    ram_total = format_memory_size(memory.total)  # Total available RAM
    logger.info(f"Total RAM: {ram_total}")
    return ram_total

def get_raw_ram_amount():
    """Returns the total amount of RAM on the host in bytes."""
    memory = psutil.virtual_memory()
    return memory.total  # Total RAM in bytes

def format_memory_size(bytes_size):
    """Converts bytes to a human-readable memory format (e.g., GB, MB, KB)."""
    if bytes_size >= 1024**3:
        return f"{bytes_size / 1024**3:.0f}GB"
    elif bytes_size >= 1024**2:
        return f"{bytes_size / 1024**2:.0f}MB"
    elif bytes_size >= 1024:
        return f"{bytes_size / 1024:.0f}KB"
    else:
        return f"{bytes_size}B"