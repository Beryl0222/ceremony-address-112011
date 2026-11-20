"""交接资料格式和原文完整性测试。"""
from pathlib import Path
import json
import subprocess
import sys
import unittest

from corpus import read_collection

ROOT = Path(__file__).resolve().parents[1]


def records(name):
    return read_collection(ROOT / "data" / (name + ".json"))


class MaterialTests(unittest.TestCase):
    def test_collections_are_utf8_record_arrays(self):
        for name, fields in FIELDS.items():
            with self.subTest(collection=name):
                rows = records(name)
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(set(row), set(fields))
                    self.assertIsInstance(row["id"], str)
                    self.assertTrue(row["id"])

    def test_reader_retains_exact_text_and_order(self):
        result = subprocess.check_output(
            [sys.executable, "-m", "corpus", PRIMARY, "--id", LINE_ID],
            cwd=ROOT, text=True, encoding="utf-8",
        )
        original = [row for row in records(PRIMARY) if row["id"] == LINE_ID]
        self.assertTrue(original)
        self.assertEqual(json.loads(result), original)

    def test_concurrent_cues_and_newer_family_message_have_separate_records(self):
        programme = {row["id"]: row for row in records("programme")}
        self.assertEqual(programme["S2"]["时间"], programme["S3"]["时间"])
        self.assertNotEqual(programme["S2"]["地点"], programme["S3"]["地点"])
        calls = {row["id"]: row for row in records("run_of_show")}
        messages = {row["id"]: row for row in records("requests")}
        self.assertEqual(messages["M3"]["发件"], messages["M6"]["发件"])
        self.assertLess(calls["C2"]["创建"], calls["C4"]["创建"])
        self.assertLess(calls["C4"]["创建"], messages["M6"]["时间"])


PRIMARY = 'requests'
LINE_ID = 'M6'
FIELDS = {
    "people": [
        "id",
        "家庭",
        "登记名",
        "台上可用名",
        "读音",
        "读音范围"
    ],
    "programme": [
        "id",
        "家庭",
        "时间",
        "环节",
        "地点",
        "接收岗"
    ],
    "requests": [
        "id",
        "时间",
        "家庭",
        "发件",
        "范围",
        "内容"
    ],
    "run_of_show": [
        "id",
        "环节",
        "岗位",
        "创建",
        "时态",
        "正文"
    ],
    "desk_notes": [
        "id",
        "环节",
        "文字"
    ]
}
