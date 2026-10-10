import random
from hashlib import sha256
import statics

def get_private_value(string) -> int:
    return sum([ord(c) for c in string])

def generate_private_key(p):
    return p

def generate_public_key(generator, prime, private_key):
    return pow(generator, private_key, prime)

def generate_shared_secret(other_public_key, private_key, prime):
    return pow(other_public_key, private_key, prime)

def encrypt_message(shared_secret, message) -> bytes:
    key = sha256(str(shared_secret).encode()).digest()
    encrypted = bytes([b ^ key[i % len(key)] for i, b in enumerate(message.encode())])
    return encrypted

def encrypt_message_delivery(shared_secret, message) -> str:
    return encrypt_message(shared_secret, message).hex()

def decrypt_message(shared_secret, encrypted_message) -> str:
    key = sha256(str(shared_secret).encode()).digest()
    decrypted = bytes([b ^ key[i % len(key)] for i, b in enumerate(encrypted_message)])
    return decrypted.decode()

def decrypt_message_delivery(shared_secret, encrypted_message) -> str:
    return decrypt_message(shared_secret, bytes.fromhex(encrypted_message))