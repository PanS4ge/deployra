def read_file(file_path: str) -> str:
    c = ""
    with open(file_path, "r") as f:
        c = f.read()
        f.close()
    return c