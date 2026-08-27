from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from telethon import TelegramClient, errors, functions, types
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.network.connection.tcpobfuscated import ConnectionTcpObfuscated
from telethon.sessions import StringSession

from .config import Settings
from .crypto import SecretBox
from .database import Database

PRIVATE_CHANNEL_LINK = re.compile(r"(?:https?://)?t\.me/c/(\d+)(?:/\d+)?/?$", re.IGNORECASE)
PUBLIC_LINK = re.compile(r"(?:https?://)?t\.me/([A-Za-z][\w\d_]{3,})(?:/\d+)?/?$", re.IGNORECASE)
INVITE_LINK = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)", re.IGNORECASE)


class TelegramGatewayError(RuntimeError):
    pass


class ProfileNotConnectedError(TelegramGatewayError):
    pass


@dataclass(slots=True)
class LoginChallenge:
    owner_id: int
    profile_id: int
    phone: str
    phone_code_hash: str
    expires_at: datetime
    client: TelegramClient
    requires_password: bool = False


class TelegramGateway:
    """Creates short-lived Telethon clients from encrypted StringSession values."""

    def __init__(self, settings: Settings, database: Database, secret_box: SecretBox) -> None:
        self.settings = settings
        self.database = database
        self.secret_box = secret_box
        self._challenges: dict[int, LoginChallenge] = {}
        self._challenge_lock = asyncio.Lock()

    def _new_client(self, session: str = "") -> TelegramClient:
        mtproxy = self.settings.mtproxy
        return TelegramClient(
            StringSession(session),
            self.settings.telegram_api_id,
            self.settings.telegram_api_hash,
            device_model="Telegram Media Mirror Bot",
            system_version="Linux",
            app_version="0.1.0",
            connection=ConnectionTcpMTProxyRandomizedIntermediate if mtproxy else ConnectionTcpObfuscated,
            proxy=mtproxy,
            connection_retries=10,
            retry_delay=3,
            auto_reconnect=True,
        )

    async def begin_login(self, owner_id: int, phone: str) -> str:
        normalized = re.sub(r"[\s()-]", "", phone)
        if not re.fullmatch(r"\+?\d{7,15}", normalized):
            raise TelegramGatewayError("Enter a valid phone number in international format, e.g. +15551234567.")
        profile_id = self.database.ensure_profile(owner_id)
        previous = self._challenges.pop(owner_id, None)
        if previous:
            await previous.client.disconnect()
        client = self._new_client()
        try:
            await client.connect()
            sent = await client.send_code_request(normalized)
            async with self._challenge_lock:
                # Keep this exact MTProto connection open until code/password
                # completion. This avoids invalidating an active login challenge.
                self._challenges[owner_id] = LoginChallenge(
                    owner_id=owner_id,
                    profile_id=profile_id,
                    phone=normalized,
                    phone_code_hash=sent.phone_code_hash,
                    expires_at=datetime.now(UTC) + timedelta(minutes=10),
                    client=client,
                )
        except errors.PhoneNumberInvalidError as exc:
            await client.disconnect()
            raise TelegramGatewayError("Telegram rejected that phone number.") from exc
        except errors.FloodWaitError as exc:
            await client.disconnect()
            raise TelegramGatewayError(f"Telegram asked to wait {exc.seconds} seconds before requesting another code.") from exc
        except Exception:
            await client.disconnect()
            raise
        return self.mask_phone(normalized)

    async def finish_login_code(self, owner_id: int, code: str) -> bool:
        challenge = self._get_challenge(owner_id)
        if challenge.requires_password:
            raise TelegramGatewayError("This login needs its two-step-verification password. Use the password step.")
        clean_code = re.sub(r"\D", "", code)
        if not 4 <= len(clean_code) <= 8:
            raise TelegramGatewayError("Enter the numeric Telegram login code.")
        client = challenge.client
        try:
            await client.sign_in(challenge.phone, clean_code, phone_code_hash=challenge.phone_code_hash)
        except errors.SessionPasswordNeededError:
            challenge.requires_password = True
            return False
        except errors.PhoneCodeInvalidError as exc:
            raise TelegramGatewayError("The login code is invalid.") from exc
        except errors.PhoneCodeExpiredError as exc:
            self._challenges.pop(owner_id, None)
            await client.disconnect()
            raise TelegramGatewayError("Telegram expired this code. Start connection again for a new code.") from exc
        except errors.FloodWaitError as exc:
            raise TelegramGatewayError(f"Telegram asked to wait {exc.seconds} seconds before trying again.") from exc
        else:
            await self._store_connected_session(challenge.profile_id, challenge.phone, client)
            self._challenges.pop(owner_id, None)
            await client.disconnect()
            return True

    async def finish_login_password(self, owner_id: int, password: str) -> None:
        challenge = self._get_challenge(owner_id)
        if not challenge.requires_password:
            raise TelegramGatewayError("Enter the Telegram login code first.")
        if not password:
            raise TelegramGatewayError("Two-step-verification password cannot be empty.")
        client = challenge.client
        try:
            await client.sign_in(password=password)
            await self._store_connected_session(challenge.profile_id, challenge.phone, client)
            self._challenges.pop(owner_id, None)
            await client.disconnect()
        except errors.PasswordHashInvalidError as exc:
            raise TelegramGatewayError("The two-step-verification password is invalid.") from exc

    def _get_challenge(self, owner_id: int) -> LoginChallenge:
        challenge = self._challenges.get(owner_id)
        if challenge is None or challenge.expires_at < datetime.now(UTC):
            self._challenges.pop(owner_id, None)
            raise TelegramGatewayError("No active login request. Start the connection again.")
        return challenge

    async def _store_connected_session(self, profile_id: int, phone: str, client: TelegramClient) -> None:
        session_text = StringSession.save(client.session)
        if not session_text:
            raise TelegramGatewayError("Telegram session was not created; please retry login.")
        self.database.save_profile_session(
            profile_id,
            self.secret_box.encrypt(session_text),
            self.mask_phone(phone),
        )

    @staticmethod
    def mask_phone(phone: str) -> str:
        digits = re.sub(r"\D", "", phone)
        return f"{'*' * max(0, len(digits) - 4)}{digits[-4:]}"

    def has_session(self, owner_id: int) -> bool:
        profile = self.database.profile_for_owner(owner_id)
        return bool(profile and profile["session_encrypted"])

    def default_profile_id(self, owner_id: int) -> int:
        return self.database.ensure_profile(owner_id)

    @asynccontextmanager
    async def client_for_profile(self, profile_id: int) -> AsyncIterator[TelegramClient]:
        profile = self.database.profile_by_id(profile_id)
        if profile is None or not profile["session_encrypted"]:
            raise ProfileNotConnectedError("No connected Telegram worker session. Use Connect Account first.")
        session_text = self.secret_box.decrypt(profile["session_encrypted"])
        client = self._new_client(session_text)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ProfileNotConnectedError("Saved Telegram worker session is no longer authorized. Connect again.")
            yield client
        finally:
            await client.disconnect()

    async def resolve_entity(self, client: TelegramClient, reference: str):
        reference = reference.strip()
        if not reference:
            raise TelegramGatewayError("A source/destination reference is required.")
        try:
            if reference.lstrip("-").isdigit():
                try:
                    return await client.get_entity(int(reference))
                except ValueError:
                    # StringSession does not persist the entity cache. Refresh dialogs
                    # before resolving a numeric private chat/channel ID.
                    await client.get_dialogs()
                    return await client.get_entity(int(reference))
            private_match = PRIVATE_CHANNEL_LINK.fullmatch(reference)
            if private_match:
                return await client.get_entity(int(f"-100{private_match.group(1)}"))
            public_match = PUBLIC_LINK.fullmatch(reference)
            if public_match:
                return await client.get_entity(public_match.group(1))
            invite_match = INVITE_LINK.match(reference)
            if invite_match:
                result = await client(functions.messages.CheckChatInviteRequest(hash=invite_match.group(1)))
                if isinstance(result, types.ChatInviteAlready):
                    return result.chat
                raise TelegramGatewayError(
                    "The account is not already a member of this invite-only chat, so it cannot be used as a source/destination."
                )
            return await client.get_entity(reference)
        except TelegramGatewayError:
            raise
        except (ValueError, TypeError, errors.RPCError) as exc:
            raise TelegramGatewayError(f"Unable to resolve Telegram chat: {self._safe_rpc_error(exc)}") from exc

    async def preflight(self, profile_id: int, source_ref: str, destination_ref: str):
        async with self.client_for_profile(profile_id) as client:
            source = await self.resolve_entity(client, source_ref)
            destination = await self.resolve_entity(client, destination_ref)
            source_id = int(source.id)
            destination_id = int(destination.id)
            if source_id == destination_id:
                raise TelegramGatewayError("Source and destination cannot be the same chat.")
            try:
                # Fetching one message verifies basic visibility/history access without modifying either chat.
                await client.get_messages(source, limit=1)
            except errors.RPCError as exc:
                raise TelegramGatewayError("The worker account cannot read this source's history.") from exc
            await self._assert_can_send_media(client, destination)
            return source, destination

    async def latest_message_id(self, profile_id: int, source_ref: str) -> int:
        """Return the current source tail for a new-files-only project baseline."""
        async with self.client_for_profile(profile_id) as client:
            source = await self.resolve_entity(client, source_ref)
            latest = await client.get_messages(source, limit=1)
            if not latest:
                return 0
            return int(latest[0].id)

    async def forum_topics(self, profile_id: int, source_ref: str) -> list[dict[str, int | str | None]]:
        """List accessible forum topics for project setup selection."""
        async with self.client_for_profile(profile_id) as client:
            source = await self.resolve_entity(client, source_ref)
            if not getattr(source, "forum", False):
                return []
            result = await client(
                functions.messages.GetForumTopicsRequest(
                    peer=source,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                    q=None,
                )
            )
            topics: list[dict[str, int | str | None]] = []
            for topic in getattr(result, "topics", []):
                topic_id = getattr(topic, "id", None)
                if topic_id is None:
                    continue
                topics.append(
                    {
                        "id": int(topic_id),
                        "title": str(getattr(topic, "title", None) or f"Topic {topic_id}"),
                        "icon_color": getattr(topic, "icon_color", None),
                        "icon_emoji_id": getattr(topic, "icon_emoji_id", None),
                    }
                )
            return topics

    async def _assert_can_send_media(self, client: TelegramClient, destination: object) -> None:
        try:
            permissions = await client.get_permissions(destination, "me")
        except errors.RPCError:
            # Telegram does not expose a uniform permission object for every chat type.
            # Actual send failures are still recorded by the worker with clear error context.
            return
        banned = getattr(permissions, "banned_rights", None)
        if banned and (getattr(banned, "send_media", False) or getattr(banned, "send_messages", False)):
            raise TelegramGatewayError("The worker account is not allowed to send media in the destination.")

    @staticmethod
    def entity_name(entity: object) -> str:
        title = getattr(entity, "title", None)
        if title:
            return str(title)
        username = getattr(entity, "username", None)
        if username:
            return f"@{username}"
        first_name = getattr(entity, "first_name", None)
        if first_name:
            return str(first_name)
        return str(getattr(entity, "id", "Unknown chat"))

    @staticmethod
    def _safe_rpc_error(exc: BaseException) -> str:
        text = str(exc).replace("\n", " ")
        return text[:240] or exc.__class__.__name__
