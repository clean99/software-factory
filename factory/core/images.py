import pathlib
import struct
import zlib


EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PNG_RENDERING = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}
JPEG_RENDERING = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF, 0xC4, 0xDB, 0xDD, 0xDA}
JFIF = b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"


def clean(data, suffix):
    suffix = suffix.lower()
    if suffix == ".png":
        return png(data)
    if suffix in {".jpg", ".jpeg"}:
        return jpeg(data)
    if suffix == ".webp":
        return webp(data)
    if suffix == ".gif":
        return gif(data)
    raise ValueError("unsupported image format")


def png(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG")
    out, metadata, pos = bytearray(data[:8]), [], 8
    while pos + 12 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        end = pos + 12 + size
        kind = data[pos + 4:pos + 8]
        if end > len(data):
            break
        payload = data[pos + 8:end - 4]
        if zlib.crc32(kind + payload) != int.from_bytes(data[end - 4:end], "big"):
            raise ValueError("invalid PNG checksum")
        if kind not in PNG_RENDERING:
            metadata.append(kind.decode("ascii", errors="replace"))
        else:
            if ((kind == b"IHDR" and size != 13) or (kind == b"IEND" and size != 0)
                    or (kind == b"PLTE" and (not size or size > 768 or size % 3))
                    or (kind == b"tRNS" and (not size or size > 256))):
                raise ValueError("invalid PNG rendering chunk")
            out += data[pos:end]
        pos = end
        if kind == b"IEND":
            if pos != len(data):
                metadata.append("trailing data")
            return bytes(out), metadata
    raise ValueError("truncated PNG")


def jpeg(data):
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("invalid JPEG")
    out, metadata, pos = bytearray(data[:2]), [], 2
    while pos < len(data):
        start = pos
        if data[pos] != 255:
            raise ValueError("invalid JPEG marker")
        while pos < len(data) and data[pos] == 255:
            pos += 1
        if pos == len(data):
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            out += data[start:pos]
            if pos != len(data):
                metadata.append("trailing data")
            return bytes(out), metadata
        if marker in (0x01, *range(0xD0, 0xD8)):
            raise ValueError("unsupported standalone JPEG marker")
        if pos + 2 > len(data):
            break
        size = int.from_bytes(data[pos:pos + 2], "big")
        end = pos + size
        if size < 2 or end > len(data):
            break
        if marker == 0xE0 and data[pos + 2:end].startswith(b"JFIF\0"):
            out += b"\xff\xe0" + struct.pack(">H", len(JFIF) + 2) + JFIF
            if data[pos + 2:end] != JFIF:
                metadata.append("APP0 fields")
        elif marker in JPEG_RENDERING:
            out += data[start:end]
        else:
            metadata.append(f"APP{marker - 0xE0}" if 0xE0 <= marker <= 0xEF else "non-rendering segment")
        pos = end
        if marker == 0xDA:
            start = pos
            while pos < len(data):
                if data[pos] != 255:
                    pos += 1
                elif pos + 1 < len(data) and (data[pos + 1] == 0 or 0xD0 <= data[pos + 1] <= 0xD7):
                    pos += 2
                else:
                    break
            out += data[start:pos]
    raise ValueError("truncated JPEG")


def webp(data):
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("invalid WebP")
    length = int.from_bytes(data[4:8], "little") + 8
    if length > len(data):
        raise ValueError("truncated WebP")
    metadata = []
    if length != len(data):
        metadata.append("trailing data")

    def chunks(source, nested=False):
        out, pos = bytearray(), 0
        allowed = {b"VP8 ", b"VP8L", b"ALPH"} | (set() if nested else {b"VP8X", b"ANIM", b"ANMF"})
        while pos + 8 <= len(source):
            kind = bytes(source[pos:pos + 4])
            size = int.from_bytes(source[pos + 4:pos + 8], "little")
            end = pos + 8 + size + size % 2
            if end > len(source):
                raise ValueError("truncated WebP chunk")
            payload = bytearray(source[pos + 8:pos + 8 + size])
            if kind not in allowed:
                metadata.append(kind.decode("ascii", errors="replace").strip())
            else:
                if kind == b"VP8X":
                    if size != 10:
                        raise ValueError("invalid VP8X")
                    payload[0] &= 0x12
                    payload[1:4] = b"\0" * 3
                elif kind == b"ANIM" and size != 6:
                    raise ValueError("invalid WebP animation")
                elif kind == b"ANMF":
                    if size < 16:
                        raise ValueError("invalid WebP frame")
                    payload[15] &= 3
                    payload = payload[:16] + chunks(payload[16:], nested=True)
                out += kind + struct.pack("<I", len(payload)) + payload
                if len(payload) % 2:
                    out += b"\0"
            pos = end
        if pos != len(source):
            raise ValueError("truncated WebP")
        return out

    out = b"WEBP" + chunks(data[12:length])
    cleaned = b"RIFF" + struct.pack("<I", len(out)) + out
    if cleaned != data and not metadata:
        metadata.append("non-rendering fields")
    return cleaned, metadata


def gif(data):
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("invalid GIF")
    pos = 13 + (3 * (2 ** ((data[10] & 7) + 1)) if data[10] & 128 else 0)
    if pos > len(data):
        raise ValueError("truncated GIF palette")
    out, metadata = bytearray(data[:pos]), []

    def blocks(at):
        while at < len(data):
            size = data[at]
            at += size + 1
            if at > len(data):
                break
            if not size:
                return at
        raise ValueError("truncated GIF blocks")

    while pos < len(data):
        start, marker = pos, data[pos]
        pos += 1
        if marker == 0x3B:
            out += data[start:pos]
            if pos != len(data):
                metadata.append("trailing data")
            return bytes(out), metadata
        if marker == 0x21 and pos < len(data):
            label = data[pos]
            pos += 1
            extension_start = pos
            pos = blocks(pos)
            payload = data[extension_start:pos]
            control = label == 0xF9 and len(payload) == 6 and payload[0] == 4 and payload[-1] == 0 and not payload[1] & 0xE0
            loop = (label == 0xFF and len(payload) == 17 and payload[0] == 11
                    and payload[1:12] in (b"NETSCAPE2.0", b"ANIMEXTS1.0")
                    and payload[12:14] == b"\x03\x01" and payload[-1] == 0)
            if control or loop:
                out += data[start:pos]
            else:
                metadata.append("comment" if label == 0xFE else "non-rendering extension")
        elif marker == 0x2C and pos + 9 <= len(data):
            packed = data[pos + 8]
            pos += 9 + (3 * (2 ** ((packed & 7) + 1)) if packed & 128 else 0)
            pos = blocks(pos + 1)
            out += data[start:pos]
        else:
            raise ValueError("invalid GIF block")
    raise ValueError("truncated GIF")


def findings(label, data, suffix):
    from factory.core.privacy import redact

    try:
        _, metadata = clean(data, suffix)
    except ValueError:
        return [f"{redact(label)}: invalid image; metadata scan incomplete"]
    return [f"{redact(label)}: image metadata ({', '.join(metadata)}); run strip-image"] if metadata else []


def strip(paths):
    prepared = []
    for path in paths:
        path = pathlib.Path(path)
        if path.is_symlink():
            raise ValueError("refusing to rewrite a symbolic link")
        cleaned, _ = clean(path.read_bytes(), path.suffix)
        prepared.append((path, cleaned))
    for path, cleaned in prepared:
        path.write_bytes(cleaned)
