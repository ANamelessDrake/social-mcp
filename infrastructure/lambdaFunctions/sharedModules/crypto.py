"""KMS envelope for provider tokens.

Tokens are small (well under the 4 KB KMS direct-encrypt limit), so plain
kms:Encrypt/Decrypt is enough; no data-key layer needed. Symmetric decrypt
does not require the key id, but encrypt does (rotated-token persistence).
"""

import os
from functools import lru_cache

import boto3


@lru_cache(maxsize=1)
def _kms():
    return boto3.client("kms")


def encrypt(plaintext: str) -> bytes:
    resp = _kms().encrypt(KeyId=os.environ["TOKEN_KMS_KEY_ID"], Plaintext=plaintext.encode())
    return resp["CiphertextBlob"]


def decrypt(ciphertext: bytes) -> str:
    resp = _kms().decrypt(CiphertextBlob=bytes(ciphertext))
    return resp["Plaintext"].decode()
