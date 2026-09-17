"""Standalone GSM 03.38 codec for SBR Pager Gateway."""

_DEFAULT_ALPHABET = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1b"
    "ÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

_EXTENSION_ALPHABET = {
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}

_DEFAULT_REVERSE = {
    character: index
    for index, character in enumerate(_DEFAULT_ALPHABET)
    if character != "\x1b"
}
_EXTENSION_REVERSE = {character: index for index, character in _EXTENSION_ALPHABET.items()}


def decode_septets(values) -> str:
    output: list[str] = []
    index = 0
    values = list(values)
    while index < len(values):
        value = values[index]
        if value == 0x1B:
            index += 1
            if index >= len(values):
                output.append("�")
                break
            output.append(_EXTENSION_ALPHABET.get(values[index], "�"))
        elif 0 <= value < len(_DEFAULT_ALPHABET):
            output.append(_DEFAULT_ALPHABET[value])
        else:
            output.append("�")
        index += 1
    return "".join(output)


def unpack_septets(data: bytes, count: int, bit_offset: int = 0) -> list[int]:
    output: list[int] = []
    for index in range(count):
        bit_position = bit_offset + index * 7
        byte_position = bit_position // 8
        shift = bit_position % 8
        if byte_position >= len(data):
            break
        value = (data[byte_position] >> shift) & 0x7F
        if shift > 1 and byte_position + 1 < len(data):
            value |= (data[byte_position + 1] << (8 - shift)) & 0x7F
        output.append(value)
    return output


def encode_gsm0338(value: str) -> bytes:
    output = bytearray()
    for position, character in enumerate(value):
        default_code = _DEFAULT_REVERSE.get(character)
        if default_code is not None:
            output.append(default_code)
            continue
        extension_code = _EXTENSION_REVERSE.get(character)
        if extension_code is not None:
            output.extend((0x1B, extension_code))
            continue
        raise UnicodeEncodeError(
            "gsm0338",
            value,
            position,
            position + 1,
            f"character {character!r} is not available in GSM 03.38",
        )
    return bytes(output)
