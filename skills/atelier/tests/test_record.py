import json
import tempfile
import unittest
from pathlib import Path

from atelier import record


class RecordTests(unittest.TestCase):
    def test_filter_and_estimate(self):
        parts = {'R1': 'abcde', 'R2': 'xxxx', 'section': ''}
        chosen = record.filter_parts({'reading': {'R2': False}}, parts)
        self.assertEqual(chosen, {'R1': 'abcde', 'section': ''})
        self.assertEqual(record.measure_read(chosen), {'R1': 2, 'section': 0})

    def test_events_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'docs/pbi/record.jsonl'
            record.append(path, {'type': 'launch', 'launch_id': 'a', 'pbi': 'P1', 'role': 'worker', 'runner': 'agent', 'model': 'm', 'effort': None, 'level': 'L1', 'tokens': {'input': 10, 'output': 4, 'total': 14}, 'cost': 0.5, 'read': {'R1': 2}})
            record.append(path, {'type': 'launch', 'launch_id': 'b', 'pbi': 'P1', 'role': 'worker', 'runner': 'agent', 'model': 'm', 'effort': None, 'level': 'L1', 'tokens': 'unknown', 'cost': 'unknown', 'read': {'R2': 1}})
            record.append(path, {'type': 'check', 'pbi': 'P1', 'rules': ['R1', 'R2'], 'stage': 'build', 'model': 'm', 'level': 'L1', 'result': 'fail', 'read_launch_ids': ['a'], 'first_pass': True})
            record.append(path, {'type': 'check', 'pbi': 'P1', 'rules': [], 'stage': 'build', 'model': 'm', 'level': 'L1', 'result': 'pass', 'read_launch_ids': ['b'], 'first_pass': False})
            record.append(path, {'type': 'fix', 'rule': 'R1', 'method': 'auto'})
            lines = path.read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 5)
            self.assertEqual(json.loads(lines[1])['tokens'], 'unknown')
            markdown, data = record.report(path)
            self.assertEqual(data['by_pbi']['P1']['tokens']['total'], {'known': 14, 'unknown': 1})
            self.assertEqual(data['by_role']['worker']['cost'], {'known': 0.5, 'unknown': 1})
            self.assertEqual(data['rules']['R1'], {'tokens_read': 2, 'fired': 1, 'fixed': 1, 'violated_while_read': 1})
            self.assertEqual(data['rules']['R2']['violated_while_read'], 0)
            self.assertEqual(data['first_pass']['L1']['m'], {'passed': 0, 'total': 1, 'rate': 0.0})
            self.assertIn('不明', markdown)
            self.assertNotIn('効いた', markdown)

    def test_missing_file_and_invalid_event(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'record.jsonl'
            _, data = record.report(path)
            self.assertEqual(data['rules'], {})
            with self.assertRaises(ValueError):
                record.append(path, {'type': 'check'})
            self.assertFalse(path.exists())
