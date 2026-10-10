from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend as crypto_default_backend

def generate_keys(private_key_path: str, public_key_path: str):
    key = rsa.generate_private_key(
        backend=crypto_default_backend(),
        public_exponent=65537,
        key_size=2048
    )

    # Zapis klucza prywatnego w formacie PEM
    private_key = key.private_bytes(
        crypto_serialization.Encoding.PEM,
        crypto_serialization.PrivateFormat.PKCS8,
        crypto_serialization.NoEncryption()
    )
    
    with open(private_key_path, 'wb') as f:
        f.write(private_key)

    # Zapis klucza publicznego w formacie OpenSSH
    public_key = key.public_key().public_bytes(
        crypto_serialization.Encoding.OpenSSH,
        crypto_serialization.PublicFormat.OpenSSH
    )
    
    with open(public_key_path, 'wb') as f:
        f.write(public_key)

def read_private_key(path: str):
    with open(path, 'rb') as f:
        return crypto_serialization.load_pem_private_key(
            f.read(),
            password=None,
            backend=crypto_default_backend()
        )

def read_public_key(path: str):
    with open(path, 'rb') as f:
        return crypto_serialization.load_ssh_public_key(
            f.read(),
            backend=crypto_default_backend()
        )