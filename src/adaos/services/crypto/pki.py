from __future__ import annotations

from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_rsa_key(bits: int = 3072) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def write_private_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    try:
        path.chmod(0o600)
    except PermissionError:
        # best effort on platforms that do not support chmod
        pass


def write_pem(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text = f"{text}\n"
    path.write_text(text, encoding="utf-8")


def make_csr(common_name: str, org: Optional[str], key: rsa.RSAPrivateKey) -> str:
    subject_attributes = [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    if org:
        subject_attributes.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(subject_attributes)).sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")


__all__ = ["generate_rsa_key", "write_private_key", "write_pem", "make_csr"]
