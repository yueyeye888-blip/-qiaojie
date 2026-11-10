import io
import unittest
from unittest.mock import patch

from integrations import CloudClient


class CloudRegionBoundaryTests(unittest.TestCase):
    def test_mainland_and_numbered_regions_use_fixed_official_hosts(self):
        for region in ('cn-hangzhou','cn-hongkong','ap-southeast-1'):
            with patch('integrations.urllib.request.urlopen') as open_url:
                open_url.return_value.__enter__.return_value=io.BytesIO(b'{"Desktops":[]}')
                self.assertEqual(CloudClient(region,credentials=('test-id','test-secret')).desktops(),[])
                self.assertEqual(open_url.call_args.args[0].full_url,'https://ecd.'+region+'.aliyuncs.com/')
        for region in ('cn-hangzhou/other','cn-hangzhou@other.invalid','cn-hangzhou\n','cn-hangzhou.other'):
            with self.assertRaises(ValueError):CloudClient(region,credentials=('test-id','test-secret'))
