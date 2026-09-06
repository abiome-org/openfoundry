import pytest
from openfoundry.data import DataService
from openfoundry.errors import IntegrityError, ValidationError
from openfoundry.stores.filesystem import FilesystemStore


@pytest.mark.parametrize("mode", ["register", "mount"])
def test_external_data_modes_and_drift(tmp_path, mode):
    source = tmp_path / "data"
    source.write_text("one")
    service = DataService()
    snapshot = service.add("dataset", source, mode)
    assert service.verify(snapshot)
    source.write_text("two")
    with pytest.raises(IntegrityError):
        service.verify(snapshot)


def test_copy_and_stream_modes(tmp_path):
    source = tmp_path / "data"
    source.write_text("one")
    service = DataService(FilesystemStore(tmp_path / "store"))
    assert service.verify(service.copy("copy", source))
    stream = service.stream(
        "events", "https://example.invalid/events", cursor_policy={"field": "id"}
    )
    assert service.verify(stream)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.invalid/x",
        "https://example.invalid/x?token=x",
        "no-scheme",
    ],
)
def test_stream_rejects_secret_or_ambiguous_url(url):
    with pytest.raises(ValidationError):
        DataService().stream("x", url, cursor_policy={"field": "id"})
