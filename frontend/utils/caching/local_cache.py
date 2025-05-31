import os
import time
import pickle
import hashlib
from functools import update_wrapper
from typing import Callable, Any

CACHE_DIR = os.path.join(os.getcwd(), "cache")
DEFAULT_TTL = 65536  # seconds

os.makedirs(CACHE_DIR, exist_ok=True)

def local_cache(func: Callable) -> Callable:
    def make_cache_key(args, kwargs) -> str:
        key_data = (func.__name__, args, tuple(sorted(kwargs.items())))
        return hashlib.md5(pickle.dumps(key_data)).hexdigest()

    def is_valid(path: str) -> bool:
        return os.path.exists(path) and (time.time() - os.path.getmtime(path)) < DEFAULT_TTL

    def wrapped(*args, **kwargs) -> Any:
        key = make_cache_key(args, kwargs)
        path = os.path.join(CACHE_DIR, f"{key}.pkl")

        if is_valid(path):
            with open(path, 'rb') as f:
                return pickle.load(f)

        result = func(*args, **kwargs)
        with open(path, 'wb') as f:
            pickle.dump(result, f)
        return result

    return update_wrapper(wrapped, func)