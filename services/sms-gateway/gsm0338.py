"""Minimal GSM 03.38 codec used by the Huawei SMS modem."""

_DEFAULT_ALPHABET = (
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\x1b"
    "\u00c6\u00e6\u00df\u00c9"
    " !\"#\u00a4%&'()*+,-./"
    "0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
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
    0x65: "\u20ac",
}

_DEFAULT_REVERSE = {
    character: index
    for index, character in enumerate(_DEFAULT_ALPHABET)
    if character != "\x1b"
}
_EXTENSION_REVERSE = {character: index for index, character in _EXTENSION_ALPHABET.items()}


def decode_septets(values) -> str:
    """Decode unpacked GSM 03.38 septets."""

    output: list[str] = []
    index = 0
    values = list(values)
    while index < len(values):
        value = values[index]
        if value == 0x1B:
            index += 1
            if index >= len(values):
                output.append("\ufffd")
                break
            output.append(_EXTENSION_ALPHABET.get(values[index], "\ufffd"))
        elif 0 <= value < len(_DEFAULT_ALPHABET):
            output.append(_DEFAULT_ALPHABET[value])
        else:
            output.append("\ufffd")
        index += 1
    return "".join(output)


def unpack_septets(data: bytes, count: int, bit_offset: int = 0) -> list[int]:
    """Unpack GSM septets from packed user data."""

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


def decode_modem_bytes(data: bytes) -> str:
    """Decode Huawei text-mode output, including Danish GSM characters."""

    if any(byte >= 0x80 for byte in data):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1", errors="replace")

    return decode_septets(data)


def encode_gsm0338(value: str) -> bytes:
    """Encode text for a modem configured with AT+CSCS=GSM."""

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
