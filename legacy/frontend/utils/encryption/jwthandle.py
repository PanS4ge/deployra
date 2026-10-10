import jwt
import statics
from utils import certgen
from datetime import datetime, timedelta, timezone
import loguru
import functools

def encode_jwt(payload: dict) -> str:
    payload['exp'] = datetime.now(tz=timezone.utc) + timedelta(days=1)
    return jwt.encode(
        payload=payload,
        key=certgen.read_private_key(statics.private_key),
        algorithm='RS256'
    )
    
@functools.cache
def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            key=certgen.read_public_key(statics.public_key),
            algorithms=['RS256']
        )
    except:
        return None