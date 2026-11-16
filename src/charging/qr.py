"""Malý generátor QR kódu (ISO/IEC 18004) bez další knihovny – pro odkaz na téma notifikací v profilu.

Umí jen to, co potřebujeme: byte mode, úroveň opravy chyb M (~15 %), verze 1–6 (do 106 bajtů),
výstup jako SVG. Postup: data -> kódová slova -> Reed-Solomon -> prokládání bloků -> matice
(vyhledávací, časovací a zarovnávací vzory) -> data v zigzagu -> maska s nejnižší penalizací -> formát.
"""
from __future__ import annotations

from html import escape
from itertools import cycle

# Úroveň M: (ECC kódových slov na blok, počet bloků) pro verze 1–6
_ECC_M = {1: (10, 1), 2: (16, 1), 3: (26, 1), 4: (18, 2), 5: (24, 2), 6: (16, 4)}
_FORMAT_ECC_M = 0  # format bits úrovně M (L=1, M=0, Q=3, H=2)

_MASKS = (
    lambda x, y: (x + y) % 2,
    lambda x, y: y % 2,
    lambda x, y: x % 3,
    lambda x, y: (x + y) % 3,
    lambda x, y: (x // 3 + y // 2) % 2,
    lambda x, y: x * y % 2 + x * y % 3,
    lambda x, y: (x * y % 2 + x * y % 3) % 2,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2,
)


def _raw_modules(version: int) -> int:
    """Počet modulů pro data + ECC (bez funkčních vzorů)."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        align = version // 7 + 2
        result -= (25 * align - 10) * align - 55
    return result


def _data_codewords(version: int) -> int:
    ecc, blocks = _ECC_M[version]
    return _raw_modules(version) // 8 - ecc * blocks


def _gf_mul(x: int, y: int) -> int:
    """Násobení v GF(2^8) s polynomem x^8 + x^4 + x^3 + x^2 + 1 (0x11D)."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def rs_remainder(data, divisor: list[int]) -> list[int]:
    """ECC kódová slova (zbytek po dělení generujícím polynomem)."""
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def format_bits(mask: int) -> int:
    """15 bitů formátu (úroveň M + maska) s BCH kódem a XOR maskou 0x5412."""
    data = _FORMAT_ECC_M << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


def _encode_data(payload: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    put(0b0100, 4)  # byte mode
    put(len(payload), 8)  # délka (verze 1–9)
    for byte in payload:
        put(byte, 8)
    capacity = _data_codewords(version) * 8
    put(0, min(4, capacity - len(bits)))  # terminátor
    put(0, -len(bits) % 8)
    for pad in cycle((0xEC, 0x11)):
        if len(bits) >= capacity:
            break
        put(pad, 8)
    return [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]


def _add_ecc_and_interleave(data: list[int], version: int) -> list[int]:
    ecc_len, blocks_count = _ECC_M[version]
    raw = _raw_modules(version) // 8
    short_blocks = blocks_count - raw % blocks_count
    short_len = raw // blocks_count
    divisor = rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(blocks_count):
        chunk = data[k:k + short_len - ecc_len + (0 if i < short_blocks else 1)]
        k += len(chunk)
        ecc = rs_remainder(chunk, divisor)
        if i < short_blocks:
            chunk = chunk + [0]
        blocks.append(chunk + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_len - ecc_len or j >= short_blocks:  # výplňový bajt krátkých bloků přeskočit
                result.append(block[i])
    return result


class _Matrix:
    def __init__(self, version: int):
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns(version)

    def set_function(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.function[y][x] = True

    def _draw_function_patterns(self, version: int) -> None:
        for i in range(self.size):  # časovací vzory
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (self.size - 4, 3), (3, self.size - 4)):  # vyhledávací vzory + okraj
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < self.size and 0 <= y < self.size:
                        self.set_function(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = [] if version == 1 else [6, self.size - 7]  # zarovnávací vzor (verze 2–6 má jeden)
        for i, px in enumerate(positions):
            for j, py in enumerate(positions):
                if (i, j) in ((0, 0), (0, len(positions) - 1), (len(positions) - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_function(px + dx, py + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)  # rezervuje místo pro formát

    def draw_format(self, mask: int) -> None:
        bits = format_bits(mask)
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        for i in range(6):
            self.set_function(8, i, bit(i))
        self.set_function(8, 7, bit(6))
        self.set_function(8, 8, bit(7))
        self.set_function(7, 8, bit(8))
        for i in range(9, 15):
            self.set_function(14 - i, 8, bit(i))
        for i in range(8):
            self.set_function(self.size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_function(8, self.size - 15 + i, bit(i))
        self.set_function(8, self.size - 8, True)  # tmavý modul

    def draw_codewords(self, codewords: list[int]) -> None:
        i, total = 0, len(codewords) * 8
        for right in range(self.size - 1, 0, -2):
            if right <= 6:
                right -= 1  # přeskočit svislý časovací vzor
            upward = (right + 1) & 2 == 0
            for vert in range(self.size):
                y = self.size - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not self.function[y][x] and i < total:
                        self.dark[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1

    def apply_mask(self, mask: int) -> None:
        pattern = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and pattern(x, y) == 0:
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        """Penalizace masky podle normy (běhy, bloky 2×2, vzory podobné vyhledávacím, poměr tmavých)."""
        score, n = 0, self.size
        for line in self.dark + [list(col) for col in zip(*self.dark)]:
            run = 1
            for a, b in zip(line, line[1:]):
                if a == b:
                    run += 1
                else:
                    score += run - 2 if run >= 5 else 0
                    run = 1
            score += run - 2 if run >= 5 else 0
            text = "".join("1" if v else "0" for v in line)
            score += 40 * (text.count("10111010000") + text.count("00001011101"))
        for y in range(n - 1):
            for x in range(n - 1):
                if self.dark[y][x] == self.dark[y][x + 1] == self.dark[y + 1][x] == self.dark[y + 1][x + 1]:
                    score += 3
        dark = sum(map(sum, self.dark))
        score += abs(dark * 20 - n * n * 10) // (n * n) * 10
        return score


def qr_matrix(text: str) -> list[list[bool]]:
    payload = text.encode("utf-8")
    version = next((v for v in _ECC_M if len(payload) <= _data_codewords(v) - 2), None)
    if version is None:
        raise ValueError("Text je pro QR kód verze 1–6 moc dlouhý.")
    matrix = _Matrix(version)
    matrix.draw_codewords(_add_ecc_and_interleave(_encode_data(payload, version), version))
    best, best_score = 0, None
    for mask in range(8):
        matrix.apply_mask(mask)
        matrix.draw_format(mask)
        score = matrix.penalty()
        if best_score is None or score < best_score:
            best, best_score = mask, score
        matrix.apply_mask(mask)  # vrátit zpět (XOR)
    matrix.apply_mask(best)
    matrix.draw_format(best)
    return matrix.dark


def qr_svg(text: str, border: int = 4) -> str:
    """QR kód jako SVG (černé moduly na bílém, i v tmavém režimu – jinak ho telefon nepřečte)."""
    dark = qr_matrix(text)
    full = len(dark) + 2 * border
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(dark) for x, on in enumerate(row) if on)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {full} {full}" class="qr" role="img" '
            f'aria-label="QR kód: {escape(text)}" shape-rendering="crispEdges"><rect width="{full}" height="{full}" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')
