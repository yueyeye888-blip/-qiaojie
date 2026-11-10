import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install_login_service as installer


class InstallationTests(unittest.TestCase):
    def test_staging_preserves_identity_and_existing_runtime_on_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'source'
            target=Path(directory)/'Application Support/controller'
            root.mkdir()
            (root/'windows').mkdir();(root/'static').mkdir();(root/'runtime').mkdir()
            (root/'controller.py').write_text('# version one')
            (root/'ram-policy.json').write_text('{}')
            (root/'runtime/admin_token').write_text('existing-test-identity')
            with sqlite3.connect(root/'runtime/bridge.sqlite3') as db:
                db.execute('CREATE TABLE identity(value TEXT)')
                db.execute("INSERT INTO identity VALUES('Yorke')")
            with patch.object(installer,'ROOT',root),patch.object(installer,'DEPLOYMENT',target):
                installer.stage({'runtime_dir':'runtime','agent_bind':'100.64.0.1'})
                self.assertEqual((target/'runtime/admin_token').read_text(),'existing-test-identity')
                with sqlite3.connect(target/'runtime/bridge.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT value FROM identity').fetchone()[0],'Yorke')
                    db.execute("UPDATE identity SET value='current runtime'")
                config=json.loads((target/'config.json').read_text())
                config['usb_proxy_enabled']=True
                (target/'config.json').write_text(json.dumps(config))
                (root/'controller.py').write_text('# version two')
                installer.stage({'runtime_dir':'runtime','agent_bind':'100.64.0.2'})
                self.assertTrue(json.loads((target/'config.json').read_text())['usb_proxy_enabled'])
                with sqlite3.connect(target/'runtime/bridge.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT value FROM identity').fetchone()[0],'current runtime')
                self.assertEqual((target/'controller.py').read_text(),'# version two')
