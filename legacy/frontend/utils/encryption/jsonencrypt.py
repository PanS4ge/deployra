import statics
from utils import certgen
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from base64 import b64encode, b64decode

def encode_json(content : str) -> str:
    encrypted = certgen.read_public_key(statics.public_key).encrypt(
        content.encode('utf-8'),
        padding.PKCS1v15()
    )
    return b64encode(encrypted).decode()
    
    
def decode_json(content : bytes) -> str:
    try:
        return certgen.read_private_key(statics.private_key).decrypt(
            b64decode(content),
            padding.PKCS1v15()
        )
    except:
        return None