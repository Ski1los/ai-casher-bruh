"""Ключ Solana и подпись транзакций — без тяжёлых библиотек.

Ключ берётся ТОЛЬКО из переменной окружения хостинга (SOLANA_TRADER_KEY): в репозиторий, чат и логи он
не попадает. Формат — как в Phantom «Показать приватный ключ» (base58, 64 байта) или массив из 64 чисел
(файл solana-keygen).

Подпись: Ed25519 из библиотеки cryptography (она уже стоит вместе с ccxt).
Транзакции собирает Jupiter, мы только ставим свою подпись в нужное место. Сами собираем лишь одну
простую транзакцию: закрыть пустые счета токенов и вернуть их залог (~0,002 SOL за каждый).
"""

from __future__ import annotations

import json
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(B58)}


class BadKey(ValueError):
    """Ключ не подходит. Текст ошибки никогда не содержит сам ключ."""


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(text: str) -> bytes:
    n = 0
    for c in text:
        if c not in _B58_INDEX:
            raise ValueError("не base58")
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def shortvec_encode(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def shortvec_decode(buf: bytes, off: int) -> tuple[int, int]:
    n = shift = 0
    for i in range(3):
        b = buf[off + i]
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, off + i + 1
    raise ValueError("битая длина в транзакции")


class Keypair:
    """Пара ключей. repr/str не показывают секрет — его нельзя случайно напечатать в лог."""

    __slots__ = ("_sk", "public")

    def __init__(self, seed: bytes) -> None:
        if len(seed) != 32:
            raise BadKey("нужен ключ из 32 или 64 байт")
        self._sk = Ed25519PrivateKey.from_private_bytes(seed)
        self.public = self._sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @classmethod
    def from_secret(cls, text: str) -> "Keypair":
        text = (text or "").strip().strip("'\"").strip()
        if not text:
            raise BadKey("ключ пустой")
        if text.startswith("["):
            try:
                raw = bytes(json.loads(text))
            except Exception:
                raise BadKey("массив ключа не читается: нужно 64 числа от 0 до 255") from None
        else:
            try:
                raw = b58decode(text)
            except ValueError:
                raise BadKey("в ключе есть лишние символы. Скопируй его из Phantom целиком, без пробелов") from None
        if len(raw) == 64:
            kp = cls(raw[:32])
            if kp.public != raw[32:]:
                raise BadKey("ключ повреждён (вторая половина не совпадает с адресом). Скопируй заново")
            return kp
        if len(raw) == 32:
            return cls(raw)
        raise BadKey(f"это не приватный ключ Solana ({len(raw)} байт вместо 64). Нужен «Приватный ключ» "
                        "из Phantom, а не адрес и не сид-фраза")

    @property
    def address(self) -> str:
        return b58encode(self.public)

    def sign(self, message: bytes) -> bytes:
        return self._sk.sign(message)

    def __repr__(self) -> str:
        return f"Keypair({self.address})"

    __str__ = __repr__


def message_keys(message: bytes) -> tuple[int, list[bytes]]:
    """(сколько подписей нужно, статические адреса) из сообщения транзакции (legacy или v0)."""
    p = 0
    if message[0] & 0x80:
        version = message[0] & 0x7F
        if version != 0:
            raise ValueError(f"незнакомая версия транзакции v{version}")
        p = 1
    required = message[p]
    p += 3
    count, p = shortvec_decode(message, p)
    keys = [message[p + 32 * i: p + 32 * (i + 1)] for i in range(count)]
    return required, keys


def sign_transaction(tx: bytes, kp: Keypair) -> bytes:
    """Ставит нашу подпись в слот нашего адреса. Остальные слоты (например, маркет-мейкера JupiterZ)
    не трогаем: их заполнит Jupiter при исполнении."""
    n, off = shortvec_decode(tx, 0)
    msg_start = off + 64 * n
    message = tx[msg_start:]
    required, keys = message_keys(message)
    try:
        idx = keys.index(kp.public)
    except ValueError:
        raise ValueError("в транзакции нет адреса кошелька бота") from None
    if idx >= required or idx >= n:
        raise ValueError("кошелёк бота не должен подписывать эту транзакцию")
    sig = kp.sign(message)
    start = off + 64 * idx
    return tx[:start] + sig + tx[start + 64:]


CLOSE_ACCOUNT = 9  # инструкция CloseAccount одинакова в Token и Token-2022


def close_accounts_tx(kp: Keypair, accounts: list[tuple[str, str]], blockhash: str) -> bytes:
    """Транзакция «закрыть пустые счета токенов, залог вернуть себе». accounts: [(счёт, программа токена)]."""
    if not accounts:
        raise ValueError("нечего закрывать")
    programs = sorted({prog for _, prog in accounts})
    keys = [kp.public] + [b58decode(a) for a, _ in accounts] + [b58decode(p) for p in programs]
    if any(len(k) != 32 for k in keys):
        raise ValueError("битый адрес")
    bh = b58decode(blockhash)
    if len(bh) != 32:
        raise ValueError("битый blockhash")
    msg = bytearray([1, 0, len(programs)])  # 1 подпись, подписант пишет; программы только читаются
    msg += shortvec_encode(len(keys)) + b"".join(keys) + bh
    msg += shortvec_encode(len(accounts))
    for i, (_, prog) in enumerate(accounts):
        msg += bytes([1 + len(accounts) + programs.index(prog)])
        msg += shortvec_encode(3) + bytes([1 + i, 0, 0])  # счёт, куда вернуть залог, владелец
        msg += shortvec_encode(1) + bytes([CLOSE_ACCOUNT])
    msg = bytes(msg)
    return shortvec_encode(1) + kp.sign(msg) + msg


# ---------------------------------------------------------------------------
# Защита от утечки: человек случайно прислал ключ или сид-фразу в чат
# ---------------------------------------------------------------------------

_SEED_RE = re.compile(r"^(?:[a-z]{3,8}\s+){11,23}[a-z]{3,8}$")


def looks_like_secret(text: str) -> bool:
    """Похоже на приватный ключ Solana (base58/массив из 64 байт) или сид-фразу (12–24 английских слова)."""
    text = (text or "").strip()
    if not text:
        return False
    words = text.split()
    if len(words) in (12, 15, 18, 21, 24) and _SEED_RE.match(" ".join(words)):
        return True
    for tok in re.findall(r"[1-9A-HJ-NP-Za-km-z]{80,90}", text):
        try:
            if len(b58decode(tok)) in (64,):
                return True
        except ValueError:
            pass
    m = re.search(r"\[\s*\d{1,3}(?:\s*,\s*\d{1,3}){63}\s*\]", text)
    return bool(m)
