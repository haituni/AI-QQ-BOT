"""Initial roles load only for a new installation, without accounts or permissions."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from bot.config import Config

class InitialRoles(unittest.TestCase):
    def test_first_run_and_subsequent_local_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'initial').mkdir()
            seed = Path(__file__).resolve().parents[1]/'initial/roleplay.json'
            shutil.copy2(seed, root/'initial/roleplay.json')
            cfg = Config(root)
            self.assertTrue(any(c['name']=='大肥鱼' for c in cfg.value['roleplay']['characters']))
            self.assertEqual(cfg.value['admin_qq'], [])
            self.assertEqual(cfg.value['bot_qq'], '')
            self.assertEqual(cfg.value['api_key'], '')
            self.assertEqual(cfg.value['conversations'], {})
            self.assertEqual(cfg.value['roleplay']['personas'], [{'id':'self','name':'我','description':''}])
            cfg.update({'roleplay_selection': {'active_character':'traveler'}})
            self.assertEqual(Config(root).value['roleplay']['active_character'], 'traveler')
            self.assertNotEqual(cfg.control_token, Config(root/'another').control_token)
