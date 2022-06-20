from typing import Optional

import dns.resolver


def _get_dns_resolver():
    my_resolver = dns.resolver.Resolver()

    # 1.1.1.1 is CloudFlare's public DNS server
    my_resolver.nameservers = ["1.1.1.1"]

    return my_resolver


def get_ns(hostname) -> [str]:
    try:
        answers = _get_dns_resolver().resolve(hostname, "NS")
    except Exception:
        return []
    return [a.to_text() for a in answers]


def get_cname_record(hostname) -> Optional[str]:
    """Return the CNAME record if exists for a domain, WITHOUT the trailing period at the end"""
    try:
        answers = _get_dns_resolver().query(hostname, "CNAME")
    except Exception:
        return None

    for a in answers:
        ret = a.to_text()
        return ret[:-1]

    return None


def get_mx_domains(hostname) -> [(int, str)]:
    """return list of (priority, domain name).
    domain name ends with a "." at the end.
    """
    try:
        answers = _get_dns_resolver().query(hostname, "MX")
    except Exception:
        return []

    ret = []

    for a in answers:
        record = a.to_text()  # for ex '20 alt2.aspmx.l.google.com.'
        parts = record.split(" ")

        ret.append((int(parts[0]), parts[1]))

    return ret


_include_spf = "include:"


def get_spf_domain(hostname) -> [str]:
    """return all domains listed in *include:*"""
    try:
        answers = _get_dns_resolver().query(hostname, "TXT")
    except Exception:
        return []

    ret = []

    for a in answers:  # type: dns.rdtypes.ANY.TXT.TXT
        for record in a.strings:
            record = record.decode()  # record is bytes

            if record.startswith("v=spf1"):
                parts = record.split(" ")
                ret.extend(
                    part[part.find(_include_spf) + len(_include_spf) :]
                    for part in parts
                    if part.startswith(_include_spf)
                )

    return ret


def get_txt_record(hostname) -> [str]:
    """return all domains listed in *include:*"""
    try:
        answers = _get_dns_resolver().query(hostname, "TXT")
    except Exception:
        return []

    ret = []

    for a in answers:  # type: dns.rdtypes.ANY.TXT.TXT
        for record in a.strings:
            record = record.decode()  # record is bytes

            ret.append(record)

    return ret
