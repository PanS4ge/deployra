import os

os.environ.setdefault("DEPLOYRA_SCRYPT_N", "16384")  # testy: tańszy scrypt (produkcja 2^17)

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
