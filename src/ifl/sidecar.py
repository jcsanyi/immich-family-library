"""The one thing we change in a sidecar: its tag list.

Immich writes the XMP sidecar itself, from its own exif row, so dates, zones,
GPS, description and rating are its encoding, not ours. We only replace the
tags, so the family copy ends up with a single `<prefix>/<member>` tag and
none of the member's. Extraction takes digiKam's TagsList ahead of Lightroom's
hierarchicalSubject and Dublin Core's subject, so all three are removed and one
TagsList is written.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "digiKam": "http://www.digikam.org/ns/1.0/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)

TAG_ELEMENTS = ("digiKam:TagsList", "lr:hierarchicalSubject", "dc:subject")
XPACKET_BEGIN = "<?xpacket begin='\ufeff' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
XPACKET_END = "\n<?xpacket end='w'?>"


def _q(name: str) -> str:
    prefix, local = name.split(":")
    return f"{{{NS[prefix]}}}{local}"


def tags_in(xmp: bytes) -> list[str]:
    """Entries of the digiKam TagsList, if any."""
    root = ET.fromstring(xmp)
    return [
        li.text or "" for li in root.iter(_q("digiKam:TagsList")) for li in li.iter(_q("rdf:li"))
    ]


def with_only_tag(xmp: bytes | None, tag: str) -> bytes:
    """The sidecar with every tag element removed and one TagsList holding `tag`.
    With no sidecar to start from, a minimal one."""
    if xmp:
        root = ET.fromstring(xmp)
        rdf = root.find(_q("rdf:RDF"))
        if rdf is None:
            raise ValueError("sidecar has no rdf:RDF element")
    else:
        root = ET.Element(_q("x:xmpmeta"))
        rdf = ET.SubElement(root, _q("rdf:RDF"))

    for desc in list(rdf.findall(_q("rdf:Description"))):
        for name in TAG_ELEMENTS:
            for el in desc.findall(_q(name)):
                desc.remove(el)
        if len(desc) == 0:
            rdf.remove(desc)

    desc = ET.SubElement(rdf, _q("rdf:Description"), {_q("rdf:about"): ""})
    seq = ET.SubElement(ET.SubElement(desc, _q("digiKam:TagsList")), _q("rdf:Seq"))
    ET.SubElement(seq, _q("rdf:li")).text = tag

    body = ET.tostring(root, encoding="unicode")
    return (XPACKET_BEGIN + body + XPACKET_END).encode("utf-8")
