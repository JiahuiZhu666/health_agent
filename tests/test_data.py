"""Tiny artificial files are integrity-test fixtures, never shipped research data."""
import csv
import json
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path
from nhis_agent import data


def adult_fixture(path, n=3):
    names=sorted(data.SOURCES['adult']['columns'])
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=names)
        writer.writeheader()
        for i in range(n):
            row={x:'1' for x in names}
            row.update(HHX=str(i),AGEP_A='40',WTFA_A='3000',PHSTAT_A='4',SRVY_YR='2024',RECTYPE='10')
            writer.writerow(row)


class DataTests(unittest.TestCase):
    def test_csv_integrity_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'adult24.csv'
            adult_fixture(p)
            result=data.inspect_csv(p,min_rows=2)
            self.assertEqual(result['row_count'],3)
            self.assertEqual(result['sha256'],data.sha256_file(p))
            self.assertEqual(result['household_count'],3)

    def test_missing_header_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bad.csv'
            p.write_text('HHX,WTFA_A\n1,20\n')
            with self.assertRaises(ValueError): data.inspect_csv(p,min_rows=1)

    def test_duplicate_adult_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'adult24.csv'
            adult_fixture(p)
            lines=p.read_text().splitlines()
            p.write_text('\n'.join(lines+[lines[1]])+'\n')
            with self.assertRaises(ValueError): data.inspect_csv(p,min_rows=1)

    def test_income_all_ten_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'adultinc24.csv'
            p.write_text('RECTYPE,HHX,IMPNUM_A,POVRATTC_A,RATCAT_A\n'+'\n'.join('30,1,%d,1.5,5'%i for i in range(1,11))+'\n')
            self.assertEqual(data.inspect_csv(p,'income',min_rows=1)['imputations'],10)
            p.write_text(p.read_text().replace('30,1,10,1.5,5\n',''))
            with self.assertRaises(ValueError): data.inspect_csv(p,'income',min_rows=1)

    def test_network_failure_never_creates_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch('nhis_agent.data._download', side_effect=OSError('DNS unavailable')):
                result=data.download(root)
            self.assertEqual(result['status'],'blocked')
            self.assertFalse(result['ready'])
            self.assertFalse((root/'data'/'adult24.csv').exists())
            self.assertFalse((root/'data'/'manifest.json').exists())
            self.assertEqual(len(result['errors']),2)

    def test_archive_cannot_traverse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            archive=root/'archive.zip'
            with zipfile.ZipFile(archive,'w') as z: z.writestr('../../adult24.csv','official-content-fixture')
            out=root/'safe.csv'
            data._extract_csv(archive,out,'adult24.csv')
            self.assertEqual(out.read_text(),'official-content-fixture')
            self.assertFalse((root.parent/'adult24.csv').exists())

    def test_status_rejects_untracked_and_tampered_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); d=root/'data'; d.mkdir(); p=d/'adult24.csv'
            adult_fixture(p,data.SOURCES["adult"]["expected_rows"])
            self.assertFalse(data.status(root)['ready'])
            entry=data.inspect_csv(p)
            entry.update(source=data.SOURCES['adult']['url'],year=2024)
            (d/'manifest.json').write_text(json.dumps({'schema_version':1,'datasets':{'adult':entry}}))
            self.assertTrue(data.status(root)['ready'])
            p.write_text(p.read_text().replace('3000','3001',1))
            self.assertFalse(data.status(root)['ready'])


if __name__=='__main__': unittest.main()
