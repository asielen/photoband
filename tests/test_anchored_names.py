"""File-name validation matches the whole name: a Windows device name is refused with or without
an extension, and a name that only starts like one is allowed."""
import pytest

from photoband import security


@pytest.mark.parametrize("name", ["con", "CON", "con.txt", "Nul.tar.gz", "com1", "LPT9.jpg", "aux.é"])
def test_device_names_refused(name):
    with pytest.raises(security.UserError):
        security.plain_file_name(name)
    assert not security.is_plain_folder_name(name)


@pytest.mark.parametrize("name", ["console.txt", "con2", "conx.jpg", "com10", "aux_photos", "my con.jpg", "nullable"])
def test_names_that_only_start_like_devices_allowed(name):
    assert security.plain_file_name(name) == name
    assert security.is_plain_folder_name(name)


@pytest.mark.parametrize("name", ["con\n", "a\nb", "con.txt\nx", "a/b", "a\b", ".."])
def test_control_characters_and_separators_refused(name):
    assert not security.is_plain_folder_name(name)
