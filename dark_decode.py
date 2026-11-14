"""Dark Tunnel (.dark / darktunnel://) decoder, server-side port.

The protocol is fixed and public:
  outer blob : AES-256-CFB with OUTER_KEY over MessagePack
  inner blob : AES-192-CFB with INNER_KEY over MessagePack
  "Encrypted*" fields : AES-192-CFB with INNER_KEY -> utf-8 text

Ported 1:1 from the app's DarkTunnel.java / VlessServer.fromDark so the API
returns exactly the same servers the phone used to decode locally.
"""

import base64
import json
import re

import msgpack
from Crypto.Cipher import AES

OUTER_KEY = b"$B&E)H@McQfThWmZq4t7w!z%C*F-JaNd"
INNER_KEY = b"F)J@NcRfUjXn2r4u7x!A%D*G"
IV = bytes.fromhex("232e39185523184a5723586242200e05")
IRAQ = "\U0001F1EE\U0001F1F6"
USA = "\U0001F1FA\U0001F1F8"


def b64(value):
    """Tolerant base64 (standard or URL-safe, padding optional)."""
    if not value:
        return b""
    v = value.strip().replace("%2B", "+").replace("%2F", "/").replace("%3D", "=")
    v = v.replace("-", "+").replace("_", "/")
    v = v.split("=")[0]
    v += "=" * (-len(v) % 4)
    try:
        return base64.b64decode(v, validate=False)
    except Exception:
        return b""


def aes_cfb(data, key):
    # Java's "AES/CFB/NoPadding" is CFB-128, not the 8-bit default.
    return AES.new(key, AES.MODE_CFB, iv=IV, segment_size=128).decrypt(data)


def _text(data):
    pt = aes_cfb(data, INNER_KEY)
    s = pt.decode("utf-8", "replace")
    if "\ufffd" in s:
        return base64.b64encode(pt).decode()
    return s


def _walk(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            if key.startswith("Encrypted") and isinstance(v, (bytes, bytearray)):
                out[key[len("Encrypted"):]] = _text(bytes(v))
            else:
                out[key] = _walk(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_walk(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return _text(bytes(value))
    return value


def decrypt_inner(encrypted_b64):
    enc = b64(encrypted_b64)
    if not enc:
        raise ValueError("empty encryptedLockedConfig")
    outer = msgpack.unpackb(aes_cfb(enc, OUTER_KEY), raw=False,
                            strict_map_key=False)
    blob = outer.get("EncryptedLockedConfig") or outer.get("encryptedLockedConfig")
    if not blob:
        return {}
    inner = msgpack.unpackb(aes_cfb(bytes(blob), INNER_KEY), raw=False,
                            strict_map_key=False)
    return _walk(inner)


def parse_json_object(raw):
    if not raw:
        return None
    t = raw.strip().lstrip("\ufeff")
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    t = t[a:b + 1]
    # Old exports leave xray template tokens unquoted in the JSON.
    t = re.sub(r":\s*\$[A-Z0-9_]+", ": true", t)
    try:
        return json.loads(t)
    except Exception:
        return None


def _config_from_v2(v2):
    if not isinstance(v2, dict):
        return None
    cfg = v2.get("EncryptedConfig")
    if not isinstance(cfg, (dict, str)):
        cfg = v2.get("Config")
    if isinstance(cfg, dict):
        return cfg
    if isinstance(cfg, str):
        parsed = parse_json_object(cfg)
        if parsed is None:
            try:
                parsed = parse_json_object(b64(cfg).decode("utf-8", "ignore"))
            except Exception:
                parsed = None
        return parsed
    if isinstance(v2.get("outbounds"), list):
        return v2
    return None


def _config_from_locked(locked):
    inner = locked.get("EncryptedLockedConfig")
    if not isinstance(inner, dict):
        inner = locked.get("encryptedLockedConfig")
    if not isinstance(inner, dict):
        inner = locked
    v2 = inner.get("V2RayConfig")
    if not isinstance(v2, dict):
        v2 = locked.get("V2RayConfig")
    return _config_from_v2(v2)


def _config_from_json_or_locked(raw):
    o = parse_json_object(raw)
    if not o:
        return None
    if isinstance(o.get("outbounds"), list):
        return o
    locked = o.get("encryptedLockedConfig")
    if isinstance(locked, dict):
        return _config_from_locked(locked)
    return _config_from_v2(o.get("V2RayConfig"))


def dark_to_config(raw):
    """Return the xray config dict inside a .dark payload, or None."""
    if not raw:
        return None
    text = raw.strip().lstrip("\ufeff").strip()
    if "darktunnel://" in text:
        text = text.split("darktunnel://", 1)[1].strip()
        text = text.split()[0] if text.split() else ""
        text = b64(text).decode("utf-8", "ignore").strip()
    if not text.startswith("{"):
        dec = b64(text).decode("utf-8", "ignore").strip()
        if dec.startswith("{"):
            text = dec
    try:
        meta = json.loads(text)
    except Exception:
        return None
    locked = meta.get("encryptedLockedConfig")
    cfg = None
    if isinstance(locked, dict):
        cfg = _config_from_locked(locked)
    elif isinstance(locked, str) and locked:
        if locked.strip().startswith("{"):
            cfg = _config_from_json_or_locked(locked)
        else:
            try:
                cfg = _config_from_inner(decrypt_inner(locked))
            except Exception:
                cfg = None
            if cfg is None:
                try:
                    cfg = _config_from_json_or_locked(
                        b64(locked).decode("utf-8", "ignore"))
                except Exception:
                    cfg = None
    if cfg is None:
        v2 = meta.get("V2RayConfig")
        if isinstance(v2, dict):
            cfg = _config_from_v2(v2)
    if cfg is None and isinstance(meta.get("outbounds"), list):
        cfg = meta
    if isinstance(cfg, dict) and not cfg.get("name"):
        cfg["name"] = meta.get("name", "")
    return cfg


def _config_from_inner(inner):
    v2 = inner.get("V2RayConfig") if isinstance(inner, dict) else None
    if not isinstance(v2, dict):
        return None
    cfg = v2.get("Config")
    if isinstance(cfg, str):
        return parse_json_object(cfg)
    if isinstance(cfg, dict):
        return cfg
    return None


def looks_like_config(text):
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    if t.startswith("darktunnel://"):
        return True
    if not t.startswith("{"):
        return not t.startswith("<")
    try:
        o = json.loads(t)
        return any(k in o for k in ("outbounds", "encryptedLockedConfig",
                                    "type", "v2rayConfig", "V2RayConfig"))
    except Exception:
        return False


def _flag(hint):
    m = re.search(r"[\U0001F1E6-\U0001F1FF]{2}", hint or "")
    return m.group(0).replace(IRAQ, USA) if m else ""


def _is_uuid(value):
    if not value:
        return False
    hexv = value.replace("-", "").strip()
    return len(hexv) == 32 and all(
        c in "0123456789abcdefABCDEF" for c in hexv)


def is_usable(s):
    if not s.get("host"):
        return False
    if str(s.get("network", "")).lower() not in ("ws", "tcp"):
        return False
    if str(s.get("security", "")).lower() not in ("tls", "none"):
        return False
    proto = str(s.get("protocol", "")).lower()
    if proto == "trojan":
        return bool(s.get("password"))
    if proto == "vmess":
        return bool(s.get("uuid"))
    return _is_uuid(s.get("uuid"))


def endpoint_key(s):
    secret = s.get("password", "") if s.get("protocol") == "trojan" else s.get("uuid", "")
    dest = s.get("wsHost") or s.get("host", "")
    return "|".join([
        str(s.get("protocol", "")).lower(),
        str(dest).lower(),
        str(s.get("port", 443)),
        str(secret),
        str(s.get("network", "")).lower(),
        str(s.get("security", "")).lower(),
        str(bool(s.get("tls"))).lower(),
        str(s.get("wsPath", "/")),
    ])


def build_server(cfg, hint, source_key, posted_at, fetched_at):
    """Map an xray config to the app's VlessServer JSON shape."""
    if not isinstance(cfg, dict):
        return None
    outbounds = cfg.get("outbounds") or []
    ob, proto = None, ""
    for wanted in ("vless", "trojan", "vmess"):
        for cand in outbounds:
            if isinstance(cand, dict) and \
                    str(cand.get("protocol", "")).lower() == wanted:
                ob, proto = cand, wanted
                break
        if ob:
            break
    if ob is None:
        return None
    trojan = proto == "trojan"
    vmess = proto == "vmess"
    settings = ob.get("settings") or {}
    host, port, uuid, password = "", 443, "", ""
    if trojan:
        servers = settings.get("servers") or []
        if not servers or not isinstance(servers[0], dict):
            return None
        sv = servers[0]
        host = sv.get("address", "")
        port = int(sv.get("port", 443) or 443)
        password = sv.get("password", "")
    else:
        vnext = settings.get("vnext") or []
        if not vnext or not isinstance(vnext[0], dict):
            return None
        vn = vnext[0]
        host = vn.get("address", "")
        port = int(vn.get("port", 443) or 443)
        users = vn.get("users") or []
        if users and isinstance(users[0], dict):
            u = users[0]
            uuid = u.get("id") or u.get("password") or ""
            if vmess and int(u.get("alterId", 0) or 0) > 0:
                return None

    stream = ob.get("streamSettings") or {}
    network = stream.get("network", "tcp")
    security = stream.get("security", "none")
    sni = ""
    ws_path = "/"
    ws_host = ""
    tls = str(security).lower() == "tls"
    tls_settings = stream.get("tlsSettings") or {}
    if isinstance(tls_settings, dict):
        sni = tls_settings.get("serverName", "")
    ws = stream.get("wsSettings") or {}
    if isinstance(ws, dict):
        ws_path = ws.get("path", "/") or "/"
        headers = ws.get("headers") or {}
        if isinstance(headers, dict):
            ws_host = headers.get("Host") or headers.get("host") or ""

    meta_name = str(cfg.get("name") or "")
    s = {
        "id": "",
        "sourceKey": source_key,
        "name": meta_name,
        "host": host,
        "port": port,
        "protocol": proto,
        "uuid": uuid,
        "password": password,
        "sni": sni or host,
        "wsPath": ws_path,
        "wsHost": ws_host,
        "network": network,
        "security": security,
        "tls": tls,
        "flag": _flag(hint),
        "postedAt": int(posted_at),
        "fetchedAt": int(fetched_at),
    }
    if not s["sni"]:
        s["sni"] = ws_host or host
    return s if is_usable(s) else None
