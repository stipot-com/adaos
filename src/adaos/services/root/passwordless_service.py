# services/root/passwordless_client.py
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding


class PasswordlessAuthClient:
    """Клиент для беспарольной аутентификации со стороны хаба"""

    def __init__(self, private_key_pem: str):
        """Инициализация с приватным ключом хаба"""
        self.private_key = serialization.load_pem_private_key(
            private_key_pem.encode(),
            password=None
        )

    def sign_challenge(self, challenge: str) -> str:
        """Подпись challenge приватным ключом"""
        challenge_bytes = challenge.encode()

        if isinstance(self.private_key, ec.EllipticCurvePrivateKey):
            signature = self.private_key.sign(
                challenge_bytes,
                ec.ECDSA(hashes.SHA256())
            )
        elif isinstance(self.private_key, rsa.RSAPrivateKey):
            signature = self.private_key.sign(
                challenge_bytes,
                padding.PKCS1v15(),
                hashes.SHA256()
            )
        else:
            raise ValueError("Unsupported private key type")

        return base64.b64encode(signature).decode()

    def get_public_key_pem(self) -> str:
        """Получение публичного ключа в PEM формате"""
        public_key = self.private_key.public_key()
        return public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()


def generate_key_pair(key_type: str = "rsa") -> tuple[str, str]:
    """Генерация ключевой пары для хаба"""
    if key_type == "rsa":
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )
    elif key_type == "ec":
        private_key = ec.generate_private_key(ec.SECP256R1())
    else:
        raise ValueError("Unsupported key type")

    # Приватный ключ
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode()

    # Публичный ключ
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    return private_key_pem, public_key_pem
