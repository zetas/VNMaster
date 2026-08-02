from vnmaster.downloads.downloader import is_url_for_host


def test_forum_thread_is_never_treated_as_a_download_file() -> None:
    assert not is_url_for_host(
        "WALKTHROUGH MOD",
        "https://f95zone.to/threads/grandmas-house-walkthrough-mod.107512/",
    )


def test_f95_attachment_remains_a_supported_https_file() -> None:
    assert is_url_for_host(
        "F95 ATTACHMENT",
        "https://attachments.f95zone.to/2026/01/guide.pdf",
    )
