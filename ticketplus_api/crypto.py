"""TicketPlus 的 id 加解密（AES-128-CBC，金鑰硬寫在前端 bundle 裡）。

前端模組 "9263"：
    key = "ILOVEFETIXFETIX!"   iv = "!@#$FETIXEVENTiv"
    加密 → hex 字串（就是網址 /activity/<32 hex> 的那串）
    解密 → "e000001451" / "s000002136"

主線搶票流程用不到這兩支（S3 目錄已經給明文 productId / ticketAreaId），但診斷時
很好用：拿網址就能知道真正的 eventId，也能反查 CONFIG_API 回來的 sessionId 對應哪一場。
"""
_KEY = b"ILOVEFETIXFETIX!"
_IV = b"!@#$FETIXEVENTiv"
_BLOCK = 16


def _cipher():
    # 主線搶票流程用不到這支，所以 cryptography 沒裝也不該讓整個套件 import 失敗
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(_KEY), modes.CBC(_IV))


def decrypt_id(hex_str: str) -> str:
    """加密 id（hex）→ 明文 id。不是合法密文就原樣回傳（呼叫端可以無腦丟）。"""
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError:
        return hex_str
    if not raw or len(raw) % _BLOCK:
        return hex_str
    dec = _cipher().decryptor()
    plain = dec.update(raw) + dec.finalize()
    pad = plain[-1]
    if 1 <= pad <= _BLOCK:
        plain = plain[:-pad]
    return plain.decode("utf-8", "replace")


def encrypt_id(plain: str) -> str:
    """明文 id → 加密 id（hex）。用來組 SPA 網址（結帳頁吃加密 id）。"""
    data = plain.encode()
    pad = _BLOCK - len(data) % _BLOCK
    enc = _cipher().encryptor()
    return (enc.update(data + bytes([pad] * pad)) + enc.finalize()).hex()
