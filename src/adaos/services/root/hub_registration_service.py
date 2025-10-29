# services/root/hub_registration_service.py
import os
import base64
from typing import Dict, Any, Optional
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding

from adaos.services.root.client import RootHttpClient
from adaos.services.root.passwordless_client import PasswordlessAuthClient, generate_key_pair


class HubRegistrationService:
    """Сервис для управления регистрацией и аутентификацией хаба"""

    def __init__(self, root_client: RootHttpClient):
        self.root_client = root_client
        self.auth_client: Optional[PasswordlessAuthClient] = None

    def generate_keys(self, key_type: str = "rsa") -> tuple[str, str]:
        """Генерация ключевой пары для хаба"""
        return generate_key_pair(key_type)

    def register_hub(
        self,
        hub_id: str,
        hub_name: str,
        public_key_pem: str
    ) -> Dict[str, Any]:
        """Регистрация хаба на root сервере"""
        return self.root_client.hub_registration_init(
            hub_id=hub_id,
            hub_name=hub_name,
            public_key=public_key_pem
        )

    def authenticate_hub(
        self,
        hub_id: str,
        private_key_pem: str
    ) -> Dict[str, Any]:
        """Полная аутентификация хаба"""
        # Создаем клиент аутентификации
        self.auth_client = PasswordlessAuthClient(private_key_pem)

        # Запрашиваем challenge
        challenge_response = self.root_client.hub_auth_challenge(hub_id)
        challenge = challenge_response["challenge"]

        # Подписываем challenge
        signature = self.auth_client.sign_challenge(challenge)

        # Верифицируем и получаем сессию
        session_response = self.root_client.hub_auth_verify(
            hub_id=hub_id,
            challenge=challenge,
            signature=signature
        )

        return session_response

    def get_session_info(self, session_token: str) -> Dict[str, Any]:
        """Получение информации о сессии"""
        return self.root_client.hub_get_session(session_token)

    def complete_registration_flow(
        self,
        hub_id: str,
        hub_name: str,
        key_type: str = "rsa"
    ) -> Dict[str, Any]:
        """Полный цикл регистрации и аутентификации"""
        # Генерируем ключи
        private_key, public_key = self.generate_keys(key_type)

        # Регистрируем хаб
        registration_result = self.register_hub(hub_id, hub_name, public_key)

        # Аутентифицируем хаб
        auth_result = self.authenticate_hub(hub_id, private_key)

        return {
            "registration": registration_result,
            "authentication": auth_result,
            "private_key": private_key,
            "public_key": public_key
        }
