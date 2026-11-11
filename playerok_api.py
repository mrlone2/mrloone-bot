"""
playerok_api.py — Асинхронный клиент для взаимодействия с маркетплейсом Playerok через aiohttp.

Включает:
  - Проверку прокси (check_proxy)
  - Получение данных профиля, рейтинга и баланса (get_profile, get_balance)
  - Получение списка активных / оплаченных заказов (get_active_orders)
  - Получение входящих сообщений от покупателей (get_incoming_messages)
  - Отправку сообщений в чат сделки (send_message)
  - Защиту от банов: случайная пауза 2–6 секунд перед каждым действием
    и ротация реалистичных браузерных User-Agent заголовков.

ПРИМЕЧАНИЕ: Playerok не предоставляет публичного официального API и работает через
внутренний GraphQL (https://playerok.com/graphql) по cookie-сессии. Если маркетплейс
обновит названия GraphQL-полей, скорректируйте запросы в константах ниже,
сверившись со вкладкой Network -> graphql в DevTools вашего браузера.
"""

import asyncio
from dataclasses import dataclass
import json
import logging
import random
from typing import Any
import aiohttp

logger = logging.getLogger("mrloone.playerok")

PLAYEROK_GRAPHQL_URL = "https://playerok.com/graphql"
PLAYEROK_ORIGIN = "https://playerok.com"

# Набор реалистичных современных User-Agent для маскировки под обычный браузер
USER_AGENTS: tuple[str, ...] = (
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
)


@dataclass
class PlayerokProfile:
    """Данные профиля продавца на Playerok."""

    username: str = "Не авторизован"
    balance: float = 0.0
    frozen_balance: float = 0.0
    total_revenue: float = 0.0
    rating: float = 0.0
    reviews_count: int = 0
    authorized: bool = False
    error: str | None = None


@dataclass
class PlayerokOrder:
    """Модель заказа на Playerok."""

    order_id: str
    product_id: str
    product_title: str
    buyer_username: str
    chat_id: str
    amount: float
    status: str  # Ожидаем "Оплачено" / "PAID"


@dataclass
class PlayerokMessage:
    """Модель входящего сообщения покупателя в чате сделки."""

    message_id: str
    chat_id: str
    buyer_username: str
    text: str
    order_id: str = ""


# Шаблоны GraphQL-запросов Playerok
GQL_ME_QUERY = """
query MeProfile {
  viewer {
    id
    username
    rating
    reviewsCount
    balance {
      available
      frozen
    }
    stats {
      totalRevenue
    }
  }
}
"""

GQL_DEALS_QUERY = """
query SellerDeals($first: Int!) {
  deals(first: $first, role: SELLER) {
    edges {
      node {
        id
        status
        price
        item {
          id
          name
        }
        user {
          username
        }
        chat {
          id
        }
      }
    }
  }
}
"""

GQL_CHATS_QUERY = """
query SellerChats($first: Int!) {
  chats(first: $first) {
    edges {
      node {
        id
        lastMessage {
          id
          text
          user {
            id
            username
          }
        }
        participants {
          id
          username
        }
      }
    }
  }
}
"""

GQL_SEND_MESSAGE_MUTATION = """
mutation CreateChatMessage($input: CreateChatMessageInput!) {
  createChatMessage(input: $input) {
    id
    text
  }
}
"""


GQL_ME_MINIMAL_QUERY = """
query MeMinimal {
  viewer {
    id
    username
  }
}
"""


def parse_cookies_input(raw: str) -> str:
    """
    Приводит ввод пользователя к стандартной строке Cookie (`k=v; k2=v2`).
    Поддерживает:
      1. JSON-массив или словарь из расширений Cookie-Editor / EditThisCookie
      2. Строку вида `Cookie: token=...; __ddg3=...` или `token=...; ...`
      3. Чистое значение cookie `token` без имени ключа
    """
    text = raw.strip()
    if not text:
        return ""

    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()

    # Поддержка JSON-экспорта из расширений браузера
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                pairs = [
                    f"{item['name']}={item['value']}"
                    for item in parsed
                    if isinstance(item, dict) and item.get("name") and "value" in item
                ]
                if pairs:
                    return "; ".join(pairs)
            elif isinstance(parsed, dict):
                pairs = [f"{k}={v}" for k, v in parsed.items() if k and v is not None]
                if pairs:
                    return "; ".join(pairs)
        except json.JSONDecodeError:
            pass

    # Если пользователь вставил просто сам токен без `=` и `;`
    if "=" not in text and ";" not in text:
        return f"token={text}"

    # Убираем случайные переносы строк
    parts = [part.strip() for part in text.replace("\n", ";").split(";") if "=" in part]
    return "; ".join(parts) if parts else text


class PlayerokAPI:
    """Асинхронный клиент Playerok с антифрод-защитой и поддержкой прокси."""

    def __init__(
        self,
        cookies: str,
        proxy_url: str | None = None,
        mock_mode: bool = False,
    ) -> None:
        self.cookies = cookies
        self.proxy_url = proxy_url
        self.mock_mode = mock_mode
        self._session: aiohttp.ClientSession | None = None
        # Блокировка гарантирует, что запросы к Playerok не пойдут параллельно
        # и каждый запрос выдержит случайную паузу 2–6 секунд.
        self._request_lock = asyncio.Lock()
        self._cached_user_id: str | None = None

    def update_cookies(self, cookies: str) -> None:
        """Обновляет cookie-сессию на лету без перезапуска бота."""
        self.cookies = parse_cookies_input(cookies)
        self._cached_user_id = None

    async def start(self) -> None:
        """Инициализирует общую асинхронную HTTP-сессию aiohttp."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        """Закрывает HTTP-сессию при остановке бота."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def _build_headers(self) -> dict[str, str]:
        """Формирует реалистичные заголовки браузера и cookie авторизации."""
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "Origin": PLAYEROK_ORIGIN,
            "Referer": f"{PLAYEROK_ORIGIN}/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if self.cookies:
            headers["Cookie"] = self.cookies
        return headers

    async def _antifraud_delay(self) -> None:
        """
        Защита от банов: случайная пауза от 2 до 6 секунд перед каждым действием
        на Playerok, чтобы имитировать поведение человека.
        """
        delay = random.uniform(2.0, 6.0)
        logger.debug("Антифрод-пауза перед запросом к Playerok: %.2f сек.", delay)
        await asyncio.sleep(delay)

    async def _graphql_request(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Выполняет GraphQL-запрос к Playerok с антифрод-паузой и обработкой ошибок."""
        await self.start()
        assert self._session is not None

        async with self._request_lock:
            await self._antifraud_delay()

            payload = {
                "query": query,
                "variables": variables or {},
            }

            try:
                async with self._session.post(
                    PLAYEROK_GRAPHQL_URL,
                    json=payload,
                    headers=self._build_headers(),
                    proxy=self.proxy_url,
                ) as resp:
                    if resp.status in (401, 403):
                        text = await resp.text()
                        logger.warning("Ошибка авторизации Playerok HTTP %s: %s", resp.status, text[:200])
                        return {"errors": [{"message": f"HTTP {resp.status}: проверьте cookies/токен"}]}

                    if resp.status >= 400:
                        text = await resp.text()
                        logger.error("Ошибка Playerok HTTP %s: %s", resp.status, text[:200])
                        return {"errors": [{"message": f"HTTP {resp.status}"}]}

                    data = await resp.json(content_type=None)
                    if isinstance(data, dict) and data.get("errors"):
                        logger.warning("GraphQL ошибки Playerok: %s", data["errors"])
                    return data if isinstance(data, dict) else {}

            except aiohttp.ClientProxyConnectionError as exc:
                logger.error("Ошибка подключения к прокси: %s", exc)
                return {"errors": [{"message": f"Прокси недоступен: {exc}"}]}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.error("Сетевая ошибка при запросе к Playerok: %s", exc)
                return {"errors": [{"message": f"Сетевая ошибка: {exc}"}]}

    # ------------------------------------------------------------------
    # Публичные методы API
    # ------------------------------------------------------------------

    async def check_proxy(self) -> dict[str, Any]:
        """
        Проверяет работоспособность настроенного прокси.
        Возвращает словарь с полями ok (bool) и status_text ('✅ работает' / '❌ ошибка').
        """
        if self.mock_mode:
            return {
                "ok": True,
                "configured": bool(self.proxy_url),
                "status_text": "✅ работает",
                "detail": "Тестовый режим (MOCK_MODE=true)",
            }

        if not self.proxy_url:
            return {
                "ok": False,
                "configured": False,
                "status_text": "❌ ошибка",
                "detail": "Прокси не указан в .env (PROXY_URL пуст, работа напрямую)",
            }

        await self.start()
        assert self._session is not None

        try:
            async with self._session.get(
                PLAYEROK_ORIGIN,
                headers=self._build_headers(),
                proxy=self.proxy_url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status < 500:
                    return {
                        "ok": True,
                        "configured": True,
                        "status_text": "✅ работает",
                        "detail": f"HTTP {resp.status}",
                    }
                return {
                    "ok": False,
                    "configured": True,
                    "status_text": "❌ ошибка",
                    "detail": f"HTTP {resp.status}",
                }
        except Exception as exc:
            logger.warning("Проверка прокси не пройдена: %s", exc)
            return {
                "ok": False,
                "configured": True,
                "status_text": "❌ ошибка",
                "detail": str(exc),
            }

    async def get_profile(self) -> PlayerokProfile:
        """Получает данные текущего профиля продавца, баланс, рейтинг и отзывы."""
        if self.mock_mode:
            return PlayerokProfile(
                username="mrloone_seller (TEST)",
                balance=1450.0,
                frozen_balance=300.0,
                total_revenue=12850.0,
                rating=5.0,
                reviews_count=42,
                authorized=True,
            )

        if not self.cookies:
            return PlayerokProfile(
                username="Не подключён",
                authorized=False,
                error="Нажмите «🔐 Подключить Playerok» в меню Профиль или Настройки.",
            )

        resp = await self._graphql_request(GQL_ME_QUERY)
        if resp.get("errors"):
            # Если часть расширенных полей отличается в текущей схеме, пробуем минимальный запрос
            minimal_resp = await self._graphql_request(GQL_ME_MINIMAL_QUERY)
            if not minimal_resp.get("errors") and (minimal_resp.get("data") or {}).get("viewer"):
                resp = minimal_resp
            else:
                err_msg = resp["errors"][0].get("message", "Ошибка GraphQL")
                return PlayerokProfile(
                    username="Ошибка подключения",
                    authorized=False,
                    error=err_msg,
                )

        viewer = (resp.get("data") or {}).get("viewer")
        if not viewer:
            return PlayerokProfile(
                username="Сессия истекла",
                authorized=False,
                error="Профиль не найден — обновите cookies Playerok",
            )

        self._cached_user_id = str(viewer.get("id", ""))
        raw_balance = viewer.get("balance")
        if isinstance(raw_balance, dict):
            avail_bal = float(raw_balance.get("available") or 0.0)
            frozen_bal = float(raw_balance.get("frozen") or 0.0)
        elif isinstance(raw_balance, (int, float)):
            avail_bal = float(raw_balance)
            frozen_bal = 0.0
        else:
            avail_bal = 0.0
            frozen_bal = 0.0

        stats_info = viewer.get("stats") or {}

        return PlayerokProfile(
            username=str(viewer.get("username") or "Продавец"),
            balance=avail_bal,
            frozen_balance=frozen_bal,
            total_revenue=float(stats_info.get("totalRevenue") or 0.0),
            rating=float(viewer.get("rating") or 0.0),
            reviews_count=int(viewer.get("reviewsCount") or 0),
            authorized=True,
        )

    async def get_balance(self) -> dict[str, Any]:
        """Возвращает данные по доступному и замороженному балансу."""
        profile = await self.get_profile()
        return {
            "authorized": profile.authorized,
            "available": profile.balance,
            "frozen": profile.frozen_balance,
            "error": profile.error,
        }

    async def get_active_orders(self, limit: int = 20) -> list[PlayerokOrder]:
        """
        Запрашивает список активных сделок продавца и возвращает заказы
        со статусом 'Оплачено' (PAID).
        """
        if self.mock_mode:
            return []

        if not self.cookies:
            return []

        resp = await self._graphql_request(GQL_DEALS_QUERY, {"first": limit})
        if resp.get("errors"):
            return []

        edges = (((resp.get("data") or {}).get("deals") or {}).get("edges")) or []
        paid_orders: list[PlayerokOrder] = []

        for edge in edges:
            node = (edge or {}).get("node") or {}
            raw_status = str(node.get("status") or "").strip().upper()
            # Поддерживаем как англ. статус GraphQL ('PAID'), так и русское представление
            if raw_status not in {"PAID", "ОПЛАЧЕНО"}:
                continue

            item = node.get("item") or {}
            buyer = node.get("user") or {}
            chat = node.get("chat") or {}

            order_id = str(node.get("id") or "")
            if not order_id:
                continue

            paid_orders.append(
                PlayerokOrder(
                    order_id=order_id,
                    product_id=str(item.get("id") or ""),
                    product_title=str(item.get("name") or "Товар Playerok"),
                    buyer_username=str(buyer.get("username") or "Покупатель"),
                    chat_id=str(chat.get("id") or ""),
                    amount=float(node.get("price") or 0.0),
                    status="Оплачено",
                )
            )

        return paid_orders

    async def get_incoming_messages(self, limit: int = 15) -> list[PlayerokMessage]:
        """
        Получает последние сообщения из чатов с покупателями.
        Фильтрует сообщения, отправленные не самим продавцом.
        """
        if self.mock_mode or not self.cookies:
            return []

        resp = await self._graphql_request(GQL_CHATS_QUERY, {"first": limit})
        if resp.get("errors"):
            return []

        edges = (((resp.get("data") or {}).get("chats") or {}).get("edges")) or []
        messages: list[PlayerokMessage] = []

        for edge in edges:
            chat = (edge or {}).get("node") or {}
            chat_id = str(chat.get("id") or "")
            last_msg = chat.get("lastMessage") or {}
            msg_id = str(last_msg.get("id") or "")
            text = str(last_msg.get("text") or "").strip()
            author = last_msg.get("user") or {}
            author_id = str(author.get("id") or "")
            author_name = str(author.get("username") or "Покупатель")

            if not chat_id or not msg_id or not text:
                continue

            # Пропускаем наши собственные исходящие сообщения
            if self._cached_user_id and author_id == self._cached_user_id:
                continue

            messages.append(
                PlayerokMessage(
                    message_id=msg_id,
                    chat_id=chat_id,
                    buyer_username=author_name,
                    text=text,
                )
            )

        return messages

    async def send_message(self, chat_id: str, text: str) -> bool:
        """
        Отправляет сообщение в чат сделки на Playerok.
        Перед отправкой автоматически выдерживается пауза 2–6 секунд.
        """
        if not chat_id or not text.strip():
            return False

        if self.mock_mode:
            await self._antifraud_delay()
            logger.info("[MOCK] Отправлено сообщение в чат %s: %s", chat_id, text[:80])
            return True

        if not self.cookies:
            logger.warning("Невозможно отправить сообщение: не указаны cookies Playerok.")
            return False

        variables = {
            "input": {
                "chatId": chat_id,
                "text": text,
            }
        }
        resp = await self._graphql_request(GQL_SEND_MESSAGE_MUTATION, variables)
        if resp.get("errors"):
            logger.error("Не удалось отправить сообщение в чат %s: %s", chat_id, resp["errors"])
            return False

        created = (resp.get("data") or {}).get("createChatMessage")
        return bool(created)
