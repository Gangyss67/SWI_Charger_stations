"""QR kód pro odkaz na téma notifikací. Kontrolní hodnoty jsou z normy / známého příkladu „HELLO WORLD“ 1-M,
navíc QR kód zpětně přečteme z matice (formát, maska, Reed-Solomon, data)."""
import pytest

from charging.qr import _ECC_M, _MASKS, format_bits, qr_matrix, qr_svg, rs_divisor, rs_remainder


def test_reed_solomon_matches_hello_world_1m_example():
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    assert rs_remainder(data, rs_divisor(10)) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]


def test_format_bits_level_m_match_standard_table():
    table = ["101010000010010", "101000100100101", "101111001111100", "101101101001011",
             "100010111111001", "100000011001110", "100111110010111", "100101010100000"]
    assert [format(format_bits(mask), "015b") for mask in range(8)] == table


def read_back(dark):
    """Zjednodušená čtečka pro 1 blok (verze 1–3): formát -> maska -> zigzag -> RS kontrola -> data."""
    size = len(dark)
    bits = sum(int(dark[i][8]) << i for i in range(6)) | int(dark[7][8]) << 6 | int(dark[8][8]) << 7 \
        | int(dark[8][7]) << 8 | sum(int(dark[8][14 - i]) << i for i in range(9, 15))
    mask = next(m for m in range(8) if format_bits(m) == bits)
    version = (size - 17) // 4
    function = [[False] * size for _ in range(size)]
    for y in range(size):
        for x in range(size):
            in_finder = (x < 9 and y < 9) or (x >= size - 8 and y < 9) or (x < 9 and y >= size - 8)
            in_align = version > 1 and abs(x - (size - 7)) <= 2 and abs(y - (size - 7)) <= 2
            function[y][x] = in_finder or in_align or x == 6 or y == 6
    stream = []
    for right in range(size - 1, 0, -2):
        if right <= 6:
            right -= 1
        upward = (right + 1) & 2 == 0
        for vert in range(size):
            y = size - 1 - vert if upward else vert
            for x in (right, right - 1):
                if not function[y][x]:
                    stream.append(dark[y][x] ^ (_MASKS[mask](x, y) == 0))
    codewords = [int("".join("1" if b else "0" for b in stream[i:i + 8]), 2) for i in range(0, len(stream) - 7, 8)]
    ecc_len, blocks = _ECC_M[version]
    assert blocks == 1
    total = ((16 * version + 128) * version + 64 - (0 if version == 1 else 25 * 2 * 2 - 10 * 2 - 55)) // 8
    data, ecc = codewords[:total - ecc_len], codewords[total - ecc_len:total]
    assert rs_remainder(data, rs_divisor(ecc_len)) == ecc  # Reed-Solomon sedí
    bitstr = "".join(format(b, "08b") for b in data)
    assert bitstr[:4] == "0100"  # byte mode
    length = int(bitstr[4:12], 2)
    return bytes(int(bitstr[12 + 8 * i:20 + 8 * i], 2) for i in range(length)).decode("utf-8"), mask


@pytest.mark.parametrize("text, size", [("VoltBook", 21), ("https://ntfy.sh/voltbook-k7m3-q9xp-h2td", 29)])
def test_qr_reads_back_to_the_same_text(text, size):
    dark = qr_matrix(text)
    assert len(dark) == size  # verze 1, resp. 3
    assert read_back(dark)[0] == text
    # vyhledávací vzory v rozích (7×7 rámeček se středem 3×3)
    for ox, oy in ((0, 0), (size - 7, 0), (0, size - 7)):
        assert all(dark[oy][ox + i] and dark[oy + 6][ox + i] and dark[oy + i][ox] and dark[oy + i][ox + 6] for i in range(7))
        assert all(dark[oy + 2 + i][ox + 2 + j] for i in range(3) for j in range(3))
        assert not dark[oy + 1][ox + 1]


def test_qr_svg_and_limits():
    svg = qr_svg("https://ntfy.sh/voltbook-k7m3-q9xp-h2td")
    assert svg.startswith("<svg") and 'viewBox="0 0 37 37"' in svg and 'fill="#fff"' in svg
    assert len(qr_matrix("x" * 106)) == 41  # verze 6
    with pytest.raises(ValueError):
        qr_matrix("x" * 107)
