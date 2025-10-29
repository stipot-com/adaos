from __future__ import annotations

import os
import base64
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.exceptions import InvalidSignature
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
        subject_attributes.append(
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    csr = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name(subject_attributes)).sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")


class PKIService:
    def __init__(self):
        # Убираем in-memory хранилище
        # self.challenges: Dict[str, Dict[str, Any]] = {}
        self.challenge_ttl = 300

    def register_hub(self, hub_id: str, public_key_pem: str, hub_name: str) -> Dict[str, Any]:
        """Регистрация нового хаба с публичным ключом"""
        print(f"🔐 Регистрация хаба: {hub_id}")

        # Валидируем публичный ключ
        try:
            public_key = serialization.load_pem_public_key(
                public_key_pem.encode()
            )
            if not isinstance(public_key, (ec.EllipticCurvePublicKey, rsa.RSAPublicKey)):
                raise ValueError("Поддерживаются только ECDSA или RSA ключи")
        except Exception as e:
            raise ValueError(f"Неверный формат публичного ключа: {e}")

        # Сохраняем в базу используя существующий интерфейс
        from adaos.adapters.db.sqlite import save_hub_registration

        save_hub_registration(
            hub_id=hub_id,
            public_key=public_key_pem,
            hub_name=hub_name,
            capabilities=["basic", "skills", "scenarios"],
            status="active"
        )

        print(f"✅ Хаб {hub_id} успешно зарегистрирован")
        return {"status": "registered", "hub_id": hub_id}

    def create_auth_challenge(self, hub_id: str) -> str:
        """Создание cryptographic challenge для хаба"""
        print(f"🔐 Создание challenge для хаба: {hub_id}")

        # Проверяем, что хаб зарегистрирован
        from adaos.adapters.db.sqlite import get_hub_registration
        hub = get_hub_registration(hub_id)
        if not hub:
            raise ValueError("Хаб не зарегистрирован")
        if hub.get("status") != "active":
            raise ValueError("Хаб неактивен")

        # Генерируем случайный challenge
        challenge_bytes = os.urandom(32)
        challenge_b64 = base64.b64encode(challenge_bytes).decode()

        # Сохраняем challenge в БД
        from adaos.adapters.db.sqlite import save_auth_challenge
        save_auth_challenge(hub_id, challenge_b64, self.challenge_ttl)

        print(f"✅ Challenge создан для хаба {hub_id}")
        return challenge_b64

    def verify_challenge_signature(self, hub_id: str, challenge: str, signature_b64: str) -> bool:
        """Верификация подписи challenge"""
        print(f"🔐 Верификация подписи для хаба: {hub_id}")

        # Получаем challenge из БД
        from adaos.adapters.db.sqlite import get_auth_challenge, delete_auth_challenge
        challenge_record = get_auth_challenge(hub_id)

        if not challenge_record:
            raise ValueError("Challenge не найден или истек")

        # Проверяем соответствие challenge
        if challenge_record['challenge'] != challenge:
            raise ValueError("Challenge не совпадает")

        # Получаем публичный ключ хаба
        from adaos.adapters.db.sqlite import get_hub_registration
        hub = get_hub_registration(hub_id)
        if not hub:
            raise ValueError("Хаб не найден")

        try:
            public_key = serialization.load_pem_public_key(
                hub['public_key'].encode()
            )

            # Верифицируем подпись
            signature = base64.b64decode(signature_b64)

            if isinstance(public_key, ec.EllipticCurvePublicKey):
                public_key.verify(
                    signature,
                    challenge.encode(),
                    ec.ECDSA(hashes.SHA256())
                )
            elif isinstance(public_key, rsa.RSAPublicKey):
                public_key.verify(
                    signature,
                    challenge.encode(),
                    padding.PKCS1v15(),
                    hashes.SHA256()
                )
            else:
                raise ValueError("Неподдерживаемый тип ключа")

            # Удаляем использованный challenge из БД
            delete_auth_challenge(hub_id)
            print(f"✅ Подпись верифицирована для хаба {hub_id}")
            return True

        except InvalidSignature:
            print(f"❌ Неверная подпись для хаба {hub_id}")
            return False
        except Exception as e:
            raise ValueError(f"Ошибка верификации: {e}")

    def create_auth_session(self, hub_id: str) -> Dict[str, Any]:
        """Создание сессии после успешной аутентификации"""
        print(f"🔐 Создание сессии для хаба: {hub_id}")

        session_token = base64.b64encode(os.urandom(32)).decode()

        from adaos.adapters.db.sqlite import save_auth_session

        save_auth_session(
            session_token=session_token,
            hub_id=hub_id,
            permissions=["api:read", "api:write", "repo:access"],
            ttl_hours=24
        )

        print(f"✅ Сессия создана для хаба {hub_id}")
        return {
            "session_token": session_token,
            "hub_id": hub_id,
            "permissions": ["api:read", "api:write", "repo:access"]
        }

    def generate_hub_certificate(self, hub_id: str, public_key_pem: str, hub_name: str) -> str:
        """Генерация сертификата для хаба"""
        print(f"🔐 Генерация сертификата для хаба: {hub_id}")

        try:
            # Загружаем CA из базы (используем существующий механизм)
            from adaos.adapters.db.sqlite import ca_load, ca_update_serial

            ca_data = ca_load()
            ca_key = serialization.load_pem_private_key(
                ca_data["ca_key_pem"].encode(),
                password=None
            )
            ca_cert = x509.load_pem_x509_certificate(
                ca_data["ca_cert_pem"].encode()
            )

            # Загружаем публичный ключ хаба
            public_key = serialization.load_pem_public_key(
                public_key_pem.encode())

            # Создаем сертификат
            subject = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, hub_name),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AdaOS Hub"),
            ])

            builder = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(ca_cert.subject)
                .public_key(public_key)
                .serial_number(ca_data["next_serial"])
                .not_valid_before(datetime.utcnow())
                .not_valid_after(datetime.utcnow() + timedelta(days=365))
                .add_extension(
                    x509.SubjectAlternativeName(
                        [x509.DNSName(f"{hub_id}.adaos")]),
                    critical=False,
                )
            )

            # Подписываем сертификат
            certificate = builder.sign(
                private_key=ca_key,
                algorithm=hashes.SHA256(),
            )

            # Обновляем серийный номер
            ca_update_serial(ca_data["next_serial"] + 1)

            cert_pem = certificate.public_bytes(
                serialization.Encoding.PEM).decode()
            print(f"✅ Сертификат сгенерирован для хаба {hub_id}")
            return cert_pem

        except Exception as e:
            raise ValueError(f"Ошибка генерации сертификата: {e}")

    def cleanup_expired_challenges(self):
        """Очистка просроченных challenges из БД"""
        from adaos.adapters.db.sqlite import cleanup_expired_challenges
        deleted_count = cleanup_expired_challenges()
        if deleted_count > 0:
            print(f"🧹 Очищено {deleted_count} просроченных challenges из БД")


# Создаем глобальный экземпляр сервиса для удобства использования
_pki_service: Optional[PKIService] = None


def get_pki_service() -> PKIService:
    """Получение глобального экземпляра PKI сервиса"""
    global _pki_service
    if _pki_service is None:
        _pki_service = PKIService()
    return _pki_service


__all__ = [
    "generate_rsa_key",
    "write_private_key",
    "write_pem",
    "make_csr",
    "PKIService",
    "get_pki_service"
]
