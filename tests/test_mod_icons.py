import http.client
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode
import zipfile
import zlib

from consolecraft.cli import Application
from consolecraft.config import ConfigStore
from consolecraft import commands
from consolecraft.mod_icons import IconError, ModIcons, MAX_ICON_BYTES, raster
from consolecraft.mods import inspect_jar, ModError


def png(width=2, height=2):
    def chunk(name, data):
        return struct.pack(">I",len(data))+name+data+struct.pack(">I",zlib.crc32(name+data)&0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,8,6,0,0,0))+chunk(b"IDAT",zlib.compress((b"\0"+b"\x22\x99\x55\xff"*min(width,4))*min(height,4)))+chunk(b"IEND",b"")


def jar(path, loader="fabric", icon="assets/icon.png", image=None):
    with zipfile.ZipFile(path,"w") as archive:
        if loader == "fabric":
            archive.writestr("fabric.mod.json",json.dumps({"id":"icons","version":"1.0","icon":icon}))
        else:
            archive.writestr("META-INF/"+("neoforge.mods.toml" if loader=="neoforge" else "mods.toml"),
                             '[[mods]]\nmodId="icons"\nversion="1.0"\nlogoFile="assets/icon.png"\n')
        archive.writestr("assets/icon.png",png() if image is None else image)
    return path


class ModIconTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.game=self.root/"game";(self.game/"mods").mkdir(parents=True)
        self.icons=ModIcons(self.root/"cache")

    def test_fabric_string_size_map_forge_neoforge_icons_are_original_bytes(self):
        for index,(loader,icon) in enumerate([("fabric","assets/icon.png"),("fabric",{"16":"missing.png","128":"assets/icon.png"}),("forge",None),("neoforge",None)]):
            with self.subTest(loader=loader,icon=icon):
                path=jar(self.game/"mods"/f"{index}.jar",loader,icon)
                self.assertEqual(inspect_jar(path).icon_path,"assets/icon.png")
                result=self.icons.local(self.game,path.name)
                self.assertEqual(result.content,png());self.assertEqual(result.content_type,"image/png")

    def test_disk_cache_avoids_reopening_jar_and_survives_new_service(self):
        path=jar(self.game/"mods"/"test.jar")
        self.icons.local(self.game,path.name)
        with patch("consolecraft.mod_icons.inspect_jar",side_effect=AssertionError("cache miss")):
            result=ModIcons(self.root/"cache").local(self.game,path.name)
        self.assertEqual(result.content,png())
        renamed=path.with_suffix(".jar.disabled");path.rename(renamed)
        self.assertEqual(self.icons.local(self.game,renamed.name).content,png())

    def test_missing_icons_negative_cached_and_malicious_paths_never_extracted(self):
        for index,value in enumerate(["../secret.png","/absolute.png","C:/secret.png","assets\\bad.png","missing.png"]):
            path=jar(self.game/"mods"/f"bad{index}.jar",icon=value)
            with self.subTest(value=value), self.assertRaises(IconError):self.icons.local(self.game,path.name)
        with self.assertRaises((IconError,ModError)):self.icons.local(self.game,"../bad.jar")
        self.assertFalse((self.root/"secret.png").exists())
        with patch("consolecraft.mod_icons.inspect_jar",side_effect=AssertionError("negative cache missed")),self.assertRaises(IconError):
            self.icons.local(self.game,"bad4.jar")

    def test_invalid_formats_crc_dimensions_and_oversize_rejected(self):
        for content in [b"<svg onload='alert(1)'/>",b"<html>",png(65535,65535),png()[:-1],b"x"*(MAX_ICON_BYTES+1)]:
            with self.subTest(size=len(content)),self.assertRaises(IconError):raster(content)
        path=jar(self.game/"mods"/"large.jar",image=b"x"*(MAX_ICON_BYTES+1))
        with self.assertRaises(IconError):self.icons.local(self.game,path.name)

    def test_modrinth_original_icon_cached_and_official_host_only(self):
        self.icons.catalog=Mock();self.icons.catalog.project.return_value={"icon_url":"https://cdn.modrinth.com/data/mod/icon.png"}
        response=Mock(status_code=200,headers={});response.iter_content.return_value=[png()]
        self.icons.http=Mock();self.icons.http.get.return_value=response
        first=self.icons.project("sodium");second=self.icons.project("sodium")
        self.assertEqual(first.content,png());self.assertEqual(first,second)
        self.icons.http.get.assert_called_once();self.icons.catalog.project.assert_called_once()
        self.icons.catalog.project.return_value={"icon_url":"http://127.0.0.1/private.png"}
        with self.assertRaises(IconError):self.icons.project("untrusted")
        self.icons.http.get.assert_called_once()
