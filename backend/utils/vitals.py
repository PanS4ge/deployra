import psutil
import os
import statics

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
    try:
        with open("/proc/meminfo", "r") as f:
            mem_info = f.readlines()
        total_mem = int([line for line in mem_info if line.startswith("MemTotal")][0].split()[1])
        return format_memory_size(total_mem * 1024)
    except Exception as e:
        print(f"Nie udało się odczytać pamięci z /proc/meminfo: {e}")
        memory = psutil.virtual_memory()
        return format_memory_size(memory.total)

def get_raw_ram_amount():
    try:
        with open("/proc/meminfo", "r") as f:
            mem_info = f.readlines()
        total_mem = int([line for line in mem_info if line.startswith("MemTotal")][0].split()[1])
        return total_mem * 1024
    except Exception as e:
        print(f"Nie udało się odczytać pamięci z /proc/meminfo: {e}")
        memory = psutil.virtual_memory()
        return memory.total

def get_cpu_core_count():
    return psutil.cpu_count(logical=True)

def get_cpu_usage_percentage():
    cpu_usage = psutil.cpu_percent(interval=statics.cpu_usage_interval, percpu=True)
    average_cpu_usage = sum(cpu_usage) / len(cpu_usage)
    return average_cpu_usage

def get_cpu_usages_percentage():
    cpu_usage = psutil.cpu_percent(interval=statics.cpu_usage_interval, percpu=True)
    return cpu_usage