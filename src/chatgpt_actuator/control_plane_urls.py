"""Keep MCP DOM away from local agent administration and credential pages."""
import ipaddress
import socket
from urllib.parse import urlsplit

ADMIN_PORTS=frozenset({8081,8090,8092,8188,8189,8764,8765,8766,8767})

def loopback(host):
    host=(host or '').casefold().rstrip('.')
    if host=='localhost' or host.endswith('.localhost'):return True
    try:address=ipaddress.ip_address(host)
    except ValueError:
        try:address=ipaddress.ip_address(socket.inet_aton(host))
        except (OSError,ValueError):return False
    mapped=getattr(address,'ipv4_mapped',None)
    return address.is_loopback or bool(mapped and mapped.is_loopback)

def protected_origin(url):
    try:
        parsed=urlsplit(url)
        return parsed.scheme in {'http','https'} and parsed.port in ADMIN_PORTS and loopback(parsed.hostname)
    except (TypeError,ValueError):return False
