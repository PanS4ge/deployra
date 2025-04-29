import psutil

def get_ram_usage_percentage():
    memory = psutil.virtual_memory()
    return memory.percent

def format_memory_size(bytes_size):
    if bytes_size >= 1024**3:
        return f"{bytes_size / 1024**3:.0f}GB"
    elif bytes_size >= 1024**2:
        return f"{bytes_size / 1024**2:.0f}MB"
    elif bytes_size >= 1024:
        return f"{bytes_size / 1024:.0f}KB"
    else:
        return f"{bytes_size}B"

def get_ram_amount():
    memory = psutil.virtual_memory()
    return format_memory_size(memory.total)

def get_raw_ram_amount():
    memory = psutil.virtual_memory()
    return memory.total

def get_cpu_core_count():
    return psutil.cpu_count(logical=False)
