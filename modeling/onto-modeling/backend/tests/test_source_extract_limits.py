"""Synthetic Office containers: supported size boundaries and bounded reads."""
import hashlib
import importlib.util
import io
from pathlib import Path
import zipfile

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'assets/workspace-toolkit/scripts/source_extract.py'
MIB = 1024 * 1024
BODY = (b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b'<w:body><w:p><w:r><w:t>ITEM-01 RULE-01</w:t></w:r></w:p></w:body></w:document>')


@pytest.fixture
def source():
    spec = importlib.util.spec_from_file_location('source_limit_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package(body=BODY, media_size=0, compression=zipfile.ZIP_STORED):
    """Unused synthetic media deliberately carries no business text."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=compression) as archive:
        archive.writestr('word/document.xml', body)
        archive.writestr('word/media/synthetic.bin', b'\0' * media_size)
    return output.getvalue()


def sized_docx(path, size):
    overhead = len(package())
    data = package(media_size=size-overhead)
    assert len(data) == size
    path.write_bytes(data)
    return data


@pytest.mark.parametrize('size', [8*MIB-1, 8*MIB, 8*MIB+1, 11*MIB, 20*MIB-1, 20*MIB])
def test_valid_docx_upload_size_range(tmp_path, source, size):
    path = tmp_path / 'synthetic.docx'
    original = sized_docx(path, size)
    result = source.extract(path)
    assert result['status'] == 'ok', result
    assert source.located_text(result, 'p:1') == 'ITEM-01 RULE-01'
    assert result['source_sha256'] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original


def test_raw_file_above_upload_limit_remains_unverifiable(tmp_path, source):
    path = tmp_path / 'synthetic.docx'
    sized_docx(path, 20*MIB+1)
    result = source.extract(path)
    assert result['status'] == 'unverifiable'
    assert result['segments'] == []
    assert '原文件' in result['reason']


def test_image_heavy_docx_does_not_expand_unused_media(tmp_path, source, monkeypatch):
    path = tmp_path / 'synthetic.docx'
    path.write_bytes(package(media_size=33*MIB, compression=zipfile.ZIP_DEFLATED))
    original_open = zipfile.ZipFile.open
    opened = []

    def tracked_open(archive, name, *args, **kwargs):
        member = name.filename if isinstance(name, zipfile.ZipInfo) else name
        opened.append(member)
        assert member == 'word/document.xml', 'Unused media must not be decompressed'
        return original_open(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'open', tracked_open)
    result = source.extract(path)
    assert result['status'] == 'ok', result
    assert opened == ['word/document.xml']


def test_docx_body_budget_is_still_enforced(tmp_path, source, monkeypatch):
    monkeypatch.setattr(source, 'MAX_EXPANDED_BYTES', 64)
    path = tmp_path / 'synthetic.docx'
    path.write_bytes(package())
    result = source.extract(path)
    assert result['status'] == 'unverifiable'
    assert result['segments'] == []
    assert '正文' in result['reason'] and '超限' in result['reason']


def test_member_count_and_duplicates_are_rejected(tmp_path, source, monkeypatch):
    monkeypatch.setattr(source, 'MAX_ARCHIVE_MEMBERS', 1)
    path = tmp_path / 'synthetic.docx'
    path.write_bytes(package())
    assert '成员数' in source.extract(path)['reason']
    monkeypatch.setattr(source, 'MAX_ARCHIVE_MEMBERS', 10000)
    with pytest.warns(UserWarning, match='Duplicate name'):
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('word/document.xml', BODY)
    result = source.extract(path)
    assert result['status'] == 'unverifiable'
    assert '重复' in result['reason']


def test_bounded_member_read_ignores_understated_directory_size(tmp_path, source, monkeypatch):
    path = tmp_path / 'synthetic.docx'
    path.write_bytes(package())
    monkeypatch.setattr(source, 'MAX_EXPANDED_BYTES', 512)
    reads = []

    class OversizeMember(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    monkeypatch.setattr(zipfile.ZipFile, 'open', lambda *args, **kwargs: OversizeMember(b' ' * 1024))
    result = source.extract(path)
    assert reads == [513]
    assert result['status'] == 'unverifiable' and result['segments'] == []


def test_bounded_source_read_handles_size_change(tmp_path, source, monkeypatch):
    path = tmp_path / 'synthetic.txt'
    path.write_bytes(b'x' * 65)
    monkeypatch.setattr(source, 'MAX_BYTES', 64)
    monkeypatch.setattr(Path, 'stat', lambda self: type('Stat', (), {'st_size': 0})())
    result = source.extract(path)
    assert result['status'] == 'unverifiable'
    assert result['segments'] == []


def test_xlsx_total_expansion_limit_is_not_relaxed(tmp_path, source, monkeypatch):
    monkeypatch.setattr(source, 'MAX_EXPANDED_BYTES', 64)
    path = tmp_path / 'synthetic.xlsx'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('xl/worksheets/sheet1.xml', b' ' * 65)
    result = source.extract(path)
    assert result['status'] == 'unverifiable'
    assert 'Office 解压内容超限' in result['reason']
