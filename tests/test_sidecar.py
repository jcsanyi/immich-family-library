"""The sidecar tag rewrite, on XMP shaped like what Immich and Lightroom write."""

from __future__ import annotations

from ifl.sidecar import tags_in, with_only_tag

IMMICH_XMP = b"""<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/' x:xmptk='Image::ExifTool 13.59'>
<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about='' xmlns:dc='http://purl.org/dc/elements/1.1/'>
  <dc:description><rdf:Alt><rdf:li xml:lang='x-default'>Ferry crossing</rdf:li></rdf:Alt></dc:description>
 </rdf:Description>
 <rdf:Description rdf:about='' xmlns:digiKam='http://www.digikam.org/ns/1.0/'>
  <digiKam:TagsList><rdf:Seq><rdf:li>Holidays/PEI</rdf:li><rdf:li>family</rdf:li></rdf:Seq></digiKam:TagsList>
 </rdf:Description>
 <rdf:Description rdf:about='' xmlns:exif='http://ns.adobe.com/exif/1.0/'>
  <exif:DateTimeOriginal>2026-05-04T15:30:00.000+02:00</exif:DateTimeOriginal>
  <exif:GPSLatitude>44,42.0N</exif:GPSLatitude>
 </rdf:Description>
 <rdf:Description rdf:about='' xmlns:xmp='http://ns.adobe.com/xap/1.0/'>
  <xmp:Rating>4</xmp:Rating>
 </rdf:Description>
</rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""

LIGHTROOM_XMP = b"""<x:xmpmeta xmlns:x='adobe:ns:meta/'>
<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about='' xmlns:dc='http://purl.org/dc/elements/1.1/'
   xmlns:lr='http://ns.adobe.com/lightroom/1.0/' xmlns:tiff='http://ns.adobe.com/tiff/1.0/'>
  <dc:subject><rdf:Bag><rdf:li>beach</rdf:li></rdf:Bag></dc:subject>
  <lr:hierarchicalSubject><rdf:Bag><rdf:li>Places|Beach</rdf:li></rdf:Bag></lr:hierarchicalSubject>
  <tiff:Orientation>6</tiff:Orientation>
 </rdf:Description>
</rdf:RDF>
</x:xmpmeta>"""


def test_tags_in_reads_the_digikam_list():
    assert tags_in(IMMICH_XMP) == ["Holidays/PEI", "family"]
    assert tags_in(LIGHTROOM_XMP) == []


def test_rewrite_keeps_everything_but_tags():
    out = with_only_tag(IMMICH_XMP, "from/alice")
    assert tags_in(out) == ["from/alice"]
    text = out.decode()
    for kept in ("Ferry crossing", "2026-05-04T15:30:00.000+02:00", "44,42.0N", "<xmp:Rating>4"):
        assert kept in text
    assert "Holidays/PEI" not in text and ">family<" not in text
    assert text.startswith("<?xpacket begin=") and text.rstrip().endswith("<?xpacket end='w'?>")


def test_rewrite_strips_lightroom_and_dublin_core_keywords():
    out = with_only_tag(LIGHTROOM_XMP, "from/bob").decode()
    assert "beach" not in out and "Places|Beach" not in out
    assert "hierarchicalSubject" not in out and "dc:subject" not in out
    assert "<tiff:Orientation>6" in out  # unrelated fields pass through
    assert tags_in(out.encode()) == ["from/bob"]


def test_rewrite_keeps_properties_written_as_attributes():
    # The shorthand form, as Lightroom and digiKam write it. Once the keywords
    # are gone the block has no child elements, but it still holds the rest.
    xmp = b"""<x:xmpmeta xmlns:x='adobe:ns:meta/'>
<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about='' xmlns:dc='http://purl.org/dc/elements/1.1/'
   xmlns:xmp='http://ns.adobe.com/xap/1.0/' xmlns:exif='http://ns.adobe.com/exif/1.0/'
   xmp:Rating='4' exif:DateTimeOriginal='2024-05-01T10:00:00'>
  <dc:subject><rdf:Bag><rdf:li>beach</rdf:li></rdf:Bag></dc:subject>
 </rdf:Description>
</rdf:RDF>
</x:xmpmeta>"""
    out = with_only_tag(xmp, "from/alice")
    text = out.decode()
    assert 'xmp:Rating="4"' in text and 'exif:DateTimeOriginal="2024-05-01T10:00:00"' in text
    assert "beach" not in text and tags_in(out) == ["from/alice"]
