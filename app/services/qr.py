import asyncio
from functools import lru_cache
import io
import qrcode
from aiogram.types import BufferedInputFile

@lru_cache(maxsize=32)
def _generate_qr_sync(address: str) -> bytes:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(address)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()

async def make_address_qr(address: str) -> BufferedInputFile:
    data = await asyncio.to_thread(_generate_qr_sync, address)
    return BufferedInputFile(data, filename="pay.png")

def deep_link(network: str, address: str, amount: "Decimal", units: int) -> str | None:
    """Deep-link у гаманець.

    ВАЖЛИВО: Telegram приймає в URL-кнопках inline-клавіатури ТІЛЬКИ схеми
    http, https і tg. Схеми `ethereum:` (EIP-681) і `solana:` (Solana Pay)
    відхиляються з помилкою "Wrong HTTP URL", і повідомлення взагалі не
    надсилається. Тому кнопку «Відкрити в гаманці» ми не показуємо —
    користувач копіює адресу тапом або сканує QR.

    Функція лишається як точка розширення: якщо колись знадобиться, сюди
    можна повернути https-редиректор (напр. metamask.app.link), який уже
    є валідним HTTP-URL.
    """
    return None
